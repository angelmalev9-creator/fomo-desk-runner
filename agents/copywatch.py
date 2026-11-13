"""CopyWatcher: follow tracked FOMO traders and open PAPER copies ($10 each).

PAPER only. Never places an order, never calls a swap/execute endpoint, never
touches a private key. Real money only if the owner taps the FOMO button and
buys in the FOMO app themselves. Links come only from links.py (r=savipww).

Live feed: Solana public websocket logsSubscribe {mentions:[wallet]} per trader
(recycled before the ~6 min public-RPC drop) + polling fallback
(getSignaturesForAddress) so nothing is missed while the socket is down.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from typing import Any, Callable

import links
import session
import trader_refresh
from agents.risk import close_paper, stop_price_at_entry
from copytrade import TX_OPTS, copyable_traders, entry_gate, parse_fomo_trade, sell_fraction, setting, source_label
from fields import dex_float
from util import agent_logger, iso_utc, round_cents, utcnow

log = agent_logger("copywatch")


def fmt_px(p: float | None) -> str:
    if p is None:
        return "—"
    if p >= 1:
        return f"${p:,.4f}".rstrip("0").rstrip(".")
    return f"${p:.4g}"


def buy_alert_text(handle: str, name: str, symbol: str | None, trade: dict, entry: float | None,
                   size: float, cfg, note: str | None = None) -> str:
    stop = float(cfg.get("risk.stop_pct") or 6)
    trail = float(cfg.get("risk.trail_pct") or 12)
    pad = float(setting(cfg, "copy.slippage_pct"))
    sym = f" ({symbol})" if symbol and symbol != name else ""
    lines = [
        f"PAPER · копие от {handle}",
        f"{handle} купи {name}{sym} за ${trade['usdc']:,.2f} на {fmt_px(trade['price_usd'])}",
    ]
    if entry is not None:
        lines.append(f"PAPER копие: ${size:.2f} на {fmt_px(entry)} (+{pad:g}% подложка)")
        lines.append(f"Изход: когато {handle} продаде · стоп -{stop:g}% · трейл {trail:g}%")
    if note:
        lines.append(note)
    lines.append("Нищо не е купено с реални пари. Реална покупка има само ако натиснеш бутона и купиш във FOMO.")
    return "\n".join(lines)


def buy_buttons(mint: str) -> list[dict]:
    return [
        {"text": "Купи във FOMO", "url": links.coin_url(mint, "solana")},
        {"text": "Пропусни", "callback": f"cskip:{mint}"},
    ]


def on_copy_skip(db, mint: str) -> list[dict]:
    """Owner tapped Пропусни: drop the PAPER copy (no P&L) and ignore this coin tonight."""
    rows = [dict(r) for r in db.query(
        "SELECT * FROM positions WHERE mint=? AND source='copy' AND status='open'", (mint,))]
    for p in rows:
        db.update_position(p["id"], status="cancelled")
    db.add_skip(mint)
    try:
        db.update_signal_status(mint, "skipped", "пропуснато от теб")
    except Exception:
        pass
    return rows


class CopyWatcher:
    def __init__(self, db, cfg, bus, rpc, sources=None, notifier=None,
                 ws_connect: Callable | None = None, refresh: Callable | None = None) -> None:
        self.db = db
        self.cfg = cfg
        self.bus = bus
        self.rpc = rpc
        self.sources = sources
        self.notifier = notifier
        self._ws_connect = ws_connect
        self._refresh = refresh or trader_refresh.refresh
        self.queue: asyncio.Queue = asyncio.Queue()
        self.inflight: set[str] = set()
        self.retries: dict[str, int] = {}
        self.ws_ok = False
        self.traders_version = 0

    # ------------------------------------------------------------ traders
    def traders(self) -> dict[str, str]:
        """wallet -> handle for traders we are allowed to copy."""
        return {r["wallet"]: r["handle"] for r in copyable_traders(self.db, self.cfg)}

    # ------------------------------------------------------------ rpc helpers
    async def _rpc(self, method: str, params: list) -> Any:
        body = await asyncio.to_thread(self.rpc.call, method, params)
        return (body or {}).get("result") if isinstance(body, dict) else body

    def seen(self, sig: str) -> bool:
        return self.db.query_one("SELECT 1 FROM copy_seen WHERE signature=?", (sig,)) is not None

    def mark_seen(self, sig: str, handle: str, trade: dict | None, action: str) -> None:
        self.db.execute(
            "INSERT OR IGNORE INTO copy_seen(signature, handle, ts, side, mint, action) VALUES(?,?,?,?,?,?)",
            (sig, handle, iso_utc(), (trade or {}).get("side"), (trade or {}).get("mint"), action),
        )

    # ------------------------------------------------------------ main loops
    async def run(self, stop: asyncio.Event) -> None:
        trader_refresh.seed_traders(self.db)
        tasks = [
            asyncio.create_task(self._ws_loop(stop), name="copy-ws"),
            asyncio.create_task(self._poll_loop(stop), name="copy-poll"),
            asyncio.create_task(self._worker(stop), name="copy-worker"),
        ]
        log.info("copywatch up, traders=%s", sorted(self.traders().values()))
        try:
            await stop.wait()
        finally:
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _ws_loop(self, stop: asyncio.Event) -> None:
        import websockets

        connect = self._ws_connect or websockets.connect
        backoff = 5.0
        while not stop.is_set():
            wallets = self.traders()
            version = self.traders_version
            if not wallets or not bool(setting(self.cfg, "copy.enabled")):
                await _sleep(stop, 60)
                continue
            try:
                async with connect(str(setting(self.cfg, "copy.ws_url")), ping_interval=20,
                                   ping_timeout=30, open_timeout=20, close_timeout=5) as ws:
                    ids: dict[int, str] = {}
                    for i, w in enumerate(wallets):
                        await ws.send(json.dumps({"jsonrpc": "2.0", "id": i, "method": "logsSubscribe",
                                                  "params": [{"mentions": [w]}, {"commitment": "confirmed"}]}))
                        ids[i] = w
                    subs: dict[int, str] = {}
                    self.ws_ok = True
                    backoff = 5.0
                    loop = asyncio.get_running_loop()
                    until = loop.time() + float(setting(self.cfg, "copy.ws_recycle_s"))
                    log.info("ws connected, %s subscriptions", len(ids))
                    while not stop.is_set() and loop.time() < until and version == self.traders_version:
                        try:
                            raw = await asyncio.wait_for(ws.recv(), timeout=5)
                        except asyncio.TimeoutError:
                            self.db.heartbeat("copywatch")
                            continue
                        msg = json.loads(raw)
                        if "id" in msg and "result" in msg:
                            subs[msg["result"]] = ids.get(msg["id"], "")
                            continue
                        params = msg.get("params") or {}
                        val = ((params.get("result") or {}).get("value")) or {}
                        wallet = subs.get(params.get("subscription"))
                        if wallet and val.get("signature") and not val.get("err"):
                            await self.queue.put((val["signature"], wallet, "ws"))
                    log.info("ws recycle")
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("ws down: %s, retry in %.0fs (polling covers the gap)", type(e).__name__, backoff)
                self.ws_ok = False
                await _sleep(stop, backoff)
                backoff = min(60.0, backoff * 2)
            finally:
                self.ws_ok = False

    async def _poll_loop(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            self.db.heartbeat("copywatch")
            try:
                await self.maybe_refresh()
            except Exception as e:
                log.warning("refresh failed: %s", type(e).__name__)
            if bool(setting(self.cfg, "copy.enabled")):
                try:
                    await self.poll_once()
                except Exception as e:
                    log.warning("poll failed: %s", type(e).__name__)
            wait = float(setting(self.cfg, "copy.poll_s"))
            await _sleep(stop, wait if self.ws_ok else min(wait, 30.0))

    async def maybe_refresh(self) -> None:
        before = self.traders()
        res = await asyncio.to_thread(self._refresh, self.db, self.cfg, self.rpc)
        if self.traders() != before:
            self.traders_version += 1
            log.info("trader list changed: %s", res)

    async def poll_once(self) -> int:
        n = 0
        for wallet, handle in self.traders().items():
            key = f"copy.last_sig.{wallet}"
            last = self.db.kv_get(key)
            opts: dict[str, Any] = {"limit": 25, "commitment": "confirmed"}
            if last:
                opts["until"] = last
            res = await self._rpc("getSignaturesForAddress", [wallet, opts]) or []
            if not res:
                continue
            self.db.kv_set(key, res[0]["signature"])
            if not last:
                continue  # first sight: start from now, never copy old buys
            for s in reversed(res):
                if not s.get("err") and not self.seen(s["signature"]):
                    await self.queue.put((s["signature"], wallet, "poll"))
                    n += 1
        return n

    async def _worker(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                sig, wallet, via = await asyncio.wait_for(self.queue.get(), timeout=5)
            except asyncio.TimeoutError:
                continue
            try:
                await self.handle_signature(sig, wallet, via)
            except Exception as e:
                log.warning("handle %s… failed: %s", sig[:10], type(e).__name__)

    # ------------------------------------------------------------ trade handling
    async def handle_signature(self, sig: str, wallet: str, via: str = "?") -> str | None:
        handle = self.traders().get(wallet)
        if not handle or sig in self.inflight or self.seen(sig):
            return None
        self.inflight.add(sig)
        try:
            tx = await self._rpc("getTransaction", [sig, TX_OPTS])
            if tx is None:
                tries = self.retries.get(sig, 0) + 1
                self.retries[sig] = tries
                if tries <= 3:
                    asyncio.get_running_loop().call_later(
                        5 * tries, lambda: self.queue.put_nowait((sig, wallet, via)))
                return None
            self.retries.pop(sig, None)
            trade = parse_fomo_trade(tx, wallet, sig)
            if not trade:
                self.mark_seen(sig, handle, None, "not_trade")
                return None
            action = await self.on_trade(handle, trade)
            self.mark_seen(sig, handle, trade, action)
            log.info("%s %s %s %s via=%s -> %s", handle, trade["side"], trade["mint"], round(trade["usdc"], 2), via, action)
            return action
        finally:
            self.inflight.discard(sig)

    async def on_trade(self, handle: str, trade: dict) -> str:
        bt = trade.get("block_time")
        max_age = float(setting(self.cfg, "copy.max_age_min")) * 60
        if bt and (utcnow().timestamp() - bt) > max_age:
            return "too_old"
        if trade["usdc"] < float(setting(self.cfg, "copy.min_usdc")):
            return "dust"
        if trade["side"] == "buy":
            return await self.open_copy(handle, trade)
        return await self.close_copies(handle, trade)

    async def _pair(self, mint: str) -> dict | None:
        if self.sources is None:
            return None
        try:
            pairs = await asyncio.to_thread(self.sources.any_dex_pairs, [mint])
            return pairs.get(mint)
        except Exception as e:
            log.warning("dex price failed: %s", type(e).__name__)
            return None

    async def open_copy(self, handle: str, trade: dict) -> str:
        mint = trade["mint"]
        cfg, db = self.cfg, self.db
        if not bool(setting(cfg, "copy.enabled")) or cfg.is_paused("copy"):
            return "disabled"
        if db.is_skipped(mint):
            return "skipped_coin"
        if session.entries_paused(cfg):
            return "entries_paused"
        size = float(setting(cfg, "copy.size_usd"))
        pad = float(setting(cfg, "copy.slippage_pct")) / 100.0
        pair = await self._pair(mint)
        price = dex_float(pair, "priceUsd") if pair else None
        base = (pair or {}).get("baseToken") or {}
        name = base.get("name") or (db.get_coin(mint) or {}).get("name") or mint[:6]
        symbol = base.get("symbol")
        db.upsert_coin(mint=mint, name=name, symbol=symbol)
        link = links.coin_url(mint, "solana")
        note, action, entry = None, "copied", None
        open_all = db.open_positions()
        if price is None or price <= 0:
            note, action = "Няма пазарна цена от DexScreener → не отварям PAPER копие (fail closed).", "no_price"
        elif any(p["mint"] == mint for p in open_all):
            note, action = "Вече има отворена позиция в тази монета → без второ PAPER копие.", "already_open"
        else:
            stack = session.current_stack(db, cfg)
            free = stack - sum(float(p["size_usd"] or 0) for p in open_all)
            if free + 1e-9 < size:
                note, action = f"Няма свободен PAPER стек (${free:.2f}) → без копие.", "no_stack"
        if action == "copied":
            gate = entry_gate(pair, trade, cfg, open_all, handle)
            if gate is None:
                gate = await self._top10_gate(mint, pair)
            if gate is not None:
                # Quiet skip: no Telegram push, the reason is in the log and copy_seen.action.
                log.info("gate skip %s %s %s: %s", handle, mint, gate[0], gate[1])
                self._log_counterfactual(handle, mint, gate[0], price)
                return gate[0]
            # Four-agent desk: Whale Tail trades only inside its own sleeve. Quiet skip (no push).
            try:
                from agents import strategies

                full = strategies.sleeve_check(db, cfg, "whale", size)
            except Exception as e:  # fail closed
                full = f"sleeve check failed ({type(e).__name__})"
            if full:
                log.info("sleeve skip %s %s: %s", handle, mint, full)
                self._log_counterfactual(handle, mint, "sleeve_full", price)
                return "sleeve_full"
        if action == "copied":
            entry = price * (1 + pad)
            stop_pct = float(cfg.get("risk.stop_pct") or 6)
            db.open_position(mint=mint, entry_price=entry, size_usd=size, peak_price=entry,
                             stop_price=stop_price_at_entry(price, entry, stop_pct, cfg), mode="PAPER", status="open",
                             name=name, source="copy", copy_handle=handle)
            stack = session.current_stack(db, cfg)
            db.add_signal(mint=mint, price=entry, size_usd=size,
                          stack_pct=round(size / stack * 100, 2) if stack else None,
                          link=link, mode="PAPER", status="bought", reason=source_label(handle))
            db.add_activity(kind="bought", mint=mint, agent="copy", title=f"Bought {name}",
                            subtitle=f"{source_label(handle)} · {utcnow().strftime('%H:%M')} UTC",
                            value=f"${size:.2f} · PAPER")
            if self.bus is not None:
                await self.bus.risk.put({"mint": mint, "event": "filled"})
        text = buy_alert_text(handle, name, symbol, trade, entry, size, cfg, note)
        if self.notifier is not None:
            try:
                await self.notifier.send(text, buttons=buy_buttons(mint), mint=mint)
            except Exception as e:
                log.warning("copy alert failed: %s", type(e).__name__)
        return action

    async def _top10_gate(self, mint: str, pair: dict | None) -> tuple[str, str] | None:
        """Optional top-10 holder cap (chainstacklabs/pumpfun-bonkfun-bot top_holders check). Fails closed."""
        cap = float(setting(self.cfg, "copy.max_top10_pct") or 0)
        if cap <= 0 or not bool(setting(self.cfg, "copy.gate_enabled")):
            return None
        try:
            from agents.scanner import _top10

            pct = await asyncio.to_thread(_top10, self.rpc, mint, (pair or {}).get("pairAddress"), None)
        except Exception:
            pct = None
        if pct is None:
            return "gate_no_holders", "Няма данни за топ 10 държатели (fail closed) → пропускам."
        if pct > cap:
            return "gate_top10", f"Топ 10 държат {pct:.0f}% (> {cap:g}%) → пропускам."
        return None

    def _log_counterfactual(self, handle: str, mint: str, reason: str, price: float | None) -> None:
        """Write the skip to logs/gate_counterfactual.jsonl and check the price again later.

        Lets us validate each gate on fresh data instead of trusting it
        (DeeKalshiWay/solana-pumpfun-bot: analyzer/counterfactual.py + tools/holdout_validate.py).
        """
        rec = {"ts": iso_utc(), "handle": handle, "mint": mint, "reason": reason, "price": price}
        _append_jsonl(rec)
        wait = float(setting(self.cfg, "copy.gate_followup_min") or 0) * 60
        if wait <= 0 or price is None or price <= 0:
            return
        try:
            asyncio.get_running_loop().create_task(self._followup(rec, wait))
        except RuntimeError:
            pass

    async def _followup(self, rec: dict, wait: float) -> None:
        try:
            await asyncio.sleep(wait)
            pair = await self._pair(rec["mint"])
            now = dex_float(pair, "priceUsd") if pair else None
            out = {**rec, "followup_ts": iso_utc(), "followup_min": round(wait / 60, 1), "price_after": now,
                   "change_pct": round((now / rec["price"] - 1) * 100, 2) if now else None}
            _append_jsonl(out)
        except Exception as e:  # pure logging, never break the watcher
            log.warning("counterfactual followup failed: %s", type(e).__name__)

    async def _close_partial(self, pos: dict, frac: float, exit_px: float, handle: str, src: str, pad: float) -> None:
        """Sell `frac` of a copy: a closed slice row + a smaller open row (pro-rata copy exit)."""
        mint = pos["mint"]
        size = float(pos["size_usd"])
        part = round(size * frac, 6)
        slice_pos = {**pos, "size_usd": part}
        close = close_paper(slice_pos, exit_px, "trader_partial", self.cfg)
        sid = self.db.open_position(mint=mint, entry_price=pos["entry_price"], size_usd=part,
                                    peak_price=max(float(pos.get("peak_price") or 0), exit_px),
                                    stop_price=pos["stop_price"], mode=pos.get("mode") or "PAPER", status="closed",
                                    name=pos.get("name"), source="copy", copy_handle=handle)
        self.db.update_position(sid, opened_at=pos["opened_at"], closed_at=iso_utc(), exit_price=exit_px,
                                exit_reason="trader_partial", pnl_usd=close["pnl_usd"], fees_usd=close["fees_usd"],
                                status="closed")
        self.db.update_position(pos["id"], size_usd=round(size - part, 6))
        last = self.db.last_balance()
        if last is not None:
            self.db.add_balance(round_cents(last + close["pnl_usd"]))
        pnl, pct = close["pnl_usd"], close["pct"]
        name = pos.get("name") or mint[:6]
        sign = "+" if pnl >= 0 else "-"
        self.db.add_activity(kind="sold", mint=mint, agent="copy", title=f"Sold {frac:.0%} {name}",
                             subtitle=f"{source_label(handle)} · трейдърът продаде {frac:.0%}",
                             value=f"{sign}${abs(pnl):.2f} · {pct:+g}%")
        text = (f"PAPER · {handle} продаде {frac:.0%} от {name} → продадох {frac:.0%} от копието\n"
                f"{sign}${abs(pnl):.2f} ({pct:+g}%) на ${part:.2f} · изход {fmt_px(exit_px)} ({src}, -{pad*100:g}% подложка)\n"
                f"Остават ${size - part:.2f} в копието. Ако имаш реална позиция във FOMO, продай същия дял там.")
        if self.notifier is not None:
            try:
                await self.notifier.send(text, buttons=[{"text": "Продай във FOMO", "url": links.coin_url(mint, "solana")}], mint=mint)
            except Exception as e:
                log.warning("copy partial alert failed: %s", type(e).__name__)

    async def close_copies(self, handle: str, trade: dict) -> str:
        mint = trade["mint"]
        rows = [dict(r) for r in self.db.query(
            "SELECT * FROM positions WHERE mint=? AND source='copy' AND copy_handle=? AND status='open'",
            (mint, handle))]
        if not rows:
            return "sell_no_copy"
        pad = float(setting(self.cfg, "copy.slippage_pct")) / 100.0
        pair = await self._pair(mint)
        price = dex_float(pair, "priceUsd") if pair else None
        src = "DexScreener"
        if price is None or price <= 0:
            price, src = trade["price_usd"], f"цената на {handle}"
        exit_px = price * (1 - pad)
        frac = sell_fraction(trade)
        if bool(setting(self.cfg, "copy.follow_partial_sells")) and frac < float(setting(self.cfg, "copy.partial_full_above")):
            min_left = float(setting(self.cfg, "copy.partial_min_usd") or 0)
            partial_rows = [p for p in rows if float(p["size_usd"]) * (1 - frac) >= min_left and frac > 0]
            for pos in partial_rows:
                await self._close_partial(pos, frac, exit_px, handle, src, pad)
            if len(partial_rows) == len(rows):
                return "partial_copy" if partial_rows else "sell_no_copy"
            rows = [p for p in rows if p not in partial_rows]  # remainder too small: close it fully
        for pos in rows:
            close = close_paper(pos, exit_px, "trader_sold", self.cfg)
            self.db.update_position(pos["id"], closed_at=iso_utc(), exit_price=exit_px, exit_reason="trader_sold",
                                    pnl_usd=close["pnl_usd"], fees_usd=close["fees_usd"], status="closed",
                                    peak_price=max(float(pos.get("peak_price") or 0), exit_px))
            last = self.db.last_balance()
            if last is not None:
                self.db.add_balance(round_cents(last + close["pnl_usd"]))
            pnl, pct = close["pnl_usd"], close["pct"]
            name = pos.get("name") or mint[:6]
            sign = "+" if pnl >= 0 else "-"
            self.db.add_activity(kind="sold", mint=mint, agent="copy", title=f"Sold {name}",
                                 subtitle=f"{source_label(handle)} · трейдърът продаде",
                                 value=f"{sign}${abs(pnl):.2f} · {pct:+g}%")
            text = (f"PAPER · {handle} продаде {name} → затворих копието\n"
                    f"{sign}${abs(pnl):.2f} ({pct:+g}%) · изход {fmt_px(exit_px)} ({src}, -{pad*100:g}% подложка)\n"
                    "Ако имаш реална позиция във FOMO, продай я там.")
            if self.notifier is not None:
                try:
                    await self.notifier.send(text, buttons=[{"text": "Продай във FOMO", "url": links.coin_url(mint, "solana")}], mint=mint)
                except Exception as e:
                    log.warning("copy sell alert failed: %s", type(e).__name__)
        return "closed_copy"


def _append_jsonl(rec: dict) -> None:
    try:
        import util

        path = util.LOG_DIR / "gate_counterfactual.jsonl"
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception as e:
        log.warning("counterfactual log failed: %s", type(e).__name__)


async def _sleep(stop: asyncio.Event, s: float) -> None:
    try:
        await asyncio.wait_for(stop.wait(), timeout=s)
    except asyncio.TimeoutError:
        pass


async def run_agent(db, cfg, bus, rpc, sources=None, notifier=None, stop_event: asyncio.Event | None = None) -> None:
    stop = stop_event or asyncio.Event()
    await CopyWatcher(db, cfg, bus, rpc, sources, notifier).run(stop)

"""Risk: poll open positions and send the Sell button on exit rules."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import fees
import links
from fields import dex_float
from sources import maybe_note_jup_price
from util import agent_logger, as_float, iso_utc, round_cents, utcnow

log = agent_logger("risk")


def parse_ts(raw: str | None) -> datetime | None:
    if not raw:
        return None
    s = str(raw).replace("Z", "+00:00")
    try:
        t = datetime.fromisoformat(s)
    except ValueError:
        return None
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    return t


def stop_pct_of(position: dict) -> float:
    """The stop this position carries, from its stop line and entry."""
    entry = float(position.get("entry_price") or 0)
    stop = float(position.get("stop_price") or 0)
    if entry <= 0:
        return 0.0
    return max(0.0, (1.0 - stop / entry) * 100.0)


def stop_price_for(entry: float, stop_pct: float) -> float:
    return entry * (1.0 - float(stop_pct) / 100.0)


def _live_setting(cfg, key: str):
    """kv `setting.<key>` (live, no restart). Missing or unreadable -> None."""
    db = getattr(cfg, "db", None)
    if db is None:
        return None
    try:
        raw = db.kv_get("setting." + key)
    except Exception:
        return None
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except Exception:
        return raw


def stop_basis_of(cfg) -> str:
    """'market' or 'fill'. Live kv `risk.stop_basis` then `copy.stop_basis`, else fill.

    'market' = 1% under the market price at entry. 'fill' = 1% under our (possibly
    padded PAPER) fill. The stop distance itself is always risk.stop_pct (1%).
    """
    raw = _live_setting(cfg, "risk.stop_basis")
    if raw is None:
        raw = _live_setting(cfg, "copy.stop_basis")
    if raw is None:
        try:
            from copytrade import setting as _copy_setting
            raw = _copy_setting(cfg, "copy.stop_basis")
        except Exception:
            raw = "fill"
    return "market" if str(raw or "").lower() == "market" else "fill"


def stop_price_at_entry(market: float, fill: float, stop_pct: float, cfg) -> float:
    """1% (or stop_pct) under market or fill, according to stop_basis_of(cfg)."""
    ref = float(market) if stop_basis_of(cfg) == "market" else float(fill)
    if ref <= 0:
        ref = float(fill) or float(market)
    return stop_price_for(ref, stop_pct)


def rebase_open_stops_to_market(db, cfg, pad_pct: float | None = None) -> int:
    """Point open 1% stops at market basis. Never widen a tightened (lock/trail) stop.

    Copy fills are padded (entry = market * (1+pad)); market is recovered as
    entry / (1+pad). Other agents already use the market as entry. A fill-basis
    copy stop sits above the market; rebasing lowers it to market * 0.99. If the
    live stop has already been raised above the original fill-basis line, leave it.
    Returns how many positions were updated.
    """
    if pad_pct is None:
        pad_pct = _live_setting(cfg, "copy.slippage_pct")
        if pad_pct is None:
            try:
                from copytrade import setting as _copy_setting
                pad_pct = _copy_setting(cfg, "copy.slippage_pct")
            except Exception:
                pad_pct = 1.5
    pad = float(pad_pct or 0) / 100.0
    stop_pct = float(cfg.get("risk.stop_pct") or 1)
    n = 0
    for pos in db.open_positions():
        entry = float(pos.get("entry_price") or 0)
        current = float(pos.get("stop_price") or 0)
        if entry <= 0 or current <= 0:
            continue
        src = str(pos.get("source") or "").lower()
        if src == "copy" and pad > 0:
            market = entry / (1.0 + pad)
        else:
            market = entry
        proposed = stop_price_for(market, stop_pct)
        fill_line = stop_price_for(entry, stop_pct)
        # Leave a stop that was tightened (profit lock / trail) above the fill line.
        if current > fill_line + 1e-15:
            continue
        if abs(proposed - current) <= 1e-15:
            continue
        db.update_position(pos["id"], stop_price=proposed)
        n += 1
    return n


def apply_stop_setting(position: dict, new_stop_pct: float) -> dict:
    """A new stop applies to an open position only if it is tighter (never moves down)."""
    entry = float(position["entry_price"])
    current = float(position["stop_price"])
    proposed = stop_price_for(entry, new_stop_pct)
    # Tighter stop = higher stop price (closer to entry from below).
    if proposed > current + 1e-12:
        updated = dict(position)
        updated["stop_price"] = proposed
        return updated
    return position


@dataclass
class PollDecision:
    action: str  # hold | exit
    reason: str | None  # stop | trail | timeout | pulled | None
    peak: float
    trail_active: bool
    exit_price: float | None
    stop_line: float
    trail_line: float | None


def poll_position(
    position: dict,
    price: float | None,
    now: datetime,
    cfg,
    liquidity_usd: float | None = None,
) -> PollDecision:
    """Apply RULES in order. Never invents a price."""
    entry = float(position["entry_price"])
    peak = max(float(position.get("peak_price") or entry), entry)
    trail_active = bool(position.get("trail_active"))
    stop_line = float(position["stop_price"])
    trail_pct = float(cfg.get("risk.trail_pct") or 12)
    trail_start = float(cfg.get("risk.trail_start_pct") or 20)
    timeout_h = float(cfg.get("risk.timeout_h") or 3)
    timeout_gain = float(cfg.get("risk.timeout_min_gain_pct") or 5)
    opened = parse_ts(position.get("opened_at")) or now

    if liquidity_usd is not None and liquidity_usd <= 0 and price is not None:
        return PollDecision("exit", "pulled", peak, trail_active, price, stop_line, None)

    if price is None:
        trail_line = peak * (1 - trail_pct / 100.0) if trail_active else None
        return PollDecision("hold", None, peak, trail_active, None, stop_line, trail_line)

    peak = max(peak, price)
    trail_line = None
    if price >= entry * (1 + trail_start / 100.0):
        trail_active = True
    if trail_active:
        trail_line = peak * (1 - trail_pct / 100.0)

    # Profit lock: after +lock_trigger the stop moves up to +lock_floor (never down).
    lock_trigger = float(cfg.get("risk.lock_trigger_pct") or 0)
    lock_floor = float(cfg.get("risk.lock_floor_pct") or 0)
    if lock_trigger > 0 and lock_floor < lock_trigger and peak >= entry * (1 + lock_trigger / 100.0):
        stop_line = max(stop_line, entry * (1 + lock_floor / 100.0))

    if price <= stop_line:
        reason = "lock" if stop_line > entry else "stop"
        return PollDecision("exit", reason, peak, trail_active, price, stop_line, trail_line)
    if trail_active and trail_line is not None and price <= trail_line:
        return PollDecision("exit", "trail", peak, trail_active, price, stop_line, trail_line)
    age = now - opened
    if age >= timedelta(hours=timeout_h) and price < entry * (1 + timeout_gain / 100.0):
        return PollDecision("exit", "timeout", peak, trail_active, price, stop_line, trail_line)
    return PollDecision("hold", None, peak, trail_active, None, stop_line, trail_line)


def close_paper(position: dict, exit_price: float, reason: str, cfg) -> dict[str, Any]:
    entry = float(position["entry_price"])
    size = float(position["size_usd"])
    discount = float(cfg.get("desk.fees_ref_discount") or 0.10)
    pnl, fee = fees.pnl_usd(size, entry, exit_price, discount)
    pct = ((exit_price - entry) / entry * 100.0) if entry else 0.0
    return {
        "pnl_usd": pnl,
        "fees_usd": fee,
        "pct": round_cents(pct),
        "exit_price": exit_price,
        "exit_reason": reason,
        "status": "closed",
    }


def push_and_activity(position: dict, decision: PollDecision, close: dict, cfg) -> dict[str, Any]:
    name = position.get("name") or position.get("mint")
    mint = position["mint"]
    link = links.coin_url(mint, "solana")
    mode = str(position.get("mode") or cfg.get("desk.mode") or "PAPER").upper()
    stop_pct = stop_pct_of({**position, "stop_price": decision.stop_line})
    # If the position already stored a stop, use that exact carried stop.
    stop_pct = stop_pct_of(position)
    timeout_h = float(cfg.get("risk.timeout_h") or 3)
    pnl = close["pnl_usd"]
    pct = close["pct"]
    prefix = "PAPER · " if mode == "PAPER" else ""
    reason = decision.reason
    if reason == "trail":
        text = f"{prefix}Sold {name} · +${pnl:.2f} (+{pct:g}%)" if pnl >= 0 else f"{prefix}Sold {name} · -${abs(pnl):.2f} ({pct:g}%)"
        # Spec trail text: "Sold {name} · +${pnl} (+{pct}%)"
        sign = "+" if pnl >= 0 else "-"
        pct_sign = "+" if pct >= 0 else ""
        text = f"{prefix}Sold {name} · {sign}${abs(pnl):.2f} ({pct_sign}{pct:g}%)"
        kind = "sold"
        title = f"Sold {name}"
        subtitle = "Risk · trailing stop"
        value = f"{sign}${abs(pnl):.2f} · {pct_sign}{pct:g}%"
    elif reason == "lock":
        sign = "+" if pnl >= 0 else "-"
        pct_sign = "+" if pct >= 0 else ""
        text = f"{prefix}Sold {name} · profit lock · {sign}${abs(pnl):.2f} ({pct_sign}{pct:g}%)"
        kind = "sold"
        title = f"Sold {name}"
        subtitle = "Risk · profit lock"
        value = f"{sign}${abs(pnl):.2f} · {pct_sign}{pct:g}%"
    elif reason == "stop":
        loss = abs(pnl)
        text = f"{prefix}{name} hit the -{stop_pct:g}% stop · -${loss:.2f}"
        kind = "stopped"
        title = f"Stopped {name}"
        subtitle = f"Risk · -{stop_pct:g}% stop"
        value = f"-${loss:.2f}"
    elif reason in ("target", "faded"):
        sign = "+" if pnl >= 0 else "-"
        pct_sign = "+" if pct >= 0 else ""
        label = "profit target" if reason == "target" else "volume faded"
        text = f"{prefix}Sold {name} · {label} · {sign}${abs(pnl):.2f} ({pct_sign}{pct:g}%)"
        kind = "sold"
        title = f"Sold {name}"
        subtitle = f"Risk · {label}"
        value = f"{sign}${abs(pnl):.2f} · {pct_sign}{pct:g}%"
    elif reason == "timeout":
        text = f"{prefix}Sell {name}, dead money · {pct:g}% in {timeout_h:g}h"
        kind = "sold"
        title = f"Sold {name}"
        subtitle = f"Risk · timeout {timeout_h:g}h"
        value = f"{pct:g}%"
    else:  # pulled
        text = f"{prefix}Sell {name}, liquidity pulled"
        kind = "sold"
        title = f"Sold {name}"
        subtitle = "Risk · pulled"
        value = f"${pnl:.2f}"
    status = "closed" if mode == "PAPER" else "exiting"
    return {
        "agent": "risk",
        "mint": mint,
        "name": name,
        "ts": iso_utc(),
        "reason": reason,
        "entryPrice": float(position["entry_price"]),
        "exitPrice": close["exit_price"],
        "peakPrice": decision.peak,
        "pnlUsd": close["pnl_usd"],
        "pct": close["pct"],
        "feesUsd": close["fees_usd"],
        "stopPct": stop_pct,
        "mode": mode,
        "status": status,
        "link": link,
        "text": text,
        "kind": kind,
        "title": title,
        "subtitle": subtitle,
        "value": value,
    }


def sell_buttons(link: str, mint: str) -> list[dict]:
    return [
        {"text": "Sell on FOMO", "url": link},
        {"text": "Sold", "callback": f"sold:{mint}"},
    ]


async def run_agent(
    db,
    cfg,
    bus,
    notifier=None,
    sources=None,
    stop_event: asyncio.Event | None = None,
) -> None:
    missing: dict[int, int] = {}
    exiting_since: dict[int, datetime] = {}
    reminded: set[int] = set()
    while stop_event is None or not stop_event.is_set():
        db.heartbeat("risk")
        # Drain fill notifications without blocking the poll cadence.
        while True:
            try:
                bus.risk.get_nowait()
            except asyncio.QueueEmpty:
                break
        if not cfg.is_paused("risk"):
            try:
                await _poll_once(db, cfg, bus, notifier, sources, missing, exiting_since, reminded)
            except Exception as e:
                log.warning("poll failed: %s", type(e).__name__)
        poll_s = float(cfg.get("risk.poll_s") or 15)
        try:
            if stop_event:
                await asyncio.wait_for(stop_event.wait(), timeout=poll_s)
            else:
                await asyncio.sleep(poll_s)
        except asyncio.TimeoutError:
            pass


async def _poll_once(db, cfg, bus, notifier, sources, missing, exiting_since, reminded) -> None:
    positions = db.open_positions()
    if not positions:
        return
    mints = list({p["mint"] for p in positions})
    pairs: dict[str, dict] = {}
    if sources is not None:
        try:
            pairs = sources.pumpswap_pairs(mints)
        except Exception as e:
            status = getattr(e, "status", None)
            if status == 429:
                wait = float(cfg.get("risk.dex_429_wait_s") or 30)
                log.warning("dex 429, backing off %ss, missed polls", wait)
                await asyncio.sleep(wait)
                return
            log.warning("dex poll failed: %s", type(e).__name__)

    # Copy trades can live on any DEX: fill the gaps from the most liquid pair.
    copy_missing = [
        p["mint"] for p in positions if p.get("source") in ("copy", "momentum", "dip") and p["mint"] not in pairs
    ]
    if copy_missing and sources is not None and hasattr(sources, "any_dex_pairs"):
        try:
            pairs.update(sources.any_dex_pairs(list(set(copy_missing))))
        except Exception as e:
            log.warning("any-dex poll failed: %s", type(e).__name__)

    now = utcnow()
    discount = float(cfg.get("desk.fees_ref_discount") or 0.10)
    for pos in positions:
        pid = pos["id"]
        mint = pos["mint"]
        pair = pairs.get(mint)
        price = dex_float(pair, "priceUsd") if pair else None
        vol = dex_float(pair, "volume.m5") if pair else None
        liq = dex_float(pair, "liquidity.usd") if pair else None
        if price is not None:
            db.add_price(mint, price, vol)
            missing[pid] = 0
        else:
            missing[pid] = missing.get(pid, 0) + 1
            if missing[pid] >= int(cfg.get("risk.no_price_polls") or 4):
                name = pos.get("name") or mint
                mode = str(pos.get("mode") or "PAPER")
                prefix = "PAPER · " if mode.upper() == "PAPER" else ""
                text = f"{prefix}{name}: no price for 1 min"
                link = links.coin_url(mint, "solana")
                if notifier:
                    await _push(notifier, text, sell_buttons(link, mint), mint)
                missing[pid] = 0
            log.info(
                "mint=%s price=none peak=%s stop=%s trail=%s action=no-price",
                mint,
                pos.get("peak_price"),
                pos.get("stop_price"),
                None,
            )
            continue

        if pos.get("status") == "exiting":
            # LIVE: wait for [Sold]; re-push once after confirm_window_min.
            started = exiting_since.get(pid)
            if started is None:
                exiting_since[pid] = now
                started = now
            window = float(cfg.get("risk.confirm_window_min") or 2)
            if pid not in reminded and now - started >= timedelta(minutes=window):
                link = links.coin_url(mint, "solana")
                name = pos.get("name") or mint
                prefix = ""
                text = f"{name} still open"
                if notifier:
                    await _push(notifier, text, sell_buttons(link, mint), mint)
                reminded.add(pid)
            continue

        decision = poll_position(pos, price, now, cfg, liquidity_usd=liq)
        if decision.action != "exit":
            # Four-agent desk: per-agent profit target / volume-faded exit (flags in kv, default off
            # for Sniper + Whale Tail). Never replaces the stop; only runs when the stop holds.
            try:
                from agents import strategies

                extra = strategies.extra_exit(pos, price, vol, cfg, db, now)
            except Exception as e:  # fail safe: keep the normal rules
                log.warning("extra exit check failed: %s", type(e).__name__)
                extra = None
            if extra:
                decision = PollDecision("exit", extra, decision.peak, decision.trail_active, price,
                                        decision.stop_line, decision.trail_line)
        log.info(
            "mint=%s price=%s peak=%s stop=%s trail=%s action=%s",
            mint,
            price,
            decision.peak,
            decision.stop_line,
            decision.trail_line,
            decision.reason or "hold",
        )
        updates = {"peak_price": decision.peak, "trail_active": 1 if decision.trail_active else 0}
        if decision.stop_line > float(pos.get("stop_price") or 0) + 1e-15:
            updates["stop_price"] = decision.stop_line  # profit lock raised the stop: persist it
        # Tighter live stop setting may have been applied elsewhere.
        db.update_position(pid, **updates)
        if decision.action != "exit":
            continue
        close = close_paper(pos, decision.exit_price, decision.reason, cfg)
        payload = push_and_activity(pos, decision, close, cfg)
        if payload["mode"] == "PAPER":
            db.update_position(
                pid,
                closed_at=iso_utc(now),
                exit_price=close["exit_price"],
                exit_reason=decision.reason,
                pnl_usd=close["pnl_usd"],
                fees_usd=close["fees_usd"],
                status="closed",
                peak_price=decision.peak,
                trail_active=1 if decision.trail_active else 0,
            )
            _apply_realized(db, close["pnl_usd"])
        else:
            db.update_position(
                pid,
                exit_price=close["exit_price"],
                exit_reason=decision.reason,
                pnl_usd=close["pnl_usd"],
                fees_usd=close["fees_usd"],
                status="exiting",
                peak_price=decision.peak,
                trail_active=1 if decision.trail_active else 0,
            )
            exiting_since[pid] = now
        db.add_activity(
            kind=payload["kind"],
            mint=mint,
            agent="risk",
            title=payload["title"],
            subtitle=payload["subtitle"],
            value=payload["value"],
        )
        if payload.get("mode") == "PAPER":
            maybe_note_jup_price(db, sources, mint, close["exit_price"], "exit", "risk")
        if notifier:
            await _push(notifier, payload["text"], sell_buttons(payload["link"], mint), mint)
        await bus.emit("exit", payload)


def _apply_realized(db, pnl: float) -> None:
    last = db.last_balance()
    if last is None:
        return
    db.add_balance(round_cents(last + pnl))


async def _push(notifier, text: str, buttons, mint: str) -> bool:
    for attempt in range(3):
        try:
            res = notifier.send(text, buttons=buttons, mint=mint)
            if asyncio.iscoroutine(res):
                res = await res
            if res is not False:
                return True
        except Exception:
            pass
        await asyncio.sleep(1)
    return False


def on_sold(db, mint: str) -> dict | None:
    """LIVE callback: mark the exiting (or open, if user sold early) position closed."""
    opens = [p for p in db.open_positions() if p["mint"] == mint]
    if not opens:
        return None
    pos = opens[-1]
    now = utcnow()
    exit_price = pos.get("exit_price") or pos.get("peak_price") or pos.get("entry_price")
    db.update_position(
        pos["id"],
        closed_at=iso_utc(now),
        status="closed",
        exit_price=exit_price,
    )
    return pos


def tighten_open_stops(db, new_stop_pct: float) -> int:
    """Apply a new stop setting to open positions only if it is tighter."""
    n = 0
    for pos in db.open_positions():
        updated = apply_stop_setting(pos, new_stop_pct)
        if updated["stop_price"] != pos["stop_price"]:
            db.update_position(pos["id"], stop_price=updated["stop_price"])
            n += 1
    return n

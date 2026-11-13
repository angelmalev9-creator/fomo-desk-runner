"""Owner-only Telegram commands and Buy/Sell push buttons (STEP 9)."""

from __future__ import annotations

import asyncio
import contextvars
import re
from typing import Any, Callable

import httpx

import links
import live_gate
import session
from agents.executor import on_bought, on_skip
from agents.risk import on_sold, tighten_open_stops
from util import agent_logger, secret

log = agent_logger("telegram")

API = "https://api.telegram.org/bot{token}/{method}"

HELP = (
    "Commands:\n"
    "buy X — resend the Buy button, or audit a new coin\n"
    "sell X — Sell on FOMO for an open position\n"
    "why X — which agent stopped it\n"
    "pause / resume {agent}\n"
    "set stop to -8% — any setting by name or alias\n"
    "stack 1240 — set the LIVE stack\n"
    "status — mode, balance, open positions\n"
    "paper / live — switch mode (live needs you to type LIVE)\n"
    "invite — referral signup link\n"
    "report — last night summary\n"
    "quiet / quiet 8 — тих режим: задържа известията (по желание за N часа)\n"
    "wake — край на тишината, едно обобщение\n"
    "sellall — PAPER: продай всичко (с потвърждение) и пауза на входовете\n"
    "resume — пусни новите входове отново\n"
    "X can be a name, symbol or mint."
)

AGENT_NAMES = ("scanner", "auditor", "analyst", "executor", "risk", "copy")

# True while handling the owner's own command: replies always go through quiet mode.
_REPLY = contextvars.ContextVar("tg_reply", default=False)


class TelegramNotifier:
    def __init__(self, token: str | None, owner_chat_id: int | None, db=None, cfg=None) -> None:
        self.token = token
        self.owner_chat_id = int(owner_chat_id) if owner_chat_id else None
        self.db = db
        self.cfg = cfg
        self._offset: int | None = None
        self._awaiting_live = False
        self.on_fill: Callable | None = None
        self.on_sold_cb: Callable | None = None
        self.sources = None  # HttpSources, set by main.py (for PAPER sell all)
        # Do not start at 0 in a way that drops the known owner; we only
        # consume updates when run_polling is started by main.py.
        self.enabled = bool(self.token)

    def _url(self, method: str) -> str:
        return API.format(token=self.token, method=method)

    def send_sync(self, text: str, buttons: list[dict] | None = None, chat_id: int | None = None) -> bool:
        if not self.token:
            return False
        cid = chat_id or self.owner_chat_id
        if not cid:
            log.warning("no owner chat id, not sending")
            return False
        payload: dict[str, Any] = {"chat_id": cid, "text": text, "disable_web_page_preview": True}
        if buttons:
            payload["reply_markup"] = _inline(buttons)
        try:
            with httpx.Client(timeout=20.0) as c:
                res = c.post(self._url("sendMessage"), json=payload)
                if res.status_code >= 400:
                    log.warning("sendMessage failed status=%s", res.status_code)
                    return False
                return True
        except httpx.HTTPError as e:
            log.warning("sendMessage error %s", type(e).__name__)
            return False

    async def send(
        self,
        text: str,
        buttons: list[dict] | None = None,
        mint: str | None = None,
        chat_id: int | None = None,
        critical: bool = False,
    ) -> bool:
        if not critical and not _REPLY.get() and session.is_quiet(self.db):
            self.db.queue_push(text, mint)
            log.info("quiet: held push mint=%s", mint)
            return True
        return await asyncio.to_thread(self.send_sync, text, buttons, chat_id)

    async def check_auto_wake(self) -> bool:
        if self.db is None or self.cfg is None or not session.due_auto_wake(self.db):
            return False
        summary = session.end_quiet(self.db, self.cfg)
        await self.send(summary["text"], critical=True)
        return True

    def _get_updates(self, timeout: int = 20) -> list[dict]:
        params: dict[str, Any] = {"timeout": timeout, "allowed_updates": ["message", "callback_query"]}
        if self._offset is not None:
            params["offset"] = self._offset
        with httpx.Client(timeout=timeout + 10) as c:
            res = c.get(self._url("getUpdates"), params=params)
            res.raise_for_status()
            data = res.json()
        return data.get("result") or []

    async def run_polling(self, stop_event: asyncio.Event | None = None) -> None:
        if not self.token:
            log.info("no TG_TOKEN, telegram polling disabled")
            return
        log.info("telegram polling start, owner=%s", self.owner_chat_id)
        while stop_event is None or not stop_event.is_set():
            try:
                updates = await asyncio.to_thread(self._get_updates, 20)
            except Exception as e:
                log.warning("getUpdates failed: %s", type(e).__name__)
                await asyncio.sleep(2)
                continue
            for upd in updates:
                self._offset = int(upd["update_id"]) + 1
                try:
                    await self._handle_update(upd)
                except Exception as e:
                    log.warning("update error: %s", type(e).__name__)
            if self.db is not None:
                self.db.heartbeat("telegram")
                try:
                    await self.check_auto_wake()
                except Exception as e:
                    log.warning("auto wake failed: %s", type(e).__name__)

    async def _handle_update(self, upd: dict) -> None:
        token = _REPLY.set(True)
        try:
            await self._handle_update_inner(upd)
        finally:
            _REPLY.reset(token)

    async def _handle_update_inner(self, upd: dict) -> None:
        if "callback_query" in upd:
            await self._handle_callback(upd["callback_query"])
            return
        msg = upd.get("message") or upd.get("edited_message")
        if not msg:
            return
        chat = msg.get("chat") or {}
        chat_id = chat.get("id")
        text = (msg.get("text") or "").strip()
        if not chat_id or not text:
            return
        if not self._is_owner(chat_id, text):
            return
        await self._handle_command(text, int(chat_id))

    def _is_owner(self, chat_id: int, text: str) -> bool:
        # First /start becomes owner only when none is stored.
        if self.owner_chat_id is None:
            if text.startswith("/start") or text.lower() == "start":
                self.owner_chat_id = int(chat_id)
                if self.cfg is not None:
                    try:
                        self.cfg.set("desk.owner_chat_id", int(chat_id), who="telegram")
                    except Exception:
                        pass
                log.info("owner locked")
                return True
            return False
        return int(chat_id) == int(self.owner_chat_id)

    async def _handle_callback(self, cb: dict) -> None:
        chat_id = ((cb.get("message") or {}).get("chat") or {}).get("id")
        data = cb.get("data") or ""
        uid = ((cb.get("from") or {}).get("id"))
        if self.owner_chat_id is not None and chat_id and int(chat_id) != int(self.owner_chat_id):
            return
        if self.owner_chat_id is not None and uid and int(uid) != int(self.owner_chat_id) and chat_id and int(chat_id) != int(self.owner_chat_id):
            # lock is on the chat
            pass
        if not self._owner_ok(chat_id):
            return
        # answer the callback so the spinner stops
        try:
            await asyncio.to_thread(self._answer_callback, cb.get("id"))
        except Exception:
            pass
        if self.db is None or self.cfg is None:
            return
        if data.startswith("bought:"):
            mint = data.split(":", 1)[1]
            result = on_bought(self.db, self.cfg, mint)
            if result and self.on_fill:
                await _maybe(self.on_fill(mint))
            await self.send("Marked bought." if result else "No open signal to fill.")
        elif data.startswith("cskip:"):
            from agents.copywatch import on_copy_skip

            mint = data.split(":", 1)[1]
            rows = on_copy_skip(self.db, mint)
            name = (rows[0].get("name") if rows else None) or mint[:6]
            await self.send(
                f"Пропуснато: {name}. PAPER копието е махнато (без печалба/загуба) и няма да копирам тази монета тази нощ."
                if rows else f"Пропуснато: {name}. Няма да копирам тази монета тази нощ."
            )
        elif data.startswith("skip:"):
            mint = data.split(":", 1)[1]
            on_skip(self.db, mint)
            await self.send("Skipped for the night.")
        elif data == "sellall:yes":
            await self._do_sellall()
        elif data == "sellall:no":
            await self.send("Отказано. Нищо не е продадено.")
        elif data.startswith("sold:"):
            mint = data.split(":", 1)[1]
            pos = on_sold(self.db, mint)
            if pos and self.on_sold_cb:
                await _maybe(self.on_sold_cb(mint))
            await self.send("Marked sold." if pos else "No open position.")

    def _owner_ok(self, chat_id) -> bool:
        if self.owner_chat_id is None:
            return False
        return chat_id is not None and int(chat_id) == int(self.owner_chat_id)

    def _answer_callback(self, callback_id: str | None) -> None:
        if not callback_id or not self.token:
            return
        with httpx.Client(timeout=10.0) as c:
            c.post(self._url("answerCallbackQuery"), json={"callback_query_id": callback_id})

    async def _handle_command(self, text: str, chat_id: int) -> None:
        raw = text.strip()
        if raw.startswith("/"):
            raw = raw[1:]
            # /command@bot
            raw = raw.split("@", 1)[0] + raw[raw.find(" ") :] if " " in raw and "@" in raw.split()[0] else raw
            if "@" in raw.split()[0]:
                cmd, *rest = raw.split(None, 1)
                raw = cmd.split("@", 1)[0] + ((" " + rest[0]) if rest else "")
        if self._awaiting_live:
            if raw.strip() == "LIVE":
                self._awaiting_live = False
                await self._switch_live(confirmed=True)
            else:
                self._awaiting_live = False
                await self.send("LIVE cancelled. Type live and then LIVE to confirm.")
            return
        low = raw.lower().strip()
        if low in ("start sellall", "sellall", "sell all", "stop & sell all"):
            await self._confirm_sellall()
            return
        if low == "wake":
            await self._wake()
            return
        m = re.match(r"quiet(?:\s+([0-9]+(?:\.[0-9]+)?)\s*h?)?$", low)
        if m:
            await self._quiet(float(m.group(1)) if m.group(1) else None)
            return
        if low == "resume":
            if self.cfg is None:
                return
            was = session.resume_entries(self.cfg)
            await self.send("Новите входове продължават." if was else "Входовете не бяха на пауза.")
            return
        if low in ("start", "help", "?"):
            await self.send("Your desk is online.\n" + HELP)
            return
        if low == "status":
            await self.send(*self._status())
            return
        if low == "invite":
            await self.send(f"Invite a friend:\n{links.SIGNUP_URL}")
            return
        if low == "paper":
            if self.cfg:
                self.cfg.set("desk.mode", "PAPER", who="telegram")
            await self.send("Mode is PAPER.")
            return
        if low == "live":
            await self._switch_live(confirmed=False)
            return
        if low == "report":
            await self._report()
            return
        m = re.match(r"pause\s+(\w+)$", low)
        if m:
            await self._pause(m.group(1), True)
            return
        m = re.match(r"resume\s+(\w+)$", low)
        if m:
            await self._pause(m.group(1), False)
            return
        m = re.match(r"stack\s+([0-9_.]+)$", low)
        if m:
            await self._set("desk.live_stack", m.group(1).replace("_", ""), extra_ticket=True)
            return
        if low.startswith("set "):
            await self._set_command(raw[4:].strip())
            return
        m = re.match(r"(buy|sell|why)\s+(.+)$", raw, re.I)
        if m:
            await self._coin_command(m.group(1).lower(), m.group(2).strip())
            return
        await self.send("I didn't catch that.\n" + HELP)

    async def _confirm_sellall(self) -> None:
        if self.db is None or self.cfg is None:
            return
        opens = self.db.open_positions()
        names = ", ".join((p.get("name") or p["mint"][:6]) for p in opens) or "няма отворени позиции"
        await self.send(
            "PAPER · Спри и продай всичко?\n"
            f"Отворени: {len(opens)} ({names})\n"
            "Затваря всички PAPER позиции по текущата цена от DexScreener и спира новите входове до /resume. "
            "Не пипа FOMO и истински пари.",
            [
                {"text": "Да, продай всичко", "callback": "sellall:yes"},
                {"text": "Отказ", "callback": "sellall:no"},
            ],
        )

    async def _do_sellall(self) -> None:
        if self.db is None or self.cfg is None:
            return
        mints = list({p["mint"] for p in self.db.open_positions()})
        pairs: dict = {}
        if mints and self.sources is not None:
            try:
                pairs = await asyncio.to_thread(self.sources.pumpswap_pairs, mints)
            except Exception as e:
                log.warning("sellall price fetch failed: %s", type(e).__name__)
                pairs = {}
            missing = [m for m in mints if m not in pairs]
            if missing and hasattr(self.sources, "any_dex_pairs"):
                try:
                    pairs.update(await asyncio.to_thread(self.sources.any_dex_pairs, missing))
                except Exception as e:
                    log.warning("sellall any-dex fetch failed: %s", type(e).__name__)
        result = session.sell_all_paper(self.db, self.cfg, pairs)
        await self.send(session.sellall_text(result, self.db, self.cfg))

    async def _quiet(self, hours: float | None) -> None:
        if self.db is None or self.cfg is None:
            return
        st = session.start_quiet(self.db, self.cfg, hours=hours)
        when = f" до {_sofia_hm(st['until'])} (Europe/Sofia)" if st.get("until") else " до /wake"
        await self.send(
            f"Тих режим{when}. Задържам известията за сделки и ще ти пратя едно обобщение накрая. "
            "Критичните аларми минават."
        )

    async def _wake(self) -> None:
        if self.db is None or self.cfg is None:
            return
        if not session.is_quiet(self.db):
            summary = session.wake_summary(self.db, self.cfg)
            await self.send("Тихият режим не беше включен.\n" + summary["text"])
            return
        summary = session.end_quiet(self.db, self.cfg)
        await self.send(summary["text"])

    async def _switch_live(self, confirmed: bool) -> None:
        if self.cfg is None or self.db is None:
            await self.send("Desk is not ready.")
            return
        # Existing night gate first, then the hard win-rate gate. Never skip either.
        reason = live_gate.refuse_live(self.db, self.cfg)
        if reason:
            await self.send(reason)
            return
        if not confirmed:
            self._awaiting_live = True
            await self.send("Type LIVE to confirm.")
            return
        self.cfg.set("desk.mode", "LIVE", who="telegram")
        await self.send("Mode is LIVE.")

    async def _pause(self, name: str, pause: bool) -> None:
        agent = name.lower()
        if agent not in AGENT_NAMES:
            await self.send("Agents: " + ", ".join(AGENT_NAMES))
            return
        if self.cfg is None or self.db is None:
            return
        if pause and agent == "risk" and self.db.open_positions():
            await self.send("Risk can't be paused while a position is open.")
            return
        if pause:
            self.cfg.pause(agent, who="telegram")
            await self.send(f"Paused {agent}.")
        else:
            self.cfg.resume(agent, who="telegram")
            await self.send(f"Resumed {agent}.")

    async def _set_command(self, body: str) -> None:
        # "stop to -8%" or "stop -8%" or "scanner.min_liquidity_usd 50000"
        body = re.sub(r"^to\s+", "", body, flags=re.I)
        m = re.match(r"([A-Za-z0-9_.]+)\s+(?:to\s+)?(.+)$", body)
        if not m:
            await self.send("Use: set stop to -8%")
            return
        await self._set(m.group(1), m.group(2).strip())

    async def _set(self, name: str, raw: str, extra_ticket: bool = False) -> None:
        if self.cfg is None:
            return
        try:
            key, old, new = self.cfg.set(name, raw, who="telegram")
        except KeyError:
            await self.send(f"Unknown setting {name}.")
            return
        except ValueError as e:
            await self.send(str(e))
            return
        pretty_old = _pretty(key, old)
        pretty_new = _pretty(key, new)
        msg = f"{_short(key)} {pretty_old} -> {pretty_new}"
        if key == "risk.stop_pct" and self.db is not None:
            tightened = tighten_open_stops(self.db, float(new))
            if tightened:
                msg += f"\nTightened {tightened} open stop(s). Looser stops on open positions were kept."
        ticket_keys = {"executor.min_ticket_usd", "executor.stack_pct", "executor.below_min", "desk.live_stack", "desk.paper_start_stack"}
        if key in ticket_keys or extra_ticket:
            check = self.cfg.ticket_check()
            if check.get("message"):
                msg += "\n" + check["message"]
                if check["below_min"] == "skip" and check["block"]:
                    msg += "\nSwitch to bump or raise the stack."
        await self.send(msg)

    def _status(self) -> tuple[str, list | None]:
        if self.cfg is None or self.db is None:
            return ("Desk is not ready.", None)
        mode = str(self.cfg.get("desk.mode") or "PAPER")
        bal = self.db.last_balance()
        if bal is None:
            bal = float(self.cfg.get("desk.paper_start_stack") or 0)
        lines = [f"{mode}", f"Balance ${bal:,.2f}"]
        buttons: list[dict] = []
        opens = self.db.open_positions()
        if not opens:
            lines.append("No open positions.")
        for p in opens:
            name = p.get("name") or p["mint"]
            entry = p.get("entry_price")
            size = p.get("size_usd")
            lines.append(f"{name} · ${size:.2f} @ {entry} · {p.get('status')}")
            link = links.coin_url(p["mint"], "solana")
            lines.append(link)
            buttons.append({"text": f"Sell on FOMO · {name}", "url": link})
        return "\n".join(lines), buttons or None

    async def _report(self) -> None:
        if self.db is None or self.cfg is None:
            await self.send("No report yet.")
            return
        from report import build_night_summary

        data = build_night_summary(self.db, self.cfg)
        md = data.get("markdown") or "No night to report yet."
        # Telegram cap 4096
        if len(md) > 4000:
            md = md[:3990] + "…"
        await self.send(md)

    async def _coin_command(self, verb: str, query: str) -> None:
        if self.db is None or self.cfg is None:
            return
        coins = self.db.find_coins(query)
        if not coins:
            await self.send(f"I don't know {query}. Send the mint?")
            return
        if len(coins) > 1:
            lines = ["Which one?"]
            buttons = []
            for c in coins:
                label = f"{c.get('name') or ''} ({c.get('symbol') or c['mint'][:6]})"
                url = links.coin_url(c["mint"], "solana")
                lines.append(f"{label}\n{url}")
                buttons.append({"text": label[:32], "url": url})
            await self.send("\n".join(lines), buttons)
            return
        coin = coins[0]
        mint = coin["mint"]
        name = coin.get("name") or coin.get("symbol") or mint
        url = links.coin_url(mint, "solana")
        if verb == "buy":
            night = self.db.current_night()
            since = night["started_at"] if night else ""
            pushed = []
            if since:
                pushed = [s for s in self.db.signals_since(since) if s["mint"] == mint]
            if pushed:
                last = pushed[-1]
                mode = str(self.cfg.get("desk.mode") or "PAPER")
                prefix = "PAPER · " if mode.upper() == "PAPER" else ""
                size = last.get("size_usd") or 0
                pct = last.get("stack_pct") or 0
                text = f"{prefix}Buy {name} · ${size:.2f} · {pct:g}% of stack"
                await self.send(
                    text,
                    [
                        {"text": "Buy on FOMO", "url": url},
                        {"text": "Bought", "callback": f"bought:{mint}"},
                        {"text": "Skip", "callback": f"skip:{mint}"},
                    ],
                )
                return
            # not sized: run auditor now if we can? Without live RPC in this
            # command path we show last audit layers, or say we'll need sources.
            layers = self.db.audits_for(mint)
            lines = [f"{name}", "not sized, not on the desk", url]
            if layers:
                for l in layers:
                    mark = "ok" if l["pass"] == 1 else ("red" if l["pass"] == 0 else "—")
                    lines.append(f"{l['layer']}. {l['name']}: {l['value_text']} ({mark})")
            else:
                lines.append("No audit on file yet.")
            await self.send("\n".join(lines), [{"text": "Buy on FOMO", "url": url}])
            return
        if verb == "sell":
            opens = [p for p in self.db.open_positions() if p["mint"] == mint]
            if not opens:
                await self.send(f"No open position in {name}.\n{url}")
                return
            await self.send(
                f"Sell {name}",
                [{"text": "Sell on FOMO", "url": url}, {"text": "Sold", "callback": f"sold:{mint}"}],
            )
            return
        # why
        await self.send(self._why(coin) + f"\n{url}")

    def _why(self, coin: dict) -> str:
        mint = coin["mint"]
        name = coin.get("name") or coin.get("symbol") or mint
        scan = self.db.query_one("SELECT * FROM scans WHERE mint=? ORDER BY id DESC LIMIT 1", (mint,))
        if scan and scan["verdict"] == "kill":
            return f"Scanner stopped {name}: {scan['reason']} at {scan['ts']}"
        red = self.db.query_one(
            "SELECT * FROM audits WHERE mint=? AND pass=0 ORDER BY id DESC LIMIT 1", (mint,)
        )
        if red:
            return f"Auditor stopped {name} at layer {red['layer']} ({red['name']}): {red['value_text']} at {red['ts']}"
        verd = self.db.query_one("SELECT * FROM verdicts WHERE mint=? ORDER BY id DESC LIMIT 1", (mint,))
        if verd and not verd["pass"]:
            return f"Analyst stopped {name}: {verd['reason']} (score {verd['score']}) at {verd['ts']}"
        sig = self.db.last_signal(mint)
        if sig and sig["status"] in ("skipped", "missed"):
            return f"Executor {sig['status']} {name}: {sig['reason']} at {sig['ts']}"
        pos = self.db.query_one("SELECT * FROM positions WHERE mint=? ORDER BY id DESC LIMIT 1", (mint,))
        if pos and pos["exit_reason"]:
            return f"Risk exited {name}: {pos['exit_reason']} at {pos['closed_at'] or pos['opened_at']}"
        return f"No agent has stopped {name} (yet)."


def _sofia_hm(iso: str | None) -> str:
    from zoneinfo import ZoneInfo
    from agents.risk import parse_ts

    t = parse_ts(iso)
    return t.astimezone(ZoneInfo("Europe/Sofia")).strftime("%d.%m %H:%M") if t else "?"


def _inline(buttons: list[dict]) -> dict:
    rows = []
    row: list[dict] = []
    for b in buttons:
        if "url" in b:
            btn = {"text": b["text"], "url": b["url"]}
        else:
            btn = {"text": b["text"], "callback_data": b.get("callback") or b.get("callback_data") or "noop"}
        row.append(btn)
        if len(row) == 3:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    return {"inline_keyboard": rows}


def _short(key: str) -> str:
    return key.split(".")[-1].replace("_pct", "").replace("_", " ")


def _pretty(key: str, value: Any) -> str:
    if value is None:
        return "n/a"
    if key.endswith("_pct") or key in ("risk.stop_pct", "risk.trail_pct"):
        try:
            n = float(value)
            if key.startswith("risk.stop") or key.endswith("stop_pct"):
                return f"-{n:g}%"
            return f"{n:g}%"
        except (TypeError, ValueError):
            return str(value)
    if isinstance(value, float):
        return f"{value:g}"
    return str(value)


async def _maybe(res):
    if asyncio.iscoroutine(res):
        return await res
    return res


def sample_buy_push(mint: str, name: str = "TEST") -> tuple[str, list[dict]]:
    """PAPER-prefixed sample used to prove button rendering."""
    text = f"PAPER · Buy {name}"
    url = links.coin_url(mint, "solana")
    buttons = [{"text": "Buy on FOMO", "url": url}]
    return text, buttons

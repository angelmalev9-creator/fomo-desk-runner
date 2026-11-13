"""Desk entrypoint: start the night and the five agents (Telegram only)."""

from __future__ import annotations

import asyncio
import signal

from agents import analyst, auditor, copywatch, executor, risk, scanner, strategies
from bus import Bus
from config import Config
from db import DB
from rpc import Rpc
from sources import HttpSources
from telegram import TelegramNotifier, sample_buy_push
from util import ROOT, agent_logger, load_secrets, same_night, secret, utcnow

log = agent_logger("main")


async def start_night(db: DB, cfg: Config, notifier: TelegramNotifier | None) -> dict:
    """Ticket check, seed PAPER balance, open a nights row.

    A restart inside the same trading night (report_time to report_time, Europe/Sofia)
    continues the open night instead of opening a new one.
    """
    mode = str(cfg.get("desk.mode") or "PAPER").upper()
    report_time = str(cfg.get("desk.report_time") or "08:00")
    cur = db.current_night()
    if cur and str(cur.get("mode") or "").upper() == mode and same_night(cur.get("started_at"), utcnow(), report_time):
        if mode == "PAPER" and db.last_balance() is None:
            db.add_balance(float(cur.get("start_stack") or cfg.get("desk.paper_start_stack") or 200))
        log.info("night %s continued after restart mode=%s", cur["id"], mode)
        return {"night_id": cur["id"], "ticket": {}, "mode": mode, "start_stack": cur.get("start_stack"), "continued": True}
    start_stack = (
        float(cfg.get("desk.live_stack") or 0)
        if mode == "LIVE"
        else float(cfg.get("desk.paper_start_stack") or 200)
    )
    check = cfg.ticket_check(stack=start_stack)
    if notifier and check.get("message"):
        text = check["message"]
        if check.get("block"):
            text += "\nSwitch to bump or raise the stack. Don't start an empty night silently."
        prefix = "PAPER · " if mode == "PAPER" else ""
        await notifier.send(prefix + text if not text.startswith("PAPER") else text)
    if mode == "PAPER" and db.last_balance() is None:
        db.add_balance(start_stack)
    night_id = db.start_night(mode, start_stack)
    log.info("night %s mode=%s stack=%s ticket=%s", night_id, mode, start_stack, check.get("message"))
    return {"night_id": night_id, "ticket": check, "mode": mode, "start_stack": start_stack}


async def run(stop_event: asyncio.Event | None = None) -> None:
    load_secrets()
    db = DB()
    cfg = Config(db=db)
    # Defensive: if desk.mode was somehow left LIVE, snap back to PAPER when the gate fails.
    # No Telegram message here — only a log line.
    if str(cfg.get("desk.mode") or "PAPER").upper() == "LIVE":
        try:
            import live_gate
            reason = live_gate.refuse_live(db, cfg)
        except Exception:
            reason = "live gate check failed"
        if reason:
            cfg.set("desk.mode", "PAPER", who="main")
            log.info("LIVE refused at startup, stayed PAPER: %s", reason.split(".")[0])
    bus = Bus()
    rpc = Rpc(
        str(cfg.get("desk.rpc_url")),
        retries=int(cfg.get("auditor.retries") or 3),
        retry_wait_s=float(cfg.get("auditor.retry_wait_s") or 2),
    )
    sources = HttpSources()
    owner = cfg.get("desk.owner_chat_id") or 0
    notifier = TelegramNotifier(
        token=secret("TG_TOKEN"),
        owner_chat_id=int(owner) if owner else None,
        db=db,
        cfg=cfg,
    )

    stop = stop_event or asyncio.Event()

    def _stop(*_a) -> None:
        stop.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _stop)
        except NotImplementedError:
            pass

    await start_night(db, cfg, notifier)

    async def on_fill(mint: str):
        await bus.risk.put({"mint": mint, "event": "filled"})

    notifier.on_fill = on_fill
    notifier.sources = sources

    tasks = [
        asyncio.create_task(scanner.run_agent(db, cfg, bus, rpc=rpc, sources=sources, stop_event=stop), name="scanner"),
        asyncio.create_task(
            auditor.run_agent(db, cfg, bus, rpc=rpc, sources=sources, notifier=notifier, stop_event=stop),
            name="auditor",
        ),
        asyncio.create_task(analyst.run_agent(db, cfg, bus, sources=sources, stop_event=stop), name="analyst"),
        asyncio.create_task(
            executor.run_agent(db, cfg, bus, notifier=notifier, sources=sources, stop_event=stop),
            name="executor",
        ),
        asyncio.create_task(
            risk.run_agent(db, cfg, bus, notifier=notifier, sources=sources, stop_event=stop),
            name="risk",
        ),
        asyncio.create_task(notifier.run_polling(stop_event=stop), name="telegram"),
        asyncio.create_task(
            copywatch.run_agent(db, cfg, bus, rpc, sources=sources, notifier=notifier, stop_event=stop),
            name="copywatch",
        ),
        asyncio.create_task(
            strategies.run_agent(db, cfg, bus, sources=sources, notifier=notifier, stop_event=stop),
            name="strategies",
        ),
        asyncio.create_task(_heartbeat_loop(db, stop), name="heartbeats"),
        asyncio.create_task(_equity_loop(db, cfg, stop), name="equity"),
    ]
    log.info("desk up, %s tasks", len(tasks))
    try:
        await stop.wait()
    finally:
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        log.info("desk stopped")


async def _heartbeat_loop(db: DB, stop: asyncio.Event) -> None:
    while not stop.is_set():
        db.heartbeat("main")
        try:
            await asyncio.wait_for(stop.wait(), timeout=30)
        except asyncio.TimeoutError:
            pass


async def _equity_loop(db: DB, cfg: Config, stop: asyncio.Event) -> None:
    """PAPER equity snapshots (realized stack + open marks) for the live chart."""
    import session

    while not stop.is_set():
        try:
            session.record_equity(db, cfg)
        except Exception as e:
            log.warning("equity snapshot failed: %s", type(e).__name__)
        try:
            await asyncio.wait_for(stop.wait(), timeout=60)
        except asyncio.TimeoutError:
            pass


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()

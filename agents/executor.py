"""Executor: size the trade and send one Buy button, never place an order."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Callable

import links
from agents.risk import stop_price_at_entry
from fields import dex_float
from sources import maybe_note_jup_price
from util import agent_logger, as_float, iso_utc, round_cents, utcnow

log = agent_logger("executor")


@dataclass
class SizeResult:
    size_usd: float
    stack_pct: float  # actual size / stack * 100 after bump/caps
    raw_size: float
    cap: str | None  # "pool" | "bump" | None
    skip_reason: str | None


def size_trade(
    stack: float,
    open_notional: float,
    liquidity_usd: float | None,
    cfg,
) -> SizeResult:
    """Arithmetic only. Never places an order."""
    stack = float(stack or 0)
    free = stack - float(open_notional or 0)
    pct = float(cfg.get("executor.stack_pct") or 8)
    max_pool = float(cfg.get("executor.max_pool_pct") or 2)
    min_ticket = float(cfg.get("executor.min_ticket_usd") or 47.50)
    below = str(cfg.get("executor.below_min") or "bump").lower()

    raw = stack * (pct / 100.0)
    cap = None
    size = raw
    if liquidity_usd is not None:
        pool_cap = float(liquidity_usd) * (max_pool / 100.0)
        if size > pool_cap:
            size = pool_cap
            cap = "pool"
    if size < min_ticket:
        if below == "bump":
            if free + 1e-9 >= min_ticket:
                size = min_ticket
                cap = "bump"
            else:
                return SizeResult(0.0, 0.0, raw, cap, f"under ${min_ticket:.2f}")
        else:
            return SizeResult(0.0, 0.0, raw, cap, f"under ${min_ticket:.2f}")
    size = round_cents(size)
    if size > free + 1e-9:
        return SizeResult(0.0, 0.0, raw, cap, "no free stack")
    stack_pct = (size / stack * 100.0) if stack else 0.0
    return SizeResult(size, stack_pct, raw, cap, None)


def recheck(
    pair: dict | None,
    scanner_liq: float | None,
    analyst_price: float | None,
    cfg,
) -> str | None:
    price = dex_float(pair, "priceUsd")
    liq = dex_float(pair, "liquidity.usd")
    if price is None:
        return "no price"
    drop_pct = float(cfg.get("executor.recheck_liq_drop_pct") or 20)
    up_pct = float(cfg.get("executor.recheck_price_up_pct") or 15)
    if scanner_liq and liq is not None and scanner_liq > 0:
        if (scanner_liq - liq) / scanner_liq * 100.0 > drop_pct:
            return "liquidity dropping"
    if analyst_price and analyst_price > 0 and price is not None:
        if (price - analyst_price) / analyst_price * 100.0 > up_pct:
            return "ran before the push"
    return None


def push_text(
    name: str,
    size_usd: float,
    stack_pct: float,
    score: int | float | None,
    stop_pct: float,
    trail_pct: float,
    mode: str,
) -> str:
    prefix = "PAPER · " if str(mode).upper() == "PAPER" else ""
    return (
        f"{prefix}Buy {name} · ${size_usd:.2f} · {stack_pct:g}% of stack\n"
        f"Auditor 8/8 · Analyst {score} · stop -{stop_pct:g}% · trail {trail_pct:g}%"
    )


def decide(
    mint: str,
    name: str,
    stack: float,
    open_notional: float,
    pair: dict | None,
    scanner: dict[str, Any] | None,
    analyst: dict[str, Any] | None,
    already_signaled: bool,
    already_open: bool,
    skipped: bool,
    cfg,
) -> dict[str, Any]:
    mode = str(cfg.get("desk.mode") or "PAPER").upper()
    link = links.coin_url(mint, "solana")
    base = {
        "agent": "executor",
        "mint": mint,
        "signalPrice": None,
        "sizeUsd": 0.0,
        "stackPct": 0.0,
        "link": link,
        "mode": mode,
        "status": "skipped",
        "reason": None,
        "text": None,
        "raw_size": None,
        "cap": None,
    }
    if already_signaled or already_open or skipped:
        base["reason"] = "already handled"
        return base
    price = dex_float(pair, "priceUsd")
    if price is None:
        base["reason"] = "no price"
        return base
    liq = dex_float(pair, "liquidity.usd")
    sized = size_trade(stack, open_notional, liq, cfg)
    base["raw_size"] = sized.raw_size
    base["cap"] = sized.cap
    if sized.skip_reason:
        base["reason"] = sized.skip_reason
        return base
    scanner_liq = None
    if scanner:
        scanner_liq = as_float(scanner.get("liquidityUsd") or scanner.get("liquidity_usd"))
    analyst_price = None
    if analyst:
        analyst_price = as_float(analyst.get("priceUsd") or analyst.get("signalPrice"))
    # Analyst JSON has no price; the fresh pair is compared to a price
    # stashed on the payload when Analyst ran, if present.
    if analyst and analyst.get("dexPrice") is not None:
        analyst_price = as_float(analyst.get("dexPrice"))
    reason = recheck(pair, scanner_liq, analyst_price, cfg)
    if reason:
        base["reason"] = reason
        return base
    stop_pct = float(cfg.get("risk.stop_pct") or 6)
    trail_pct = float(cfg.get("risk.trail_pct") or 12)
    score = (analyst or {}).get("score")
    text = push_text(name, sized.size_usd, sized.stack_pct, score, stop_pct, trail_pct, mode)
    base.update(
        {
            "signalPrice": price,
            "sizeUsd": sized.size_usd,
            "stackPct": sized.stack_pct,
            "status": "sent",
            "reason": None,
            "text": text,
        }
    )
    return base


async def run_agent(
    db,
    cfg,
    bus,
    notifier=None,
    sources=None,
    stop_event: asyncio.Event | None = None,
) -> None:
    """Size, push one Buy button, fill PAPER immediately, wait for LIVE tap."""
    while stop_event is None or not stop_event.is_set():
        try:
            item = await asyncio.wait_for(bus.executor.get(), timeout=2.0)
        except asyncio.TimeoutError:
            db.heartbeat("executor")
            await _expire_missed(db, cfg)
            continue
        db.heartbeat("executor")
        if cfg.is_paused("executor"):
            continue
        mint = item.get("mint")
        if not mint:
            continue
        already = db.last_signal(mint) is not None
        open_pos = [p for p in db.open_positions() if p["mint"] == mint]
        skipped = db.is_skipped(mint)
        pair = item.get("pair")
        if pair is None and sources is not None:
            try:
                pair = sources.pumpswap_pair(mint)
            except Exception:
                pair = None
        open_notional = sum(float(p["size_usd"] or 0) for p in db.open_positions())
        stack = _stack_for_sizing(db, cfg)
        coin = db.get_coin(mint) or {}
        name = item.get("name") or coin.get("name") or mint
        result = decide(
            mint=mint,
            name=name,
            stack=stack,
            open_notional=open_notional,
            pair=pair,
            scanner=item.get("scanner"),
            analyst=item.get("analyst"),
            already_signaled=already,
            already_open=bool(open_pos),
            skipped=skipped,
            cfg=cfg,
        )
        log.info(
            "mint=%s stack=%s raw=%s cap=%s final=%s mode=%s status=%s link=%s",
            mint,
            stack,
            result.get("raw_size"),
            result.get("cap"),
            result.get("sizeUsd"),
            result.get("mode"),
            result.get("status"),
            result.get("link"),
        )
        if result["status"] != "skipped":
            # Four-agent desk: Sniper trades only inside its own sleeve (flag agents.sleeves_enabled).
            try:
                from agents import strategies

                full = strategies.sleeve_check(db, cfg, "sniper", float(result.get("sizeUsd") or 0), stack)
            except Exception as e:  # fail closed
                full = f"sleeve check failed ({type(e).__name__})"
            if full:
                result["status"] = "skipped"
                result["reason"] = full
        if result["status"] == "skipped":
            db.add_signal(
                mint=mint,
                price=result.get("signalPrice"),
                size_usd=result.get("sizeUsd"),
                stack_pct=result.get("stackPct"),
                link=result.get("link"),
                mode=result.get("mode"),
                status="skipped",
                reason=result.get("reason"),
                score=(item.get("analyst") or {}).get("score"),
            )
            if result.get("reason") and result["reason"] != "already handled":
                db.add_skip(mint)
            await bus.emit("signal", result)
            continue

        db.add_signal(
            mint=mint,
            price=result["signalPrice"],
            size_usd=result["sizeUsd"],
            stack_pct=result["stackPct"],
            link=result["link"],
            mode=result["mode"],
            status="sent",
            score=(item.get("analyst") or {}).get("score"),
        )
        pushed = await _push_buy(notifier, result, mint)
        if not pushed:
            log.warning("push failed mint=%s", mint)
            if result["mode"] == "LIVE":
                db.update_signal_status(mint, "skipped", "push failed")
                result["status"] = "skipped"
                result["reason"] = "push failed"
                await bus.emit("signal", result)
                continue
            # PAPER: fill anyway so the night stays honest.
        if result["mode"] == "PAPER":
            _fill(db, cfg, result, name, sources=sources)
            result["status"] = "bought"
            db.update_signal_status(mint, "bought")
            await bus.risk.put({"mint": mint, "event": "filled"})
        await bus.emit("signal", result)


def _stack_for_sizing(db, cfg) -> float:
    mode = str(cfg.get("desk.mode") or "PAPER").upper()
    if mode == "LIVE":
        return float(cfg.get("desk.live_stack") or 0)
    last = db.last_balance()
    if last is not None:
        return float(last)
    return float(cfg.get("desk.paper_start_stack") or 0)


def _fill(db, cfg, result: dict[str, Any], name: str, sources=None) -> int:
    entry = float(result["signalPrice"])
    stop_pct = float(cfg.get("risk.stop_pct") or 6)
    stop_price = stop_price_at_entry(entry, entry, stop_pct, cfg)
    pid = db.open_position(
        mint=result["mint"],
        entry_price=entry,
        size_usd=result["sizeUsd"],
        peak_price=entry,
        stop_price=stop_price,
        mode=result["mode"],
        status="open",
        name=name,
    )
    now = utcnow()
    hhmm = now.strftime("%H:%M")
    db.add_activity(
        kind="bought",
        mint=result["mint"],
        agent="executor",
        title=f"Bought {name}",
        subtitle=f"Executor · {hhmm}",
        value=f"${result['sizeUsd']:.2f} · {result['stackPct']:g}% of stack",
    )
    if str(result.get("mode") or "").upper() == "PAPER":
        maybe_note_jup_price(db, sources, result["mint"], entry, "entry", "executor")
    return pid


async def _push_buy(notifier, result: dict[str, Any], mint: str) -> bool:
    if notifier is None:
        return True
    buttons = [
        {"text": "Buy on FOMO", "url": result["link"]},
        {"text": "Bought", "callback": f"bought:{mint}"},
        {"text": "Skip", "callback": f"skip:{mint}"},
    ]
    for attempt in range(3):
        try:
            ok = await maybe_await(notifier.send(result["text"], buttons=buttons, mint=mint))
            if ok is not False:
                return True
        except Exception as e:
            log.warning("telegram push attempt %s failed: %s", attempt + 1, type(e).__name__)
        await asyncio.sleep(1)
    return False


async def maybe_await(res):
    if asyncio.iscoroutine(res):
        return await res
    return res


async def _expire_missed(db, cfg) -> None:
    window = float(cfg.get("executor.confirm_window_min") or 5)
    # LIVE signals still "sent" older than the window become "missed"
    rows = db.query(
        "SELECT * FROM signals WHERE status='sent' AND mode='LIVE'"
    )
    now = utcnow()
    from datetime import datetime, timezone, timedelta

    for row in rows:
        ts = _parse_ts(row["ts"])
        if ts and now - ts >= timedelta(minutes=window):
            db.update_signal_status(row["mint"], "missed", "no tap")
            log.info("mint=%s status=missed", row["mint"])


def _parse_ts(raw: str | None):
    if not raw:
        return None
    from datetime import datetime, timezone

    s = raw.replace("Z", "+00:00")
    try:
        t = datetime.fromisoformat(s)
    except ValueError:
        return None
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    return t


def on_bought(db, cfg, mint: str) -> dict[str, Any] | None:
    """LIVE callback: open the position at the signal price."""
    sig = db.last_signal(mint)
    if not sig or sig.get("status") not in ("sent",):
        return None
    if any(p["mint"] == mint for p in db.open_positions()):
        return None
    result = {
        "mint": mint,
        "signalPrice": sig["price"],
        "sizeUsd": sig["size_usd"],
        "stackPct": sig["stack_pct"],
        "mode": sig["mode"],
        "link": sig["link"],
        "status": "bought",
    }
    coin = db.get_coin(mint) or {}
    _fill(db, cfg, result, coin.get("name") or mint)
    db.update_signal_status(mint, "bought")
    return result


def on_skip(db, mint: str) -> None:
    db.update_signal_status(mint, "skipped", "skipped by owner")
    db.add_skip(mint)

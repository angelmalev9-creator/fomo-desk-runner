"""Video-inspired PAPER features: quiet mode, wake summary, stop & sell all, equity.

PAPER only. Nothing here places an order, logs into FOMO, or touches a wallet.
Every coin link comes from links.py.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import fees
import links
from agents.risk import close_paper, parse_ts
from fields import dex_float
from util import ROOT, agent_logger, iso_utc, round_cents, utcnow

log = agent_logger("session")

PUBLIC_CONFIG = ROOT / "public_config.json"
GIST_CONFIG = ROOT / "gist_config.json"
DEFAULT_DASHBOARD = "https://angelmalev9-creator.github.io/fomo-desk-dashboard/"


# ---------------------------------------------------------------- config
def public_config() -> dict[str, Any]:
    try:
        return json.loads(PUBLIC_CONFIG.read_text(encoding="utf-8"))
    except Exception:
        return {}


def bot_username() -> str | None:
    return public_config().get("bot_username") or None


def sellall_deep_link() -> str | None:
    u = bot_username()
    return f"https://t.me/{u}?start=sellall" if u else None


def dashboard_url() -> str:
    base = public_config().get("dashboard_base") or DEFAULT_DASHBOARD
    try:
        gid = json.loads(GIST_CONFIG.read_text(encoding="utf-8")).get("gist_id")
    except Exception:
        gid = None
    return f"{base}#g={gid}" if gid else base


# ---------------------------------------------------------------- quiet mode
def quiet_state(db) -> dict[str, Any]:
    on = (db.kv_get("quiet.on") or "") == "1"
    until = db.kv_get("quiet.until") or None
    started = db.kv_get("quiet.started_at") or None
    start_stack = db.kv_get("quiet.start_stack")
    return {
        "on": on,
        "until": until,
        "started_at": started,
        "start_stack": float(start_stack) if start_stack not in (None, "") else None,
        "queued": len(db.queued_pushes()) if on else 0,
    }


def is_quiet(db) -> bool:
    return db is not None and (db.kv_get("quiet.on") or "") == "1"


def current_stack(db, cfg) -> float:
    last = db.last_balance()
    if last is not None:
        return float(last)
    return float(cfg.get("desk.paper_start_stack") or 200)


def start_quiet(db, cfg, hours: float | None = None, now: datetime | None = None) -> dict[str, Any]:
    now = now or utcnow()
    if not is_quiet(db):
        db.kv_set("quiet.started_at", iso_utc(now))
        db.kv_set("quiet.start_stack", str(current_stack(db, cfg)))
    db.kv_set("quiet.on", "1")
    db.kv_set("quiet.until", iso_utc(now + timedelta(hours=float(hours))) if hours else "")
    log.info("quiet on until=%s", db.kv_get("quiet.until") or "manual")
    return quiet_state(db)


def due_auto_wake(db, now: datetime | None = None) -> bool:
    if not is_quiet(db):
        return False
    until = parse_ts(db.kv_get("quiet.until") or None)
    return bool(until and (now or utcnow()) >= until)


def wake_summary(db, cfg, now: datetime | None = None) -> dict[str, Any]:
    """Build the ONE summary sent on /wake. Read-only."""
    now = now or utcnow()
    started = db.kv_get("quiet.started_at") or None
    night = db.current_night()
    since = started or (night or {}).get("started_at") or "1970-01-01T00:00:00.000Z"
    raw_start = db.kv_get("quiet.start_stack") if started else None
    start_stack = float(raw_start) if raw_start not in (None, "") else (
        float((night or {}).get("start_stack") or cfg.get("desk.paper_start_stack") or 200)
    )
    end_stack = current_stack(db, cfg)
    closed = db.closed_positions_since(since)
    wins = [p for p in closed if (p.get("pnl_usd") or 0) >= 0]
    losses = [p for p in closed if (p.get("pnl_usd") or 0) < 0]
    stops = [p for p in closed if p.get("exit_reason") == "stop"]
    best = sorted(closed, key=lambda p: float(p.get("pnl_usd") or 0), reverse=True)[:3]
    opens = db.open_positions()
    queued = db.queued_pushes()
    mult = (end_stack / start_stack) if start_stack else None
    change = end_stack - start_stack
    pct = (change / start_stack * 100.0) if start_stack else 0.0
    mode = str(cfg.get("desk.mode") or "PAPER").upper()
    hours = None
    st = parse_ts(started)
    if st:
        hours = (now - st).total_seconds() / 3600.0

    lines = [f"{'PAPER · ' if mode == 'PAPER' else ''}Обобщение" + (f" за {hours:.1f} ч тишина" if hours is not None else "")]
    sign = "+" if change >= 0 else "-"
    lines.append(f"Стек ${start_stack:,.2f} → ${end_stack:,.2f} ({sign}${abs(change):,.2f}, {pct:+.2f}%)")
    if mult is not None:
        lines.append(f"Множител ×{mult:.2f}")
    if closed:
        green_pct = len(wins) / len(closed) * 100.0
        lines.append(f"Сделки: {len(closed)} · зелени {len(wins)} ({green_pct:.0f}%) · червени {len(losses)} · стопове {len(stops)}")
    else:
        lines.append("Сделки: 0 · нямаше затворени сделки")
    if best:
        lines.append("Най-добри:")
        for p in best:
            name = p.get("name") or p["mint"]
            pnl = float(p.get("pnl_usd") or 0)
            lines.append(f"  {name} {'+' if pnl >= 0 else '-'}${abs(pnl):.2f} · {links.coin_url(p['mint'], 'solana')}")
    if opens:
        lines.append(f"Още отворени: {len(opens)}")
        for p in opens:
            name = p.get("name") or p["mint"]
            lines.append(f"  {name} · ${float(p.get('size_usd') or 0):.2f} · {links.coin_url(p['mint'], 'solana')}")
    else:
        lines.append("Още отворени: няма")
    lines.append(f"Задържани известия: {len(queued)}")
    lines.append(f"Табло: {dashboard_url()}")
    return {
        "text": "\n".join(lines),
        "start_stack": start_stack,
        "end_stack": end_stack,
        "multiplier": mult,
        "trades": len(closed),
        "wins": len(wins),
        "losses": len(losses),
        "stops": len(stops),
        "best": best,
        "open": opens,
        "queued": len(queued),
    }


def end_quiet(db, cfg, now: datetime | None = None) -> dict[str, Any]:
    """Build the summary, mark held pushes delivered, clear quiet state."""
    summary = wake_summary(db, cfg, now=now)
    db.mark_pushes_delivered()
    db.kv_set("quiet.on", "0")
    db.kv_set("quiet.until", "")
    db.kv_set("quiet.started_at", "")
    db.kv_set("quiet.start_stack", "")
    log.info("quiet off, held=%s trades=%s", summary["queued"], summary["trades"])
    return summary


# ---------------------------------------------------------------- stop & sell all
def entries_paused(cfg) -> bool:
    return cfg.is_paused("executor")


def sell_all_paper(db, cfg, pairs: dict[str, dict], now: datetime | None = None) -> dict[str, Any]:
    """Close every open PAPER position at the given DexScreener price and pause entries.

    A position with no fresh price stays open (never invent a price).
    LIVE positions are never touched here: FOMO has no order API.
    """
    now = now or utcnow()
    closed: list[dict[str, Any]] = []
    no_price: list[dict[str, Any]] = []
    live: list[dict[str, Any]] = []
    for pos in db.open_positions():
        mode = str(pos.get("mode") or "PAPER").upper()
        if mode != "PAPER":
            live.append(pos)
            continue
        price = dex_float(pairs.get(pos["mint"]), "priceUsd") if pairs.get(pos["mint"]) else None
        if price is None or price <= 0:
            no_price.append(pos)
            continue
        close = close_paper(pos, price, "sellall", cfg)
        db.update_position(
            pos["id"],
            closed_at=iso_utc(now),
            exit_price=close["exit_price"],
            exit_reason="sellall",
            pnl_usd=close["pnl_usd"],
            fees_usd=close["fees_usd"],
            status="closed",
            peak_price=max(float(pos.get("peak_price") or 0), price),
        )
        last = db.last_balance()
        if last is not None:
            db.add_balance(round_cents(last + close["pnl_usd"]))
        db.add_price(pos["mint"], price, None)
        pnl = close["pnl_usd"]
        name = pos.get("name") or pos["mint"]
        db.add_activity(
            kind="sold",
            mint=pos["mint"],
            agent="risk",
            title=f"Sold {name}",
            subtitle="Спри и продай всичко · PAPER",
            value=f"{'+' if pnl >= 0 else '-'}${abs(pnl):.2f} · {close['pct']:+g}%",
        )
        closed.append({**pos, **close, "name": name, "link": links.coin_url(pos["mint"], "solana")})
    if not entries_paused(cfg):
        cfg.pause("executor", who="sellall")
    db.kv_set("sellall.last_at", iso_utc(now))
    total = round_cents(sum(c["pnl_usd"] for c in closed))
    log.info("sellall closed=%s no_price=%s live=%s pnl=%s", len(closed), len(no_price), len(live), total)
    return {"closed": closed, "no_price": no_price, "live": live, "pnl_usd": total}


def sellall_text(result: dict[str, Any], db, cfg) -> str:
    lines = ["PAPER · Спри и продай всичко"]
    if not result["closed"] and not result["no_price"] and not result["live"]:
        lines.append("Нямаше отворени позиции.")
    for c in result["closed"]:
        pnl = c["pnl_usd"]
        lines.append(f"Продадено {c['name']} · {'+' if pnl >= 0 else '-'}${abs(pnl):.2f} ({c['pct']:+g}%)")
    for p in result["no_price"]:
        lines.append(f"{p.get('name') or p['mint']}: няма цена от DexScreener, остава отворена. Risk я следи.")
    for p in result["live"]:
        lines.append(f"{p.get('name') or p['mint']} е LIVE, продай я сама във FOMO: {links.coin_url(p['mint'], 'solana')}")
    if result["closed"]:
        t = result["pnl_usd"]
        lines.append(f"Общо: {'+' if t >= 0 else '-'}${abs(t):.2f}")
    lines.append(f"Стек: ${current_stack(db, cfg):,.2f}")
    lines.append("Новите входове са на пауза. Напиши /resume, за да продължи.")
    return "\n".join(lines)


def resume_entries(cfg) -> bool:
    was = entries_paused(cfg)
    if was:
        cfg.resume("executor", who="telegram")
    return was


# ---------------------------------------------------------------- equity
def equity_now(db, cfg) -> dict[str, Any]:
    realized = current_stack(db, cfg)
    discount = float(cfg.get("desk.fees_ref_discount") or 0.10)
    unreal = 0.0
    opens = db.open_positions()
    for p in opens:
        row = db.query_one("SELECT price FROM prices WHERE mint=? ORDER BY ts DESC LIMIT 1", (p["mint"],))
        if row and row["price"] and p.get("entry_price") and p.get("size_usd"):
            pnl, _fee = fees.pnl_usd(float(p["size_usd"]), float(p["entry_price"]), float(row["price"]), discount)
            unreal += pnl
    return {"value": round_cents(realized + unreal), "realized": realized, "open_n": len(opens)}


def record_equity(db, cfg, min_gap_s: int = 300, now: datetime | None = None) -> bool:
    """Store a snapshot when the value moved, or at most every min_gap_s when flat."""
    now = now or utcnow()
    snap = equity_now(db, cfg)
    last = db.last_equity()
    if last:
        age = (now - (parse_ts(last["ts"]) or now)).total_seconds()
        if abs(float(last["value"]) - snap["value"]) < 0.005 and last["open_n"] == snap["open_n"] and age < min_gap_s:
            return False
    db.add_equity(snap["value"], snap["realized"], snap["open_n"], ts=iso_utc(now))
    return True

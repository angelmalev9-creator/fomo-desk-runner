"""Dump the FOMO PAPER desk dashboard snapshot as JSON.

Opens desk.db read-only (WAL-safe). Does not import db.DB or config.Config,
so it never writes, never loads .secrets, and never prints tokens.

Usage:
  .venv/bin/python dashboard_data.py              # stdout
  .venv/bin/python dashboard_data.py -o out.json  # stdout + file
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import fees
import links
from util import night_window_start

ROOT = Path(__file__).resolve().parent
DB_PATH = ROOT / "desk.db"
ZONE = ZoneInfo("Europe/Sofia")
ZONE_LABEL = "Europe/Sofia"

# Expected agents (same set main.py starts).
AGENTS = ("main", "scanner", "auditor", "analyst", "executor", "risk", "telegram", "copywatch", "momentum", "dip",
          "quiet")
MARKET_AGENT_KEYS = ("momentum", "dip", "quiet")  # fed by agents/strategies.py MarketAgents
MARKET_AGENT_LABEL = {"momentum": "Momentum", "dip": "Dip Hunter", "quiet": "Quiet Breakout"}

# desk-health style: warn after 3 min, mark down after 5 min.
STALE_WARN_S = 180
STALE_DOWN_S = 300

# STEP 10 defaults — used only when settings has no override.
DEFAULTS: dict[str, Any] = {
    "desk.mode": "PAPER",
    "desk.paper_start_stack": 200.0,
    "executor.min_ticket_usd": 47.50,
    "executor.stack_pct": 8.0,
    "executor.below_min": "bump",
    "executor.max_pool_pct": 2.0,
    "risk.stop_pct": 6.0,
    "risk.trail_start_pct": 20.0,
    "risk.trail_pct": 12.0,
    "risk.timeout_h": 3.0,
    "desk.fees_ref_discount": 0.10,
    "scanner.min_liquidity_usd": 40000.0,
    "desk.report_time": "08:00",
}

SECRETISH = (
    "token",
    "secret",
    "password",
    "vapid",
    "api_key",
    "apikey",
    "private_key",
    "tg_token",
    "app_token",
    "typesafe",
)


def connect_ro(path: Path | None = None) -> sqlite3.Connection:
    db_path = path or DB_PATH
    uri = f"file:{db_path}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    return conn


def rows(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> list[dict[str, Any]]:
    return [dict(r) for r in conn.execute(sql, params)]


def one(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> dict[str, Any] | None:
    r = conn.execute(sql, params).fetchone()
    return dict(r) if r else None


def parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    s = value.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def iso_utc(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def sofia(dt: datetime | str | None) -> str | None:
    if dt is None:
        return None
    if isinstance(dt, str):
        dt = parse_ts(dt)
        if dt is None:
            return None
    local = dt.astimezone(ZONE)
    return local.strftime("%d.%m.%Y, %H:%M:%S") + f" ({ZONE_LABEL})"


def sofia_short(dt: datetime | str | None) -> str | None:
    if dt is None:
        return None
    if isinstance(dt, str):
        dt = parse_ts(dt)
        if dt is None:
            return None
    return dt.astimezone(ZONE).strftime("%H:%M:%S")


def money(value: float | None, digits: int = 2) -> str | None:
    if value is None:
        return None
    sign = "-" if value < 0 else ""
    return f"{sign}${abs(value):,.{digits}f}"


def px(value: float | None) -> str | None:
    """Format a token price that may be tiny."""
    if value is None:
        return None
    av = abs(value)
    if av == 0:
        return "$0"
    if av >= 1:
        return f"${value:.4f}"
    if av >= 0.0001:
        return f"${value:.6f}"
    # tiny prices: fixed notation with 4 significant digits, never scientific
    import math
    decimals = min(18, -int(math.floor(math.log10(av))) + 3)
    return f"${value:.{decimals}f}"


def pct(value: float | None, digits: int = 2) -> str | None:
    if value is None:
        return None
    return f"{value:.{digits}f}%"


def coin_link(mint: str) -> str:
    return links.coin_url(mint, "solana")


def is_secret_key(key: str) -> bool:
    k = (key or "").lower()
    return any(s in k for s in SECRETISH)


def load_settings(conn: sqlite3.Connection) -> dict[str, Any]:
    out = dict(DEFAULTS)
    for r in rows(conn, "SELECT key, value FROM settings"):
        key = r["key"]
        if not key or is_secret_key(key):
            continue
        if key not in DEFAULTS and key.startswith("desk.") and "handle" not in key:
            # skip unlisted desk keys such as owner_chat_id / rpc_url / paused
            if key in (
                "desk.owner_chat_id",
                "desk.rpc_url",
                "desk.notifier",
                "desk.hosting",
            ):
                continue
        raw = r["value"]
        if key in out and isinstance(out[key], float):
            try:
                out[key] = float(raw)
            except (TypeError, ValueError):
                pass
        elif key in out and isinstance(out[key], int) and not isinstance(out[key], bool):
            try:
                out[key] = int(float(raw))
            except (TypeError, ValueError):
                pass
        else:
            out[key] = raw
    return out


def ticket_info(stack: float, settings: dict[str, Any]) -> dict[str, Any]:
    ticket = float(settings["executor.min_ticket_usd"])
    stack_pct = float(settings["executor.stack_pct"])
    below = str(settings["executor.below_min"] or "bump").lower()
    natural = stack * (stack_pct / 100.0) if stack else 0.0
    min_stack = ticket / (stack_pct / 100.0) if stack_pct else float("inf")
    bumped = stack + 1e-9 < min_stack and below == "bump"
    if bumped and stack > 0:
        used = ticket
        used_pct = (ticket / stack) * 100.0
        message = f"tickets bumped to ${ticket:.2f} = {used_pct:.4g}% of stack"
    else:
        used = natural
        used_pct = stack_pct
        message = None
    return {
        "min_ticket_usd": ticket,
        "stack_pct": stack_pct,
        "below_min": below,
        "natural_ticket_usd": round(natural, 4),
        "ticket_usd": round(used, 4),
        "ticket_pct_of_stack": round(used_pct, 4),
        "bumped": bumped,
        "min_stack_usd": round(min_stack, 4) if min_stack != float("inf") else None,
        "message": message,
    }


def heartbeat_status(ts: str | None, now: datetime) -> dict[str, Any]:
    dt = parse_ts(ts)
    if dt is None:
        return {
            "ts": ts,
            "ts_sofia": None,
            "age_s": None,
            "status": "missing",
            "label": "няма сигнал",
        }
    age = (now - dt).total_seconds()
    if age > STALE_DOWN_S:
        status, label = "down", "спрял"
    elif age > STALE_WARN_S:
        status, label = "stale", "изостава"
    else:
        status, label = "ok", "жив"
    return {
        "ts": ts,
        "ts_sofia": sofia(dt),
        "ts_sofia_short": sofia_short(dt),
        "age_s": int(age),
        "status": status,
        "label": label,
    }


def latest_prices(conn: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    found: dict[str, dict[str, Any]] = {}
    for r in rows(conn, "SELECT mint, ts, price, volume_m5 FROM prices ORDER BY ts DESC"):
        if r["mint"] not in found:
            found[r["mint"]] = r
    return found


def coins_by_mint(conn: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    return {r["mint"]: r for r in rows(conn, "SELECT * FROM coins")}


def decorate_coin(mint: str, coins: dict[str, dict[str, Any]]) -> dict[str, Any]:
    c = coins.get(mint) or {}
    name = c.get("name")
    symbol = c.get("symbol")
    if name and symbol and name != symbol:
        label = f"{name} ({symbol})"
    else:
        label = name or symbol or mint
    return {
        "mint": mint,
        "name": name,
        "symbol": symbol,
        "label": label,
        "fomo_url": coin_link(mint) if mint else None,
    }


def enrich_position(
    pos: dict[str, Any],
    coins: dict[str, dict[str, Any]],
    prices: dict[str, dict[str, Any]],
    settings: dict[str, Any],
    now: datetime,
) -> dict[str, Any]:
    mint = pos["mint"]
    info = decorate_coin(mint, coins)
    entry = pos.get("entry_price")
    size = pos.get("size_usd")
    opened = parse_ts(pos.get("opened_at"))
    timeout_h = float(settings["risk.timeout_h"])
    open_s = int((now - opened).total_seconds()) if opened else None
    timeout_s = int(timeout_h * 3600)
    remaining_s = (timeout_s - open_s) if open_s is not None else None
    last = prices.get(mint)
    last_price = last["price"] if last else None
    last_ts = last["ts"] if last else None
    discount = float(settings["desk.fees_ref_discount"])
    unrealized = fees_usd = pnl_pct = None
    if (
        pos.get("status") in ("open", "exiting")
        and entry
        and last_price is not None
        and size
    ):
        unrealized, fees_usd = fees.pnl_usd(size, entry, last_price, discount)
        if size:
            pnl_pct = round(unrealized / size * 100.0, 2)
    trail = bool(pos.get("trail_active"))
    if trail:
        trail_state = "trail активен"
    else:
        trail_state = "стоп (trail още не е тръгнал)"
    out = {
        **info,
        "id": pos.get("id"),
        "opened_at": pos.get("opened_at"),
        "opened_at_sofia": sofia(pos.get("opened_at")),
        "entry_price": entry,
        "entry_price_display": px(entry),
        "last_price": last_price,
        "last_price_display": px(last_price),
        "last_price_ts": last_ts,
        "last_price_ts_sofia": sofia(last_ts),
        "size_usd": size,
        "size_display": money(size),
        "unrealized_pnl_usd": unrealized,
        "unrealized_pnl_display": money(unrealized),
        "unrealized_pnl_pct": pnl_pct,
        "fees_usd": fees_usd,
        "stop_price": pos.get("stop_price"),
        "stop_price_display": px(pos.get("stop_price")),
        "peak_price": pos.get("peak_price"),
        "trail_active": trail,
        "trail_state": trail_state,
        "open_s": open_s,
        "timeout_h": timeout_h,
        "timeout_s": timeout_s,
        "remaining_s": remaining_s,
        "status": pos.get("status"),
        "mode": pos.get("mode"),
        "name_stored": pos.get("name"),
        **source_fields(pos),
    }
    return out


def source_fields(pos: dict[str, Any]) -> dict[str, Any]:
    """Where the trade came from: the desk's own pipeline or a copied trader."""
    agent = agent_of_source(pos.get("source"))
    if pos.get("source") == "copy" and pos.get("copy_handle"):
        return {"source": "copy", "copy_handle": pos["copy_handle"], "source_label": f"копие от {pos['copy_handle']}",
                "agent": agent}
    if pos.get("source") in MARKET_AGENT_KEYS:
        label = MARKET_AGENT_LABEL[pos["source"]]
        return {"source": pos["source"], "copy_handle": None, "source_label": label, "agent": agent}
    return {"source": "desk", "copy_handle": None, "source_label": None, "agent": agent}


def copy_block(conn: sqlite3.Connection, closed: list[dict[str, Any]], opens: list[dict[str, Any]]) -> dict[str, Any]:
    traders = safe_rows(conn, "SELECT handle, active, rank, source FROM copy_traders ORDER BY active DESC, handle")
    c_closed = [p for p in closed if p.get("source") == "copy"]
    c_open = [p for p in opens if p.get("source") == "copy"]
    per: dict[str, dict[str, Any]] = {}
    for p in c_closed:
        s = per.setdefault(p["copy_handle"], {"handle": p["copy_handle"], "closed": 0, "pnl_usd": 0.0, "open": 0})
        s["closed"] += 1
        s["pnl_usd"] = round(s["pnl_usd"] + float(p.get("pnl_usd") or 0), 2)
    for p in c_open:
        s = per.setdefault(p["copy_handle"], {"handle": p["copy_handle"], "closed": 0, "pnl_usd": 0.0, "open": 0})
        s["open"] += 1
    return {
        "mode": "PAPER",
        "traders": [
            {"handle": t["handle"], "active": bool(t["active"]), "rank": t.get("rank"),
             "profile_url": links.profile_url(t["handle"])}
            for t in traders
        ],
        "per_trader": sorted(per.values(), key=lambda x: -x["pnl_usd"]),
        "open_count": len(c_open),
        "closed_count": len(c_closed),
        "pnl_usd": round(sum(float(p.get("pnl_usd") or 0) for p in c_closed), 2),
    }


def enrich_closed(pos: dict[str, Any], coins: dict[str, dict[str, Any]]) -> dict[str, Any]:
    info = decorate_coin(pos["mint"], coins)
    entry = pos.get("entry_price")
    exit_ = pos.get("exit_price")
    size = pos.get("size_usd")
    pnl = pos.get("pnl_usd")
    pnl_pct = None
    if pnl is not None and size:
        pnl_pct = round(float(pnl) / float(size) * 100.0, 2)
    price_pct = None
    if entry and exit_:
        price_pct = round((float(exit_) - float(entry)) / float(entry) * 100.0, 2)
    return {
        **info,
        "id": pos.get("id"),
        "opened_at": pos.get("opened_at"),
        "opened_at_sofia": sofia(pos.get("opened_at")),
        "closed_at": pos.get("closed_at"),
        "closed_at_sofia": sofia(pos.get("closed_at")),
        "entry_price": entry,
        "entry_price_display": px(entry),
        "exit_price": exit_,
        "exit_price_display": px(exit_),
        "exit_reason": pos.get("exit_reason"),
        "size_usd": size,
        "pnl_usd": pnl,
        "pnl_display": money(pnl),
        "pnl_pct": pnl_pct,
        "price_change_pct": price_pct,
        "fees_usd": pos.get("fees_usd"),
        "mode": pos.get("mode"),
        "name_stored": pos.get("name"),
        **source_fields(pos),
    }


def skip_reason(conn: sqlite3.Connection, mint: str) -> str | None:
    v = one(
        conn,
        "SELECT reason FROM verdicts WHERE mint=? ORDER BY id DESC LIMIT 1",
        (mint,),
    )
    if v and v.get("reason"):
        return v["reason"]
    s = one(
        conn,
        "SELECT reason FROM signals WHERE mint=? ORDER BY id DESC LIMIT 1",
        (mint,),
    )
    if s and s.get("reason"):
        return s["reason"]
    a = one(
        conn,
        "SELECT subtitle, title FROM activity WHERE mint=? ORDER BY id DESC LIMIT 1",
        (mint,),
    )
    if a:
        return a.get("subtitle") or a.get("title")
    return None


def auditor_summary(
    conn: sqlite3.Connection, start: str, end: str, coins: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    audits = rows(
        conn,
        "SELECT * FROM audits WHERE ts>=? AND ts<=? ORDER BY id",
        (start, end),
    )
    by_mint: dict[str, list[dict[str, Any]]] = {}
    for a in audits:
        by_mint.setdefault(a["mint"], []).append(a)
    passed: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    pending: list[dict[str, Any]] = []
    for mint, items in by_mint.items():
        info = decorate_coin(mint, coins)
        fails = [a for a in items if a.get("pass") == 0]
        if fails:
            f = fails[0]
            rejected.append(
                {
                    **info,
                    "failed_layer": f.get("layer"),
                    "failed_layer_name": f.get("name"),
                    "failed_value": f.get("value_text"),
                    "source": f.get("source"),
                    "ts": f.get("ts"),
                    "ts_sofia": sofia(f.get("ts")),
                }
            )
        elif any(a.get("pass") == 1 for a in items):
            passed.append({**info, "layers": len(items), "ts": items[-1].get("ts")})
        else:
            pending.append({**info, "layers": len(items)})
    return {
        "audits_rows": len(audits),
        "mints": len(by_mint),
        "passed": passed,
        "rejected": rejected,
        "pending": pending,
        "passed_count": len(passed),
        "rejected_count": len(rejected),
        "pending_count": len(pending),
    }


def collect(conn: sqlite3.Connection, now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    settings = load_settings(conn)
    night = one(conn, "SELECT * FROM nights WHERE ended_at IS NULL ORDER BY id DESC LIMIT 1")
    if night is None:
        night = one(conn, "SELECT * FROM nights ORDER BY id DESC LIMIT 1")
    night_count = one(conn, "SELECT COUNT(*) AS n FROM nights")
    nights_total = int(night_count["n"]) if night_count else 0

    start_stack = None
    mode = str(settings.get("desk.mode") or "PAPER").upper()
    started_at = None
    session_night_ids: list[int] = []
    if night:
        start_stack = night.get("start_stack")
        mode = str(night.get("mode") or mode).upper()
        started_at = night.get("started_at")
        # One trading night runs report_time → report_time (Europe/Sofia). Rows opened by
        # process restarts inside that window belong to the same night/session view.
        report_time = str(settings.get("desk.report_time") or "08:00")
        win = night_window_start(started_at, report_time)
        for n in rows(conn, "SELECT id, started_at, mode FROM nights WHERE id<=? ORDER BY id", (night["id"],)):
            if str(n.get("mode") or "").upper() == mode and night_window_start(n["started_at"], report_time) == win:
                session_night_ids.append(int(n["id"]))
                if n["started_at"] < started_at:
                    started_at = n["started_at"]
    if start_stack is None:
        start_stack = float(settings["desk.paper_start_stack"])

    bal = one(conn, "SELECT ts, value FROM balance ORDER BY ts DESC LIMIT 1")
    current_stack = float(bal["value"]) if bal and bal.get("value") is not None else float(start_stack)
    ticket = ticket_info(current_stack, settings)

    hb_raw = {r["agent"]: r["ts"] for r in rows(conn, "SELECT agent, ts FROM heartbeats")}
    heartbeats = []
    for agent in AGENTS:
        item = heartbeat_status(hb_raw.get(agent), now)
        item["agent"] = agent
        heartbeats.append(item)
    # any unexpected agents
    for agent, ts in hb_raw.items():
        if agent not in AGENTS:
            item = heartbeat_status(ts, now)
            item["agent"] = agent
            heartbeats.append(item)

    coins = coins_by_mint(conn)
    prices = latest_prices(conn)

    open_rows = rows(
        conn,
        "SELECT * FROM positions WHERE status IN ('open','exiting') ORDER BY id",
    )
    closed_rows = rows(
        conn,
        "SELECT * FROM positions WHERE status='closed' OR closed_at IS NOT NULL OR exit_reason IS NOT NULL ORDER BY id",
    )
    # avoid listing still-open exiting twice if they have exit_reason already
    open_ids = {p["id"] for p in open_rows}
    closed_rows = [p for p in closed_rows if p["id"] not in open_ids]

    open_positions = [enrich_position(p, coins, prices, settings, now) for p in open_rows]
    closed_trades = [enrich_closed(p, coins) for p in closed_rows]

    wins = [t for t in closed_trades if t.get("pnl_usd") is not None and t["pnl_usd"] >= 0]
    losses = [t for t in closed_trades if t.get("pnl_usd") is not None and t["pnl_usd"] < 0]
    total_pnl = round(sum(float(t["pnl_usd"]) for t in closed_trades if t.get("pnl_usd") is not None), 2)
    closed_n = len(closed_trades)
    scored = len(wins) + len(losses)
    win_rate = round(len(wins) / scored * 100.0, 1) if scored else None

    start = started_at or "1970-01-01T00:00:00.000Z"
    end = (night or {}).get("ended_at") or iso_utc(now)

    # Migrations this night: waiting_room ∪ coins with migrated_at in window.
    waiting = rows(conn, "SELECT * FROM waiting_room")
    migrations: dict[str, dict[str, Any]] = {}
    for w in waiting:
        ts = w.get("migrated_at") or w.get("last_scan_ts")
        if ts and (ts < start or ts > end):
            # still include if last_scan is in window
            if not (w.get("last_scan_ts") and start <= w["last_scan_ts"] <= end):
                continue
        info = decorate_coin(w["mint"], coins)
        migrations[w["mint"]] = {
            **info,
            "migrated_at": w.get("migrated_at"),
            "migrated_at_sofia": sofia(w.get("migrated_at")),
            "waiting_status": w.get("status"),
            "pool": w.get("pool"),
        }
    for c in coins.values():
        ts = c.get("migrated_at")
        if not ts or ts < start or ts > end:
            continue
        cur = migrations.get(c["mint"]) or decorate_coin(c["mint"], coins)
        cur["migrated_at"] = cur.get("migrated_at") or ts
        cur["migrated_at_sofia"] = cur.get("migrated_at_sofia") or sofia(ts)
        if c.get("pool"):
            cur["pool"] = c.get("pool")
        migrations[c["mint"]] = cur

    scans = rows(
        conn,
        "SELECT * FROM scans WHERE ts>=? AND ts<=? ORDER BY id",
        (start, end),
    )
    latest_scan: dict[str, dict[str, Any]] = {}
    for s in scans:
        latest_scan[s["mint"]] = s
    for mint, s in latest_scan.items():
        cur = migrations.get(mint) or decorate_coin(mint, coins)
        cur["scan"] = {
            "id": s["id"],
            "ts": s["ts"],
            "ts_sofia": sofia(s["ts"]),
            "liquidity_usd": s.get("liquidity_usd"),
            "top10_pct": s.get("top10_pct"),
            "h1_change_pct": s.get("h1_change_pct"),
            "age_min": s.get("age_min"),
            "verdict": s.get("verdict"),
            "reason": s.get("reason"),
        }
        migrations[mint] = cur

    migration_list = sorted(
        migrations.values(),
        key=lambda m: m.get("migrated_at") or "",
    )
    scan_pass = sum(1 for s in scans if s.get("verdict") == "pass")
    scan_kill = sum(1 for s in scans if s.get("verdict") == "kill")
    scan_wait = sum(1 for s in scans if s.get("verdict") == "wait")

    auditor = auditor_summary(conn, start, end, coins)

    skip_rows = rows(conn, "SELECT mint, until FROM skips")
    skips = []
    for s in skip_rows:
        info = decorate_coin(s["mint"], coins)
        reason = skip_reason(conn, s["mint"])
        until = s.get("until")
        skips.append(
            {
                **info,
                "until": until,
                "until_sofia": sofia(until) if until and until != "night" else None,
                "until_label": "до края на нощта" if until == "night" else sofia(until) or until,
                "reason": reason,
            }
        )

    verdicts = rows(
        conn,
        "SELECT * FROM verdicts WHERE ts>=? AND ts<=? ORDER BY id",
        (start, end),
    )
    analyst_rejects = [
        {
            **decorate_coin(v["mint"], coins),
            "ts": v["ts"],
            "ts_sofia": sofia(v["ts"]),
            "score": v.get("score"),
            "reason": v.get("reason"),
            "passed": bool(v.get("pass")),
        }
        for v in verdicts
        if not v.get("pass")
    ]

    activity = rows(conn, "SELECT * FROM activity ORDER BY id DESC LIMIT 30")
    activity_out = []
    for a in reversed(activity):
        info = decorate_coin(a["mint"], coins) if a.get("mint") else {}
        activity_out.append(
            {
                "id": a["id"],
                "ts": a["ts"],
                "ts_sofia": sofia(a["ts"]),
                "ts_sofia_short": sofia_short(a["ts"]),
                "kind": a.get("kind"),
                "agent": a.get("agent"),
                "title": a.get("title"),
                "subtitle": a.get("subtitle"),
                "value": a.get("value"),
                "mint": a.get("mint"),
                "label": info.get("label"),
                "fomo_url": info.get("fomo_url"),
            }
        )

    last_actions = agent_last_actions(conn, coins)
    for h in heartbeats:
        h["role"] = AGENT_ROLE.get(h["agent"], h["agent"])
        la = last_actions.get(h["agent"])
        if h["agent"] == "risk":
            n_open = len(open_positions)
            watch = f"следи {n_open} отворени" if n_open else "няма отворени позиции за следене"
            la = {"ts": (la or {}).get("ts") or h.get("ts"), "text": watch + (f" · последно: {la['text']}" if la else "")}
        if la:
            h["last_action"] = la["text"]
            h["last_action_ts"] = la["ts"]
            h["last_action_sofia"] = sofia_short(la["ts"])

    ok_agents = sum(1 for h in heartbeats if h["status"] == "ok" and h["agent"] in AGENTS)
    down_agents = [h["agent"] for h in heartbeats if h["status"] != "ok" and h["agent"] in AGENTS]

    return {
        "milky": milky_block(conn),
        "generated_at": iso_utc(now),
        "generated_at_sofia": sofia(now),
        "timezone": ZONE_LABEL,
        "header": {
            "mode": mode,
            "night_id": (night or {}).get("id"),
            "session_night_ids": session_night_ids,
            "session_first_night_id": session_night_ids[0] if session_night_ids else (night or {}).get("id"),
            "restarts_in_session": max(0, len(session_night_ids) - 1),
            "night_window_start": iso_utc(night_window_start(started_at, str(settings.get("desk.report_time") or "08:00"))) if started_at else None,
            "nights_total": nights_total,
            "night_in_progress": bool(night and not night.get("ended_at")),
            "started_at": started_at,
            "started_at_sofia": sofia(started_at),
            "ended_at": (night or {}).get("ended_at"),
            "start_stack": start_stack,
            "start_stack_display": money(start_stack),
            "current_stack": current_stack,
            "current_stack_display": money(current_stack),
            "balance_ts": bal["ts"] if bal else None,
            "balance_ts_sofia": sofia(bal["ts"]) if bal else None,
            "ticket": ticket,
            "risk": {
                "stop_pct": settings["risk.stop_pct"],
                "trail_start_pct": settings["risk.trail_start_pct"],
                "trail_pct": settings["risk.trail_pct"],
                "timeout_h": settings["risk.timeout_h"],
            },
        },
        "heartbeats": heartbeats,
        "heartbeats_ok": ok_agents,
        "heartbeats_expected": len(AGENTS),
        "heartbeats_down": down_agents,
        "open_positions": open_positions,
        "closed_trades": closed_trades,
        "totals": {
            "open_count": len(open_positions),
            "closed_count": closed_n,
            "wins": len(wins),
            "losses": len(losses),
            "win_rate_pct": win_rate,
            "total_pnl_usd": total_pnl if closed_n else None,
            "total_pnl_display": money(total_pnl) if closed_n else None,
        },
        "pipeline": {
            "window_start": start,
            "window_end": end,
            "migrations_count": len(migration_list),
            "migrations": migration_list[-60:],
            "scans_count": len(scans),
            "scans_pass": scan_pass,
            "scans_kill": scan_kill,
            "scans_wait": scan_wait,
            "auditor": auditor,
            "analyst_skips": skips,
            "analyst_skips_count": len(skips),
            "analyst_rejects": analyst_rejects,
            "analyst_rejects_count": len(analyst_rejects),
            "verdicts_count": len(verdicts),
        },
        "activity": activity_out,
        "activity_count": len(activity_out),
        "equity_series": equity_series(conn),
        "session": session_block(conn, settings, current_stack, closed_trades, open_positions),
        "controls": controls_block(conn, settings),
        "fills": recent_fills(closed_trades, open_positions),
        "copy": copy_block(conn, closed_trades, open_positions),
        "scanner": scanner_block(conn, coins, heartbeats, settings, start, end, now),
        "feed": activity_feed(conn, closed_trades, open_positions, start),
        "desk4": desk4_block(conn, settings, now, float(start_stack), current_stack, started_at, open_positions,
                             heartbeats, coins, mode),
        "schema": 4,
    }


# ---------------------------------------------------------------- video features
MAX_SERIES = 160


def safe_rows(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> list[dict[str, Any]]:
    try:
        return rows(conn, sql, params)
    except sqlite3.OperationalError:
        return []


def kv(conn: sqlite3.Connection, key: str) -> str | None:
    r = safe_rows(conn, "SELECT value FROM kv WHERE key=?", (key,))
    return r[0]["value"] if r else None


def downsample(points: list[list[Any]], limit: int = MAX_SERIES) -> list[list[Any]]:
    """Keep first, last, and every changed value; then thin evenly to `limit`."""
    if len(points) <= limit:
        return points
    keep = [points[0]]
    for i in range(1, len(points) - 1):
        if points[i][1] != points[i - 1][1] or points[i][1] != points[i + 1][1]:
            keep.append(points[i])
    keep.append(points[-1])
    if len(keep) <= limit:
        return keep
    step = (len(keep) - 1) / (limit - 1)
    return [keep[round(i * step)] for i in range(limit)]


def equity_series(conn: sqlite3.Connection) -> list[list[Any]]:
    """[[iso_ts, value], ...] from equity snapshots plus realized balance rows."""
    pts: dict[str, float] = {}
    for r in safe_rows(conn, "SELECT ts, value FROM balance ORDER BY ts"):
        if r.get("value") is not None:
            pts[r["ts"]] = round(float(r["value"]), 2)
    for r in safe_rows(conn, "SELECT ts, value FROM equity ORDER BY ts"):
        if r.get("value") is not None:
            pts[r["ts"]] = round(float(r["value"]), 2)
    series = [[ts, v] for ts, v in sorted(pts.items())]
    return downsample(series)


AGENT_ROLE = {
    "main": "Процес",
    "scanner": "Скенер · нови миграции",
    "auditor": "Одитор · 9 проверки",
    "analyst": "Анализатор · оценка",
    "executor": "Изпълнител · размер и вход",
    "risk": "Риск · стоп и trail",
    "telegram": "Telegram · команди",
    "copywatch": "Копиране · топ трейдъри (PAPER)",
    "momentum": "Momentum · volume breakouts (PAPER)",
    "dip": "Dip Hunter · −30% wicks (PAPER)",
    "quiet": "Quiet Breakout · calm breakouts (PAPER)",
}


def agent_last_actions(conn: sqlite3.Connection, coins: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}

    def label(mint: str | None) -> str:
        return decorate_coin(mint, coins)["label"] if mint else ""

    r = safe_rows(conn, "SELECT mint, ts, verdict, reason FROM scans ORDER BY id DESC LIMIT 1")
    if r:
        x = r[0]
        word = {"pass": "пусна", "kill": "отряза", "wait": "чака"}.get(x.get("verdict") or "", x.get("verdict") or "")
        out["scanner"] = {"ts": x["ts"], "text": f"{word} {label(x['mint'])}" + (f" · {x['reason']}" if x.get("reason") else "")}
    r = safe_rows(conn, "SELECT mint, ts, layer, name, pass FROM audits ORDER BY id DESC LIMIT 1")
    if r:
        x = r[0]
        mark = "ok" if x.get("pass") == 1 else ("червено" if x.get("pass") == 0 else "без данни")
        out["auditor"] = {"ts": x["ts"], "text": f"{label(x['mint'])} · слой {x.get('layer')} {x.get('name') or ''}: {mark}"}
    r = safe_rows(conn, "SELECT mint, ts, score, pass, reason FROM verdicts ORDER BY id DESC LIMIT 1")
    if r:
        x = r[0]
        out["analyst"] = {"ts": x["ts"], "text": f"{label(x['mint'])} · оценка {x.get('score')} · {'да' if x.get('pass') else 'не'}"}
    r = safe_rows(conn, "SELECT mint, ts, status, size_usd, reason FROM signals ORDER BY id DESC LIMIT 1")
    if r:
        x = r[0]
        out["executor"] = {"ts": x["ts"], "text": f"{label(x['mint'])} · {x.get('status')}" + (f" · {x['reason']}" if x.get("reason") else "") + (f" · ${float(x['size_usd']):.2f}" if x.get("size_usd") else "")}
    r = safe_rows(conn, "SELECT mint, ts, title, subtitle, value FROM activity WHERE agent='copy' ORDER BY id DESC LIMIT 1")
    if r:
        x = r[0]
        out["copywatch"] = {"ts": x["ts"], "text": f"{x.get('title') or ''} · {x.get('subtitle') or ''} · {x.get('value') or ''}"}
    r = safe_rows(conn, "SELECT mint, ts, title, value FROM activity WHERE agent='risk' ORDER BY id DESC LIMIT 1")
    if r:
        x = r[0]
        out["risk"] = {"ts": x["ts"], "text": f"{x.get('title') or ''} {x.get('value') or ''}".strip()}
    return out


def session_block(
    conn: sqlite3.Connection,
    settings: dict[str, Any],
    current_stack: float,
    closed_trades: list[dict[str, Any]],
    open_positions: list[dict[str, Any]],
) -> dict[str, Any]:
    start = float(settings["desk.paper_start_stack"])
    change = round(current_stack - start, 2)
    pct_v = round(change / start * 100.0, 2) if start else None
    mult = round(current_stack / start, 4) if start else None
    trades = sorted(
        [t for t in closed_trades if t.get("pnl_usd") is not None],
        key=lambda t: t.get("closed_at") or "",
    )
    wins = [t for t in trades if float(t["pnl_usd"]) >= 0]
    losses = [t for t in trades if float(t["pnl_usd"]) < 0]
    stops = [t for t in trades if t.get("exit_reason") == "stop"]
    replay = [
        {
            "label": t.get("symbol") or t.get("label") or t["mint"][:6],
            "name": t.get("label"),
            "fomo_url": t.get("fomo_url"),
            "opened_at": t.get("opened_at"),
            "closed_at": t.get("closed_at"),
            "size_usd": t.get("size_usd"),
            "pnl_usd": round(float(t["pnl_usd"]), 2),
            "pnl_pct": t.get("pnl_pct"),
            "exit_reason": t.get("exit_reason"),
            "source_label": t.get("source_label"),
        }
        for t in trades
    ]
    best = sorted(replay, key=lambda t: t["pnl_usd"], reverse=True)[:5]
    unreal = sum(float(p.get("unrealized_pnl_usd") or 0) for p in open_positions)
    return {
        "start_stack": start,
        "current_stack": current_stack,
        "equity_now": round(current_stack + unreal, 2),
        "change_usd": change,
        "change_pct": pct_v,
        "multiplier": mult,
        "trades": len(trades),
        "wins": len(wins),
        "losses": len(losses),
        "stops": len(stops),
        "green_pct": round(len(wins) / len(trades) * 100.0, 1) if trades else None,
        "best": best,
        "replay": replay[-200:],
    }


def controls_block(conn: sqlite3.Connection, settings: dict[str, Any]) -> dict[str, Any]:
    pub: dict[str, Any] = {}
    try:
        pub = json.loads((ROOT / "public_config.json").read_text(encoding="utf-8"))
    except Exception:
        pub = {}
    bot = pub.get("bot_username")
    paused_raw = str(settings.get("desk.paused") or "")
    paused = [p.strip() for p in paused_raw.split(",") if p.strip()]
    quiet_on = (kv(conn, "quiet.on") or "") == "1"
    queued = 0
    if quiet_on:
        r = safe_rows(conn, "SELECT COUNT(*) AS n FROM push_queue WHERE delivered_at IS NULL")
        queued = int(r[0]["n"]) if r else 0
    until = kv(conn, "quiet.until") or None
    return {
        "bot_username": bot,
        "bot_url": f"https://t.me/{bot}" if bot else None,
        "sellall_url": f"https://t.me/{bot}?start=sellall" if bot else None,
        "entries_paused": "executor" in paused,
        "paused_agents": paused,
        "quiet": {
            "on": quiet_on,
            "until": until if quiet_on else None,
            "until_sofia": sofia(until) if quiet_on and until else None,
            "started_at": (kv(conn, "quiet.started_at") or None) if quiet_on else None,
            "held": queued,
        },
        "last_sellall_at": kv(conn, "sellall.last_at") or None,
    }


def recent_fills(closed: list[dict[str, Any]], opens: list[dict[str, Any]]) -> list[dict[str, Any]]:
    ev: list[dict[str, Any]] = []
    for p in opens + closed:
        if p.get("opened_at"):
            ev.append({"ts": p["opened_at"], "ts_sofia_short": sofia_short(p["opened_at"]), "side": "buy",
                       "label": p.get("label"), "fomo_url": p.get("fomo_url"), "size_usd": p.get("size_usd"),
                       "price_display": p.get("entry_price_display"), "source_label": p.get("source_label")})
        if p.get("closed_at"):
            ev.append({"ts": p["closed_at"], "ts_sofia_short": sofia_short(p["closed_at"]), "side": "sell",
                       "label": p.get("label"), "fomo_url": p.get("fomo_url"), "size_usd": p.get("size_usd"),
                       "price_display": p.get("exit_price_display"), "pnl_usd": p.get("pnl_usd"),
                       "exit_reason": p.get("exit_reason"), "source_label": p.get("source_label")})
    ev.sort(key=lambda e: e["ts"], reverse=True)
    return ev[:20]


def _liq_short(v: float | None) -> str | None:
    if v is None:
        return None
    v = float(v)
    if v >= 1_000_000:
        return f"${v / 1_000_000:.1f}M"
    if v >= 1000:
        return f"${v / 1000:.1f}K"
    return f"${v:.0f}"


def scanner_block(
    conn: sqlite3.Connection,
    coins: dict[str, dict[str, Any]],
    heartbeats: list[dict[str, Any]],
    settings: dict[str, Any],
    start: str,
    end: str,
    now: datetime,
) -> dict[str, Any]:
    """Live Scanner card: waiting room + recent verdicts for the whole night (survives restarts)."""
    hb = next((h for h in heartbeats if h.get("agent") == "scanner"), {})
    min_liq = float(settings.get("scanner.min_liquidity_usd") or 40000)
    scores = {r["mint"]: r for r in safe_rows(conn, "SELECT mint, score, pass, reason FROM verdicts ORDER BY id")}
    waiting = safe_rows(
        conn,
        "SELECT mint, migrated_at, last_scan_ts, status FROM waiting_room WHERE status='waiting' AND COALESCE(last_scan_ts, migrated_at, '')>=? ORDER BY COALESCE(last_scan_ts, migrated_at) DESC LIMIT 8",
        (start,),
    )
    scans = safe_rows(conn, "SELECT * FROM scans WHERE ts>=? AND ts<=? ORDER BY id DESC LIMIT 200", (start, end))
    latest: dict[str, dict[str, Any]] = {}
    for sc in scans:
        latest.setdefault(sc["mint"], sc)
    items: list[dict[str, Any]] = []
    for w in waiting:
        if w["mint"] in latest:
            continue
        info = decorate_coin(w["mint"], coins)
        mig = parse_ts(w.get("migrated_at"))
        items.append({
            **info, "state": "wait", "ts": w.get("last_scan_ts") or w.get("migrated_at"),
            "ts_sofia_short": sofia_short(w.get("last_scan_ts") or w.get("migrated_at")),
            "reason": "чака проверка", "liq_usd": None, "liq_display": None,
            "age_min": int((now - mig).total_seconds() // 60) if mig else None, "score": None,
        })
    for mint, sc in list(latest.items())[:12]:
        info = decorate_coin(mint, coins)
        v = scores.get(mint)
        items.append({
            **info, "state": sc.get("verdict") or "wait", "ts": sc["ts"], "ts_sofia_short": sofia_short(sc["ts"]),
            "reason": sc.get("reason"), "liq_usd": sc.get("liquidity_usd"), "liq_display": _liq_short(sc.get("liquidity_usd")),
            "age_min": sc.get("age_min"), "score": (v or {}).get("score"),
        })
    items.sort(key=lambda x: x.get("ts") or "", reverse=True)
    kills = [s for s in scans if s.get("verdict") == "kill"]
    liq_kills = sum(1 for s in kills if str(s.get("reason") or "").startswith("liq"))
    all_scans = safe_rows(conn, "SELECT COUNT(*) AS n, SUM(verdict='kill') AS k, SUM(verdict='pass') AS p FROM scans WHERE ts>=? AND ts<=?", (start, end))
    n_scans = int((all_scans[0] or {}).get("n") or 0) if all_scans else 0
    return {
        "status": hb.get("status") or "down",
        "state_label": "сканира" if hb.get("status") == "ok" else ("бавно" if hb.get("status") == "warn" else "спрял"),
        "last_seen_ts": hb.get("ts"),
        "min_liq_usd": min_liq,
        "min_liq_display": _liq_short(min_liq),
        "waiting_count": len(safe_rows(conn, "SELECT mint FROM waiting_room WHERE status='waiting'")),
        "scans_count": n_scans,
        "kills_count": int((all_scans[0] or {}).get("k") or 0) if all_scans else 0,
        "pass_count": int((all_scans[0] or {}).get("p") or 0) if all_scans else 0,
        "liq_kills_recent": liq_kills,
        "last_scan_ts": scans[0]["ts"] if scans else None,
        "last_scan_sofia": sofia_short(scans[0]["ts"]) if scans else None,
        "items": items[:12],
    }


def activity_feed(
    conn: sqlite3.Connection, closed: list[dict[str, Any]], opens: list[dict[str, Any]], start: str
) -> list[dict[str, Any]]:
    """Video-style feed: Bought/Sold rows with size, time, P&L, source tag. Links from links.py only."""
    ev: list[dict[str, Any]] = []
    for p in opens + closed:
        base = {"label": p.get("symbol") or p.get("label"), "name": p.get("label"), "fomo_url": p.get("fomo_url"),
                "size_usd": p.get("size_usd"), "source_label": p.get("source_label"), "mc_usd": None,
                "in_session": (p.get("opened_at") or "") >= start}
        if p.get("opened_at"):
            ev.append({**base, "ts": p["opened_at"], "ts_sofia_short": sofia_short(p["opened_at"]), "side": "buy",
                       "price_display": p.get("entry_price_display")})
        if p.get("closed_at"):
            ev.append({**base, "ts": p["closed_at"], "ts_sofia_short": sofia_short(p["closed_at"]), "side": "sell",
                       "price_display": p.get("exit_price_display"), "pnl_usd": p.get("pnl_usd"),
                       "pnl_pct": p.get("pnl_pct"), "exit_reason": p.get("exit_reason")})
    ev.sort(key=lambda e: e["ts"], reverse=True)
    return ev[:30]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Dump desk dashboard data as JSON (read-only).")
    parser.add_argument("-o", "--out", type=Path, help="Write JSON to this file as well as stdout.")
    parser.add_argument("--db", type=Path, default=DB_PATH, help="Path to desk.db")
    args = parser.parse_args(argv)
    conn = connect_ro(args.db)
    try:
        data = collect(conn)
    finally:
        conn.close()
    text = json.dumps(data, ensure_ascii=False, indent=2)
    sys.stdout.write(text + "\n")
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())



# ---------------------------------------------------------------- four-agent desk (video 3 layout)
DESK4_AGENTS = [
    {"key": "sniper", "name": "Sniper", "icon": "target", "color": "#f97316", "sub": "fresh migrations"},
    {"key": "momentum", "name": "Momentum", "icon": "bolt", "color": "#3b82f6", "sub": "volume breakouts"},
    {"key": "dip", "name": "Dip Hunter", "icon": "radar", "color": "#14b8a6", "sub": "−30% wicks"},
    {"key": "quiet", "name": "Quiet Breakout", "icon": "quiet", "color": "#eab308", "sub": "calm breakouts"},
    {"key": "whale", "name": "Whale Tail", "icon": "whale", "color": "#8b5cf6", "sub": "smart wallets"},
]
GOAL_NODES = [1.25, 1.5, 2.0, 3.0, 4.0, 5.0]
GOAL_X = 2.0
TICKER_MAX_AGE_S = 15 * 60


def agent_of_source(src: Any) -> str:
    src = str(src or "").lower()
    if src == "copy":
        return "whale"
    if src in MARKET_AGENT_KEYS:
        return src
    return "sniper"


def _kv_float(conn: sqlite3.Connection, key: str, default: float) -> float:
    raw = kv(conn, "setting." + key)
    try:
        return float(json.loads(raw)) if raw is not None else default
    except Exception:
        return default


def _kv_bool(conn: sqlite3.Connection, key: str, default: bool) -> bool:
    raw = kv(conn, "setting." + key)
    try:
        return bool(json.loads(raw)) if raw is not None else default
    except Exception:
        return default


def _live_gate_block(conn: sqlite3.Connection) -> dict[str, Any]:
    """Small LIVE-gate readout for the four-agent board. Fail closed on errors."""
    try:
        import live_gate
        info = live_gate.evaluate(conn)
        return {
            "line": live_gate.dashboard_line(info),
            "ok": bool(info["ok"]),
            "win_rate_pct": info["win_rate_pct"],
            "min_win_rate_pct": info["min_win_rate_pct"],
            "closed": info["closed"],
            "wins": info["wins"],
            "min_closed_trades": info["min_closed_trades"],
            "nights": info["nights"],
        }
    except Exception:
        return {"line": "LIVE gate: locked", "ok": False}


def _mini_series(conn: sqlite3.Connection, mint: str, since: str | None, entry: float | None) -> list[float]:
    pts = safe_rows(conn, "SELECT ts, price FROM prices WHERE mint=? AND ts>=? AND price IS NOT NULL ORDER BY ts",
                    (mint, since or "1970"))
    vals = [float(p["price"]) for p in pts]
    if entry:
        vals = [float(entry)] + vals
    if len(vals) > 40:
        step = len(vals) / 40.0
        vals = [vals[int(i * step)] for i in range(40)] + [vals[-1]]
    return [round(v / float(entry) - 1, 5) if entry else v for v in vals]


def _held(opened: str | None, closed: str | None) -> str | None:
    a, b = parse_ts(opened), parse_ts(closed)
    if not a or not b:
        return None
    sec = max(0, int((b - a).total_seconds()))
    if sec < 60:
        return f"{sec}s"
    if sec < 3600:
        return f"{sec // 60}m"
    return f"{sec // 3600}h {sec % 3600 // 60}m"


def _sym(coins: dict[str, dict[str, Any]], mint: str, fallback: str | None = None) -> str:
    c = coins.get(mint) or {}
    sym = c.get("symbol") or c.get("name") or fallback or mint[:6]
    return "$" + str(sym).lstrip("$")


def desk4_block(conn, settings, now, start_stack, current_stack, started_at, open_positions, heartbeats, coins,
                mode) -> dict[str, Any]:
    """Four-agent view. Real PAPER data only; nothing here is simulated."""
    since = started_at or "1970-01-01T00:00:00.000Z"
    sleeve_pct = _kv_float(conn, "agents.sleeve_pct", 25.0)
    sleeves_on = _kv_bool(conn, "agents.sleeves_enabled", True)
    sleeve_usd = round(current_stack * sleeve_pct / 100.0, 2)
    hb = {h["agent"]: h for h in heartbeats}
    closed = safe_rows(conn, "SELECT * FROM positions WHERE status='closed' AND closed_at>=? ORDER BY closed_at",
                       (since,))
    pos_src = {r["mint"]: r.get("source") for r in safe_rows(conn, "SELECT mint, source FROM positions ORDER BY id")}
    active_traders = safe_rows(conn, "SELECT handle FROM copy_traders WHERE active=1")
    waiting = one(conn, "SELECT COUNT(*) AS n FROM waiting_room WHERE status='waiting'") or {}
    scans_1h = one(conn, "SELECT COUNT(DISTINCT mint) AS n FROM scans WHERE ts>=?",
                   (iso_utc(now - timedelta(hours=1)),)) or {}

    def status_kv(name: str) -> dict[str, Any]:
        try:
            return json.loads(kv(conn, f"agent.{name}.status") or "{}")
        except Exception:
            return {}

    unreal_total = 0.0
    agents_out = []
    for meta in DESK4_AGENTS:
        key = meta["key"]
        mine_open = [p for p in open_positions if (p.get("agent") or agent_of_source(p.get("source"))) == key]
        mine_closed = [c for c in closed if agent_of_source(c.get("source")) == key]
        scored = [c for c in mine_closed if c.get("pnl_usd") is not None and c.get("exit_reason") != "trader_partial"]
        wins = sum(1 for c in scored if float(c["pnl_usd"]) >= 0)
        losses = len(scored) - wins
        realized = round(sum(float(c.get("pnl_usd") or 0) for c in mine_closed), 2)
        unreal = round(sum(float(p.get("unrealized_pnl_usd") or 0) for p in mine_open), 2)
        unreal_total += unreal
        used = round(sum(float(p.get("size_usd") or 0) for p in mine_open), 2)
        if key in MARKET_AGENT_KEYS:
            st = status_kv(key)
            enabled = _kv_bool(conn, f"{key}.enabled", True) and _kv_bool(conn, "agents.enabled", True)
            scanning = int(st.get("scanning") or 0)
            scan_text = f"Scanning {scanning} pairs" if enabled else "Off"
            hb_name = key
        elif key == "whale":
            enabled = _kv_bool(conn, "copy.enabled", True)
            scanning = len(active_traders)
            scan_text = f"Watching {scanning} wallets"
            hb_name = "copywatch"
        else:
            enabled = True
            scanning = int(scans_1h.get("n") or 0) + int(waiting.get("n") or 0)
            scan_text = f"Scanning {scanning} pairs"
            hb_name = "scanner"
        positions = []
        for p in mine_open:
            positions.append({
                "id": p.get("id"),
                "mint": p.get("mint"),
                "symbol": _sym(coins, p["mint"], p.get("name_stored")),
                "label": p.get("label"),
                "size_usd": p.get("size_usd"),
                "entry_price": p.get("entry_price"),
                "last_price": p.get("last_price"),
                "pnl_usd": p.get("unrealized_pnl_usd"),
                "pnl_pct": p.get("unrealized_pnl_pct"),
                "opened_at": p.get("opened_at"),
                "fomo_url": p.get("fomo_url"),
                "series": _mini_series(conn, p["mint"], p.get("opened_at"), p.get("entry_price")),
                "trail_active": p.get("trail_active"),
                "copy_handle": p.get("copy_handle"),
            })
        hb_age = (hb.get(hb_name) or {}).get("age_s")
        agents_out.append({
            **meta,
            "live": _agent_live(conn, key, coins, status_kv(key) if key in MARKET_AGENT_KEYS else None,
                                len(active_traders), waiting.get("n")),
            "hb_age_s": hb_age,
            "stalled": hb_age is None or float(hb_age) > STALL_S,
            "enabled": enabled,
            "state": "in_trade" if positions else ("idle" if enabled else "off"),
            "scan_text": scan_text,
            "scanning": scanning,
            "heartbeat": (hb.get(hb_name) or {}).get("status"),
            "sleeve_usd": sleeve_usd if sleeves_on else None,
            "sleeve_used_usd": used,
            "sleeve_free_usd": round(sleeve_usd - used, 2) if sleeves_on else None,
            "wins": wins,
            "losses": losses,
            "trades": len(scored),
            "realized_usd": realized,
            "unrealized_usd": unreal,
            "pnl_usd": round(realized + unreal, 2),
            "positions": positions,
        })

    equity = round(current_stack + unreal_total, 2)
    mult = round(equity / start_stack, 4) if start_stack else None
    profit = round(equity - start_stack, 2)
    nxt = next((n for n in GOAL_NODES if mult is not None and n > mult + 1e-9), None)
    all_scored = [c for c in closed if c.get("pnl_usd") is not None and c.get("exit_reason") != "trader_partial"]
    n_w = sum(1 for c in all_scored if float(c["pnl_usd"]) >= 0)
    streak = 0
    for c in reversed(all_scored):
        if float(c["pnl_usd"]) >= 0:
            streak += 1
        else:
            break

    # events (banners): real buys/sells of the last 30 min, newest first
    cutoff = iso_utc(now - timedelta(minutes=30))
    events = []
    for c in safe_rows(conn, "SELECT * FROM positions WHERE closed_at>=? ORDER BY closed_at DESC LIMIT 8", (cutoff,)):
        ag = agent_of_source(c.get("source"))
        name = next(a["name"] for a in DESK4_AGENTS if a["key"] == ag)
        pnl = float(c.get("pnl_usd") or 0)
        pct_v = (float(c["exit_price"]) / float(c["entry_price"]) - 1) * 100 if c.get("exit_price") and c.get("entry_price") else 0
        held = _held(c.get("opened_at"), c.get("closed_at"))
        verb = {"stop": "stopped", "lock": "locked profit on", "target": "hit target on", "faded": "sold (volume faded)",
                "trail": "sold", "timeout": "timed out", "trader_partial": "trimmed"}.get(str(c.get("exit_reason")), "sold")
        events.append({"id": f"c{c['id']}", "ts": c["closed_at"], "agent": ag, "kind": verb, "good": pnl >= 0,
                       "text": f"{name} {verb} {_sym(coins, c['mint'], c.get('name'))} {pct_v:+.0f}%" + (f" • held {held}" if held else ""),
                       "value": ("+" if pnl >= 0 else "-") + f"${abs(pnl):.2f}"})
    for o in safe_rows(conn, "SELECT * FROM positions WHERE opened_at>=? ORDER BY opened_at DESC LIMIT 8", (cutoff,)):
        ag = agent_of_source(o.get("source"))
        name = next(a["name"] for a in DESK4_AGENTS if a["key"] == ag)
        events.append({"id": f"o{o['id']}", "ts": o["opened_at"], "agent": ag, "kind": "bought", "good": True,
                       "text": f"{name} bought {_sym(coins, o['mint'], o.get('name'))}",
                       "value": f"${float(o.get('size_usd') or 0):.2f}"})
    events.sort(key=lambda e: e["ts"] or "", reverse=True)

    # activity (color-coded by agent)
    acts = []
    for a in reversed(safe_rows(conn, "SELECT * FROM activity ORDER BY id DESC LIMIT 40")):
        raw = str(a.get("agent") or "")
        if raw in MARKET_AGENT_KEYS:
            ag = raw
        elif raw == "copy":
            ag = "whale"
        elif raw == "risk":
            ag = agent_of_source(pos_src.get(a.get("mint")))
        elif raw in ("executor", "scanner", "auditor", "analyst"):
            ag = "sniper"
        else:
            ag = "desk"
        acts.append({"id": a["id"], "ts": a["ts"], "agent": ag, "kind": a.get("kind"), "title": a.get("title"),
                     "subtitle": a.get("subtitle"), "value": a.get("value")})

    ticker = []
    try:
        t = json.loads(kv(conn, "ticker") or "{}")
        ts = parse_ts(t.get("ts"))
        if ts and (now - ts).total_seconds() <= TICKER_MAX_AGE_S:
            ticker = [{"symbol": i.get("symbol"), "price": i.get("price"), "ch24": i.get("ch24")}
                      for i in t.get("items") or []]
    except Exception:
        ticker = []

    return {
        "mode": mode,
        "label": f"{mode} {len(DESK4_AGENTS)} agents",
        "agents": agents_out,
        "sleeve_pct": sleeve_pct,
        "sleeves_enabled": sleeves_on,
        "equity": {"start": start_stack, "value": equity, "realized_stack": current_stack,
                   "unrealized": round(unreal_total, 2), "profit": profit,
                   "profit_pct": round(profit / start_stack * 100, 2) if start_stack else None, "mult": mult},
        "stats": {"trades": len(all_scored), "wins": n_w,
                  "win_pct": round(n_w / len(all_scored) * 100) if all_scored else None, "streak": streak},
        "goal": {"x": GOAL_X, "nodes": GOAL_NODES, "next": nxt,
                 "to_go": round(nxt * start_stack - equity, 2) if nxt else 0.0,
                 "done": [n for n in GOAL_NODES if mult is not None and mult >= n], "mult": mult},
        "ticker": ticker,
        "events": events[:8],
        "activity": acts,
        "series": [pt for pt in equity_series(conn) if str(pt[0]) >= since][-160:],
        "live_gate": _live_gate_block(conn),
    }


STALL_S = 180
ACTION_TEXT = {
    "copied": "copied (PAPER)", "sell_no_copy": "sell → no copy", "already_open": "already open → no copy",
    "too_old": "old tx → ignored", "gate_low_liq": "low liquidity → no copy", "gate_bonding_curve": "still on curve → no copy",
    "sleeve_full": "sleeve full → no copy", "no_price": "no price → no copy", "no_stack": "no stack → no copy",
    "trader_sold": "trader sold → we sold", "trader_partial": "trader trimmed → we trimmed",
}


def _agent_live(conn, key, coins, st, n_traders, waiting_n) -> dict[str, Any]:
    """What the agent is doing right now, from real desk rows. All ts are ISO UTC."""
    if key in MARKET_AGENT_KEYS:
        st = st or {}
        last = st.get("last") or None
        return {"kind": "market", "phase": st.get("phase"), "cycle_ts": st.get("cycle_ts") or st.get("ts"),
                "done_ts": st.get("done_ts"), "scanning": st.get("scanning"), "candidates": st.get("candidates"),
                "checked": st.get("checked"), "top_reasons": st.get("top_reasons") or [], "last": last}
    if key == "sniper":
        mig = one(conn, "SELECT MAX(ts) AS ts FROM (SELECT MAX(migrated_at) AS ts FROM coins UNION ALL "
                        "SELECT MAX(migrated_at) FROM waiting_room)") or {}
        sc = one(conn, "SELECT mint, ts, verdict, reason FROM scans ORDER BY id DESC LIMIT 1")
        au = one(conn, "SELECT mint, ts, title, subtitle FROM activity WHERE agent IN ('auditor','analyst','executor') "
                       "ORDER BY id DESC LIMIT 1")
        last = None
        if sc:
            last = {"symbol": _sym(coins, sc["mint"]), "verdict": sc.get("verdict"), "reason": sc.get("reason"),
                    "ts": sc.get("ts")}
        if au and (not last or str(au["ts"]) > str(last["ts"])):
            sub = str(au.get("subtitle") or "")
            last = {"symbol": _sym(coins, au["mint"]) if au.get("mint") else "", "verdict": "audit",
                    "reason": sub.split(" · ", 1)[-1], "ts": au["ts"]}
        return {"kind": "sniper", "waiting": int(waiting_n or 0), "last_migration_ts": mig.get("ts"), "last": last}
    # whale
    any_row = one(conn, "SELECT ts FROM copy_seen ORDER BY ts DESC LIMIT 1") or {}
    ev = one(conn, "SELECT handle, ts, side, mint, action FROM copy_seen WHERE action NOT IN ('not_trade') "
                   "ORDER BY ts DESC LIMIT 1")
    last = None
    if ev:
        last = {"handle": ev.get("handle"), "side": ev.get("side"),
                "symbol": _sym(coins, ev["mint"]) if ev.get("mint") else "",
                "verdict": ev.get("action"), "reason": ACTION_TEXT.get(ev.get("action"), str(ev.get("action") or "")),
                "ts": ev.get("ts")}
    return {"kind": "whale", "wallets": n_traders, "last_tx_ts": any_row.get("ts"), "last": last}


LIVE_ACTIVITY = 30


def milky_block(conn: sqlite3.Connection, limit: int = 15) -> dict[str, Any]:
    """'Video setups (log only)': @milkybids dump-bounce setups, recorded, never traded."""
    status = {}
    try:
        status = json.loads(kv(conn, "agent.milky.status") or "{}")
    except (ValueError, TypeError):
        status = {}
    try:
        gecko = json.loads(kv(conn, "gecko.stats") or "{}")
    except (ValueError, TypeError):
        gecko = {}
    try:
        raw = rows(conn, "SELECT * FROM milky_setups ORDER BY trigger_ts DESC LIMIT ?", (limit,))
        totals = rows(conn, "SELECT entry_kind, COUNT(*) AS n FROM milky_setups GROUP BY entry_kind")
    except sqlite3.Error:
        raw, totals = [], []
    items = []
    for r in raw:
        try:
            filt = json.loads(r.get("filters") or "[]")
        except ValueError:
            filt = []
        try:
            out = json.loads(r.get("outcome") or "null")
        except ValueError:
            out = None
        items.append({
            "symbol": r.get("symbol"), "mint": r.get("mint"), "entry": r.get("entry_kind"),
            "trigger_ts": r.get("trigger_ts"), "trigger_sofia": sofia(r.get("trigger_ts")) if r.get("trigger_ts") else None,
            "trigger_price": r.get("trigger_price"), "mcap": r.get("mcap_at_trigger"), "ath_mcap": r.get("ath_mcap"),
            "vol24": r.get("vol24_usd"), "filters": filt, "outcome": out, "final": bool(r.get("outcome_final")),
        })
    return {
        "title": "Video setups (log only)",
        "note": "PAPER research log of the @milkybids dump-bounce pattern. No trades, no alerts.",
        "enabled": bool(status.get("enabled")) if status else None,
        "status": status or None,
        "counts": {str(t["entry_kind"]): int(t["n"]) for t in totals},
        "items": items,
        "gecko": gecko or None,
    }


def live_collect(conn: sqlite3.Connection, now: datetime | None = None) -> dict[str, Any]:
    """Small real-time file (fomo-live.json): heartbeats, agents, open marks, newest events, ticker."""
    full = collect(conn, now)
    d4 = dict(full["desk4"])
    d4["activity"] = d4.get("activity", [])[-LIVE_ACTIVITY:]
    d4["series"] = d4.get("series", [])[-120:]
    h = full["header"]
    return {
        "kind": "live",
        "generated_at": full["generated_at"],
        "timezone": full.get("timezone"),
        "header": {"mode": h.get("mode"), "night_id": h.get("night_id"), "start_stack": h.get("start_stack"),
                   "current_stack": h.get("current_stack")},
        "heartbeats": [{"agent": x["agent"], "status": x["status"], "age_s": x.get("age_s"), "ts": x.get("ts")}
                       for x in full["heartbeats"]],
        "desk4": d4,
        "milky": {k: full["milky"].get(k) for k in ("title", "enabled", "status", "counts")}
        | {"items": full["milky"].get("items", [])[:5]},
        "schema": 4,
    }

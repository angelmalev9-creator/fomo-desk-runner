"""Hard LIVE gate: refuse LIVE unless PAPER results clear the bar.

In addition to the existing rule (>=1 full PAPER night + the owner typing LIVE),
LIVE also needs a win rate of at least live.min_win_rate_pct over at least
live.min_closed_trades closed PAPER trades (all agents, all nights).

Win = realized PnL after fees > 0. Thresholds live in kv `setting.live.*` and
cannot be lowered below the hard floors (85% and 20 trades). PAPER only; this
module never places an order or touches a wallet.
"""

from __future__ import annotations

import json
from typing import Any

# Hard floors: kv may raise these, never lower them.
FLOOR_WIN_RATE_PCT = 85.0
FLOOR_CLOSED_TRADES = 20

DEFAULTS: dict[str, Any] = {
    "live.min_win_rate_pct": FLOOR_WIN_RATE_PCT,
    "live.min_closed_trades": FLOOR_CLOSED_TRADES,
}

_CLOSED_SQL = (
    "SELECT pnl_usd FROM positions "
    "WHERE status='closed' AND upper(coalesce(mode, 'PAPER'))='PAPER' AND pnl_usd IS NOT NULL"
)
_NIGHTS_SQL = "SELECT COUNT(*) AS n FROM nights WHERE mode='PAPER' AND ended_at IS NOT NULL"


def _rows(source, sql: str) -> list[Any]:
    if source is None:
        return []
    if hasattr(source, "query"):
        return list(source.query(sql))
    cur = source.execute(sql)
    found = cur.fetchall()
    # sqlite3.Row or tuples
    return found


def _cell(row: Any, key: str, idx: int = 0) -> Any:
    if row is None:
        return None
    try:
        return row[key]
    except (KeyError, TypeError, IndexError):
        try:
            return row[idx]
        except Exception:
            return None


def _kv_raw(cfg=None, source=None, key: str = "") -> Any:
    raw = None
    db = getattr(cfg, "db", None) if cfg is not None else None
    if db is None and source is not None and hasattr(source, "kv_get"):
        db = source
    if db is not None:
        try:
            raw = db.kv_get("setting." + key)
        except Exception:
            raw = None
    if raw is None and source is not None and hasattr(source, "execute"):
        try:
            row = source.execute("SELECT value FROM kv WHERE key=?", ("setting." + key,)).fetchone()
            raw = None if row is None else _cell(row, "value")
        except Exception:
            raw = None
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except Exception:
        return raw


def thresholds(cfg=None, source=None) -> tuple[float, int]:
    """(min_win_rate_pct, min_closed_trades), never below the hard floors."""
    rate_raw = _kv_raw(cfg, source, "live.min_win_rate_pct")
    n_raw = _kv_raw(cfg, source, "live.min_closed_trades")
    try:
        rate = float(rate_raw) if rate_raw is not None else FLOOR_WIN_RATE_PCT
    except (TypeError, ValueError):
        rate = FLOOR_WIN_RATE_PCT
    try:
        n = int(float(n_raw)) if n_raw is not None else FLOOR_CLOSED_TRADES
    except (TypeError, ValueError):
        n = FLOOR_CLOSED_TRADES
    return max(rate, FLOOR_WIN_RATE_PCT), max(n, FLOOR_CLOSED_TRADES)


def paper_closed_pnls(source) -> list[float]:
    out: list[float] = []
    for row in _rows(source, _CLOSED_SQL):
        val = _cell(row, "pnl_usd")
        try:
            if val is not None:
                out.append(float(val))
        except (TypeError, ValueError):
            continue
    return out


def completed_paper_nights(source) -> int:
    rows = _rows(source, _NIGHTS_SQL)
    if not rows:
        return 0
    try:
        return int(_cell(rows[0], "n") or 0)
    except (TypeError, ValueError):
        return 0


def evaluate(source, cfg=None, conn=None) -> dict[str, Any]:
    """Score the LIVE gate against closed PAPER trades + completed nights."""
    knobs = conn if conn is not None else source
    min_rate, min_n = thresholds(cfg, knobs)
    pnls = paper_closed_pnls(source)
    closed = len(pnls)
    wins = sum(1 for p in pnls if p > 0)
    win_rate = (wins / closed * 100.0) if closed else 0.0
    nights = completed_paper_nights(source)
    nights_ok = nights >= 1
    trades_ok = closed >= min_n
    rate_ok = closed > 0 and win_rate + 1e-12 >= min_rate
    gate_ok = trades_ok and rate_ok  # the new win-rate gate alone
    ok = nights_ok and gate_ok
    return {
        "ok": ok,
        "nights_ok": nights_ok,
        "gate_ok": gate_ok,
        "trades_ok": trades_ok,
        "rate_ok": rate_ok,
        "nights": nights,
        "closed": closed,
        "wins": wins,
        "win_rate_pct": round(win_rate, 1),
        "min_win_rate_pct": min_rate,
        "min_closed_trades": min_n,
    }


def locked_text(info: dict[str, Any]) -> str:
    """Informal Bulgarian reply when the owner asks for LIVE and the gate fails."""
    rate = info.get("win_rate_pct") or 0.0
    closed = int(info.get("closed") or 0)
    wins = int(info.get("wins") or 0)
    min_rate = info.get("min_win_rate_pct") or FLOOR_WIN_RATE_PCT
    min_n = int(info.get("min_closed_trades") or FLOOR_CLOSED_TRADES)
    return (
        f"LIVE е заключен. За да го пуснеш трябват поне {min_n} затворени PAPER сделки "
        f"и поне {min_rate:g}% печеливши (печалба след такси). "
        f"Сега: {rate:.1f}% ({wins} от {closed} сделки). Оставаме на PAPER."
    )


def dashboard_line(info: dict[str, Any]) -> str:
    rate = info.get("win_rate_pct") or 0.0
    min_rate = info.get("min_win_rate_pct") or FLOOR_WIN_RATE_PCT
    closed = int(info.get("closed") or 0)
    min_n = int(info.get("min_closed_trades") or FLOOR_CLOSED_TRADES)
    return f"LIVE gate: {rate:.1f}% / {min_rate:g}% · {closed}/{min_n} trades"


def refuse_live(source, cfg=None) -> str | None:
    """None if LIVE may be switched on, else a refusal message (nights first, then win-rate)."""
    info = evaluate(source, cfg)
    if info["ok"]:
        return None
    if not info["nights_ok"]:
        return "LIVE needs at least 1 full PAPER night in the db first."
    return locked_text(info)

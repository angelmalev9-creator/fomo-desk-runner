"""Night summary data builder (STEP 12). Page writing is done later."""

from __future__ import annotations

from collections import Counter
from typing import Any

import links
from util import iso_utc


def build_night_summary(db, cfg, night: dict | None = None) -> dict[str, Any]:
    """Return a JSON-able dict plus a markdown body for the morning report."""
    night = night or db.current_night() or _last_night(db)
    if not night:
        return {"markdown": "No night to report yet.", "nights": 0, "trades": []}

    start = night["started_at"]
    end = night.get("ended_at") or iso_utc()

    def since(sql: str, extra=()) -> list[dict]:
        return [dict(r) for r in db.query(sql, (start, end, *extra))]

    positions = since(
        "SELECT * FROM positions WHERE opened_at>=? AND opened_at<=? ORDER BY id"
    )
    closed = [p for p in positions if p.get("closed_at") or p.get("exit_reason")]
    red = [p for p in closed if (p.get("pnl_usd") or 0) < 0]
    green = [p for p in closed if (p.get("pnl_usd") or 0) >= 0]
    red.sort(key=lambda p: p.get("pnl_usd") or 0)
    green.sort(key=lambda p: -(p.get("pnl_usd") or 0))

    scans = since("SELECT * FROM scans WHERE ts>=? AND ts<=?")
    kills = [s for s in scans if s.get("verdict") == "kill"]
    kills_by = Counter(s.get("reason") or "unknown" for s in kills)
    coins_seen = len({s["mint"] for s in scans} | {p["mint"] for p in positions})

    audits_red = since("SELECT * FROM audits WHERE ts>=? AND ts<=? AND pass=0")
    refusals_by_layer = Counter(f"L{a['layer']} {a['name']}" for a in audits_red)

    verdicts = since("SELECT * FROM verdicts WHERE ts>=? AND ts<=?")
    rejects = [v for v in verdicts if not v.get("pass")]
    reject_rules = Counter(v.get("reason") or "unknown" for v in rejects)
    jev_tokens = sum(int(v.get("input_tokens") or 0) for v in verdicts)
    fees_paid = sum(float(p.get("fees_usd") or 0) for p in closed)

    l8 = db.kv_get("auditor.l8_killed")

    def trade_line(p: dict) -> str:
        name = p.get("name") or p["mint"]
        url = links.coin_url(p["mint"], "solana")
        pnl = p.get("pnl_usd")
        pct = None
        if p.get("entry_price") and p.get("exit_price"):
            pct = (p["exit_price"] - p["entry_price"]) / p["entry_price"] * 100
        pnl_s = "n/a" if pnl is None else f"${pnl:.2f}"
        pct_s = "" if pct is None else f" ({pct:+.1f}%)"
        return (
            f"- [{name}]({url}) entry {p.get('entry_price')} exit {p.get('exit_price')} "
            f"{pnl_s}{pct_s} · {p.get('exit_reason') or 'open'}"
        )

    lines = [
        f"# Night report",
        f"Started {start}" + (f", ended {end}" if night.get("ended_at") else " (in progress)"),
        f"Mode: {night.get('mode') or cfg.get('desk.mode')}",
        "",
        "## Red trades",
    ]
    lines.extend(trade_line(p) for p in red) if red else lines.append("- none")
    lines += ["", "## Green trades"]
    lines.extend(trade_line(p) for p in green) if green else lines.append("- none")
    lines += [
        "",
        f"## Coins seen: {coins_seen}",
        "",
        "## Scanner kills by reason",
    ]
    if kills_by:
        lines.extend(f"- {k}: {n}" for k, n in kills_by.most_common())
    else:
        lines.append("- none")
    lines += ["", "## Auditor refusals by layer"]
    if refusals_by_layer:
        lines.extend(f"- {k}: {n}" for k, n in refusals_by_layer.most_common())
    else:
        lines.append("- none")
    if l8:
        lines.append(f"- layer 8 (no data: delegations) killed {l8} coin(s) this night")
    lines += ["", "## Analyst rejects"]
    if reject_rules:
        lines.extend(f"- {k}: {n}" for k, n in reject_rules.most_common())
    else:
        lines.append("- none")
    lines += [
        "",
        f"Jev tokens used: {jev_tokens}",
        f"Fees paid: ${fees_paid:.2f}",
        "",
        "New to FOMO? " + links.SIGNUP_URL,
    ]
    data = {
        "night": night,
        "red": red,
        "green": green,
        "coins_seen": coins_seen,
        "scanner_kills": dict(kills_by),
        "auditor_refusals": dict(refusals_by_layer),
        "analyst_rejects": dict(reject_rules),
        "jev_tokens": jev_tokens,
        "fees_paid": fees_paid,
        "l8_killed": l8,
        "markdown": "\n".join(lines),
    }
    return data


def finish_night(db, cfg) -> dict[str, Any]:
    """Close the current night, build the summary, and clear db.skips."""
    night = db.current_night()
    if night:
        db.end_night(night["id"], db.last_balance())
        night = db.get_position and db.query_one("SELECT * FROM nights WHERE id=?", (night["id"],))
        night = dict(night) if night else None
    data = build_night_summary(db, cfg, night)
    db.clear_skips()
    return data


def _last_night(db) -> dict | None:
    row = db.query_one("SELECT * FROM nights ORDER BY id DESC LIMIT 1")
    return dict(row) if row else None

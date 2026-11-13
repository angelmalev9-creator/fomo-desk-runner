"""Daily copy-trader list refresh (PAPER research, public pages only).

1) Provadata public 30-day leaderboard -> current top N handles.
2) Handles without a known wallet are resolved through fomowalletfinder.com's
   public lookup, at most `copy.resolve_per_day` (2) per day.
3) A resolved wallet is kept only if a recent tx is co-signed by the FOMO signer.
Pinned handles (the original 9) always stay tracked.
"""

from __future__ import annotations

import html as htmllib
import re
from datetime import datetime, timezone
from typing import Any, Callable

import httpx

from copytrade import FOMO_SIGNER, TX_OPTS, can_activate, load_seed_wallets, setting, signers_of
from util import agent_logger, iso_utc

log = agent_logger("copywatch")

PROVADATA_URL = "https://provadata.com/leaderboard/30d"
FINDER_REDIRECT = "https://fomowalletfinder.com/cielo/{handle}/sol"
UA = {"User-Agent": "Mozilla/5.0 (fomo-desk paper research)"}
_B58 = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")

_ROW = re.compile(
    r'<tr><td><span class="rank">(\d+)</span>.*?href="/traders/([^"]+)".*?<td class="num[^"]*">([^<]*)</td>'
    r'.*?<td class="hide-sm">(.*?)</td><td class="num muted hide-md">([^<]*)</td></tr>',
    re.S,
)


def parse_leaderboard(page: str) -> list[dict[str, Any]]:
    out = []
    for rank, handle, pnl, wallets_cell, last in _ROW.findall(page):
        out.append({
            "rank": int(rank),
            "handle": htmllib.unescape(handle).strip(),
            "pnl_30d": htmllib.unescape(pnl).strip(),
            "verified": "Verified" in wallets_cell,
            "last_active": htmllib.unescape(last).strip(),
        })
    return out


def active_recently(last_active: str, max_days: int = 2) -> bool:
    s = (last_active or "").lower()
    if not s:
        return False
    if re.search(r"\b(min|h|hour|hours|just now|sec)\b", s) or "ago" in s and "day" not in s:
        return True
    m = re.match(r"(\d+)\s*day", s)
    return bool(m) and int(m.group(1)) <= max_days


def fetch_leaderboard(get: Callable[[str], str] | None = None) -> list[dict[str, Any]]:
    if get is None:
        with httpx.Client(timeout=20.0, headers=UA) as c:
            r = c.get(PROVADATA_URL)
            r.raise_for_status()
            page = r.text
    else:
        page = get(PROVADATA_URL)
    return parse_leaderboard(page)


def resolve_wallet(handle: str, head: Callable[[str], str | None] | None = None) -> str | None:
    """Public redirect /cielo/<handle>/sol -> https://app.cielo.finance/profile/<wallet>."""
    url = FINDER_REDIRECT.format(handle=handle)
    if head is None:
        with httpx.Client(timeout=20.0, headers=UA, follow_redirects=False) as c:
            r = c.get(url)
            loc = r.headers.get("location") if r.status_code in (301, 302, 303, 307, 308) else None
    else:
        loc = head(url)
    if not loc:
        return None
    m = re.search(r"/profile/([1-9A-HJ-NP-Za-km-z]{32,44})", loc)
    return m.group(1) if m and _B58.match(m.group(1)) else None


def verify_fomo_wallet(rpc, wallet: str, depth: int = 50) -> bool:
    """True if one of the last `depth` txs is co-signed by the FOMO signer. Fails closed.

    Busy wallets get lots of spam/dust transfers, so 10 is not enough.
    """
    try:
        sigs = (rpc.call("getSignaturesForAddress", [wallet, {"limit": depth}]) or {}).get("result") or []
        for s in sigs[:depth]:
            if s.get("err"):
                continue
            tx = (rpc.call("getTransaction", [s["signature"], TX_OPTS]) or {}).get("result")
            if tx and FOMO_SIGNER in signers_of(tx):
                return True
    except Exception as e:
        log.warning("verify wallet failed: %s", type(e).__name__)
    return False


def seed_traders(db) -> int:
    n = 0
    if db.query_one("SELECT 1 FROM copy_traders LIMIT 1"):
        return 0
    for h, w in load_seed_wallets().items():
        db.execute(
            "INSERT OR IGNORE INTO copy_traders(handle, wallet, active, pinned, source, updated_at) VALUES(?,?,1,1,?,?)",
            (h, w, "video top-10 (fomowalletfinder, verified on-chain)", iso_utc()),
        )
        n += 1
    return n


def refresh(db, cfg, rpc, fetch=fetch_leaderboard, resolve=resolve_wallet, verify=verify_fomo_wallet,
            today: str | None = None) -> dict[str, Any]:
    """One refresh. Safe to call often: does real work once per day."""
    today = today or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if db.kv_get("copy.refresh_day") == today:
        return {"skipped": "already today"}
    top_n = int(setting(cfg, "copy.top_n"))
    budget_key = f"copy.resolved.{today}"
    used = int(db.kv_get(budget_key) or 0)
    limit = int(setting(cfg, "copy.resolve_per_day"))
    try:
        board = fetch()
    except Exception as e:
        log.warning("provadata fetch failed: %s (keeping current list)", type(e).__name__)
        return {"error": "fetch"}
    if not board:
        log.warning("provadata: parsed 0 rows (keeping current list)")
        return {"error": "empty"}
    known = {r["handle"].lower(): dict(r) for r in db.query("SELECT * FROM copy_traders")}
    wanted = [r for r in board if r["verified"] and active_recently(r["last_active"])][:top_n]
    added, resolved, unresolved = [], [], []
    for r in board:
        k = known.get(r["handle"].lower())
        if k:
            db.execute("UPDATE copy_traders SET rank=?, pnl_30d=?, updated_at=? WHERE handle=?",
                       (r["rank"], r["pnl_30d"], iso_utc(), k["handle"]))
    wanted_keys = {r["handle"].lower() for r in wanted}
    for r in wanted:
        k = known.get(r["handle"].lower())
        if k and k.get("wallet"):
            if str(k.get("source") or "").startswith("unverified"):
                # Wallet already resolved on an earlier day: re-check on-chain only (no lookup budget).
                if verify(rpc, k["wallet"]):
                    if can_activate(cfg, k):
                        db.execute("UPDATE copy_traders SET active=1, source=?, updated_at=? WHERE handle=?",
                                   ("provadata 30d + fomowalletfinder", iso_utc(), k["handle"]))
                        added.append(k["handle"])
                    else:
                        db.execute("UPDATE copy_traders SET source=?, updated_at=? WHERE handle=?",
                                   ("provadata 30d + fomowalletfinder", iso_utc(), k["handle"]))
                else:
                    unresolved.append(k["handle"])
                continue
            if can_activate(cfg, k):
                db.execute("UPDATE copy_traders SET active=1 WHERE handle=?", (k["handle"],))
            continue
        if used >= limit:
            unresolved.append(r["handle"])
            continue
        used += 1
        db.kv_set(budget_key, str(used))
        wallet = None
        try:
            wallet = resolve(r["handle"])
        except Exception as e:
            log.warning("resolve %s failed: %s", r["handle"], type(e).__name__)
        if wallet and verify(rpc, wallet):
            activate = 1 if can_activate(cfg, k) else 0
            db.execute(
                """INSERT INTO copy_traders(handle, wallet, active, pinned, rank, pnl_30d, source, updated_at)
                   VALUES(?,?,?,0,?,?,?,?) ON CONFLICT(handle) DO UPDATE SET wallet=excluded.wallet,
                   active=CASE WHEN excluded.active=1 THEN 1 ELSE copy_traders.active END,
                   rank=excluded.rank, pnl_30d=excluded.pnl_30d, updated_at=excluded.updated_at""",
                (r["handle"], wallet, activate, r["rank"], r["pnl_30d"], "provadata 30d + fomowalletfinder", iso_utc()),
            )
            resolved.append(r["handle"])
            if activate:
                added.append(r["handle"])
        else:
            if wallet:
                # Keep the lookup so tomorrow only the on-chain check repeats; never copied until verified.
                db.execute(
                    """INSERT INTO copy_traders(handle, wallet, active, pinned, rank, pnl_30d, source, updated_at)
                       VALUES(?,?,0,0,?,?,?,?) ON CONFLICT(handle) DO NOTHING""",
                    (r["handle"], wallet, r["rank"], r["pnl_30d"], "unverified (no FOMO tx in last 50)", iso_utc()),
                )
            unresolved.append(r["handle"])
    # Auto-added traders who fell out of the active top N stop being copied (pinned stay).
    for k in known.values():
        if not k["pinned"] and k["active"] and k["handle"].lower() not in wanted_keys:
            db.execute("UPDATE copy_traders SET active=0, updated_at=? WHERE handle=?", (iso_utc(), k["handle"]))
    if not unresolved:
        db.kv_set("copy.refresh_day", today)
    else:
        # Come back tomorrow for the rest; today's budget is spent.
        db.kv_set("copy.refresh_day", today if used >= limit else "")
    db.kv_set("copy.refresh_last_utc", iso_utc())
    log.info("trader refresh: board=%s wanted=%s added=%s unresolved=%s budget=%s/%s",
             len(board), len(wanted), added, unresolved, used, limit)
    return {"board": len(board), "wanted": [r["handle"] for r in wanted], "added": added,
            "unresolved": unresolved, "resolved_today": used}

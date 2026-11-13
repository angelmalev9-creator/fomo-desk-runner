"""Slow overnight 30-day history fetcher for the copy-trade wallets (PAPER research).

Public Solana RPC only (getSignaturesForAddress + getTransaction, jsonParsed,
maxSupportedTransactionVersion=1). Read-only: no orders, no swaps, no keys.
Checkpoints every step in copy_history.db, so a box freeze or a kill loses at
most one request. Exit 0 = done, non-zero = restart me (see RUN_HISTORY_FETCH.txt).
"""

from __future__ import annotations

import argparse
import logging
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import httpx

from copytrade import HISTORY_DB, TX_OPTS, load_seed_wallets, parse_fomo_trade
from util import LOG_DIR

RPC_URL = "https://api.mainnet-beta.solana.com"
WINDOW_DAYS = 30

SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS wallets (
    handle TEXT PRIMARY KEY, wallet TEXT NOT NULL,
    before_sig TEXT, listing_done INTEGER DEFAULT 0, sig_count INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS sigs (
    signature TEXT PRIMARY KEY, handle TEXT, wallet TEXT, block_time INTEGER,
    status TEXT DEFAULT 'pending', attempts INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_sigs_status ON sigs(status, handle);
CREATE TABLE IF NOT EXISTS trades (
    signature TEXT PRIMARY KEY, handle TEXT, wallet TEXT, block_time INTEGER,
    time_utc TEXT, mint TEXT, side TEXT, token_amount REAL, usdc REAL, price_usd REAL
);
CREATE INDEX IF NOT EXISTS idx_trades_handle ON trades(handle, block_time);
"""

log = logging.getLogger("history_fetch")


def connect(path: Path | str = HISTORY_DB) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


def meta_get(conn, key: str) -> str | None:
    r = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return r["value"] if r else None


def meta_set(conn, key: str, value: Any) -> None:
    conn.execute(
        "INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, str(value)),
    )
    conn.commit()


class GentleRpc:
    """Sync JSON-RPC with a fixed gap and adaptive backoff on 429/5xx."""

    def __init__(self, url: str = RPC_URL, gap_s: float = 1.2, post: Callable | None = None,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        self.url = url
        self.base_gap = gap_s
        self.gap = gap_s
        self._last = 0.0
        self._post = post
        self._sleep = sleep
        self._client: httpx.Client | None = None

    def _do_post(self, payload: dict) -> tuple[int, Any]:
        if self._post is not None:
            return self._post(payload)
        if self._client is None:
            self._client = httpx.Client(timeout=40.0)
        r = self._client.post(self.url, json=payload)
        body = None
        try:
            body = r.json()
        except ValueError:
            pass
        return r.status_code, body

    def call(self, method: str, params: list, max_tries: int = 8) -> Any:
        payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
        for attempt in range(max_tries):
            wait = self.gap - (time.monotonic() - self._last)
            if wait > 0:
                self._sleep(wait)
            self._last = time.monotonic()
            try:
                status, body = self._do_post(payload)
            except httpx.HTTPError as e:
                log.warning("%s net error %s, retry %s", method, type(e).__name__, attempt + 1)
                self._client = None
                self._sleep(min(60, 5 * (attempt + 1)))
                continue
            if status == 429 or status >= 500:
                self.gap = min(10.0, self.gap * 2)
                log.info("%s http %s, gap now %.1fs", method, status, self.gap)
                self._sleep(min(60, 5 * (attempt + 1)))
                continue
            if isinstance(body, dict) and body.get("error"):
                err = body["error"]
                code = err.get("code") if isinstance(err, dict) else None
                if code in (429, -32429) or code == -32005:
                    self.gap = min(10.0, self.gap * 2)
                    self._sleep(min(60, 5 * (attempt + 1)))
                    continue
                raise RuntimeError(f"rpc error {code}: {(err or {}).get('message') if isinstance(err, dict) else err}")
            # success: slowly drift back to the base gap
            self.gap = max(self.base_gap, self.gap * 0.9)
            return (body or {}).get("result")
        raise RuntimeError(f"{method} gave up after {max_tries} tries")


def seed(conn, wallets: dict[str, str]) -> None:
    for h, w in wallets.items():
        conn.execute("INSERT OR IGNORE INTO wallets(handle, wallet) VALUES(?,?)", (h, w))
    conn.commit()
    if meta_get(conn, "cutoff") is None:
        cutoff = int(time.time()) - WINDOW_DAYS * 86400
        meta_set(conn, "cutoff", cutoff)
        meta_set(conn, "started_utc", datetime.now(timezone.utc).isoformat())


def list_signatures(conn, rpc: GentleRpc, handle: str) -> None:
    row = conn.execute("SELECT * FROM wallets WHERE handle=?", (handle,)).fetchone()
    if not row or row["listing_done"]:
        return
    cutoff = int(meta_get(conn, "cutoff") or 0)
    before = row["before_sig"]
    while True:
        p: dict[str, Any] = {"limit": 1000, "commitment": "confirmed"}
        if before:
            p["before"] = before
        res = rpc.call("getSignaturesForAddress", [row["wallet"], p]) or []
        keep = [s for s in res if (s.get("blockTime") or 0) >= cutoff and not s.get("err")]
        conn.executemany(
            "INSERT OR IGNORE INTO sigs(signature, handle, wallet, block_time) VALUES(?,?,?,?)",
            [(s["signature"], handle, row["wallet"], s.get("blockTime")) for s in keep],
        )
        done = len(res) < 1000 or not res or (res[-1].get("blockTime") or 0) < cutoff
        before = res[-1]["signature"] if res else before
        n = conn.execute("SELECT COUNT(*) c FROM sigs WHERE handle=?", (handle,)).fetchone()["c"]
        conn.execute(
            "UPDATE wallets SET before_sig=?, listing_done=?, sig_count=? WHERE handle=?",
            (before, 1 if done else 0, n, handle),
        )
        conn.commit()
        log.info("list %s: +%s (total %s)%s", handle, len(keep), n, " done" if done else "")
        if done:
            return


def process_one(conn, rpc: GentleRpc, sig_row) -> str:
    sig = sig_row["signature"]
    try:
        tx = rpc.call("getTransaction", [sig, TX_OPTS])
    except RuntimeError as e:
        att = sig_row["attempts"] + 1
        status = "failed" if att >= 3 else "pending"
        conn.execute("UPDATE sigs SET attempts=?, status=? WHERE signature=?", (att, status, sig))
        conn.commit()
        log.warning("tx %s… %s (%s)", sig[:10], e, status)
        return status
    trade = parse_fomo_trade(tx, sig_row["wallet"], sig) if tx else None
    if trade:
        conn.execute(
            """INSERT OR REPLACE INTO trades(signature, handle, wallet, block_time, time_utc, mint,
               side, token_amount, usdc, price_usd) VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (sig, sig_row["handle"], sig_row["wallet"], trade["block_time"], trade["time_utc"],
             trade["mint"], trade["side"], trade["token_amount"], trade["usdc"], trade["price_usd"]),
        )
    status = "trade" if trade else ("missing" if not tx else "other")
    conn.execute("UPDATE sigs SET status=?, attempts=attempts+1 WHERE signature=?", (status, sig))
    conn.commit()
    return status


def progress(conn) -> dict[str, Any]:
    rows = conn.execute(
        """SELECT w.handle, w.listing_done, w.sig_count,
                  SUM(CASE WHEN s.status!='pending' THEN 1 ELSE 0 END) done,
                  SUM(CASE WHEN s.status='trade' THEN 1 ELSE 0 END) trades
           FROM wallets w LEFT JOIN sigs s ON s.handle=w.handle GROUP BY w.handle ORDER BY w.sig_count"""
    ).fetchall()
    per = {r["handle"]: {"listed": bool(r["listing_done"]), "sigs": r["sig_count"] or 0,
                         "done": r["done"] or 0, "trades": r["trades"] or 0} for r in rows}
    total = sum(v["sigs"] for v in per.values())
    done = sum(v["done"] for v in per.values())
    return {"per": per, "total": total, "done": done,
            "all_listed": all(v["listed"] for v in per.values()) if per else False,
            "finished": meta_get(conn, "finished_utc")}


def run(db_path: Path | str = HISTORY_DB, gap_s: float = 1.2, wallets: dict[str, str] | None = None,
        rpc: GentleRpc | None = None, max_tx: int | None = None) -> int:
    conn = connect(db_path)
    seed(conn, wallets if wallets is not None else load_seed_wallets())
    rpc = rpc or GentleRpc(gap_s=gap_s)
    # Phase 1: list signatures, smallest wallets first (useful partial data sooner).
    handles = [r["handle"] for r in conn.execute("SELECT handle FROM wallets WHERE listing_done=0")]
    for h in handles:
        list_signatures(conn, rpc, h)
    # Phase 2: fetch txs, wallet with the fewest sigs first.
    n = 0
    t0 = time.monotonic()
    while True:
        row = conn.execute(
            """SELECT s.* FROM sigs s JOIN wallets w ON w.handle=s.handle
               WHERE s.status='pending' ORDER BY w.sig_count, s.block_time DESC LIMIT 1"""
        ).fetchone()
        if row is None:
            break
        process_one(conn, rpc, row)
        n += 1
        if n % 100 == 0:
            p = progress(conn)
            rate = n / max(1.0, time.monotonic() - t0)
            left = p["total"] - p["done"]
            log.info("progress %s/%s (%.2f tx/s, ETA %.1fh)", p["done"], p["total"], rate, left / max(rate, 1e-6) / 3600)
            meta_set(conn, "last_progress_utc", datetime.now(timezone.utc).isoformat())
        if max_tx is not None and n >= max_tx:
            return 3
    meta_set(conn, "finished_utc", datetime.now(timezone.utc).isoformat())
    p = progress(conn)
    log.info("DONE %s/%s sigs, trades=%s", p["done"], p["total"], sum(v["trades"] for v in p["per"].values()))
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--gap", type=float, default=1.2, help="seconds between RPC calls")
    ap.add_argument("--status", action="store_true", help="print progress and exit")
    a = ap.parse_args(argv)
    LOG_DIR.mkdir(exist_ok=True)
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.FileHandler(LOG_DIR / "history_fetch.log")] if not a.status else [logging.StreamHandler()],
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    if a.status:
        import json
        print(json.dumps(progress(connect()), indent=1))
        return 0
    try:
        return run(gap_s=a.gap)
    except Exception as e:  # keep the supervisor loop simple
        log.exception("crashed: %s", type(e).__name__)
        return 1


if __name__ == "__main__":
    sys.exit(main())

"""SQLite persistence for coins, scans, prices, positions, and activity (STEP 4)."""

from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Iterator

from util import ROOT, iso_utc, utcnow

DEFAULT_DB_PATH = ROOT / "desk.db"

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS coins (
    mint TEXT PRIMARY KEY,
    name TEXT,
    symbol TEXT,
    pool TEXT,
    migrated_at TEXT,
    creator TEXT,
    creator_balance_at_migration REAL,
    holders INTEGER
);

CREATE TABLE IF NOT EXISTS scans (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    mint TEXT NOT NULL,
    ts TEXT NOT NULL,
    liquidity_usd REAL,
    top10_pct REAL,
    h1_change_pct REAL,
    age_min INTEGER,
    verdict TEXT,
    reason TEXT
);

CREATE TABLE IF NOT EXISTS audits (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    mint TEXT NOT NULL,
    ts TEXT NOT NULL,
    layer INTEGER,
    name TEXT,
    value_text TEXT,
    pass INTEGER,
    source TEXT
);

CREATE TABLE IF NOT EXISTS verdicts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    mint TEXT NOT NULL,
    ts TEXT NOT NULL,
    alive_6h REAL,
    organic REAL,
    flush REAL,
    chase REAL,
    score INTEGER,
    pass INTEGER,
    reason TEXT,
    input_tokens INTEGER
);

CREATE TABLE IF NOT EXISTS signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    mint TEXT NOT NULL,
    ts TEXT NOT NULL,
    price REAL,
    size_usd REAL,
    stack_pct REAL,
    link TEXT,
    mode TEXT,
    status TEXT,
    reason TEXT,
    score INTEGER
);

CREATE TABLE IF NOT EXISTS positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    mint TEXT NOT NULL,
    opened_at TEXT NOT NULL,
    entry_price REAL,
    size_usd REAL,
    peak_price REAL,
    stop_price REAL,
    trail_active INTEGER DEFAULT 0,
    closed_at TEXT,
    exit_price REAL,
    exit_reason TEXT,
    pnl_usd REAL,
    fees_usd REAL,
    mode TEXT,
    status TEXT,
    name TEXT
);

CREATE TABLE IF NOT EXISTS activity (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    kind TEXT,
    mint TEXT,
    agent TEXT,
    title TEXT,
    subtitle TEXT,
    value TEXT
);

CREATE TABLE IF NOT EXISTS prices (
    mint TEXT NOT NULL,
    ts TEXT NOT NULL,
    price REAL,
    volume_m5 REAL
);
CREATE INDEX IF NOT EXISTS idx_prices_mint_ts ON prices(mint, ts);

CREATE TABLE IF NOT EXISTS balance (
    ts TEXT PRIMARY KEY,
    value REAL
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT,
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS change_log (
    ts TEXT NOT NULL,
    key TEXT,
    old TEXT,
    new TEXT,
    who TEXT
);

CREATE TABLE IF NOT EXISTS skips (
    mint TEXT PRIMARY KEY,
    until TEXT
);

CREATE TABLE IF NOT EXISTS heartbeats (
    agent TEXT PRIMARY KEY,
    ts TEXT
);

CREATE TABLE IF NOT EXISTS push_subs (
    endpoint TEXT PRIMARY KEY,
    p256dh TEXT,
    auth TEXT,
    created_at TEXT
);

CREATE TABLE IF NOT EXISTS nights (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    mode TEXT,
    start_stack REAL,
    end_balance REAL
);

CREATE TABLE IF NOT EXISTS waiting_room (
    mint TEXT PRIMARY KEY,
    name TEXT,
    symbol TEXT,
    pool TEXT,
    migrated_at TEXT,
    creator TEXT,
    creator_balance REAL,
    curve TEXT,
    first_liq_usd REAL,
    last_scan_ts TEXT,
    status TEXT
);

CREATE TABLE IF NOT EXISTS kv (
    key TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS push_queue (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    text TEXT,
    mint TEXT,
    delivered_at TEXT
);

CREATE TABLE IF NOT EXISTS copy_traders (
    handle TEXT PRIMARY KEY,
    wallet TEXT,
    active INTEGER DEFAULT 1,
    pinned INTEGER DEFAULT 0,
    rank INTEGER,
    pnl_30d TEXT,
    source TEXT,
    updated_at TEXT,
    winnable INTEGER
);

CREATE TABLE IF NOT EXISTS copy_seen (
    signature TEXT PRIMARY KEY,
    handle TEXT,
    ts TEXT,
    side TEXT,
    mint TEXT,
    action TEXT
);

CREATE TABLE IF NOT EXISTS milky_setups (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    mint TEXT NOT NULL,
    symbol TEXT,
    pool TEXT,
    entry_kind TEXT NOT NULL,
    trigger_ts TEXT NOT NULL,
    trigger_price REAL,
    ath_price REAL,
    ath_ts TEXT,
    mcap_at_trigger REAL,
    ath_mcap REAL,
    vol24_usd REAL,
    filters TEXT,
    detected_at TEXT,
    outcome TEXT,
    outcome_final INTEGER DEFAULT 0,
    UNIQUE (mint, entry_kind, trigger_ts)
);

CREATE TABLE IF NOT EXISTS equity (
    ts TEXT PRIMARY KEY,
    value REAL,
    realized REAL,
    open_n INTEGER
);
"""


class DB:
    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path else DEFAULT_DB_PATH
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self._init()

    def _connect(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.path, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            self._local.conn = conn
        return conn

    def _init(self) -> None:
        conn = self._connect()
        conn.executescript(SCHEMA)
        cols = {r[1] for r in conn.execute("PRAGMA table_info(positions)")}
        for col in ("source", "copy_handle"):
            if col not in cols:
                conn.execute(f"ALTER TABLE positions ADD COLUMN {col} TEXT")
        trader_cols = {r[1] for r in conn.execute("PRAGMA table_info(copy_traders)")}
        if "winnable" not in trader_cols:
            conn.execute("ALTER TABLE copy_traders ADD COLUMN winnable INTEGER")
        conn.commit()

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        conn = self._connect()
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    def execute(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor:
        conn = self._connect()
        cur = conn.execute(sql, tuple(params))
        conn.commit()
        return cur

    def executemany(self, sql: str, rows: Iterable[Iterable[Any]]) -> None:
        conn = self._connect()
        conn.executemany(sql, list(rows))
        conn.commit()

    def query(self, sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
        return self._connect().execute(sql, tuple(params)).fetchall()

    def query_one(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Row | None:
        return self._connect().execute(sql, tuple(params)).fetchone()

    # ----- coins / waiting room -----
    def upsert_coin(self, **kw: Any) -> None:
        mint = kw["mint"]
        existing = self.query_one("SELECT * FROM coins WHERE mint=?", (mint,))
        if existing is None:
            self.execute(
                """INSERT INTO coins (mint, name, symbol, pool, migrated_at, creator,
                   creator_balance_at_migration, holders)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (
                    mint,
                    kw.get("name"),
                    kw.get("symbol"),
                    kw.get("pool"),
                    kw.get("migrated_at"),
                    kw.get("creator"),
                    kw.get("creator_balance_at_migration"),
                    kw.get("holders"),
                ),
            )
            return
        fields = {
            k: kw[k]
            for k in (
                "name",
                "symbol",
                "pool",
                "migrated_at",
                "creator",
                "creator_balance_at_migration",
                "holders",
            )
            if k in kw and kw[k] is not None
        }
        if not fields:
            return
        sets = ", ".join(f"{k}=?" for k in fields)
        self.execute(f"UPDATE coins SET {sets} WHERE mint=?", (*fields.values(), mint))

    def get_coin(self, mint: str) -> dict | None:
        row = self.query_one("SELECT * FROM coins WHERE mint=?", (mint,))
        return dict(row) if row else None

    def find_coins(self, q: str) -> list[dict]:
        q = (q or "").strip()
        if not q:
            return []
        # exact mint
        row = self.get_coin(q)
        if row:
            return [row]
        like = q.lower()
        rows = self.query(
            "SELECT * FROM coins WHERE lower(name)=? OR lower(symbol)=?",
            (like, like),
        )
        if rows:
            return [dict(r) for r in rows]
        rows = self.query(
            "SELECT * FROM coins WHERE lower(name) LIKE ? OR lower(symbol) LIKE ?",
            (f"%{like}%", f"%{like}%"),
        )
        return [dict(r) for r in rows]

    def waiting_put(self, **kw: Any) -> None:
        self.execute(
            """INSERT INTO waiting_room (mint, name, symbol, pool, migrated_at, creator,
                creator_balance, curve, first_liq_usd, last_scan_ts, status)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(mint) DO UPDATE SET
                  name=COALESCE(excluded.name, waiting_room.name),
                  symbol=COALESCE(excluded.symbol, waiting_room.symbol),
                  pool=COALESCE(excluded.pool, waiting_room.pool),
                  last_scan_ts=excluded.last_scan_ts,
                  status=excluded.status,
                  first_liq_usd=COALESCE(waiting_room.first_liq_usd, excluded.first_liq_usd)
            """,
            (
                kw["mint"],
                kw.get("name"),
                kw.get("symbol"),
                kw.get("pool"),
                kw.get("migrated_at"),
                kw.get("creator"),
                kw.get("creator_balance"),
                kw.get("curve"),
                kw.get("first_liq_usd"),
                kw.get("last_scan_ts"),
                kw.get("status", "waiting"),
            ),
        )

    def waiting_all(self, status: str = "waiting") -> list[dict]:
        rows = self.query("SELECT * FROM waiting_room WHERE status=?", (status,))
        return [dict(r) for r in rows]

    def waiting_set_status(self, mint: str, status: str) -> None:
        self.execute("UPDATE waiting_room SET status=? WHERE mint=?", (status, mint))

    # ----- scans / audits / verdicts / signals -----
    def add_scan(self, **kw: Any) -> int:
        cur = self.execute(
            """INSERT INTO scans (mint, ts, liquidity_usd, top10_pct, h1_change_pct,
                age_min, verdict, reason) VALUES (?,?,?,?,?,?,?,?)""",
            (
                kw["mint"],
                kw.get("ts") or iso_utc(),
                kw.get("liquidity_usd"),
                kw.get("top10_pct"),
                kw.get("h1_change_pct"),
                kw.get("age_min"),
                kw.get("verdict"),
                kw.get("reason"),
            ),
        )
        return int(cur.lastrowid)

    def add_audit_layer(self, **kw: Any) -> int:
        cur = self.execute(
            """INSERT INTO audits (mint, ts, layer, name, value_text, pass, source)
                VALUES (?,?,?,?,?,?,?)""",
            (
                kw["mint"],
                kw.get("ts") or iso_utc(),
                kw["layer"],
                kw.get("name"),
                kw.get("value_text"),
                None if kw.get("passed") is None else (1 if kw.get("passed") else 0),
                kw.get("source"),
            ),
        )
        return int(cur.lastrowid)

    def add_verdict(self, **kw: Any) -> int:
        cur = self.execute(
            """INSERT INTO verdicts (mint, ts, alive_6h, organic, flush, chase, score,
                pass, reason, input_tokens) VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (
                kw["mint"],
                kw.get("ts") or iso_utc(),
                kw.get("alive_6h"),
                kw.get("organic"),
                kw.get("flush"),
                kw.get("chase"),
                kw.get("score"),
                1 if kw.get("passed") else 0,
                kw.get("reason"),
                kw.get("input_tokens"),
            ),
        )
        return int(cur.lastrowid)

    def add_signal(self, **kw: Any) -> int:
        cur = self.execute(
            """INSERT INTO signals (mint, ts, price, size_usd, stack_pct, link, mode,
                status, reason, score) VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (
                kw["mint"],
                kw.get("ts") or iso_utc(),
                kw.get("price"),
                kw.get("size_usd"),
                kw.get("stack_pct"),
                kw.get("link"),
                kw.get("mode"),
                kw.get("status"),
                kw.get("reason"),
                kw.get("score"),
            ),
        )
        return int(cur.lastrowid)

    def update_signal_status(self, mint: str, status: str, reason: str | None = None) -> None:
        if reason is None:
            self.execute(
                "UPDATE signals SET status=? WHERE id=(SELECT id FROM signals WHERE mint=? ORDER BY id DESC LIMIT 1)",
                (status, mint),
            )
        else:
            self.execute(
                "UPDATE signals SET status=?, reason=? WHERE id=(SELECT id FROM signals WHERE mint=? ORDER BY id DESC LIMIT 1)",
                (status, reason, mint),
            )

    def last_signal(self, mint: str) -> dict | None:
        row = self.query_one("SELECT * FROM signals WHERE mint=? ORDER BY id DESC LIMIT 1", (mint,))
        return dict(row) if row else None

    def signals_since(self, ts: str) -> list[dict]:
        return [dict(r) for r in self.query("SELECT * FROM signals WHERE ts>=? ORDER BY id", (ts,))]

    # ----- positions -----
    def open_position(self, **kw: Any) -> int:
        cur = self.execute(
            """INSERT INTO positions (mint, opened_at, entry_price, size_usd, peak_price,
                stop_price, trail_active, mode, status, name, source, copy_handle)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                kw["mint"],
                kw.get("opened_at") or iso_utc(),
                kw["entry_price"],
                kw["size_usd"],
                kw.get("peak_price") or kw["entry_price"],
                kw["stop_price"],
                1 if kw.get("trail_active") else 0,
                kw.get("mode", "PAPER"),
                kw.get("status", "open"),
                kw.get("name"),
                kw.get("source"),
                kw.get("copy_handle"),
            ),
        )
        return int(cur.lastrowid)

    def get_position(self, pid: int) -> dict | None:
        row = self.query_one("SELECT * FROM positions WHERE id=?", (pid,))
        return dict(row) if row else None

    def open_positions(self) -> list[dict]:
        rows = self.query("SELECT * FROM positions WHERE status IN ('open','exiting') ORDER BY id")
        return [dict(r) for r in rows]

    def update_position(self, pid: int, **kw: Any) -> None:
        if not kw:
            return
        sets = ", ".join(f"{k}=?" for k in kw)
        self.execute(f"UPDATE positions SET {sets} WHERE id=?", (*kw.values(), pid))

    # ----- activity / prices / balance -----
    def add_activity(self, **kw: Any) -> int:
        cur = self.execute(
            """INSERT INTO activity (ts, kind, mint, agent, title, subtitle, value)
                VALUES (?,?,?,?,?,?,?)""",
            (
                kw.get("ts") or iso_utc(),
                kw.get("kind"),
                kw.get("mint"),
                kw.get("agent"),
                kw.get("title"),
                kw.get("subtitle"),
                kw.get("value"),
            ),
        )
        return int(cur.lastrowid)

    def add_price(self, mint: str, price: float | None, volume_m5: float | None, ts: str | None = None) -> None:
        self.execute(
            "INSERT INTO prices (mint, ts, price, volume_m5) VALUES (?,?,?,?)",
            (mint, ts or iso_utc(), price, volume_m5),
        )

    def add_balance(self, value: float, ts: str | None = None) -> None:
        self.execute(
            "INSERT OR REPLACE INTO balance (ts, value) VALUES (?,?)",
            (ts or iso_utc(), value),
        )

    def last_balance(self) -> float | None:
        row = self.query_one("SELECT value FROM balance ORDER BY ts DESC LIMIT 1")
        return float(row["value"]) if row else None

    # ----- settings / change_log / skips / heartbeats -----
    def get_setting(self, key: str) -> str | None:
        row = self.query_one("SELECT value FROM settings WHERE key=?", (key,))
        return None if row is None else row["value"]

    def all_settings(self) -> dict[str, str]:
        return {r["key"]: r["value"] for r in self.query("SELECT key, value FROM settings")}

    def set_setting(self, key: str, value: str, who: str = "system", old: str | None = None) -> None:
        now = iso_utc()
        if old is None:
            prev = self.get_setting(key)
        else:
            prev = old
        self.execute(
            "INSERT INTO settings (key, value, updated_at) VALUES (?,?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            (key, value, now),
        )
        self.execute(
            "INSERT INTO change_log (ts, key, old, new, who) VALUES (?,?,?,?,?)",
            (now, key, prev, value, who),
        )

    def add_skip(self, mint: str, until: str | None = None) -> None:
        self.execute(
            "INSERT OR REPLACE INTO skips (mint, until) VALUES (?,?)",
            (mint, until or "night"),
        )

    def is_skipped(self, mint: str) -> bool:
        return self.query_one("SELECT mint FROM skips WHERE mint=?", (mint,)) is not None

    def clear_skips(self) -> None:
        self.execute("DELETE FROM skips")

    def heartbeat(self, agent: str, ts: str | None = None) -> None:
        self.execute(
            "INSERT INTO heartbeats (agent, ts) VALUES (?,?) ON CONFLICT(agent) DO UPDATE SET ts=excluded.ts",
            (agent, ts or iso_utc()),
        )

    def all_heartbeats(self) -> dict[str, str]:
        return {r["agent"]: r["ts"] for r in self.query("SELECT agent, ts FROM heartbeats")}

    def kv_get(self, key: str, default: str | None = None) -> str | None:
        row = self.query_one("SELECT value FROM kv WHERE key=?", (key,))
        return default if row is None else row["value"]

    def kv_set(self, key: str, value: str) -> None:
        self.execute(
            "INSERT INTO kv (key, value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )

    def start_night(self, mode: str, start_stack: float) -> int:
        cur = self.execute(
            "INSERT INTO nights (started_at, mode, start_stack) VALUES (?,?,?)",
            (iso_utc(), mode, start_stack),
        )
        return int(cur.lastrowid)

    def end_night(self, night_id: int, end_balance: float | None) -> None:
        self.execute(
            "UPDATE nights SET ended_at=?, end_balance=? WHERE id=?",
            (iso_utc(), end_balance, night_id),
        )

    def completed_paper_nights(self) -> int:
        row = self.query_one(
            "SELECT COUNT(*) AS n FROM nights WHERE mode='PAPER' AND ended_at IS NOT NULL"
        )
        return int(row["n"]) if row else 0

    def current_night(self) -> dict | None:
        row = self.query_one("SELECT * FROM nights WHERE ended_at IS NULL ORDER BY id DESC LIMIT 1")
        return dict(row) if row else None

    def audits_for(self, mint: str) -> list[dict]:
        return [dict(r) for r in self.query("SELECT * FROM audits WHERE mint=? ORDER BY id", (mint,))]

    # ----- quiet mode queue / equity snapshots -----
    def queue_push(self, text: str, mint: str | None = None) -> int:
        cur = self.execute(
            "INSERT INTO push_queue (ts, text, mint) VALUES (?,?,?)",
            (iso_utc(), text, mint),
        )
        return int(cur.lastrowid)

    def queued_pushes(self) -> list[dict]:
        rows = self.query("SELECT * FROM push_queue WHERE delivered_at IS NULL ORDER BY id")
        return [dict(r) for r in rows]

    def mark_pushes_delivered(self) -> int:
        cur = self.execute(
            "UPDATE push_queue SET delivered_at=? WHERE delivered_at IS NULL", (iso_utc(),)
        )
        return int(cur.rowcount or 0)

    def add_equity(self, value: float, realized: float, open_n: int, ts: str | None = None) -> None:
        self.execute(
            "INSERT OR REPLACE INTO equity (ts, value, realized, open_n) VALUES (?,?,?,?)",
            (ts or iso_utc(), value, realized, open_n),
        )

    def last_equity(self) -> dict | None:
        row = self.query_one("SELECT * FROM equity ORDER BY ts DESC LIMIT 1")
        return dict(row) if row else None

    def closed_positions_since(self, ts: str) -> list[dict]:
        rows = self.query(
            "SELECT * FROM positions WHERE status='closed' AND closed_at>=? ORDER BY closed_at",
            (ts,),
        )
        return [dict(r) for r in rows]

    def close(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None


# Process-wide handle, overwritten in tests.
db = DB()

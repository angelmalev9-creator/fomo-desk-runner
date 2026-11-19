"""Apply the 2026-11-19 PAPER 'more opportunities' policy to desk.db.

Runs after the Actions gist restore and before the desk starts, so the change
takes effect on the next self-dispatch (never mid-run, never on this box).

Sets:
  agents.max_position_pct 45 (clears the min-ticket dead zone at the ~$117 stack)
  Quiet entry filters loosened ~25% (noise, volume, age, chase, shared liquidity)
  momentum.enabled true; Momentum uses the same max_position_pct sizing in code

Does not touch: the 1% market stop, copy/Whale, Dip, LIVE gating, sleeve_pct.
Idempotent via setting.policy.opportunities. Never prints secrets.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
POLICY_ID = "2026-11-19-more-opportunities"
POLICY_KEY = "setting.policy.opportunities"

# kv `setting.<key>` values that this policy writes.
WANTED: dict[str, object] = {
    "agents.max_position_pct": 45.0,
    "agents.min_liquidity_usd": 6000.0,
    "momentum.enabled": True,
    "quiet.enabled": True,
    "quiet.max_noise_pct": 1.5,
    "quiet.vol_x": 1.5,
    "quiet.min_vol30_usd": 3750.0,
    "quiet.min_age_min": 22,
    "quiet.max_per_hour": 3,
    "quiet.max_chase_pct": 4.0,
}

# Must remain exactly as they are. The apply step refuses to start if a write
# would target any of these keys.
PROTECTED_PREFIXES = (
    "risk.stop",
    "copy.",
    "dip.",
    "live.",
    "whale.",
    "agents.sleeve_pct",
)


def _db_path() -> Path:
    raw = (os.environ.get("DESK_DB") or "").strip()
    return Path(raw) if raw else ROOT / "desk.db"


def _dump(value: object) -> str:
    return json.dumps(value)


def _load(raw: str | None) -> object | None:
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except Exception:
        return raw


def _log(msg: str) -> None:
    print(msg, flush=True)


def _unpause_momentum(conn: sqlite3.Connection) -> str | None:
    """Remove only 'momentum' from settings.desk.paused. Leave every other agent."""
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "settings" not in tables:
        return None
    row = conn.execute("SELECT value FROM settings WHERE key='desk.paused'").fetchone()
    if not row or not (row[0] or "").strip():
        return None
    old = str(row[0])
    names = [p.strip() for p in old.split(",") if p.strip()]
    kept = [p for p in names if p.lower() != "momentum"]
    if kept == names:
        return None
    new = ",".join(kept)
    conn.execute(
        "INSERT INTO settings(key,value,updated_at) VALUES('desk.paused',?,datetime('now')) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
        (new,),
    )
    return f"desk.paused {old!r} -> {new!r}"


def apply(path: Path | None = None) -> list[str]:
    db = path or _db_path()
    if not db.exists():
        _log(f"apply_paper_policy: no desk.db at {db.name}; skip")
        return []
    conn = sqlite3.connect(str(db))
    changes: list[str] = []
    try:
        conn.execute("CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT)")
        already = conn.execute("SELECT value FROM kv WHERE key=?", (POLICY_KEY,)).fetchone()
        if already and _load(already[0]) == POLICY_ID:
            _log(f"policy already applied ({POLICY_ID}); no changes")
            return []
        for key, new in WANTED.items():
            if any(key == p or key.startswith(p) for p in PROTECTED_PREFIXES):
                raise RuntimeError(f"refusing to write protected key {key}")
            kv = "setting." + key
            row = conn.execute("SELECT value FROM kv WHERE key=?", (kv,)).fetchone()
            old = _load(row[0]) if row else None
            if old == new:
                continue
            conn.execute(
                "INSERT INTO kv(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (kv, _dump(new)),
            )
            changes.append(f"{key} {old!r} -> {new!r}")
        note = _unpause_momentum(conn)
        if note:
            changes.append(note)
        conn.execute(
            "INSERT INTO kv(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (POLICY_KEY, _dump(POLICY_ID)),
        )
        conn.commit()
    finally:
        conn.close()
    if changes:
        for line in changes:
            _log(f"policy {POLICY_ID}: {line}")
        _log(f"policy applied, {len(changes)} change(s)")
    else:
        _log(f"policy {POLICY_ID}: already at target values")
    return changes


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    path = Path(args[0]) if args else _db_path()
    apply(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

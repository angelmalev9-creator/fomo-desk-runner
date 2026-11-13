"""ci.state: WAL-safe backup and gzip+base64 round trip (no network)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import ci.state as state


def test_backup_and_roundtrip(tmp_path, monkeypatch):
    src = tmp_path / "desk.db"
    conn = sqlite3.connect(str(src))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE kv (key TEXT PRIMARY KEY, value TEXT)")
    conn.execute("INSERT INTO kv VALUES ('setting.agents.sleeve_pct', '100.0')")
    conn.commit()
    conn.close()
    monkeypatch.setattr(state, "DB_PATH", src)
    dest = tmp_path / "copy.db"
    state.consistent_backup(dest)
    blob = state.encode_backup(dest)
    out = tmp_path / "restored.db"
    state.decode_backup(blob, out)
    c = sqlite3.connect(str(out))
    assert c.execute("SELECT value FROM kv WHERE key='setting.agents.sleeve_pct'").fetchone()[0] == "100.0"
    c.close()
    assert "BEGIN" not in blob and "PRIVATE" not in blob

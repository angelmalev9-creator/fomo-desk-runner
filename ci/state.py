"""Restore and checkpoint desk.db to a private GitHub Gist (no secrets in the file).

The dashboard gist is public; this state gist is private and holds only a gzip+base64
SQLite backup of desk.db (PAPER trades/settings). Never writes .secrets or tokens.

Usage:
  python ci/state.py restore
  python ci/state.py save
"""

from __future__ import annotations

import base64
import gzip
import json
import os
import sqlite3
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DB_PATH = ROOT / "desk.db"
FILE_NAME = "desk.db.gz.b64"
API = "https://api.github.com"


def _token() -> str:
    t = (os.environ.get("GIST_TOKEN") or os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN") or "").strip()
    if not t:
        raise SystemExit("GIST_TOKEN or GITHUB_TOKEN is not set")
    return t


def _state_gist_id() -> str:
    gid = (os.environ.get("STATE_GIST_ID") or "").strip()
    if not gid:
        raise SystemExit("STATE_GIST_ID is not set")
    return gid


def _request(method: str, url: str, body: dict | None = None, timeout: int = 60) -> tuple[int, dict, bytes]:
    data = None if body is None else json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={
            "Authorization": f"Bearer {_token()}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "fomo-desk-runner",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as res:
            raw = res.read()
            headers = dict(res.headers)
            return res.status, headers, raw
    except urllib.error.HTTPError as e:
        raw = e.read() if e.fp else b""
        return e.code, {}, raw


def consistent_backup(dest: Path) -> Path:
    """Copy desk.db via the SQLite backup API (WAL-safe) into dest (uncompressed)."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        dest.unlink()
    if not DB_PATH.exists():
        raise FileNotFoundError("desk.db is missing")
    src = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    try:
        dst = sqlite3.connect(str(dest))
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()
    return dest


def encode_backup(raw_db: Path) -> str:
    return base64.b64encode(gzip.compress(raw_db.read_bytes(), compresslevel=6)).decode("ascii")


def decode_backup(blob: str, dest: Path) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(gzip.decompress(base64.b64decode(blob)))
    return dest


def save() -> None:
    with tempfile.TemporaryDirectory(prefix="desk-bak-") as td:
        raw = Path(td) / "desk.db"
        consistent_backup(raw)
        raw_bytes = raw.read_bytes()
        gz = gzip.compress(raw_bytes, compresslevel=6)
        payload = base64.b64encode(gz).decode("ascii")
        size = len(raw_bytes)
    gid = _state_gist_id()
    status, _, body = _request(
        "PATCH",
        f"{API}/gists/{gid}",
        {"files": {FILE_NAME: {"content": payload}}},
    )
    if status >= 300:
        raise SystemExit(f"state gist save failed HTTP {status} ({len(body)} B body)")
    artifact = ROOT / "desk-backup.db.gz"
    artifact.write_bytes(gz)
    print(f"state saved ok db_bytes={size} gist={gid[:8]}… artifact={artifact.name}", flush=True)


def restore() -> None:
    gid = _state_gist_id()
    status, _, body = _request("GET", f"{API}/gists/{gid}")
    if status >= 300:
        raise SystemExit(f"state gist load failed HTTP {status}")
    data = json.loads(body.decode("utf-8"))
    files = data.get("files") or {}
    item = files.get(FILE_NAME)
    if not item or not item.get("content"):
        print("state gist has no checkpoint yet; starting with empty/missing desk.db", flush=True)
        return
    content = item["content"]
    # If truncated, fetch raw_url
    if item.get("truncated") and item.get("raw_url"):
        req = urllib.request.Request(
            item["raw_url"],
            headers={
                "Authorization": f"Bearer {_token()}",
                "User-Agent": "fomo-desk-runner",
                "Accept": "application/vnd.github.raw",
            },
        )
        with urllib.request.urlopen(req, timeout=60) as res:
            content = res.read().decode("utf-8")
    # drop stale WAL/SHM so the restored db is the source of truth
    for suffix in ("-wal", "-shm"):
        p = Path(str(DB_PATH) + suffix)
        if p.exists():
            p.unlink()
    decode_backup(content, DB_PATH)
    # sanity: can open
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    try:
        n = conn.execute("SELECT count(*) FROM sqlite_master").fetchone()[0]
        kv = conn.execute("SELECT count(*) FROM kv").fetchone()[0]
    finally:
        conn.close()
    print(f"state restored ok db_bytes={DB_PATH.stat().st_size} tables/objs={n} kv_rows={kv}", flush=True)


def loop(interval_s: int = 600) -> None:
    while True:
        try:
            save()
        except Exception as e:
            print(f"checkpoint failed: {type(e).__name__}", flush=True)
        time.sleep(interval_s)


def main(argv: list[str] | None = None) -> int:
    cmd = (argv or sys.argv)[1] if (argv or sys.argv)[1:] else ""
    if cmd == "save":
        save()
        return 0
    if cmd == "restore":
        restore()
        return 0
    if cmd == "loop":
        loop(int(os.environ.get("STATE_INTERVAL_S") or 600))
        return 0
    print("usage: python ci/state.py restore|save|loop", flush=True)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())

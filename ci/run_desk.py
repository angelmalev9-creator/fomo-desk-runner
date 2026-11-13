"""Run main.py + gist_push.py in PAPER, checkpoint the db, self-retrigger before timeout.

Never prints secret values. Stops at ~5h45, dispatches the next workflow run, then
SIGTERM both processes.
"""

from __future__ import annotations

import json
import os
import signal
import sqlite3
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import ci.state as state  # noqa: E402

RUN_S = int(os.environ.get("DESK_RUN_S") or 345 * 60)  # 5h45
CHECKPOINT_S = int(os.environ.get("STATE_INTERVAL_S") or 600)
LOG_DIR = ROOT / "logs"


def _log(msg: str) -> None:
    print(msg, flush=True)


def force_paper() -> None:
    db = ROOT / "desk.db"
    if not db.exists():
        return
    conn = sqlite3.connect(str(db))
    try:
        row = conn.execute("SELECT value FROM settings WHERE key='desk.mode'").fetchone()
        mode = (row[0] if row else "PAPER") or "PAPER"
        if str(mode).upper() != "PAPER":
            conn.execute(
                "INSERT INTO settings(key,value,updated_at) VALUES('desk.mode','PAPER',datetime('now')) "
                "ON CONFLICT(key) DO UPDATE SET value='PAPER'"
            )
            conn.commit()
            _log("CI guard: desk.mode was not PAPER; forced PAPER")
        else:
            _log("CI guard: desk.mode is PAPER")
    finally:
        conn.close()


def _dispatch_next() -> None:
    repo = (os.environ.get("GITHUB_REPOSITORY") or "").strip()
    if not repo:
        _log("no GITHUB_REPOSITORY; skip self-dispatch")
        return
    token = (os.environ.get("GIST_TOKEN") or os.environ.get("GITHUB_TOKEN") or "").strip()
    if not token:
        _log("no token; skip self-dispatch")
        return
    ref = (os.environ.get("GITHUB_REF_NAME") or "main").strip()
    url = f"https://api.github.com/repos/{repo}/actions/workflows/desk.yml/dispatches"
    data = json.dumps({"ref": ref}).encode()
    req = urllib.request.Request(
        url,
        data=data,
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "fomo-desk-runner",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as res:
            _log(f"self-dispatch HTTP {res.status} ref={ref}")
    except Exception as e:
        _log(f"self-dispatch failed: {type(e).__name__}")


def _start(cmd: list[str], log_name: str) -> subprocess.Popen:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    logf = open(LOG_DIR / log_name, "ab", buffering=0)
    return subprocess.Popen(
        cmd,
        cwd=str(ROOT),
        stdout=logf,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )


def _term(proc: subprocess.Popen, name: str, wait_s: int = 30) -> None:
    if proc.poll() is not None:
        _log(f"{name} already exited rc={proc.returncode}")
        return
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    t0 = time.time()
    while time.time() - t0 < wait_s:
        if proc.poll() is not None:
            _log(f"{name} stopped rc={proc.returncode}")
            return
        time.sleep(0.5)
    _log(f"{name} still running after SIGTERM; sending SIGKILL")
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    proc.wait(timeout=10)


def _safe_save(label: str) -> None:
    try:
        state.save()
        _log(f"checkpoint {label} ok")
    except Exception as e:
        _log(f"checkpoint {label} failed: {type(e).__name__}")


def main() -> int:
    os.chdir(ROOT)
    os.environ.setdefault("PYTHONUNBUFFERED", "1")
    force_paper()
    py = sys.executable
    main_p = _start([py, "main.py"], "main.out")
    gist_p = _start([py, "gist_push.py"], "gist_push.log")
    _log(f"started main pid={main_p.pid} gist_push pid={gist_p.pid} run_s={RUN_S}")
    t0 = time.time()
    last_save = t0
    rc = 0
    try:
        while True:
            time.sleep(5)
            now = time.time()
            if main_p.poll() is not None:
                _log(f"main.py exited early rc={main_p.returncode}")
                rc = main_p.returncode or 1
                break
            if gist_p.poll() is not None:
                _log(f"gist_push.py exited rc={gist_p.returncode}; restarting")
                gist_p = _start([py, "gist_push.py"], "gist_push.log")
            if now - last_save >= CHECKPOINT_S:
                _safe_save("periodic")
                last_save = now
            if now - t0 >= RUN_S:
                _log("run window reached; checkpoint, self-dispatch, stop")
                _safe_save("pre-dispatch")
                _dispatch_next()
                break
    except KeyboardInterrupt:
        _log("interrupted")
        rc = 0
    finally:
        _term(main_p, "main.py")
        _term(gist_p, "gist_push.py")
        _safe_save("final")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())

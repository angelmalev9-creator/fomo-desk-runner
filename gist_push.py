"""Push a sanitized desk dashboard snapshot to a secret GitHub Gist.

Runs as a standalone loop (does not touch main.py). Opens desk.db only through
dashboard_data (read-only). GITHUB_TOKEN is read from the environment and is
never printed, logged, or written to disk.

Usage:
  GITHUB_TOKEN=… .venv/bin/python gist_push.py
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
import urllib.error
import urllib.request
from copy import deepcopy
from pathlib import Path
from typing import Any

import dashboard_data

ROOT = Path(__file__).resolve().parent
LOG_PATH = ROOT / "logs" / "gist_push.log"
CONFIG_PATH = ROOT / "gist_config.json"
FILENAME = "fomo-desk.json"
LIVE_FILENAME = "fomo-live.json"  # small real-time file (agents, marks, newest events, ticker)
INTERVAL_S = 45  # big history file cadence
# GitHub limits gist PATCH separately (x-ratelimit-resource=gist_update, 100 per window, measured 13.11.2026).
# So: one PATCH carries both files; a steady push every LIVE_INTERVAL_S, plus an early push as soon as a
# real trade event appears (buy/sell/stop) — paced so the window budget is never exhausted.
LIVE_INTERVAL_S = float(os.environ.get("GIST_LIVE_INTERVAL_S") or 40)
EVENT_MIN_GAP_S = 12.0
TICK_S = 3.0
BUDGET_RESERVE = 12
FRESHNESS_PUSH_S = 90
API = "https://api.github.com"

SECRETISH = tuple(dashboard_data.SECRETISH) + (
    "owner_chat_id",
    "owner_chat",
    "chat_id",
    "authorization",
    "ghp_",
    "github_token",
)

log = logging.getLogger("gist_push")


def setup_log() -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    handler = logging.FileHandler(LOG_PATH, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    log.setLevel(logging.INFO)
    log.handlers.clear()
    log.addHandler(handler)
    log.propagate = False


def load_gist_id() -> str:
    env = (os.environ.get("GIST_ID") or "").strip()
    if env:
        return env
    if CONFIG_PATH.exists():
        cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        gid = str(cfg.get("gist_id") or cfg.get("GIST_ID") or "").strip()
        if gid:
            return gid
    boot_path = ROOT / "settings.bootstrap.json"
    if boot_path.exists():
        boot = json.loads(boot_path.read_text(encoding="utf-8"))
        gid = str(boot.get("GIST_ID") or "").strip()
        if gid:
            return gid
    raise SystemExit("GIST_ID missing (set env GIST_ID or gist_config.json)")


def token() -> str:
    # GIST_TOKEN is the Actions secret (a PAT). Do not overwrite GITHUB_TOKEN in
    # Actions — that auto token is used to dispatch the next workflow run.
    t = (
        os.environ.get("GIST_TOKEN")
        or os.environ.get("GITHUB_TOKEN")
        or os.environ.get("GH_TOKEN")
        or ""
    ).strip()
    if not t:
        raise SystemExit("GIST_TOKEN or GITHUB_TOKEN is not set")
    return t


def is_secret_key(key: str) -> bool:
    k = (key or "").lower()
    return any(s in k for s in SECRETISH)


def looks_like_secret(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    v = value.strip()
    if v.startswith(("ghp_", "github_pat_", "gho_", "ghu_", "ghs_")):
        return True
    if len(v) > 20 and v.lower().startswith("bot"):
        # Telegram bot tokens look like 123:AA...
        if ":" in v and v.split(":", 1)[0].isdigit():
            return True
    return False


def sanitize(obj: Any) -> Any:
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if is_secret_key(str(k)) or looks_like_secret(v):
                continue
            out[k] = sanitize(v)
        return out
    if isinstance(obj, list):
        return [sanitize(x) for x in obj]
    return obj


def canonical(data: dict[str, Any]) -> str:
    """Compare payload ignoring freshness timestamps so we can skip no-op pushes."""
    d = deepcopy(data)
    for k in ("generated_at", "generated_at_sofia"):
        d.pop(k, None)
    pipe = d.get("pipeline")
    if isinstance(pipe, dict):
        pipe.pop("window_end", None)
    return json.dumps(d, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def api_request(method: str, path: str, body: dict[str, Any] | None = None) -> tuple[int, dict[str, str], Any]:
    data = None if body is None else json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        API + path,
        data=data,
        method=method,
        headers={
            "Authorization": "Bearer " + token(),
            "Accept": "application/vnd.github+json",
            "User-Agent": "fomo-desk-gist-push",
            "X-GitHub-Api-Version": "2022-11-28",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read()
            payload = json.loads(raw.decode("utf-8")) if raw else {}
            headers = {k.lower(): v for k, v in resp.headers.items()}
            return resp.status, headers, payload
    except urllib.error.HTTPError as e:
        raw = e.read() if e.fp else b""
        try:
            payload = json.loads(raw.decode("utf-8")) if raw else {}
        except json.JSONDecodeError:
            payload = {"message": "http-error"}
        # Never log response body — might echo request debug. Just status + github message key.
        headers = {k.lower(): v for k, v in e.headers.items()} if e.headers else {}
        return e.code, headers, payload if isinstance(payload, dict) else {"message": "http-error"}


def retry_after(headers: dict[str, str], fallback: float) -> float:
    raw = headers.get("retry-after")
    if raw:
        try:
            return max(fallback, float(raw))
        except ValueError:
            pass
    reset = headers.get("x-ratelimit-reset")
    if reset:
        try:
            wait = int(reset) - int(time.time())
            if wait > 0:
                return min(max(wait, fallback), 900)
        except ValueError:
            pass
    return fallback


def push(gist_id: str, text: str | dict[str, str]) -> tuple[bool, float | None]:
    files = {FILENAME: text} if isinstance(text, str) else text
    status, headers, payload = api_request(
        "PATCH",
        f"/gists/{gist_id}",
        {"files": {name: {"content": body} for name, body in files.items()}},
    )
    RATE.update({k: headers.get(k) for k in ("x-ratelimit-remaining", "x-ratelimit-used", "x-ratelimit-limit",
                                             "x-ratelimit-resource", "x-ratelimit-reset")})
    if status in (200, 201):
        return True, None
    msg = ""
    if isinstance(payload, dict):
        msg = str(payload.get("message") or "")[:120]
    log.warning("gist PATCH failed status=%s message=%s", status, msg)
    if status in (403, 429):
        return False, retry_after(headers, 60.0)
    if status in (500, 502, 503, 504):
        return False, 30.0
    return False, 20.0


RATE: dict[str, Any] = {}


def event_mark(conn) -> tuple:
    """Changes whenever a real trade event happens (buy / sell / stop / lock / partial)."""
    a = conn.execute("SELECT MAX(id) FROM activity WHERE kind IN ('bought','sold','stopped','partial','locked')").fetchone()
    p = conn.execute("SELECT COUNT(*), MAX(closed_at) FROM positions").fetchone()
    return (a[0] if a else None, tuple(p) if p else None)


def paced_interval(now_epoch: float) -> float:
    """Steady interval that fits the remaining gist_update budget until its reset."""
    try:
        remaining = int(RATE.get("x-ratelimit-remaining"))
        reset = int(RATE.get("x-ratelimit-reset"))
    except (TypeError, ValueError):
        return LIVE_INTERVAL_S
    left = max(1.0, reset - now_epoch)
    usable = remaining - BUDGET_RESERVE
    if usable <= 0:
        return max(LIVE_INTERVAL_S, left + 5)
    return max(LIVE_INTERVAL_S, left / usable)


def loop() -> int:
    setup_log()
    gist_id = load_gist_id()
    log.info("gist_push started gist_id=%s steady=%ss event_gap=%ss", gist_id, LIVE_INTERVAL_S, EVENT_MIN_GAP_S)
    last_canonical = None
    last_big_mono = 0.0
    last_push_mono = 0.0
    last_mark = None
    backoff = 0.0
    n_push = 0
    while True:
        t0 = time.monotonic()
        try:
            conn = dashboard_data.connect_ro()
            try:
                mark = event_mark(conn)
                since = t0 - last_push_mono
                steady = paced_interval(time.time())
                try:
                    budget_ok = int(RATE.get("x-ratelimit-remaining")) > 2 * BUDGET_RESERVE
                except (TypeError, ValueError):
                    budget_ok = True
                event = last_mark is not None and mark != last_mark and since >= EVENT_MIN_GAP_S and budget_ok
                due = last_push_mono == 0 or since >= steady or event
                files: dict[str, str] = {}
                if due:
                    live = sanitize(dashboard_data.live_collect(conn))
                    files[LIVE_FILENAME] = json.dumps(live, ensure_ascii=False, separators=(",", ":")) + "\n"
                    data = sanitize(dashboard_data.collect(conn))
                    can = canonical(data)
                    if can != last_canonical or (t0 - last_big_mono) >= FRESHNESS_PUSH_S:
                        files[FILENAME] = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
            finally:
                conn.close()
            if files:
                ok, wait = push(gist_id, files)
                if ok:
                    n_push += 1
                    last_push_mono = time.monotonic()
                    last_mark = mark
                    if FILENAME in files:
                        last_canonical = can
                        last_big_mono = last_push_mono
                    if event or n_push % 20 == 0 or n_push <= 2:
                        log.info("gist updated (%s, live %s B%s) budget %s/%s left, reset in %ss, steady %.0fs",
                                 "event" if event else "steady", len(files[LIVE_FILENAME].encode("utf-8")),
                                 f", big {len(files[FILENAME].encode('utf-8'))} B" if FILENAME in files else "",
                                 RATE.get("x-ratelimit-remaining"), RATE.get("x-ratelimit-limit"),
                                 (int(RATE["x-ratelimit-reset"]) - int(time.time())) if RATE.get("x-ratelimit-reset") else "?",
                                 paced_interval(time.time()))
                    backoff = 0.0
                elif wait:
                    backoff = wait
        except Exception as exc:  # noqa: BLE001 — keep the loop alive
            # Do not include exception args that might echo an Authorization header.
            log.exception("cycle failed: %s", type(exc).__name__)
            backoff = min(max(backoff * 2, 20.0), 300.0)

        sleep_for = backoff if backoff > 0 else max(0.5, TICK_S - (time.monotonic() - t0))
        time.sleep(sleep_for)
        if backoff > 0:
            log.info("backoff over, resuming (slept %.0fs)", sleep_for)
            backoff = 0.0
    return 0


if __name__ == "__main__":
    raise SystemExit(loop())

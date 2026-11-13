"""Shared helpers: paths, secrets, logging, numbers, time."""

from __future__ import annotations

import logging
import os
import re
from datetime import datetime, timezone
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
LOG_DIR = ROOT / "logs"
SECRETS_PATH = ROOT / ".secrets"

_SECRET_KEYS = {
    "TYPESAFE_API_KEY",
    "TG_TOKEN",
    "VAPID_PRIVATE_KEY",
    "VAPID_PRIVATE",
    "APP_TOKEN",
    "JUPITER_API_KEY",
}

_loaded_secrets = False
_loggers: dict[str, logging.Logger] = {}


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso_utc(ts: datetime | None = None) -> str:
    t = ts or utcnow()
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    return t.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def as_float(value: Any, default: float | None = None) -> float | None:
    if value is None or value == "":
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def as_int(value: Any, default: int | None = None) -> int | None:
    f = as_float(value, None)
    if f is None:
        return default
    return int(f)


def clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def load_secrets(path: Path | None = None) -> dict[str, str]:
    """Load KEY=VALUE from .secrets into os.environ if the key is not already set.

    Values are never logged or printed.
    """
    global _loaded_secrets
    out: dict[str, str] = {}
    p = path or SECRETS_PATH
    if p.exists():
        for raw in p.read_text().splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or line.startswith("-----") or "=" not in line:
                continue
            key, val = line.split("=", 1)
            key = key.strip()
            val = val.strip().strip("'").strip('"')
            out[key] = val
            if key not in os.environ:
                os.environ[key] = val
    for key in ("TYPESAFE_API_KEY", "TG_TOKEN", "APP_TOKEN", "VAPID_PRIVATE_KEY", "VAPID_PUBLIC_KEY", "JUPITER_API_KEY"):
        if os.environ.get(key):
            out.setdefault(key, os.environ[key])
    _loaded_secrets = True
    return out


def secret(name: str, default: str | None = None) -> str | None:
    load_secrets()
    return os.environ.get(name, default)


def redact(text: str) -> str:
    """Strip known secret values from a string (for logs)."""
    load_secrets()
    out = text
    for key in _SECRET_KEYS:
        val = os.environ.get(key)
        if val and len(val) >= 8:
            out = out.replace(val, f"[{key}]")
    return out


def agent_logger(name: str) -> logging.Logger:
    """Daily-rotated logger writing one line-oriented file under logs/."""
    if name in _loggers:
        return _loggers[name]
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log = logging.getLogger(f"desk.{name}")
    log.setLevel(logging.INFO)
    log.propagate = False
    if not any(isinstance(h, TimedRotatingFileHandler) for h in log.handlers):
        path = LOG_DIR / f"{name}.log"
        handler = TimedRotatingFileHandler(
            path, when="midnight", interval=1, backupCount=30, utc=True, encoding="utf-8"
        )
        handler.suffix = "%Y-%m-%d"
        fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
        handler.setFormatter(fmt)
        log.addHandler(handler)
        stream = logging.StreamHandler()
        stream.setFormatter(fmt)
        # Don't echo secrets if a formatter string ever included one
        log.addHandler(stream)
    _loggers[name] = log
    return log


_EMOJI_RE = re.compile(
    "["
    "\U0001F300-\U0001F6FF"
    "\U0001F700-\U0001F77F"
    "\U0001F780-\U0001F7FF"
    "\U0001F800-\U0001F8FF"
    "\U0001F900-\U0001F9FF"
    "\U0001FA00-\U0001FAFF"
    "\U00002600-\U000026FF"
    "\U00002700-\U000027BF"
    "]+",
    flags=re.UNICODE,
)


def copycat_key(text: str | None) -> str:
    """Lowercase, strip spaces, digits and emoji — for Scanner copycat checks."""
    if not text:
        return ""
    s = _EMOJI_RE.sub("", str(text))
    s = s.lower()
    s = re.sub(r"[\s\d]+", "", s)
    return s


def round_cents(value: float) -> float:
    return round(float(value) + 0.0, 2)


# ---------------------------------------------------------------- trading night window
def night_window_start(ts: datetime | str | None, report_time: str = "08:00", zone: str = "Europe/Sofia") -> datetime | None:
    """Start (UTC) of the trading night that contains `ts`.

    A trading night runs from one report_time (Europe/Sofia) to the next. A process
    restart inside the same window continues the same night.
    """
    from datetime import timedelta
    from zoneinfo import ZoneInfo

    if ts is None:
        return None
    if isinstance(ts, str):
        try:
            ts = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except ValueError:
            return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    try:
        hh, mm = (int(x) for x in str(report_time or "08:00").split(":")[:2])
    except ValueError:
        hh, mm = 8, 0
    z = ZoneInfo(zone)
    local = ts.astimezone(z)
    boundary = local.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if local < boundary:
        boundary = (boundary - timedelta(days=1)).replace(hour=hh, minute=mm)
    return boundary.astimezone(timezone.utc)


def same_night(a: datetime | str | None, b: datetime | str | None, report_time: str = "08:00") -> bool:
    wa, wb = night_window_start(a, report_time), night_window_start(b, report_time)
    return wa is not None and wa == wb

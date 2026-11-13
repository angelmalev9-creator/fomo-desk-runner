"""Load settings and secrets for the trading desk (STEP 10)."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from util import ROOT, as_float, load_secrets

BOOTSTRAP_PATH = ROOT / "settings.bootstrap.json"

# Aliases accepted by the `set` / `stack` commands (and the Account tab later).
ALIASES = {
    "stop": "risk.stop_pct",
    "stop_pct": "risk.stop_pct",
    "trail": "risk.trail_pct",
    "trail_pct": "risk.trail_pct",
    "trail_start": "risk.trail_start_pct",
    "lock_trigger": "risk.lock_trigger_pct",
    "lock_floor": "risk.lock_floor_pct",
    "stack": "desk.live_stack",
    "live_stack": "desk.live_stack",
    "mode": "desk.mode",
    "paper_start_stack": "desk.paper_start_stack",
    "below_min": "executor.below_min",
    "stack_pct": "executor.stack_pct",
    "min_ticket": "executor.min_ticket_usd",
    "min_ticket_usd": "executor.min_ticket_usd",
    "rpc": "desk.rpc_url",
    "rpc_url": "desk.rpc_url",
    "analyst_mode": "analyst.mode",
    "fomo_handle": "desk.fomo_handle",
    "notifier": "desk.notifier",
    "hosting": "desk.hosting",
    "jup_layer": "auditor.jup_layer",
}


@dataclass(frozen=True)
class Setting:
    key: str
    default: Any
    kind: str  # int float percent ratio money bool str choice time
    min: float | None = None
    max: float | None = None
    choices: tuple[str, ...] | None = None
    group: str = ""


# Every STEP 10 setting, with a sane min/max (or a closed choice list).
SETTINGS: list[Setting] = [
    Setting("scanner.min_liquidity_usd", 40000, "money", 0, 10_000_000, group="scanner"),
    Setting("scanner.max_top10_pct", 25, "percent", 0, 100, group="scanner"),
    Setting("scanner.max_h1_change_pct", 80, "percent", 0, 10_000, group="scanner"),
    Setting("scanner.poll_s", 60, "int", 5, 3600, group="scanner"),
    Setting("scanner.min_pool_age_min", 10, "int", 0, 1440, group="scanner"),
    Setting("scanner.max_wait_min", 60, "int", 1, 1440, group="scanner"),
    Setting("scanner.runner_h24_pct", 300, "percent", 0, 100_000, group="scanner"),
    Setting("scanner.ws_silence_s", 120, "int", 10, 3600, group="scanner"),
    Setting("scanner.dex_429_wait_s", 30, "int", 1, 600, group="scanner"),
    Setting("auditor.enabled", True, "bool", group="auditor"),
    Setting("auditor.max_dev_pct", 5, "percent", 0, 100, group="auditor"),
    Setting("auditor.max_top10_pct", 25, "percent", 0, 100, group="auditor"),
    Setting("auditor.min_lp_locked_pct", 99, "percent", 0, 100, group="auditor"),
    Setting("auditor.min_pool_age_min", 10, "int", 0, 1440, group="auditor"),
    Setting("auditor.max_bundled", 5, "int", 1, 1000, group="auditor"),
    Setting("auditor.max_delegated_accounts", 10, "int", 1, 10_000, group="auditor"),
    Setting("auditor.retries", 3, "int", 0, 10, group="auditor"),
    Setting("auditor.retry_wait_s", 2, "int", 0, 30, group="auditor"),
    Setting("auditor.max_liq_drop_pct", 50, "percent", 0, 100, group="auditor"),
    Setting("auditor.jup_layer", True, "bool", group="auditor"),
    Setting("analyst.mode", "jev", "choice", choices=("jev", "rules"), group="analyst"),
    Setting("analyst.min_alive_6h", 0.70, "ratio", 0, 1, group="analyst"),
    Setting("analyst.min_organic", 0.65, "ratio", 0, 1, group="analyst"),
    Setting("analyst.max_chase", 0.30, "ratio", 0, 1, group="analyst"),
    Setting("analyst.min_score", 70, "int", 0, 100, group="analyst"),
    Setting("analyst.timeout_s", 10, "int", 1, 120, group="analyst"),
    Setting("analyst.retry_wait_s", 2, "int", 0, 30, group="analyst"),
    Setting("analyst.queue_429_s", 60, "int", 0, 600, group="analyst"),
    Setting("analyst.state_max_chars", 3000, "int", 200, 20_000, group="analyst"),
    Setting("analyst.alive_volume_h1", 25000, "money", 0, 10_000_000, group="analyst"),
    Setting("analyst.organic_buys_h1", 150, "int", 0, 100_000, group="analyst"),
    Setting("analyst.bundled_penalty", 0.5, "ratio", 0, 1, group="analyst"),
    Setting("analyst.bundled_penalty_from", 3, "int", 1, 1000, group="analyst"),
    Setting("analyst.flush_drop_m5_pct", 5, "percent", 0, 100, group="analyst"),
    Setting("analyst.chase_h1_pct", 80, "percent", 0, 10_000, group="analyst"),
    Setting("executor.stack_pct", 8, "percent", 0.01, 100, group="executor"),
    Setting("executor.max_pool_pct", 2, "percent", 0.01, 100, group="executor"),
    Setting("executor.min_ticket_usd", 47.50, "money", 1, 100_000, group="executor"),
    Setting("executor.below_min", "bump", "choice", choices=("bump", "skip"), group="executor"),
    Setting("executor.recheck_liq_drop_pct", 20, "percent", 0, 100, group="executor"),
    Setting("executor.recheck_price_up_pct", 15, "percent", 0, 1000, group="executor"),
    Setting("executor.confirm_window_min", 5, "int", 1, 60, group="executor"),
    Setting("risk.poll_s", 15, "int", 2, 600, group="risk"),
    Setting("risk.stop_pct", 6, "percent", 0.1, 90, group="risk"),
    Setting("risk.trail_start_pct", 20, "percent", 0, 10_000, group="risk"),
    Setting("risk.trail_pct", 12, "percent", 0.1, 90, group="risk"),
    Setting("risk.timeout_h", 3, "float", 0.01, 72, group="risk"),
    # Profit lock: once price reaches entry +lock_trigger_pct, the stop moves UP to entry +lock_floor_pct
    # (above the ~3.6% round-trip FOMO fee on a $20 ticket). 0 = off. Only ever tightens the stop.
    Setting("risk.lock_trigger_pct", 0, "percent", 0, 1000, group="risk"),
    Setting("risk.lock_floor_pct", 4.5, "percent", 0, 1000, group="risk"),
    Setting("risk.timeout_min_gain_pct", 5, "percent", 0, 1000, group="risk"),
    Setting("risk.confirm_window_min", 2, "int", 1, 60, group="risk"),
    Setting("risk.no_price_polls", 4, "int", 1, 60, group="risk"),
    Setting("risk.dex_429_wait_s", 30, "int", 1, 600, group="risk"),
    Setting("desk.mode", "PAPER", "choice", choices=("PAPER", "LIVE"), group="desk"),
    Setting("desk.paper_start_stack", 200, "money", 1, 10_000_000, group="desk"),
    Setting("desk.live_stack", 0, "money", 0, 10_000_000, group="desk"),
    Setting("desk.report_time", "08:00", "time", group="desk"),
    Setting("desk.notifier", "telegram", "str", group="desk"),
    Setting("desk.rpc_url", "https://api.mainnet-beta.solana.com", "str", group="desk"),
    Setting("desk.push_rejects", False, "bool", group="desk"),
    Setting("desk.fees_ref_discount", 0.10, "ratio", 0, 1, group="desk"),
    Setting("desk.fomo_handle", "", "str", group="desk"),
    Setting("desk.owner_chat_id", 0, "int", 0, 10**16, group="desk"),
    Setting("desk.hosting", "none", "str", group="desk"),
    Setting("desk.paused", "", "str", group="desk"),  # comma-separated agent names
]

SPEC: dict[str, Setting] = {s.key: s for s in SETTINGS}


def _parse_bool(raw: Any) -> bool:
    if isinstance(raw, bool):
        return raw
    s = str(raw).strip().lower()
    if s in ("1", "true", "yes", "on"):
        return True
    if s in ("0", "false", "no", "off"):
        return False
    raise ValueError("must be true or false")


def _parse_time(raw: Any) -> str:
    s = str(raw).strip()
    if not re.fullmatch(r"[0-2]?\d:[0-5]\d", s):
        raise ValueError("must be HH:MM")
    hh, mm = s.split(":")
    h = int(hh)
    if h > 23:
        raise ValueError("must be HH:MM (00:00-23:59)")
    return f"{h:02d}:{mm}"


def coerce(setting: Setting, raw: Any) -> Any:
    if setting.kind == "bool":
        return _parse_bool(raw)
    if setting.kind == "time":
        return _parse_time(raw)
    if setting.kind == "choice":
        val = str(raw).strip()
        if setting.key == "desk.mode":
            val = val.upper()
        else:
            val = val.lower() if setting.key != "desk.mode" else val
        # below_min and analyst.mode are lowercase; mode is uppercase
        if setting.key.endswith("mode") and setting.key != "analyst.mode":
            val = str(raw).strip().upper()
        allowed = setting.choices or ()
        if val not in allowed:
            # try case-insensitive
            match = next((c for c in allowed if c.lower() == str(raw).strip().lower()), None)
            if match is None:
                raise ValueError(f"must be one of {', '.join(allowed)}")
            val = match
        return val
    if setting.kind == "str":
        return str(raw).strip()
    if setting.kind == "int":
        num = int(float(str(raw).strip().replace("%", "").replace("$", "").replace(",", "")))
    else:
        num = float(str(raw).strip().replace("%", "").replace("$", "").replace(",", ""))
    if setting.min is not None and num < setting.min:
        raise ValueError(_range_msg(setting))
    if setting.max is not None and num > setting.max:
        raise ValueError(_range_msg(setting))
    if setting.kind == "int":
        return int(num)
    return float(num)


def _range_msg(setting: Setting) -> str:
    if setting.min is not None and setting.max is not None:
        return f"must be between {setting.min} and {setting.max}"
    if setting.min is not None:
        return f"must be >= {setting.min}"
    if setting.max is not None:
        return f"must be <= {setting.max}"
    return "out of range"


def resolve_key(name: str) -> str:
    raw = (name or "").strip()
    if raw in SPEC:
        return raw
    if raw in ALIASES:
        return ALIASES[raw]
    lower = raw.lower()
    if lower in ALIASES:
        return ALIASES[lower]
    # allow "risk.stop_pct" already, or "stop_pct" etc.
    for key in SPEC:
        if key.endswith("." + raw) or key.split(".")[-1] == raw:
            return key
    raise KeyError(f"unknown setting {name!r}")


def dump(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def load_value(setting: Setting, raw: str | None) -> Any:
    if raw is None:
        return setting.default
    return coerce(setting, raw)



def _normalize_bootstrap(data: dict[str, Any]) -> dict[str, Any]:
    mapping = {
        "fomo_handle": "desk.fomo_handle",
        "analyst_mode": "analyst.mode",
        "owner_chat_id": "desk.owner_chat_id",
        "notifier": "desk.notifier",
        "hosting": "desk.hosting",
        "rpc_url": "desk.rpc_url",
    }
    out: dict[str, Any] = {}
    for k, v in (data or {}).items():
        out[mapping.get(k, k)] = v
    return out


def load_bootstrap(path: Path | None = None) -> dict[str, Any]:
    p = path or BOOTSTRAP_PATH
    if not p.exists():
        return {}
    data = json.loads(p.read_text())
    return _normalize_bootstrap(data)


class Config:
    def __init__(self, db=None, bootstrap: dict[str, Any] | None = None) -> None:
        load_secrets()
        self.db = db
        self._cache: dict[str, Any] = {s.key: s.default for s in SETTINGS}
        boot = bootstrap if bootstrap is not None else load_bootstrap()
        boot = _normalize_bootstrap(boot)
        for k, v in boot.items():
            if k in SPEC:
                try:
                    self._cache[k] = coerce(SPEC[k], v)
                except ValueError:
                    self._cache[k] = v
        if self.db is not None:
            stored = self.db.all_settings()
            for k, raw in stored.items():
                if k in SPEC:
                    try:
                        self._cache[k] = load_value(SPEC[k], raw)
                    except ValueError:
                        pass

    def get(self, key: str, default: Any = None) -> Any:
        key = key if key in SPEC else (ALIASES.get(key) or key)
        if key in self._cache:
            return self._cache[key]
        return default

    def __getitem__(self, key: str) -> Any:
        return self.get(key)

    def as_dict(self) -> dict[str, Any]:
        return dict(self._cache)

    def set(self, name: str, raw: Any, who: str = "user") -> tuple[str, Any, Any]:
        """Validate and store a setting. Returns (key, old, new). Raises ValueError/KeyError."""
        key = resolve_key(name)
        setting = SPEC[key]
        # Special-case "set stop to -8%": the stored value is the positive 8.
        if setting.kind in ("percent", "ratio", "money", "float", "int") and isinstance(raw, str):
            s = raw.strip()
            if s.startswith("-") and setting.key in (
                "risk.stop_pct",
                "risk.trail_pct",
                "risk.trail_start_pct",
            ):
                raw = s[1:]
        new = coerce(setting, raw)
        if key == "desk.mode" and str(new).upper() == "LIVE":
            # Hard LIVE gate: refuse even via `set mode LIVE`. Fail closed if we cannot score.
            try:
                import live_gate
                reason = live_gate.refuse_live(self.db, self) if self.db is not None else (
                    "LIVE needs at least 1 full PAPER night in the db first."
                )
            except Exception:
                reason = "LIVE е заключен. Не мога да проверя вратата. Оставаме на PAPER."
            if reason:
                raise ValueError(reason)
        old = self._cache.get(key)
        self._cache[key] = new
        if self.db is not None:
            self.db.set_setting(key, dump(new), who=who, old=dump(old) if old is not None else None)
        return key, old, new

    def is_paused(self, agent: str) -> bool:
        paused = str(self.get("desk.paused") or "")
        names = {p.strip().lower() for p in paused.split(",") if p.strip()}
        return agent.lower() in names

    def pause(self, agent: str, who: str = "user") -> None:
        paused = str(self.get("desk.paused") or "")
        names = [p.strip() for p in paused.split(",") if p.strip()]
        if agent not in names:
            names.append(agent)
        self.set("desk.paused", ",".join(names), who=who)

    def resume(self, agent: str, who: str = "user") -> None:
        paused = str(self.get("desk.paused") or "")
        names = [p.strip() for p in paused.split(",") if p.strip() and p.strip().lower() != agent.lower()]
        self.set("desk.paused", ",".join(names), who=who)

    def current_stack(self) -> float:
        mode = str(self.get("desk.mode") or "PAPER").upper()
        if mode == "LIVE":
            return float(self.get("desk.live_stack") or 0)
        # PAPER: last recorded balance, else the start stack
        if self.db is not None:
            last = self.db.last_balance()
            if last is not None:
                return float(last)
        return float(self.get("desk.paper_start_stack") or 0)

    def min_stack(self) -> float:
        ticket = float(self.get("executor.min_ticket_usd"))
        pct = float(self.get("executor.stack_pct"))
        if pct <= 0:
            return float("inf")
        return ticket / (pct / 100.0)

    def ticket_check(self, stack: float | None = None) -> dict[str, Any]:
        """Run the STEP 10 ticket check. Never starts an empty night silently."""
        if stack is None:
            # At night start PAPER uses paper_start_stack (not yet marked-to-market).
            mode = str(self.get("desk.mode") or "PAPER").upper()
            if mode == "LIVE":
                stack = float(self.get("desk.live_stack") or 0)
            else:
                stack = float(self.get("desk.paper_start_stack") or 0)
        ticket = float(self.get("executor.min_ticket_usd"))
        pct = float(self.get("executor.stack_pct"))
        below = str(self.get("executor.below_min") or "bump").lower()
        min_stack = self.min_stack()
        result = {
            "ok": True,
            "stack": float(stack),
            "min_stack": min_stack,
            "min_ticket_usd": ticket,
            "stack_pct": pct,
            "below_min": below,
            "message": None,
            "warn": False,
            "block": False,
        }
        if stack + 1e-9 >= min_stack:
            return result
        p = (ticket / stack) * 100 if stack else 0.0
        if below == "bump":
            result["message"] = f"tickets bumped to ${ticket:.2f} = {p:.4g}% of stack"
            result["warn"] = True
            return result
        result["ok"] = False
        result["warn"] = True
        result["block"] = True
        result["message"] = (
            f"every coin will be skipped, stack under ${min_stack:.2f}"
        )
        return result


# Process-wide config; tests construct their own.
cfg = Config()

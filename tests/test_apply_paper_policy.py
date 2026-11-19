"""ci.apply_paper_policy: writes the approved PAPER opportunity settings, nothing else."""

from __future__ import annotations

import json
import sqlite3

import ci.apply_paper_policy as policy
from agents import strategies as S


def _kv(db, key):
    raw = db.kv_get("setting." + key)
    return None if raw is None else json.loads(raw)


def _seed_live_like(db, cfg):
    """Reproduce the 2026-11-19 gist kv (size dead zone, Momentum off, Quiet at old defaults)."""
    db.kv_set("setting.agents.max_position_pct", json.dumps(40.0))
    db.kv_set("setting.agents.sleeve_pct", json.dumps(100.0))
    db.kv_set("setting.copy.enabled", json.dumps(False))
    db.kv_set("setting.copy.only_winnable", json.dumps(True))
    db.kv_set("setting.copy.size_usd", json.dumps(20.0))
    db.kv_set("setting.copy.stop_basis", json.dumps("market"))
    db.kv_set("setting.dip.enabled", json.dumps(False))
    db.kv_set("setting.live.min_closed_trades", json.dumps(20))
    db.kv_set("setting.live.min_win_rate_pct", json.dumps(85.0))
    db.kv_set("setting.momentum.enabled", json.dumps(False))
    db.kv_set("setting.risk.stop_basis", json.dumps("market"))
    cfg.set("risk.stop_pct", "1", who="test")
    cfg.set("desk.paused", "momentum,dip", who="test")


def test_apply_on_live_like_db(db, cfg, tmp_path):
    _seed_live_like(db, cfg)
    # copy the real desk schema by using db.path
    changes = policy.apply(db.path)
    by = {line.split()[0]: line for line in changes}
    assert "agents.max_position_pct" in by
    assert _kv(db, "agents.max_position_pct") == 45.0
    assert _kv(db, "momentum.enabled") is True
    assert _kv(db, "quiet.enabled") is True
    assert _kv(db, "quiet.max_noise_pct") == 1.5
    assert _kv(db, "quiet.vol_x") == 1.5
    assert _kv(db, "quiet.min_vol30_usd") == 3750.0
    assert _kv(db, "quiet.min_age_min") == 22
    assert _kv(db, "quiet.max_per_hour") == 3
    assert _kv(db, "quiet.max_chase_pct") == 4.0
    assert _kv(db, "agents.min_liquidity_usd") == 6000.0
    # protected
    assert _kv(db, "agents.sleeve_pct") == 100.0
    assert _kv(db, "copy.enabled") is False
    assert _kv(db, "copy.only_winnable") is True
    assert _kv(db, "dip.enabled") is False
    assert _kv(db, "live.min_closed_trades") == 20
    assert _kv(db, "live.min_win_rate_pct") == 85.0
    assert _kv(db, "risk.stop_basis") == "market"
    assert db.get_setting("risk.stop_pct") in ("1", "1.0")
    # only momentum was unpaused; dip stays paused (read the db, not a stale Config cache)
    paused = (db.get_setting("desk.paused") or "")
    assert "momentum" not in paused.lower()
    assert "dip" in paused.lower()
    # setting() now sees the new values
    assert S.setting(cfg, "agents.max_position_pct") == 45.0
    assert S.setting(cfg, "momentum.enabled") is True
    assert S.position_size(117.66, cfg) == 52.94
    # idempotent
    again = policy.apply(db.path)
    assert again == []
    assert _kv(db, "agents.max_position_pct") == 45.0
    assert json.loads(db.kv_get(policy.POLICY_KEY)) == policy.POLICY_ID


def test_apply_refuses_protected_keys():
    for key in ("risk.stop_pct", "copy.enabled", "dip.enabled", "live.min_win_rate_pct",
                "agents.sleeve_pct", "copy.only_winnable"):
        assert any(key == p or key.startswith(p) for p in policy.PROTECTED_PREFIXES)


def test_apply_missing_db_is_noop(tmp_path):
    assert policy.apply(tmp_path / "nope.db") == []


def test_wanted_matches_code_defaults():
    for key, val in policy.WANTED.items():
        assert S.DEFAULTS[key] == val

"""Tests for Risk exit rules: stop, trail, timeout, and stop tightening."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from agents.risk import (
    apply_stop_setting,
    close_paper,
    poll_position,
    push_and_activity,
    stop_pct_of,
)
from util import iso_utc

NOW = datetime(2026, 11, 10, 12, 0, tzinfo=timezone.utc)


def _pos(entry=1.0, size=100.0, peak=None, stop_pct=6.0, trail=False, opened=None, name="ABC"):
    stop = entry * (1 - stop_pct / 100.0)
    return {
        "id": 1,
        "mint": "MintRisk1",
        "name": name,
        "entry_price": entry,
        "size_usd": size,
        "peak_price": peak if peak is not None else entry,
        "stop_price": stop,
        "trail_active": trail,
        "opened_at": iso_utc(opened or NOW),
        "mode": "PAPER",
        "status": "open",
    }


def test_trail_after_plus_25_then_13_off_top(cfg):
    pos = _pos(entry=1.0)
    d1 = poll_position(pos, 1.25, NOW + timedelta(minutes=5), cfg)
    assert d1.action == "hold"
    assert d1.trail_active is True  # +25% >= +20%
    assert abs(d1.peak - 1.25) < 1e-12
    pos2 = {**pos, "peak_price": d1.peak, "trail_active": d1.trail_active}
    # 13% off the top: 1.25 * 0.87 = 1.0875; trail line is 1.25 * (1-0.12) = 1.10
    d2 = poll_position(pos2, 1.25 * 0.87, NOW + timedelta(minutes=6), cfg)
    assert d2.action == "exit"
    assert d2.reason == "trail"


def test_stop_at_minus_6_1(cfg):
    pos = _pos(entry=1.0, stop_pct=6)
    d = poll_position(pos, 0.939, NOW + timedelta(minutes=1), cfg)
    assert d.action == "exit"
    assert d.reason == "stop"


def test_timeout_3h_at_plus_4(cfg):
    pos = _pos(entry=1.0)
    d = poll_position(pos, 1.04, NOW + timedelta(hours=3), cfg)
    assert d.action == "exit"
    assert d.reason == "timeout"
    d2 = poll_position(pos, 1.04, NOW + timedelta(hours=2, minutes=59), cfg)
    assert d2.action == "hold"


def test_loosening_stop_never_moves_open_stop_down():
    pos = _pos(entry=1.0, stop_pct=6)  # stop price 0.94
    # 4% would be a looser stop (stop price 0.96? Wait: stop_pct 4 → price 0.96,
    # which is HIGHER, i.e. closer to entry, i.e. tighter). The looser setting
    # is a larger percent: 8% → stop price 0.92, lower than 0.94.
    looser = apply_stop_setting(pos, 8)
    assert looser["stop_price"] == pos["stop_price"]
    tighter = apply_stop_setting(pos, 4)
    assert tighter["stop_price"] > pos["stop_price"]
    assert abs(tighter["stop_price"] - 0.96) < 1e-12


def test_set_stop_8_applies_to_new_position_not_old(cfg, db):
    """New PAPER position stops at -8%; an older -6% position keeps -6%.

    Push / activity text for the new one reads '-8%'.
    """
    cfg.set("risk.stop_pct", 6, who="test")
    old = _pos(entry=1.0, stop_pct=6, name="OLD")
    # Simulate the setting change and tightening rule (8% is looser → keep 6).
    cfg.set("risk.stop_pct", 8, who="test")
    kept = apply_stop_setting(old, 8)
    assert abs(stop_pct_of(kept) - 6) < 1e-9

    new = _pos(entry=1.0, stop_pct=float(cfg.get("risk.stop_pct")), name="NEW")
    assert abs(stop_pct_of(new) - 8) < 1e-9

    # Drop 8.1% from entry: old (stop 6%) already would have stopped, but we
    # only assert the new position's stop fires at -8.1% and the copy reads -8%.
    d = poll_position(new, 0.919, NOW + timedelta(minutes=1), cfg)
    assert d.action == "exit"
    assert d.reason == "stop"
    close = close_paper(new, d.exit_price, d.reason, cfg)
    payload = push_and_activity(new, d, close, cfg)
    assert "-8%" in payload["text"]
    assert "-8%" in payload["subtitle"]
    # Old position still carries -6%
    d_old = poll_position(kept, 0.939, NOW + timedelta(minutes=1), cfg)
    assert d_old.reason == "stop"
    close_old = close_paper(kept, d_old.exit_price, d_old.reason, cfg)
    payload_old = push_and_activity(kept, d_old, close_old, cfg)
    assert "-6%" in payload_old["text"]
    assert "-6%" in payload_old["subtitle"]
    assert "-8%" not in payload_old["subtitle"]


# ------------------------------------------------------------ profit lock
def test_profit_lock_off_by_default_keeps_old_stop(cfg):
    pos = _pos(entry=1.0, stop_pct=1, peak=1.097)  # Pussycoin: peaked +9.7%, then fell
    d = poll_position(pos, 0.985, NOW + timedelta(minutes=30), cfg)
    assert d.action == "exit" and d.reason == "stop"


def test_profit_lock_moves_stop_above_fees_after_trigger(cfg):
    cfg.set("risk.lock_trigger_pct", "8", who="test")
    cfg.set("risk.lock_floor_pct", "4.5", who="test")
    pos = _pos(entry=1.0, stop_pct=1)
    d1 = poll_position(pos, 1.07, NOW + timedelta(minutes=1), cfg)
    assert d1.action == "hold" and abs(d1.stop_line - 0.99) < 1e-12  # +7%: not armed yet
    d2 = poll_position({**pos, "peak_price": d1.peak}, 1.097, NOW + timedelta(minutes=2), cfg)
    assert d2.action == "hold" and abs(d2.stop_line - 1.045) < 1e-12  # armed at +9.7%
    d3 = poll_position({**pos, "peak_price": d2.peak, "stop_price": d2.stop_line}, 1.04,
                       NOW + timedelta(minutes=3), cfg)
    assert d3.action == "exit" and d3.reason == "lock"
    close = close_paper({**pos, "size_usd": 20.0}, 1.04, "lock", cfg)
    assert close["pnl_usd"] > 0  # +4% gross on $20 beats the ~$0.72 round-trip fee
    payload = push_and_activity({**pos, "size_usd": 20.0}, d3, close, cfg)
    assert "profit lock" in payload["text"] and payload["kind"] == "sold"


def test_profit_lock_never_lowers_stop_or_triggers_on_bad_config(cfg):
    cfg.set("risk.lock_trigger_pct", "8", who="test")
    cfg.set("risk.lock_floor_pct", "9", who="test")  # floor >= trigger: ignored
    pos = _pos(entry=1.0, stop_pct=1, peak=1.2)
    d = poll_position(pos, 1.15, NOW + timedelta(minutes=1), cfg)
    assert abs(d.stop_line - 0.99) < 1e-12


def test_profit_lock_persists_raised_stop_in_db(cfg, db):
    import asyncio
    from agents import risk as risk_mod

    cfg.set("risk.lock_trigger_pct", "8", who="test")
    cfg.set("risk.lock_floor_pct", "4.5", who="test")
    db.add_balance(200.0)
    pid = db.open_position(mint="MintLock1", entry_price=1.0, size_usd=20.0, peak_price=1.0,
                           stop_price=0.99, mode="PAPER", status="open", name="L", source="copy")

    class Src:
        price = 1.10

        def pumpswap_pairs(self, mints):
            return {m: {"priceUsd": str(self.price), "liquidity": {"usd": 50000}} for m in mints}

    class Bus:
        async def emit(self, *a, **k):
            return None

    src = Src()
    asyncio.run(risk_mod._poll_once(db, cfg, Bus(), None, src, {}, {}, set()))
    pos = db.open_positions()[0]
    assert abs(pos["stop_price"] - 1.045) < 1e-9
    src.price = 1.044
    asyncio.run(risk_mod._poll_once(db, cfg, Bus(), None, src, {}, {}, set()))
    closed = db.query_one("SELECT * FROM positions WHERE id=?", (pid,))
    assert closed["status"] == "closed" and closed["exit_reason"] == "lock" and closed["pnl_usd"] > 0


# ------------------------------------------------------------ stop basis (market vs fill)
def test_stop_basis_default_fill_and_kv_market(cfg, db):
    from agents.risk import stop_basis_of, stop_price_at_entry
    import json

    assert stop_basis_of(cfg) == "fill"
    # 1.5% PAPER pad: fill-basis 1% stop sits 0.485% above the market
    assert stop_price_at_entry(1.0, 1.015, 1.0, cfg) == pytest.approx(1.015 * 0.99)
    db.kv_set("setting.risk.stop_basis", json.dumps("market"))
    assert stop_basis_of(cfg) == "market"
    assert stop_price_at_entry(1.0, 1.015, 1.0, cfg) == pytest.approx(1.0 * 0.99)
    # stop distance stays 1%, never wider
    assert abs((1.0 - stop_price_at_entry(1.0, 1.015, 1.0, cfg) / 1.0) * 100 - 1.0) < 1e-12


def test_copy_stop_basis_kv_is_enough_without_risk_key(cfg, db):
    from agents.risk import stop_basis_of, stop_price_at_entry
    import json

    db.kv_set("setting.copy.stop_basis", json.dumps("market"))
    assert stop_basis_of(cfg) == "market"
    assert stop_price_at_entry(1.0, 1.015, 1.0, cfg) == pytest.approx(0.99)


def test_rebase_open_stops_to_market_lowers_fill_copy_keeps_lock(cfg, db):
    from agents.risk import rebase_open_stops_to_market
    import json

    cfg.set("risk.stop_pct", "1", who="test")
    db.kv_set("setting.copy.slippage_pct", json.dumps(1.5))
    # fill-basis copy: entry padded 1.5%, stop 1% under the fill (above market)
    pid = db.open_position(mint="COPY1", entry_price=1.015, size_usd=20.0, peak_price=1.015,
                           stop_price=1.015 * 0.99, mode="PAPER", status="open", name="C", source="copy")
    # momentum already at market
    pid2 = db.open_position(mint="MOMO1", entry_price=1.0, size_usd=20.0, peak_price=1.0,
                            stop_price=0.99, mode="PAPER", status="open", name="M", source="momentum")
    # profit-lock already raised: must not be lowered
    pid3 = db.open_position(mint="LOCK1", entry_price=1.015, size_usd=20.0, peak_price=1.10,
                            stop_price=1.045, mode="PAPER", status="open", name="L", source="copy")
    n = rebase_open_stops_to_market(db, cfg)
    assert n == 1
    assert db.get_position(pid)["stop_price"] == pytest.approx(1.0 * 0.99)
    assert db.get_position(pid2)["stop_price"] == pytest.approx(0.99)
    assert db.get_position(pid3)["stop_price"] == pytest.approx(1.045)

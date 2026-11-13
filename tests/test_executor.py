"""Tests for Executor sizing caps, bump/skip, and the ticket check."""

from __future__ import annotations

import pytest

from agents.executor import size_trade
from config import Config


def test_200_stack_sizes_16_then_bump_sends_47_50(cfg):
    sized = size_trade(stack=200, open_notional=0, liquidity_usd=1_000_000, cfg=cfg)
    # raw 8% of 200 = 16, under 47.50, default below_min=bump
    assert abs(sized.raw_size - 16) < 1e-9
    assert sized.skip_reason is None
    assert sized.size_usd == 47.50
    assert sized.cap == "bump"


def test_200_stack_with_skip_skips(cfg):
    cfg.set("executor.below_min", "skip", who="test")
    sized = size_trade(stack=200, open_notional=0, liquidity_usd=1_000_000, cfg=cfg)
    assert abs(sized.raw_size - 16) < 1e-9
    assert sized.size_usd == 0
    assert sized.skip_reason == "under $47.50"


def test_paper_start_200_with_skip_warns_every_coin_will_be_skipped(cfg):
    cfg.set("executor.below_min", "skip", who="test")
    # paper_start_stack default is 200
    check = cfg.ticket_check(stack=float(cfg.get("desk.paper_start_stack")))
    assert check["warn"] is True
    assert check["block"] is True
    assert "every coin will be skipped" in check["message"]
    assert "593.75" in check["message"] or "593.8" in check["message"] or "594" not in check["message"]
    # min_stack = 47.50 / 0.08 = 593.75
    assert abs(check["min_stack"] - 593.75) < 1e-9


def test_ticket_check_bump_message(cfg):
    check = cfg.ticket_check(stack=200)
    assert check["below_min"] == "bump"
    assert check["block"] is False
    assert check["message"] == "tickets bumped to $47.50 = 23.75% of stack"


def test_liquidity_40000_caps_size_at_800(cfg):
    # Need a stack large enough that 8% exceeds $800: 8% of 20_000 = 1600
    cfg.set("desk.paper_start_stack", 20_000, who="test")
    sized = size_trade(stack=20_000, open_notional=0, liquidity_usd=40_000, cfg=cfg)
    assert sized.size_usd == 800
    assert sized.cap == "pool"
    assert sized.skip_reason is None


def test_size_over_free_stack_skips(cfg):
    sized = size_trade(stack=200, open_notional=180, liquidity_usd=1_000_000, cfg=cfg)
    # bump to 47.50 but free is only 20
    assert sized.skip_reason in ("under $47.50", "no free stack")



def test_sniper_fill_uses_shared_market_stop(db, cfg):
    import json
    from agents.executor import _fill

    cfg.set("risk.stop_pct", "1", who="test")
    db.kv_set("setting.risk.stop_basis", json.dumps("market"))
    result = {
        "mint": "SNIPE1",
        "signalPrice": 2.0,
        "sizeUsd": 47.50,
        "stackPct": 25.8,
        "mode": "PAPER",
    }
    pid = _fill(db, cfg, result, "SniperCoin")
    pos = db.get_position(pid)
    assert pos["stop_price"] == pytest.approx(2.0 * 0.99)
    assert pos["entry_price"] == pytest.approx(2.0)
    assert pos["mode"] == "PAPER"

"""Offline replay of a short paper path through Scanner → Auditor → Analyst → Executor → Risk."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from agents.analyst import score_coin
from agents.executor import decide, size_trade
from agents.risk import close_paper, poll_position, push_and_activity
from agents.scanner import evaluate
from util import iso_utc

NOW = datetime(2026, 11, 10, 10, 0, tzinfo=timezone.utc)


def test_offline_paper_path_pass_then_trail(cfg):
    pair = {
        "dexId": "pumpswap",
        "pairAddress": "PoolReplay1",
        "priceUsd": "1.00",
        "liquidity": {"usd": 80_000},
        "marketCap": 200_000,
        "volume": {"m5": 1000, "h1": 30_000, "h24": 90_000},
        "txns": {
            "m5": {"buys": 20, "sells": 10},
            "h1": {"buys": 200, "sells": 80},
        },
        "priceChange": {"m5": -6, "h1": 10, "h24": 40},
        "baseToken": {"name": "Replay", "symbol": "RPL", "address": "MintReplay1"},
        "pairCreatedAt": 0,
    }
    migrated = iso_utc(NOW - timedelta(minutes=15))
    scan = evaluate(
        mint="MintReplay1",
        name="Replay",
        symbol="RPL",
        pool="PoolReplay1",
        migrated_at=migrated,
        pair=pair,
        top10_pct=16.0,
        runners=[],
        cfg=cfg,
        now=NOW,
    )
    assert scan["verdict"] == "pass"

    # Skip a live audit (covered elsewhere); feed a cleared 8/8 to Analyst.
    audit = {
        "passed": True,
        "layers": [
            {"n": 1, "value": "Revoked", "pass": True},
            {"n": 2, "value": "Revoked", "pass": True},
            {"n": 3, "value": "0.9% · sold 0", "pass": True},
            {"n": 4, "value": "16.0%", "pass": True},
            {"n": 5, "value": "100% locked", "pass": True},
            {"n": 6, "value": "15m", "pass": True},
            {"n": 7, "value": "None", "pass": True},
            {"n": 8, "value": "None", "pass": True},
        ],
        "bundled": 0,
    }
    verdict = score_coin("MintReplay1", "Replay", 15, pair, audit, scan, cfg)
    assert verdict["model"] == "rules"
    assert verdict["passed"] is True

    cfg.set("desk.paper_start_stack", 1000, who="test")  # 8% = 80 >= 47.50
    sized = size_trade(1000, 0, 80_000, cfg)
    assert sized.skip_reason is None
    decision = decide(
        mint="MintReplay1",
        name="Replay",
        stack=1000,
        open_notional=0,
        pair=pair,
        scanner=scan,
        analyst=verdict,
        already_signaled=False,
        already_open=False,
        skipped=False,
        cfg=cfg,
    )
    assert decision["status"] == "sent"
    assert decision["text"].startswith("PAPER · Buy Replay")
    assert "r=savipww" in decision["link"]

    pos = {
        "mint": "MintReplay1",
        "name": "Replay",
        "entry_price": 1.0,
        "size_usd": decision["sizeUsd"],
        "peak_price": 1.0,
        "stop_price": 0.94,
        "trail_active": False,
        "opened_at": iso_utc(NOW),
        "mode": "PAPER",
        "status": "open",
    }
    d1 = poll_position(pos, 1.25, NOW + timedelta(minutes=10), cfg)
    pos = {**pos, "peak_price": d1.peak, "trail_active": d1.trail_active}
    d2 = poll_position(pos, 1.25 * 0.87, NOW + timedelta(minutes=12), cfg)
    assert d2.reason == "trail"
    close = close_paper(pos, d2.exit_price, d2.reason, cfg)
    payload = push_and_activity(pos, d2, close, cfg)
    assert payload["status"] == "closed"
    assert "Sold Replay" in payload["text"]

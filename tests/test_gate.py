"""Tests for the Analyst score gate (rules and Jev answers)."""

from __future__ import annotations

from agents.analyst import activity_agent_label, apply_gate, rules_answers, score_coin


def test_alive_just_under_rejects():
    # others at threshold, flush high enough that score would pass
    out = apply_gate({"alive_6h": 0.69, "organic": 0.65, "flush": 1.0, "chase": 0.30})
    assert out["passed"] is False
    assert any(f.startswith("alive_6h 0.69") for f in out["failed"])


def test_organic_just_under_rejects():
    out = apply_gate({"alive_6h": 0.70, "organic": 0.64, "flush": 1.0, "chase": 0.30})
    assert out["passed"] is False
    assert any(f.startswith("organic 0.64") for f in out["failed"])


def test_chase_just_over_rejects():
    out = apply_gate({"alive_6h": 0.70, "organic": 0.65, "flush": 1.0, "chase": 0.31})
    assert out["passed"] is False
    assert any(f.startswith("chase 0.31") for f in out["failed"])


def test_score_69_rejects_even_if_three_thresholds_pass():
    # alive 0.70 + organic 0.65 + flush 0.71 + (1-0.30) = 2.76 / 4 = 0.69 → 69
    out = apply_gate({"alive_6h": 0.70, "organic": 0.65, "flush": 0.71, "chase": 0.30})
    assert out["score"] == 69
    assert out["passed"] is False
    assert any(f.startswith("score 69") for f in out["failed"])


def test_all_at_threshold_pass():
    # flush 0.75 → score 70 exactly; all three thresholds met
    out = apply_gate({"alive_6h": 0.70, "organic": 0.65, "flush": 0.75, "chase": 0.30})
    assert out["score"] == 70
    assert out["passed"] is True
    assert out["failed"] == []


def _pair(*, volume_h1, buys_h1, buys_m5, sells_m5, change_m5, change_h1):
    return {
        "volume": {"h1": volume_h1, "m5": 0},
        "txns": {"h1": {"buys": buys_h1, "sells": 0}, "m5": {"buys": buys_m5, "sells": sells_m5}},
        "priceChange": {"m5": change_m5, "h1": change_h1},
        "liquidity": {"usd": 80_000},
        "marketCap": 100_000,
        "priceUsd": "0.01",
        "baseToken": {"name": "Demo", "symbol": "DEMO"},
    }


def test_rules_mode_thresholds_and_label(cfg):
    # Construct DexScreener numbers that land exactly on the gate.
    # alive = 25000/25000 = 1
    # organic = 150/150 = 1
    # flush = 1 (m5 <= -5 and buys >= sells)
    # chase = 24/80 = 0.3
    # score = 100 * (1+1+1+0.7)/4 = 92.5 → 92 or 93; anyway >= 70
    pair = _pair(
        volume_h1=25_000,
        buys_h1=150,
        buys_m5=10,
        sells_m5=10,
        change_m5=-5,
        change_h1=24,
    )
    answers = rules_answers(pair, audit=None, cfg=cfg)
    assert answers["alive_6h"] == 1.0
    assert answers["organic"] == 1.0
    assert answers["flush"] == 1.0
    assert abs(answers["chase"] - 0.3) < 1e-9
    gated = apply_gate(answers)
    assert gated["passed"] is True

    result = score_coin(
        mint="Mint111",
        name="Demo",
        age_min=12,
        pair=pair,
        audit={"layers": [
            {"n": 3, "value": "0.9% · sold 0"},
            {"n": 4, "value": "16.8%"},
            {"n": 5, "value": "100% locked"},
            {"n": 7, "value": "None"},
            {"n": 8, "value": "None"},
        ]},
        scanner={"top10Pct": 16.8},
        cfg=cfg,
    )
    assert result["model"] == "rules"
    assert result["inputTokens"] == 0
    assert result["passed"] is True
    assert activity_agent_label(result["model"]) == "Analyst (rules)"
    # state is the required template ("buys"/"sells" are part of it) but must
    # not contain thresholds, a balance, or the verb "buy" as a word of its own.
    import re
    state = result["state"].lower()
    assert "balance" not in state
    assert "0.70" not in state and "0.65" not in state and "min_score" not in state
    assert re.search(r"\bbuy\b", state) is None


def test_rules_mode_reject_chase(cfg):
    # chase = 80/80 = 1.0, well over 0.30
    pair = _pair(
        volume_h1=25_000,
        buys_h1=150,
        buys_m5=0,
        sells_m5=0,
        change_m5=1,
        change_h1=80,
    )
    result = score_coin("Mint222", "Demo", 20, pair, None, None, cfg)
    assert result["model"] == "rules"
    assert result["passed"] is False
    assert any(f.startswith("chase ") for f in result["failed"])
    assert activity_agent_label("rules") == "Analyst (rules)"


def test_rules_bundled_penalty(cfg):
    pair = _pair(
        volume_h1=25_000,
        buys_h1=150,
        buys_m5=0,
        sells_m5=1,
        change_m5=0,
        change_h1=0,
    )
    audit = {"layers": [{"n": 7, "value": "4 bundled wallets"}], "bundled": 4}
    answers = rules_answers(pair, audit, cfg)
    # organic was 1.0, times 0.5 → 0.5, which fails min_organic 0.65
    assert answers["organic"] == 0.5

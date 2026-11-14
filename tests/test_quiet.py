"""Quiet Breakout agent (PAPER): calm coin closing above its 30m high on a volume burst."""

from __future__ import annotations

import asyncio
import json
from datetime import timedelta

from agents import strategies as S
from util import iso_utc, utcnow

NOW = utcnow()
T = NOW.timestamp()


def _set(db, key, val):
    db.kv_set("setting." + key, json.dumps(val))


def _tok(mint="QUIET1", m5=1.5, v1=20_000):
    return {
        "id": mint, "symbol": "CALM", "name": "Calm", "liquidity": 60_000, "mcap": 900_000, "organicScore": 60,
        "audit": {"mintAuthorityDisabled": True, "freezeAuthorityDisabled": True, "topHoldersPercentage": 12.0},
        "stats5m": {"priceChange": m5, "buyVolume": 3000, "sellVolume": 2000},
        "stats1h": {"priceChange": 2.0, "buyVolume": v1 / 2, "sellVolume": v1 / 2},
    }


def _calm(n=45, rng=0.5, vol=400.0, brk_close=1.02, brk_vol=2000.0, last_age_s=70):
    """n calm candles around 1.0 (range rng%), the last completed one breaks out."""
    rows = []
    for i in range(n):
        t = T - last_age_s - (n - 1 - i) * 60
        if i == n - 1:
            rows.append([t, 1.0, brk_close * 1.002, 0.999, brk_close, brk_vol])
        else:
            rows.append([t, 1.0, 1.0 + rng / 200, 1.0 - rng / 200, 1.0, vol])
    rows.append([T - 5, brk_close, brk_close, brk_close, brk_close, 10.0])  # still-open candle: ignored
    return rows


def _report(pool="POOL1"):
    return {"rugged": False, "creatorBalance": 0, "token": {"supply": 1_000_000_000, "decimals": 6},
            "markets": [{"pubkey": pool, "lp": {"lpLockedPct": 100.0}}]}


def test_defaults_without_kv(cfg):
    assert S.setting(cfg, "quiet.enabled") is True
    assert S.setting(cfg, "quiet.max_noise_pct") == 1.2
    assert S.setting(cfg, "quiet.vol_x") == 2.0
    assert S.setting(cfg, "quiet.min_vol30_usd") == 5000.0
    assert S.setting(cfg, "quiet.lookback_min") == 30
    assert S.setting(cfg, "quiet.min_age_min") == 30
    assert S.setting(cfg, "quiet.max_per_hour") == 2
    assert S.setting(cfg, "quiet.tp_pct") == 0.0 and S.setting(cfg, "quiet.fade_exit") is False
    assert "quiet" in S.AGENT_ORDER and S.AGENT_LABEL["quiet"] == "Quiet Breakout"
    assert S.agent_of({"source": "quiet"}) == "quiet"


def test_signal_passes_on_calm_breakout(cfg):
    why, note = S.quiet_signal(_tok(), 1.021, _calm(), cfg, T)
    assert why is None and "broke the 30m high" in note and "volume 5.0x" in note


def test_signal_rejects_noise_volume_high_and_chase(cfg):
    assert S.quiet_signal(_tok(), 1.02, _calm(rng=2.0), cfg, T)[0].startswith("noisy")
    assert S.quiet_signal(_tok(), 1.02, _calm(brk_vol=500), cfg, T)[0] == "no volume burst"
    assert S.quiet_signal(_tok(), 1.0, _calm(brk_close=1.0), cfg, T)[0] == "under the 30m high"
    assert "no chase" in S.quiet_signal(_tok(), 1.10, _calm(), cfg, T)[0]
    assert S.quiet_signal(_tok(), 1.02, _calm(vol=50, brk_vol=200), cfg, T)[0] == "thin volume"


def test_signal_fails_closed(cfg):
    assert S.quiet_signal(_tok(), 1.02, [], cfg, T)[0] == "no candles (fail closed)"
    assert S.quiet_signal(_tok(), 1.02, _calm(n=20), cfg, T)[0] == "no candles (fail closed)"
    assert S.quiet_signal(_tok(), 1.02, _calm(last_age_s=400), cfg, T)[0] == "no fresh candle"
    assert S.quiet_signal(_tok(), None, _calm(), cfg, T)[0] == "no price"
    assert S.quiet_signal(_tok(), 1.02, _calm(), cfg, T, migrated_ts=T - 10 * 60)[0] == "too young"
    assert S.quiet_signal(_tok(m5=-1), 1.02, _calm(), cfg, T)[0] == "no up move"
    assert S.quiet_signal(_tok(v1=1000), 1.02, _calm(), cfg, T)[0] == "thin volume"


def test_size_is_position_cap_and_respects_min_ticket(cfg):
    assert S.quiet_size(148.0, cfg) is None  # default 26% of $148 = $38.48 < $47.50 min ticket
    cfg.db.kv_set("setting.agents.max_position_pct", "40")
    assert S.quiet_size(147.98, cfg) == 59.19
    assert S.quiet_size(100.0, cfg) is None  # $40 < $47.50 min ticket


class Src:
    def __init__(self, tok, price, candles):
        self.tok, self.price, self.candles = tok, price, candles
        self.token_queries = []

    def jupiter_tokens(self, mints):
        self.token_queries.append(list(mints))
        return [self.tok] if self.tok["id"] in mints else []

    def jupiter_trending(self, cat, iv, limit):
        return []

    def any_dex_pairs(self, mints):
        return {self.tok["id"]: {"pairAddress": "POOL1", "priceUsd": str(self.price), "liquidity": {"usd": 60_000},
                                 "volume": {"m5": 3_000}, "baseToken": {"name": "Calm", "symbol": "CALM"}}}

    def gecko_ohlcv(self, pool, limit):
        return self.candles

    def rugcheck_report_polite(self, mint):
        return _report()


class Bus:
    def __init__(self):
        self.items = []
        self.risk = self

    async def put(self, x):
        self.items.append(x)


def test_cycle_buys_recent_migration_in_paper(db, cfg):
    for k in ("momentum.enabled", "dip.enabled"):
        _set(db, k, False)
    _set(db, "agents.sleeve_pct", 100.0)
    _set(db, "agents.max_position_pct", 40.0)
    cfg.set("risk.stop_pct", "1", who="test")
    db.start_night("PAPER", 147.98)
    db.upsert_coin(mint="QUIET1", name="Calm", symbol="CALM", migrated_at=iso_utc(NOW - timedelta(hours=2)))
    src = Src(_tok(), 1.021, _calm())
    stats = asyncio.run(S.MarketAgents(db, cfg, Bus(), src).cycle())
    assert any("QUIET1" in q for q in src.token_queries)
    assert stats["quiet"] == 1 and stats["bought"] == 1
    pos = db.open_positions()[0]
    assert pos["source"] == "quiet" and pos["mode"] == "PAPER"
    import session
    assert pos["size_usd"] == S.quiet_size(session.current_stack(db, cfg), cfg) >= 47.5
    assert abs(pos["stop_price"] - 1.021 * 0.99) < 1e-9  # 1% market stop, unchanged
    st = json.loads(db.kv_get("agent.quiet.status"))
    assert st["enabled"] is True and st["last"]["verdict"] == "bought"


def test_cycle_quiet_disabled_does_nothing(db, cfg):
    for k in ("momentum.enabled", "dip.enabled", "quiet.enabled"):
        _set(db, k, False)
    db.upsert_coin(mint="QUIET1", name="Calm", symbol="CALM", migrated_at=iso_utc(NOW - timedelta(hours=2)))
    src = Src(_tok(), 1.021, _calm())
    stats = asyncio.run(S.MarketAgents(db, cfg, Bus(), src).cycle())
    assert stats["bought"] == 0 and not any("QUIET1" in q for q in src.token_queries)


def test_dashboard_shows_quiet_agent(db, cfg):
    import dashboard_data as D

    db.start_night("PAPER", 150.0)
    db.open_position(mint="QX", entry_price=1.0, size_usd=59.0, stop_price=0.99, mode="PAPER", source="quiet")
    assert D.agent_of_source("quiet") == "quiet"
    assert D.source_fields({"source": "quiet"})["source_label"] == "Quiet Breakout"
    keys = [a["key"] for a in D.DESK4_AGENTS]
    assert keys == ["sniper", "momentum", "dip", "quiet", "whale"]
    assert "quiet" in D.AGENTS

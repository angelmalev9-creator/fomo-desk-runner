"""Video setups logger (@milkybids dump-bounce): record only, never trades; shared gecko fetch."""

from __future__ import annotations

import asyncio
import json
from datetime import timedelta

import httpx

import dashboard_data
import sources as SRC
from agents import milky_log as M
from agents import strategies as S
from util import iso_utc, utcnow

NOW = utcnow()
SUPPLY = 1e9


def _bar(t, o, h, l, c, v=2000.0):
    return [float(t), o, h, l, c, v]


def _e1_bars(t0):
    """Pump 1e-4 -> 1e-2 (100x, $10M ATH), hold, then a dump to -66% (E1), then a bounce."""
    bars, t, p = [], t0, 1e-4
    for _ in range(40):  # pump
        nxt = p * 1.13
        bars.append(_bar(t, p, nxt, p, nxt, 50_000)); t += 60; p = nxt
    ath = p
    for _ in range(5):
        bars.append(_bar(t, ath, ath, ath, ath, 50_000)); t += 60
    for k in range(1, 12):  # slow dump to ~0.34*ath
        nxt = ath * (1 - 0.06 * k)
        bars.append(_bar(t, p, p, nxt, nxt, 30_000)); t += 60; p = nxt
    for _ in range(240):  # bounce +10% then drift
        nxt = p * 1.001
        bars.append(_bar(t, p, nxt, p, nxt, 10_000)); t += 60; p = nxt
    return bars, ath


def test_default_enabled_and_never_trades_settings(cfg):
    assert S.setting(cfg, "milky.log_enabled") is True
    assert "milky" not in S.MARKET_AGENTS and "milky" not in S.AGENT_ORDER


def test_e1_detected_with_filters_and_outcome():
    t0 = NOW.timestamp() - 8 * 3600
    bars, ath = _e1_bars(t0)
    setups = M.find_setups(bars, t0, SUPPLY)
    e1 = [s for s in setups if s["entry_kind"] == "E1"]
    assert len(e1) == 1
    s = e1[0]
    assert s["trigger_price"] <= s["ath_price"] * 0.35
    assert s["ath_mcap"] >= 4e6 and "pattern" in s["filters"] and "loose_ath" in s["filters"]
    out = M.outcome(bars, s["trigger_ts"], NOW.timestamp())
    assert out["1h_complete"] and out["3h_complete"]
    assert out["max_up_1h_pct"] > 0 and out["max_down_1h_pct"] <= 0
    assert set(out) >= {"x1", "x2_info", "max_up_3h_pct", "max_down_3h_pct"}
    assert out["x1"]["fees_usd"] > 0


def test_no_setup_without_pump_or_on_80pct_crash():
    t0 = NOW.timestamp() - 8 * 3600
    flat = [_bar(t0 + i * 60, 1e-3, 1e-3, 1e-3, 1e-3) for i in range(300)]
    assert M.find_setups(flat, t0, SUPPLY) == []
    bars, ath = _e1_bars(t0)
    crash = bars[:45] + [_bar(bars[45][0] + i * 60, ath * 0.1, ath * 0.1, ath * 0.1, ath * 0.1) for i in range(50)]
    assert [s for s in M.find_setups(crash, t0, SUPPLY) if s["entry_kind"] == "E1"] == []


def test_x1_stop_and_x2_info():
    t = NOW.timestamp() - 7 * 3600
    bars = [_bar(t, 1.0, 1.0, 1.0, 1.0)] + [_bar(t + 60 * (i + 1), 1.0, 1.0, 0.95, 0.96) for i in range(5)]
    out = M.outcome(bars, t, NOW.timestamp())
    assert out["x1"]["exit_reason"] == "stop" and out["x1"]["pnl_usd"] < 0
    assert out["x2_info"]["done"] is False  # -5% does not hit a 10% stop


class FakeSrc:
    def __init__(self, bars, pool="POOLM"):
        self.bars, self.pool, self.gecko_calls, self.pair_calls = bars, pool, 0, 0

    def any_dex_pairs(self, mints):
        self.pair_calls += 1
        last = self.bars[-1][4]
        return {m: {"pairAddress": self.pool, "priceUsd": str(last), "fdv": last * SUPPLY,
                    "volume": {"h24": 900_000}, "baseToken": {"symbol": "MLK"}} for m in mints}

    def gecko_ohlcv(self, pool, limit=90, token=None, before_ts=None):
        self.gecko_calls += 1
        if before_ts:
            return [b for b in self.bars if b[0] < before_ts][-limit:]
        return self.bars[-limit:]


def _seed_coin(db, mint, migrated):
    db.execute("INSERT INTO coins (mint, symbol, pool, migrated_at) VALUES (?,?,?,?)", (mint, "MLK", "POOLM", migrated))


def test_cycle_logs_once_and_never_touches_positions(db, cfg, monkeypatch):
    monkeypatch.setattr(M, "PAIRS_GAP_S", 0)
    t0 = NOW.timestamp() - 8 * 3600
    bars, _ = _e1_bars(t0)
    _seed_coin(db, "MILKY1pump", iso_utc(NOW - timedelta(hours=8)))
    src = FakeSrc(bars)
    ml = M.MilkyLogger(db, cfg, src)
    before = {t: db.query(f"SELECT COUNT(*) AS n FROM {t}")[0]["n"] for t in ("positions", "signals")}
    stack = db.kv_get("stack.current")
    st1 = asyncio.run(ml.cycle())
    st2 = asyncio.run(ml.cycle())
    assert st1["new_setups"] >= 1 and st2["new_setups"] == 0
    rows = [dict(r) for r in db.query("SELECT * FROM milky_setups")]
    kinds = [r["entry_kind"] for r in rows]
    assert kinds.count("E1") == 1
    r = next(r for r in rows if r["entry_kind"] == "E1")
    assert "pattern" in json.loads(r["filters"]) and json.loads(r["outcome"])["x1"]
    assert r["outcome_final"] == 1
    after = {t: db.query(f"SELECT COUNT(*) AS n FROM {t}")[0]["n"] for t in ("positions", "signals")}
    assert after == before and db.kv_get("stack.current") == stack
    assert src.gecko_calls <= 2 * M.MILKY_GECKO_PER_CYCLE
    assert src.pair_calls == 1  # DexScreener prefilter cached
    status = json.loads(db.kv_get("agent.milky.status"))
    assert status["enabled"] is True
    block = dashboard_data.milky_block(dashboard_data.connect_ro(db.path))
    assert block["title"] == "Video setups (log only)" and block["items"] and block["counts"]["E1"] == 1
    import gist_push

    full = gist_push.sanitize(dashboard_data.collect(dashboard_data.connect_ro(db.path)))
    live = gist_push.sanitize(dashboard_data.live_collect(dashboard_data.connect_ro(db.path)))
    assert full["milky"]["items"][0]["entry"] in ("E1", "E2") and live["milky"]["counts"]["E1"] == 1


def test_disabled_by_kv(db, cfg):
    db.kv_set("setting.milky.log_enabled", "false")
    src = FakeSrc([_bar(0, 1, 1, 1, 1)])
    st = asyncio.run(M.MilkyLogger(db, cfg, src).cycle())
    assert st["coins"] == 0 and src.gecko_calls == 0
    assert json.loads(db.kv_get("agent.milky.status"))["enabled"] is False


def test_gecko_shared_cache_and_429_cooldown(monkeypatch):
    calls = {"n": 0, "mode": "429"}

    def handler(req):
        calls["n"] += 1
        if calls["mode"] == "429":
            return httpx.Response(429, headers={"retry-after": "30"})
        return httpx.Response(200, json={"data": {"attributes": {"ohlcv_list": [[2, 1, 1, 1, 1, 5], [1, 1, 1, 1, 1, 5]]}}})

    monkeypatch.setattr(SRC, "_GECKO_GAP_S", 0)
    SRC._gecko_cache.clear()
    SRC._gecko_state.update(block_until=0.0, backoff_s=60.0, last_429=0.0)
    s = SRC.HttpSources(transport=httpx.MockTransport(handler), sleep=lambda x: None)
    assert s.gecko_ohlcv("P", 90, token="MINT") == []
    snap = SRC.gecko_stats_snapshot()
    assert snap["cooldown_left_s"] > 0
    assert s.gecko_ohlcv("P2", 90, token="MINT") == [] and calls["n"] == 1  # cooldown: no new request
    SRC._gecko_state["block_until"] = 0.0
    calls["mode"] = "ok"
    got = s.gecko_ohlcv("P", 90, token="MINT")
    assert [r[0] for r in got] == [1.0, 2.0]
    assert s.gecko_ohlcv("P", 90, token="MINT") == got and calls["n"] == 2  # cached
    SRC._gecko_cache.clear()
    SRC._gecko_state["last_429"] = 0.0
    SRC._gecko_cache.clear()


def test_milky_yields_after_recent_429(db, cfg, monkeypatch):
    import time as _t

    monkeypatch.setattr(M, "PAIRS_GAP_S", 0)
    SRC._gecko_state["last_429"] = _t.monotonic()
    try:
        t0 = NOW.timestamp() - 8 * 3600
        bars, _ = _e1_bars(t0)
        for k in range(3):
            _seed_coin(db, f"MILKY{k}pump", iso_utc(NOW - timedelta(hours=8)))
        src = FakeSrc(bars)
        st = asyncio.run(M.MilkyLogger(db, cfg, src).cycle())
        assert st["gecko_budget"] == 1 and src.gecko_calls == 1
    finally:
        SRC._gecko_state["last_429"] = 0.0

"""Four-agent desk: sleeves, Momentum, Dip Hunter, rug screen, extra exits (PAPER only)."""

from __future__ import annotations

import asyncio
import json

import pytest
from datetime import timedelta

from agents import strategies as S
from util import iso_utc, utcnow

NOW = utcnow()
T = NOW.timestamp()


def _set(db, key, val):
    db.kv_set("setting." + key, json.dumps(val))


def _open(db, source, size=20.0, mint=None, entry=1.0):
    return db.open_position(mint=mint or f"M{source}{size}{len(db.open_positions())}", entry_price=entry,
                            size_usd=size, stop_price=entry * 0.99, mode="PAPER", source=source)


def _tok(mint="MEME1", m5=8.0, buy=7000, sell=3000, v1_buy=8000, v1_sell=8000, h1=0.0, liq=50_000, top=15.0,
         organic=60, liq_ch=0.0, holder_ch=0.0):
    return {
        "id": mint, "symbol": "MEME", "name": "Meme", "liquidity": liq, "mcap": 2_000_000, "organicScore": organic,
        "audit": {"mintAuthorityDisabled": True, "freezeAuthorityDisabled": True, "topHoldersPercentage": top},
        "stats5m": {"priceChange": m5, "buyVolume": buy, "sellVolume": sell},
        "stats1h": {"priceChange": h1, "buyVolume": v1_buy, "sellVolume": v1_sell, "liquidityChange": liq_ch,
                    "holderChange": holder_ch},
    }


def _candles(high=1.0, low=None, n=60, end_price=1.0):
    rows = []
    for i in range(n):
        t = T - (n - i) * 60
        lo = low if (low is not None and i >= n - 5) else high * 0.97
        rows.append([t, high * 0.98, high if i == n // 2 else high * 0.99, lo, end_price, 1000])
    return rows


def _report(pool="POOL1", locked=100.0, dev=0, supply=1_000_000_000, rugged=False):
    return {"rugged": rugged, "creatorBalance": dev, "token": {"supply": supply, "decimals": 6},
            "markets": [{"pubkey": pool, "lp": {"lpLockedPct": locked}}]}


# ---------------------------------------------------------------- sleeves
def test_agent_of_maps_sources():
    assert S.agent_of({"source": "copy"}) == "whale"
    assert S.agent_of({"source": None}) == "sniper"
    assert S.agent_of({"source": "momentum"}) == "momentum"
    assert S.agent_of({"source": "dip"}) == "dip"


def test_sleeves_are_isolated(db, cfg):
    stack = 200.0  # sleeve = $50 each
    _open(db, "copy", 20)
    _open(db, "copy", 20)
    assert S.sleeve_check(db, cfg, "whale", 20, stack) is not None  # 40 + 20 > 50
    assert S.sleeve_check(db, cfg, "momentum", 20, stack) is None  # other sleeves unaffected
    assert S.sleeve_check(db, cfg, "sniper", 47.5, stack) is None
    _open(db, None, 47.5)
    assert S.sleeve_check(db, cfg, "sniper", 47.5, stack) is not None
    assert S.sleeve_check(db, cfg, "dip", 20, stack) is None


def test_position_cap_26pct(db, cfg):
    assert S.sleeve_check(db, cfg, "momentum", 53, 200.0) is not None  # 26% of 200 = 52
    _set(db, "agents.sleeves_enabled", False)
    assert S.sleeve_check(db, cfg, "momentum", 53, 200.0) is None


# ---------------------------------------------------------------- momentum
def test_momentum_breakout_passes():
    why, note = S.momentum_signal(_tok(), 1.05, _candles(high=1.0), None, T)
    assert why is None and "1h high" in note


def test_momentum_needs_new_1h_high():
    why, _ = S.momentum_signal(_tok(), 0.99, _candles(high=1.0), None, T)
    assert why and "1h high" in why


def test_momentum_fails_closed_without_candles():
    why, _ = S.momentum_signal(_tok(), 1.05, [], None, T)
    assert why and "fail closed" in why
    # only 10 minutes of history: cannot know the 1h high -> skip
    why, _ = S.momentum_signal(_tok(), 1.05, _candles(high=1.0, n=10), None, T)
    assert why and "fail closed" in why


def test_momentum_sparse_candles_ok():
    sparse = _candles(high=1.0)[::6]  # Gecko omits minutes without trades
    why, _ = S.momentum_signal(_tok(), 1.05, sparse, None, T)
    assert why is None


def test_momentum_rejects_weak_or_parabolic():
    assert S.momentum_prefilter(_tok(m5=2), None) == "no momentum"
    assert "no chase" in S.momentum_prefilter(_tok(m5=55), None)
    assert S.momentum_prefilter(_tok(buy=3000, sell=7000), None) == "sellers lead"
    assert S.momentum_prefilter(_tok(buy=700, sell=300), None) == "no volume spike"
    assert S.momentum_prefilter(_tok(organic=5), None) == "low organic score"


# ---------------------------------------------------------------- dip
def _dip_tok(**kw):
    base = dict(m5=6.0, buy=6000, sell=4000, h1=-20.0)
    base.update(kw)
    return _tok(**base)


def test_dip_buys_bouncing_wick():
    why, note = S.dip_signal(_dip_tok(), 0.70, _candles(high=1.0, low=0.62, end_price=0.70), None, T)
    assert why is None and "wick" in note


def test_dip_rejects_small_wick_no_bounce_and_recovered():
    assert "wick only" in S.dip_signal(_dip_tok(), 0.85, _candles(high=1.0, low=0.80), None, T)[0]
    assert S.dip_signal(_dip_tok(), 0.63, _candles(high=1.0, low=0.62), None, T)[0] == "no bounce yet"
    assert S.dip_signal(_dip_tok(), 0.95, _candles(high=1.0, low=0.62), None, T)[0] == "already recovered"


def test_dip_prefilter_health():
    assert S.dip_prefilter(_dip_tok(m5=-3), None) == "still falling"
    assert S.dip_prefilter(_dip_tok(liq_ch=-40), None) == "liquidity leaving"
    assert S.dip_prefilter(_dip_tok(holder_ch=-20), None) == "holders leaving"
    assert S.dip_prefilter(_dip_tok(buy=3000, sell=7000), None) == "no bids"
    assert S.dip_prefilter(_tok(h1=-2), None) == "no dip"


# ---------------------------------------------------------------- rug screen
def test_rug_check_pass_and_fail_closed():
    ok, note = S.rug_check(_tok(), _report(), "POOL1", None)
    assert ok is None and "LP burned" in note
    assert S.rug_check(_tok(), None, "POOL1", None)[0] == "no data: RugCheck"
    assert S.rug_check(_tok(), _report(pool="OTHER"), "POOL1", None)[0] == "no data: LP"
    t = _tok(); t["audit"].pop("topHoldersPercentage")
    assert S.rug_check(t, _report(), "POOL1", None)[0] == "no data: top 10"
    assert "top 10" in S.rug_check(_tok(top=45), _report(), "POOL1", None)[0]
    assert "dev holds" in S.rug_check(_tok(), _report(dev=80_000_000), "POOL1", None)[0]
    assert "locked" in S.rug_check(_tok(), _report(locked=40), "POOL1", None)[0]
    assert S.rug_check(_tok(), _report(rugged=True), "POOL1", None)[0] == "RugCheck: rugged"
    t = _tok(); t["audit"]["mintAuthorityDisabled"] = False
    assert S.rug_check(t, _report(), "POOL1", None)[0] == "mint not revoked"


# ---------------------------------------------------------------- exits
def test_extra_exit_target_and_fade(db, cfg):
    pid = _open(db, "momentum", 20, mint="MOMO", entry=1.0)
    db.update_position(pid, opened_at=iso_utc(NOW - timedelta(minutes=20)))
    db.kv_set(S.meta_key(pid), json.dumps({"entry_vol_m5": 10_000}))
    pos = db.get_position(pid)
    assert S.extra_exit(pos, 1.41, 9_000, cfg, db, NOW) == "target"
    assert S.extra_exit(pos, 1.10, 2_000, cfg, db, NOW) == "faded"
    assert S.extra_exit(pos, 1.10, 5_000, cfg, db, NOW) is None
    _set(db, "momentum.fade_exit", False)
    assert S.extra_exit(pos, 1.10, 2_000, cfg, db, NOW) is None


def test_extra_exit_off_for_whale_and_sniper_by_default(db, cfg):
    for src in ("copy", None):
        pid = _open(db, src, 20, entry=1.0)
        assert S.extra_exit(db.get_position(pid), 3.0, 1.0, cfg, db, NOW) is None


def test_fade_waits_min_age(db, cfg):
    pid = _open(db, "momentum", 20, mint="MOMO2", entry=1.0)
    db.kv_set(S.meta_key(pid), json.dumps({"entry_vol_m5": 10_000}))
    assert S.extra_exit(db.get_position(pid), 1.05, 100, cfg, db, NOW) is None


# ---------------------------------------------------------------- loop (fake sources, no network)
class FakeBus:
    def __init__(self):
        self.items = []

        class Q:
            async def put(q, x):
                self.items.append(x)

        self.risk = Q()


class FakeSources:
    def __init__(self, tok, price, candles, report):
        self.tok, self.price, self.candles, self.report = tok, price, candles, report
        self.calls = []

    def jupiter_tokens(self, mints):
        return [{"id": mints[0], "symbol": "JUP", "usdPrice": 0.5, "stats24h": {"priceChange": 3.2}}]

    def jupiter_trending(self, cat, iv, limit):
        self.calls.append((cat, iv))
        return [self.tok]

    def any_dex_pairs(self, mints):
        return {self.tok["id"]: {"pairAddress": "POOL1", "priceUsd": str(self.price), "liquidity": {"usd": 50_000},
                                 "volume": {"m5": 12_000}, "baseToken": {"name": "Meme", "symbol": "MEME"}}}

    def gecko_ohlcv(self, pool, limit, **kw):
        return self.candles

    def rugcheck_report_polite(self, mint):
        return self.report


def test_cycle_opens_paper_momentum_and_ticker(db, cfg):
    _set(db, "dip.enabled", False)
    cfg.set("risk.stop_pct", "1", who="test")
    src = FakeSources(_tok(), 1.05, _candles(high=1.0), _report())
    bus = FakeBus()
    stats = asyncio.run(S.MarketAgents(db, cfg, bus, src, notifier=None).cycle())
    assert stats["bought"] == 1
    pos = db.open_positions()[0]
    assert pos["source"] == "momentum" and pos["mode"] == "PAPER" and pos["size_usd"] == 20
    assert abs(pos["stop_price"] - 1.05 * 0.99) < 1e-9  # 1% stop, never the video's 6%
    assert bus.items and json.loads(db.kv_get("ticker"))["items"][0]["symbol"] == "JUP"
    assert json.loads(db.kv_get(S.meta_key(pos["id"])))["entry_vol_m5"] == 12_000
    # second cycle: same coin is already open -> no second buy
    assert asyncio.run(S.MarketAgents(db, cfg, bus, src).cycle())["bought"] == 0


def test_cycle_fails_closed_on_rug_and_flag(db, cfg):
    src = FakeSources(_tok(), 1.05, _candles(high=1.0), None)
    assert asyncio.run(S.MarketAgents(db, cfg, FakeBus(), src).cycle())["bought"] == 0
    _set(db, "agents.enabled", False)
    src = FakeSources(_tok(), 1.05, _candles(high=1.0), _report())
    assert asyncio.run(S.MarketAgents(db, cfg, FakeBus(), src).cycle())["bought"] == 0
    assert not db.open_positions()


def test_cycle_respects_sleeve(db, cfg):
    for i in range(2):
        _open(db, "momentum", 20, mint=f"X{i}")
    _set(db, "momentum.size_usd", 20.0)
    src = FakeSources(_tok(), 1.05, _candles(high=1.0), _report())
    assert asyncio.run(S.MarketAgents(db, cfg, FakeBus(), src).cycle())["bought"] == 0


def test_risk_closes_momentum_at_target(db, cfg):
    from agents import risk

    pid = _open(db, "momentum", 20, mint="MEME1", entry=1.0)
    src = FakeSources(_tok(), 1.45, [], _report())
    src.pumpswap_pairs = lambda mints: {}

    class Bus(FakeBus):
        async def emit(self, *a):
            pass

    asyncio.run(risk._poll_once(db, cfg, Bus(), None, src, {}, {}, set()))
    pos = db.get_position(pid)
    assert pos["status"] == "closed" and pos["exit_reason"] == "target" and pos["pnl_usd"] > 0


def test_dashboard_desk4_block(db, cfg, tmp_path):
    import dashboard_data as D
    import gist_push

    db.start_night("PAPER", 200.0)
    _open(db, "momentum", 20, mint="MOMOX", entry=1.0)
    db.add_price("MOMOX", 1.1, 5000)
    cp = _open(db, "copy", 20, mint="WHALEX", entry=1.0)
    db.update_position(cp, status="closed", closed_at=iso_utc(), exit_price=1.2, pnl_usd=3.28, exit_reason="trail")
    db.kv_set("ticker", json.dumps({"ts": iso_utc(), "items": [{"symbol": "JUP", "price": 0.26, "ch24": 4.0}]}))
    conn = D.connect_ro(db.path) if hasattr(db, "path") else D.connect_ro(tmp_path / "test.db")
    out = gist_push.sanitize(D.collect(conn))
    d4 = out["desk4"]
    by = {a["key"]: a for a in d4["agents"]}
    assert [a["key"] for a in d4["agents"]] == ["sniper", "momentum", "dip", "quiet", "whale"]
    assert by["momentum"]["state"] == "in_trade" and by["momentum"]["positions"][0]["series"][0] == 0
    assert by["whale"]["wins"] == 1 and by["whale"]["losses"] == 0 and by["dip"]["state"] == "idle"
    assert d4["label"] == "PAPER 5 agents" and d4["ticker"][0]["symbol"] == "JUP"
    assert d4["goal"]["next"] == 1.25 and any(e["agent"] == "whale" for e in d4["events"])


def test_live_status_and_summary(db, cfg):
    _set(db, "dip.enabled", False)
    _set(db, "quiet.enabled", False)
    cfg.set("risk.stop_pct", "1", who="test")
    src = FakeSources(_tok(), 0.99, _candles(high=1.0), _report())  # under the 1h high -> pass
    asyncio.run(S.MarketAgents(db, cfg, FakeBus(), src).cycle())
    st = json.loads(db.kv_get("agent.momentum.status"))
    assert st["phase"] == "waiting" and st["checked"] == 1 and st["last"]["verdict"] == "pass"
    assert "1h high" in st["last"]["reason"] and st["last"]["symbol"] == "$MEME"
    rows = [dict(r) for r in db.query("SELECT * FROM activity WHERE kind='scan'")]
    assert len(rows) == 1 and rows[0]["agent"] == "momentum"
    ma = S.MarketAgents(db, cfg, FakeBus(), src)
    ma.skip_logged["summary:momentum"] = utcnow().timestamp()
    asyncio.run(ma.cycle())  # rate-limited: no second summary row
    assert len([r for r in db.query("SELECT id FROM activity WHERE kind='scan'")]) == 1


def test_live_collect_small_and_clean(db, cfg):
    import dashboard_data as D
    import gist_push

    db.start_night("PAPER", 200.0)
    db.heartbeat("momentum")
    _open(db, "dip", 20, mint="DIPX", entry=1.0)
    db.add_price("DIPX", 0.98, 100)
    live = gist_push.sanitize(D.live_collect(D.connect_ro(db.path)))
    assert live["kind"] == "live" and live["desk4"]["agents"][2]["state"] == "in_trade"
    by = {a["key"]: a for a in live["desk4"]["agents"]}
    assert by["momentum"]["stalled"] is False and by["dip"]["stalled"] is True  # no dip heartbeat yet
    assert by["whale"]["live"]["kind"] == "whale" and by["sniper"]["live"]["kind"] == "sniper"
    assert len(json.dumps(live)) < 60_000


def test_gist_push_sends_both_files(monkeypatch):
    import gist_push

    seen = {}

    def fake(method, path, body=None):
        seen["body"] = body
        return 200, {"x-ratelimit-remaining": "4990"}, {}

    monkeypatch.setattr(gist_push, "api_request", fake)
    ok, _ = gist_push.push("gid", {gist_push.LIVE_FILENAME: "{}", gist_push.FILENAME: "{}"})
    assert ok and set(seen["body"]["files"]) == {"fomo-live.json", "fomo-desk.json"}
    assert gist_push.RATE["x-ratelimit-remaining"] == "4990"


def test_gist_paced_interval(monkeypatch):
    import gist_push

    gist_push.RATE.clear()
    assert gist_push.paced_interval(1000) == gist_push.LIVE_INTERVAL_S
    gist_push.RATE.update({"x-ratelimit-remaining": "60", "x-ratelimit-reset": str(1000 + 3600)})
    assert 74 < gist_push.paced_interval(1000) < 76  # 3600 s / 48 usable
    gist_push.RATE.update({"x-ratelimit-remaining": "5"})
    assert gist_push.paced_interval(1000) > 3600  # budget gone: wait for the reset
    gist_push.RATE.update({"x-ratelimit-remaining": "100", "x-ratelimit-reset": str(1000 + 600)})
    assert gist_push.paced_interval(1000) == gist_push.LIVE_INTERVAL_S
    gist_push.RATE.clear()


# ------------------------------------------------------------ live sleeve 30% + cash cap + market stop
def test_sleeve_30pct_covers_sniper_ticket_down_to_170(db, cfg):
    _set(db, "agents.sleeve_pct", 30.0)
    _set(db, "agents.max_position_pct", 30.0)
    # $47.50 is 27.94% of $170, under the 30% sleeve and cap
    assert S.sleeve_check(db, cfg, "sniper", 47.50, 170.0) is None
    assert S.sleeve_check(db, cfg, "sniper", 47.50, 184.14) is None
    # 30% of $150 = $45, too small for the min ticket
    assert S.sleeve_check(db, cfg, "sniper", 47.50, 150.0) is not None
    # a single position may not exceed the sleeve
    assert S.sleeve_check(db, cfg, "sniper", 51.1, 170.0) is not None  # 30% of 170 = 51


def test_cash_cap_blocks_when_sleeves_sum_over_100pct(db, cfg):
    _set(db, "agents.sleeve_pct", 30.0)
    _set(db, "agents.max_position_pct", 30.0)
    stack = 200.0  # each sleeve $60, four sleeves $240 > $200
    _open(db, None, 47.50)  # sniper
    for _ in range(3):
        _open(db, "momentum", 20)  # $60, sleeve full
    for _ in range(3):
        _open(db, "dip", 20)  # $60, sleeve full
    _open(db, "copy", 20)  # whale sleeve still has $40 room
    # 47.50+60+60+20 = 187.50, free 12.50 — cash, not the sleeve, blocks the next $20
    assert "no free stack" in (S.sleeve_check(db, cfg, "whale", 20.0, stack) or "")
    assert "no free stack" in (S.sleeve_check(db, cfg, "sniper", 47.50, stack) or "")


def test_momentum_open_uses_shared_market_stop_basis(db, cfg):
    _set(db, "dip.enabled", False)
    _set(db, "risk.stop_basis", "market")
    cfg.set("risk.stop_pct", "1", who="test")
    src = FakeSources(_tok(), 1.05, _candles(high=1.0), _report())
    stats = asyncio.run(S.MarketAgents(db, cfg, FakeBus(), src, notifier=None).cycle())
    assert stats["bought"] == 1
    pos = db.open_positions()[0]
    assert pos["source"] == "momentum"
    # entry is already the market (no pad), so market basis = 1% under 1.05
    assert pos["stop_price"] == pytest.approx(1.05 * 0.99)


# ------------------------------------------------------------ sniper-only desk (owner t39u)
def test_sniper_only_flags_block_momentum_and_dip_entries(db, cfg):
    _set(db, "momentum.enabled", False)
    _set(db, "dip.enabled", False)
    src = FakeSources(_tok(), 1.05, _candles(high=1.0), _report())
    assert asyncio.run(S.MarketAgents(db, cfg, FakeBus(), src).cycle())["bought"] == 0
    assert not db.open_positions()


def test_sniper_sleeve_100pct_and_40pct_cap_covers_ticket_at_150(db, cfg):
    _set(db, "agents.sleeve_pct", 100.0)
    _set(db, "agents.max_position_pct", 40.0)
    # $47.50 is 31.7% of $150, under the 40% cap and inside the 100% sleeve
    assert S.sleeve_check(db, cfg, "sniper", 47.50, 150.18) is None
    assert S.sleeve_check(db, cfg, "sniper", 47.50, 120.0) is None  # 40% of 120 = 48
    assert S.sleeve_check(db, cfg, "sniper", 47.50, 110.0) is not None  # 40% of 110 = 44
    # a single position still cannot take the whole stack
    assert S.sleeve_check(db, cfg, "sniper", 70.0, 150.18) is not None

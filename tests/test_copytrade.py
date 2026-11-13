"""Copy-trading (PAPER): parser, history fetcher checkpoints, backtest, CopyWatcher, refresh."""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import pytest

import backtest
import dashboard_data
import history_fetch
import links
import session
import trader_refresh
from agents import copywatch
from agents.copywatch import CopyWatcher, buy_buttons, on_copy_skip
from copytrade import FOMO_SIGNER, USDC_MINT, parse_fomo_trade
from telegram import TelegramNotifier

ROOT = Path(__file__).resolve().parents[1]
OWNER = 7659480276
STRANGER = 111222333
W = "FG4c1qnq8vEc9nn6uxmtWVjWZLE9rxTfeExBYh5GQnRb"
MINT = "HNxyGpz8AkLqmfBBKMzmugkPE5Bahj2bGPPTNgWGpump"


def tx(wallet=W, mint=MINT, usdc_delta=-10.0, tok_delta=1000.0, signer=FOMO_SIGNER, bt=None, err=None):
    pre_u, pre_t = 100.0, 500.0
    return {
        "blockTime": bt if bt is not None else int(time.time()),
        "meta": {
            "err": err,
            "preTokenBalances": [
                {"owner": wallet, "mint": USDC_MINT, "uiTokenAmount": {"uiAmount": pre_u}},
                {"owner": wallet, "mint": mint, "uiTokenAmount": {"uiAmount": pre_t}},
            ],
            "postTokenBalances": [
                {"owner": wallet, "mint": USDC_MINT, "uiTokenAmount": {"uiAmount": pre_u + usdc_delta}},
                {"owner": wallet, "mint": mint, "uiTokenAmount": {"uiAmount": None, "uiAmountString": str(pre_t + tok_delta)}},
            ],
        },
        "transaction": {"signatures": ["SIG1"], "message": {"accountKeys": [
            {"pubkey": signer, "signer": True}, {"pubkey": wallet, "signer": True}]}},
    }


# ------------------------------------------------------------ parser
def test_parse_buy_and_sell():
    b = parse_fomo_trade(tx(), W)
    assert b["side"] == "buy" and b["mint"] == MINT and b["usdc"] == pytest.approx(10.0)
    assert b["price_usd"] == pytest.approx(0.01)
    s = parse_fomo_trade(tx(usdc_delta=12.0, tok_delta=-1000.0), W)
    assert s["side"] == "sell" and s["price_usd"] == pytest.approx(0.012)


def test_parse_rejects_non_fomo_failed_and_transfers():
    assert parse_fomo_trade(tx(signer="SomeOtherSigner1111111111111111111111111111"), W) is None
    assert parse_fomo_trade(tx(err={"x": 1}), W) is None
    raw = json.loads((ROOT / "logs/raw/wallet_tx_raw_zelda89.json").read_text())
    assert parse_fomo_trade(raw["tx"], W, raw["signature"]) is None  # USDC withdrawal, not a trade


# ------------------------------------------------------------ history fetcher
class FakeRpcPost:
    def __init__(self, n_sigs=5, fail_429_first=False):
        self.calls = []
        self.n = n_sigs
        self.fail = fail_429_first

    def __call__(self, payload):
        self.calls.append(payload["method"])
        if self.fail:
            self.fail = False
            return 429, None
        if payload["method"] == "getSignaturesForAddress":
            assert payload["params"][1]["limit"] == 1000
            now = int(time.time())
            return 200, {"result": [{"signature": f"S{i}", "blockTime": now - i * 60, "err": None} for i in range(self.n)]}
        sig, opts = payload["params"]
        assert opts["maxSupportedTransactionVersion"] == 1 and opts["encoding"] == "jsonParsed"
        i = int(sig[1:])
        t = tx(usdc_delta=-10.0 if i % 2 == 0 else 12.0, tok_delta=1000.0 if i % 2 == 0 else -1000.0)
        return 200, {"result": t}


def test_history_fetch_checkpoint_resume(tmp_path):
    dbp = tmp_path / "h.db"
    post = FakeRpcPost(n_sigs=6)
    rpc = history_fetch.GentleRpc(gap_s=0, post=post, sleep=lambda s: None)
    rc = history_fetch.run(dbp, wallets={"zelda89": W}, rpc=rpc, max_tx=2)
    assert rc == 3  # stopped early = "freeze"
    p = history_fetch.progress(history_fetch.connect(dbp))
    assert p["done"] == 2 and p["total"] == 6
    lists_before = post.calls.count("getSignaturesForAddress")
    rc = history_fetch.run(dbp, wallets={"zelda89": W}, rpc=rpc)
    assert rc == 0
    assert post.calls.count("getSignaturesForAddress") == lists_before  # listing not redone
    assert post.calls.count("getTransaction") == 6  # each tx fetched once
    p = history_fetch.progress(history_fetch.connect(dbp))
    assert p["done"] == 6 and p["per"]["zelda89"]["trades"] == 6 and p["finished"]


def test_gentle_rpc_backs_off_on_429():
    sleeps = []
    rpc = history_fetch.GentleRpc(gap_s=1.0, post=FakeRpcPost(fail_429_first=True), sleep=sleeps.append)
    rpc.call("getSignaturesForAddress", [W, {"limit": 1000}])
    assert rpc.gap >= 1.0 and any(s >= 5 for s in sleeps)


# ------------------------------------------------------------ backtest
def test_backtest_zelda89_partial_data():
    rep = backtest.run("/nonexistent/x.db", [ROOT / "logs/raw/wallet_history_zelda89.json"])
    a = rep["A_follow_trader"]["total"]
    assert a["buys"] == 3 and a["invested"] == 30.0
    assert a["pnl"] < -20  # zelda89 sold HNxy ~-90%: copy loses too
    b = rep["B_desk_rules"]
    assert b["total"]["pnl"] > a["pnl"]  # the stop cut losses
    assert "selection bias" in rep["caveat"]
    assert "Бектест" in backtest.text_report(rep)


def test_backtest_gap_down_fills_at_real_price_not_stop():
    trades = [
        {"handle": "h", "mint": "M", "side": "buy", "block_time": 0, "price_usd": 1.0, "token_amount": 10, "usdc": 10, "time_utc": "a"},
        {"handle": "x", "mint": "M", "side": "buy", "block_time": 60, "price_usd": 0.5, "token_amount": 10, "usdc": 5, "time_utc": "b"},
    ]
    r = backtest.Rules(slippage_pct=0)
    b = backtest.variant_b(trades, r)
    lot_h = b["per_trader"]["h"]
    assert lot_h["realized"] < -4.5  # filled near 0.5, not at the -6% stop


# ------------------------------------------------------------ CopyWatcher
class FakeNotifier(TelegramNotifier):
    def __init__(self, db, cfg):
        super().__init__(token="x", owner_chat_id=OWNER, db=db, cfg=cfg)
        self.sent = []

    def send_sync(self, text, buttons=None, chat_id=None):
        self.sent.append({"text": text, "buttons": buttons})
        return True

    def _answer_callback(self, callback_id):
        return None


class FakeSources:
    def __init__(self, price=0.01, liq=90000, m5=2.0, buys=30, sells=20):
        self.price = price
        self.liq, self.m5, self.buys, self.sells = liq, m5, buys, sells

    def any_dex_pairs(self, mints):
        if self.price is None:
            return {}
        return {m: {"priceUsd": str(self.price), "liquidity": {"usd": self.liq},
                    "priceChange": {"m5": self.m5}, "txns": {"m5": {"buys": self.buys, "sells": self.sells}},
                    "baseToken": {"address": m, "name": "Moon Cat", "symbol": "MCAT"}} for m in mints}

    def pumpswap_pairs(self, mints):
        return {}


class FakeRpc:
    def __init__(self, txs=None, sigs=None):
        self.txs = txs or {}
        self.sigs = sigs or []

    def call(self, method, params=None):
        if method == "getTransaction":
            assert params[1]["maxSupportedTransactionVersion"] == 1
            return {"result": self.txs.get(params[0])}
        if method == "getSignaturesForAddress":
            return {"result": self.sigs}
        return {"result": None}


class Bus:
    def __init__(self):
        self.risk = asyncio.Queue()


def _cw(db, cfg, price=0.01, rpc=None):
    db.add_balance(200.0)
    db.execute("INSERT INTO copy_traders(handle, wallet, active, pinned) VALUES('zelda89', ?, 1, 1)", (W,))
    n = FakeNotifier(db, cfg)
    return CopyWatcher(db, cfg, Bus(), rpc or FakeRpc(), FakeSources(price), n), n


def run(c):
    return asyncio.run(c)


def test_copy_buy_opens_paper_position_and_alerts(db, cfg):
    cw, n = _cw(db, cfg)
    trade = parse_fomo_trade(tx(), W, "SIGB")
    assert run(cw.on_trade("zelda89", trade)) == "copied"
    pos = db.open_positions()
    assert len(pos) == 1 and pos[0]["source"] == "copy" and pos[0]["copy_handle"] == "zelda89"
    assert pos[0]["size_usd"] == 10.0 and pos[0]["mode"] == "PAPER"
    assert pos[0]["entry_price"] == pytest.approx(0.01 * 1.015)
    msg = n.sent[-1]
    assert msg["text"].startswith("PAPER · копие от zelda89")
    assert "Нищо не е купено с реални пари" in msg["text"]
    url = msg["buttons"][0]["url"]
    assert url == links.coin_url(MINT, "solana") and "r=savipww" in url
    assert msg["buttons"][1] == {"text": "Пропусни", "callback": f"cskip:{MINT}"}


def test_copy_sell_closes_copy(db, cfg):
    cw, n = _cw(db, cfg)
    run(cw.on_trade("zelda89", parse_fomo_trade(tx(), W, "SIGB")))
    cw.sources.price = 0.02
    assert run(cw.on_trade("zelda89", parse_fomo_trade(tx(usdc_delta=20, tok_delta=-1000), W, "SIGS"))) == "closed_copy"
    assert not db.open_positions()
    closed = db.query_one("SELECT * FROM positions WHERE exit_reason='trader_sold'")
    assert closed and closed["pnl_usd"] > 0
    assert "затворих копието" in n.sent[-1]["text"]
    assert db.last_balance() > 200.0


def test_copy_fail_closed_no_price_and_old_and_paused(db, cfg):
    cw, n = _cw(db, cfg, price=None)
    assert run(cw.on_trade("zelda89", parse_fomo_trade(tx(), W, "S1"))) == "no_price"
    assert not db.open_positions() and "fail closed" in n.sent[-1]["text"]
    old = parse_fomo_trade(tx(bt=int(time.time()) - 3600), W, "S2")
    assert run(cw.on_trade("zelda89", old)) == "too_old"
    cw.sources.price = 0.01
    cfg.pause("executor", who="test")  # /sellall pause
    assert run(cw.on_trade("zelda89", parse_fomo_trade(tx(), W, "S3"))) == "entries_paused"
    assert not db.open_positions()


def test_copy_alert_respects_quiet_mode(db, cfg):
    cw, n = _cw(db, cfg)
    session.start_quiet(db, cfg)
    run(cw.on_trade("zelda89", parse_fomo_trade(tx(), W, "SQ")))
    assert n.sent == []  # held
    q = db.queued_pushes()
    assert q and "копие от zelda89" in q[-1]["text"]
    assert len(db.open_positions()) == 1  # PAPER still recorded


def test_skip_button_drops_copy_and_owner_lock(db, cfg):
    cw, n = _cw(db, cfg)
    run(cw.on_trade("zelda89", parse_fomo_trade(tx(), W, "SK")))
    cb = lambda chat: {"update_id": 9, "callback_query": {"id": "c", "data": f"cskip:{MINT}", "from": {"id": chat}, "message": {"chat": {"id": chat}}}}
    run(n._handle_update(cb(STRANGER)))
    assert len(db.open_positions()) == 1  # stranger ignored
    run(n._handle_update(cb(OWNER)))
    assert not db.open_positions() and db.is_skipped(MINT)
    assert "Пропуснато" in n.sent[-1]["text"]
    assert run(cw.on_trade("zelda89", parse_fomo_trade(tx(), W, "SK2"))) == "skipped_coin"


def test_handle_signature_dedupes_and_poll_seeds(db, cfg):
    t = tx()
    rpc = FakeRpc(txs={"NEW": t}, sigs=[{"signature": "NEW", "err": None}])
    cw, n = _cw(db, cfg, rpc=rpc)
    assert run(cw.poll_once()) == 0  # first sight only stores the cursor
    assert db.kv_get(f"copy.last_sig.{W}") == "NEW"
    assert run(cw.handle_signature("NEW", W, "ws")) == "copied"
    assert run(cw.handle_signature("NEW", W, "poll")) is None  # seen
    assert len(db.open_positions()) == 1


def test_ws_loop_reconnects_and_queues(db, cfg):
    cw, _ = _cw(db, cfg)
    db.kv_set("setting.copy.ws_recycle_s", "1")
    connects = []

    class FakeWS:
        def __init__(self):
            self.out = [json.dumps({"id": 0, "result": 77}),
                        json.dumps({"params": {"subscription": 77, "result": {"value": {"signature": "WSIG", "err": None}}}})]

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def send(self, m):
            assert json.loads(m)["params"][0] == {"mentions": [W]}

        async def recv(self):
            if self.out:
                return self.out.pop(0)
            await asyncio.sleep(10)

    def connect(url, **kw):
        connects.append(url)
        if len(connects) == 1:
            raise OSError("drop")
        return FakeWS()

    cw._ws_connect = connect

    async def go():
        stop = asyncio.Event()
        copywatch._sleep_orig = copywatch._sleep

        async def fast_sleep(stop_, s):
            await asyncio.sleep(0)
        copywatch._sleep = fast_sleep
        try:
            task = asyncio.create_task(cw._ws_loop(stop))
            item = await asyncio.wait_for(cw.queue.get(), timeout=5)
            stop.set()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            return item
        finally:
            copywatch._sleep = copywatch._sleep_orig

    item = run(go())
    assert item == ("WSIG", W, "ws") and len(connects) >= 2


# ------------------------------------------------------------ trader refresh
HTML = """<tbody>
<tr><td><span class="rank">1</span><span class="mv">–</span></td><td><a class="who" href="/traders/oldguy">@oldguy</a></td><td class="num pos">&#43;$9</td><td class="num hide-sm">$1</td><td class="num hide-sm">2</td><td class="hide-sm"><span class="badge">Verified</span></td><td class="num muted hide-md">48 days ago</td></tr>
<tr><td><span class="rank">2</span><span class="mv">–</span></td><td><a class="who" href="/traders/newa">@newa</a></td><td class="num pos">&#43;$8</td><td class="num hide-sm">$1</td><td class="num hide-sm">2</td><td class="hide-sm"><span class="badge">Verified</span></td><td class="num muted hide-md">3 h ago</td></tr>
<tr><td><span class="rank">3</span><span class="mv">–</span></td><td><a class="who" href="/traders/newb">@newb</a></td><td class="num pos">&#43;$7</td><td class="num hide-sm">$1</td><td class="num hide-sm">2</td><td class="hide-sm"><span class="badge">Verified</span></td><td class="num muted hide-md">1 day ago</td></tr>
<tr><td><span class="rank">4</span><span class="mv">–</span></td><td><a class="who" href="/traders/newc">@newc</a></td><td class="num pos">&#43;$6</td><td class="num hide-sm">$1</td><td class="num hide-sm">2</td><td class="hide-sm"><span class="badge">Verified</span></td><td class="num muted hide-md">5 min ago</td></tr>
</tbody>"""


def test_refresh_respects_two_per_day(db, cfg):
    rows = trader_refresh.parse_leaderboard(HTML)
    assert [r["handle"] for r in rows] == ["oldguy", "newa", "newb", "newc"]
    resolved = []

    def resolve(h):
        resolved.append(h)
        return "4t5cayg6FDWkyQwyuYq9DqcRRCQ7e6JXyf9J7f2wfZQW"

    res = trader_refresh.refresh(db, cfg, None, fetch=lambda: rows, resolve=resolve, verify=lambda r, w: True, today="2026-11-11")
    assert resolved == ["newa", "newb"] and res["unresolved"] == ["newc"]
    again = trader_refresh.refresh(db, cfg, None, fetch=lambda: rows, resolve=resolve, verify=lambda r, w: True, today="2026-11-11")
    assert again == {"skipped": "already today"} and len(resolved) == 2
    trader_refresh.refresh(db, cfg, None, fetch=lambda: rows, resolve=resolve, verify=lambda r, w: True, today="2026-11-12")
    assert resolved == ["newa", "newb", "newc"]


def test_refresh_fails_closed_keeps_list(db, cfg):
    trader_refresh.seed_traders(db)
    before = db.query("SELECT COUNT(*) c FROM copy_traders")[0]["c"]
    assert before == 9

    def boom():
        raise RuntimeError("down")

    assert trader_refresh.refresh(db, cfg, None, fetch=boom, today="2026-11-11") == {"error": "fetch"}
    res = trader_refresh.refresh(db, cfg, None, fetch=lambda: trader_refresh.parse_leaderboard(HTML),
                                 resolve=lambda h: "4t5cayg6FDWkyQwyuYq9DqcRRCQ7e6JXyf9J7f2wfZQW",
                                 verify=lambda r, w: False, today="2026-11-11")
    assert res["added"] == []  # unverified wallet never added
    assert db.query("SELECT COUNT(*) c FROM copy_traders WHERE active=1")[0]["c"] == 9


# ------------------------------------------------------------ dashboard
def test_dashboard_shows_copy_label_and_no_secrets(db, cfg, monkeypatch):
    monkeypatch.setenv("TG_TOKEN", "123456:SECRET_TEST_TOKEN_VALUE")
    cw, n = _cw(db, cfg)
    run(cw.on_trade("zelda89", parse_fomo_trade(tx(), W, "SD")))
    data = dashboard_data.collect(db._connect())
    op = data["open_positions"][0]
    assert op["source_label"] == "копие от zelda89"
    assert data["copy"]["open_count"] == 1 and data["copy"]["mode"] == "PAPER"
    assert any(f.get("source_label") == "копие от zelda89" for f in data["fills"])
    dumped = json.dumps(data, ensure_ascii=False)
    assert "SECRET_TEST_TOKEN_VALUE" not in dumped and W not in dumped


def test_unverified_wallet_kept_inactive_then_reverified_without_budget(db, cfg):
    rows = trader_refresh.parse_leaderboard(HTML)
    calls = []
    res = trader_refresh.refresh(db, cfg, None, fetch=lambda: rows[:2],
                                 resolve=lambda h: calls.append(h) or "4t5cayg6FDWkyQwyuYq9DqcRRCQ7e6JXyf9J7f2wfZQW",
                                 verify=lambda r, w: False, today="2026-11-11")
    row = db.query_one("SELECT * FROM copy_traders WHERE handle='newa'")
    assert row["active"] == 0 and row["wallet"] and res["added"] == []
    res = trader_refresh.refresh(db, cfg, None, fetch=lambda: rows[:2],
                                 resolve=lambda h: calls.append(h) or "x", verify=lambda r, w: True, today="2026-11-12")
    assert calls == ["newa"] and res["added"] == ["newa"]
    assert db.query_one("SELECT active FROM copy_traders WHERE handle='newa'")["active"] == 1


def test_copyable_traders_only_winnable(db, cfg):
    from copytrade import copyable_traders
    w1, w2 = "4t5cayg6FDWkyQwyuYq9DqcRRCQ7e6JXyf9J7f2wfZQW", "AH75Bn6QHSt5K2Ad4p1otns3JTwpSJWZTwiwKWNhmnKx"
    db.execute("INSERT INTO copy_traders(handle, wallet, active, pinned, winnable) VALUES(?,?,1,0,1)", ("win", w1))
    db.execute("INSERT INTO copy_traders(handle, wallet, active, pinned, winnable) VALUES(?,?,1,0,0)", ("lose", w2))
    assert {r["handle"] for r in copyable_traders(db, cfg)} == {"win", "lose"}
    db.kv_set("setting.copy.only_winnable", "true")
    assert {r["handle"] for r in copyable_traders(db, cfg)} == {"win"}


def test_refresh_does_not_reactivate_non_winnable(db, cfg):
    db.kv_set("setting.copy.only_winnable", "true")
    wallet = "4t5cayg6FDWkyQwyuYq9DqcRRCQ7e6JXyf9J7f2wfZQW"
    db.execute(
        "INSERT INTO copy_traders(handle, wallet, active, pinned, winnable, source, updated_at) VALUES(?,?,0,1,0,?,?)",
        ("newa", wallet, "test", "t"),
    )
    rows = trader_refresh.parse_leaderboard(HTML)
    res = trader_refresh.refresh(db, cfg, None, fetch=lambda: rows, resolve=lambda h: wallet,
                                 verify=lambda r, w: True, today="2026-11-11")
    assert db.query_one("SELECT active FROM copy_traders WHERE handle='newa'")["active"] == 0
    newb = db.query_one("SELECT * FROM copy_traders WHERE handle='newb'")
    assert newb and newb["active"] == 0 and newb["wallet"]
    assert "newa" not in res["added"] and "newb" not in res["added"]


# ------------------------------------------------------------ entry gate (meme-strategy upgrade)
def _kv(db, key, val):
    db.kv_set("setting." + key, json.dumps(val))


@pytest.mark.parametrize("attrs,price,expected", [
    ({"liq": 5000}, 0.01, "gate_low_liq"),
    ({"liq": None}, 0.01, "gate_low_liq"),
    ({}, 0.0106, "gate_chase"),  # +6% above the trader's 0.01 fill
    ({"m5": -12.0}, 0.01, "gate_dumping"),
    ({"m5": None}, 0.01, "gate_no_m5"),
    ({"buys": 10, "sells": 40}, 0.01, "gate_sell_pressure"),
    ({"buys": None}, 0.01, "gate_no_flow"),
])
def test_copy_gate_skips_quietly_and_fails_closed(db, cfg, attrs, price, expected):
    cw, n = _cw(db, cfg, price=price)
    for k, v in attrs.items():
        setattr(cw.sources, k, v)
    before = len(n.sent)
    assert run(cw.on_trade("zelda89", parse_fomo_trade(tx(), W, "SG"))) == expected
    assert not db.open_positions()
    assert len(n.sent) == before  # quiet skip, no Telegram push


def test_copy_gate_passes_healthy_and_small_chase(db, cfg):
    cw, n = _cw(db, cfg, price=0.0104)  # +4% over the trader: still inside the 5% chase limit
    assert run(cw.on_trade("zelda89", parse_fomo_trade(tx(), W, "SOK"))) == "copied"


def test_copy_gate_off_restores_old_behaviour(db, cfg):
    _kv(db, "copy.gate_enabled", False)
    cw, n = _cw(db, cfg)
    cw.sources.liq, cw.sources.m5 = 100, -50.0
    assert run(cw.on_trade("zelda89", parse_fomo_trade(tx(), W, "SOFF"))) == "copied"


def test_copy_gate_caps_pileups(db, cfg):
    cw, n = _cw(db, cfg)
    _kv(db, "copy.max_open_per_trader", 1)
    assert run(cw.on_trade("zelda89", parse_fomo_trade(tx(), W, "P1"))) == "copied"
    other = "So11111111111111111111111111111111111111112"
    assert run(cw.on_trade("zelda89", parse_fomo_trade(tx(mint=other), W, "P2"))) == "gate_trader_pileup"
    _kv(db, "copy.max_open_per_trader", 0)
    _kv(db, "copy.max_open", 1)
    assert run(cw.on_trade("zelda89", parse_fomo_trade(tx(mint=other), W, "P3"))) == "gate_max_open"
    assert len(db.open_positions()) == 1


def test_copy_stop_basis_default_fill_and_market_flag(db, cfg):
    cfg.set("risk.stop_pct", "1", who="test")
    cw, n = _cw(db, cfg)
    run(cw.on_trade("zelda89", parse_fomo_trade(tx(), W, "B1")))
    pos = db.open_positions()[0]
    assert pos["stop_price"] == pytest.approx(0.01 * 1.015 * 0.99)  # default unchanged: 1% under our fill
    db.update_position(pos["id"], status="closed")
    _kv(db, "copy.stop_basis", "market")
    other = "So11111111111111111111111111111111111111112"
    run(cw.on_trade("zelda89", parse_fomo_trade(tx(mint=other), W, "B2")))
    pos2 = db.open_positions()[0]
    assert pos2["stop_price"] == pytest.approx(0.01 * 0.99)  # 1% under the market price


def test_copy_gate_skips_bonding_curve_without_liquidity(db, cfg):
    cw, n = _cw(db, cfg)
    orig = cw.sources.any_dex_pairs

    def curve(mints):
        out = orig(mints)
        for v in out.values():
            v["dexId"] = "pumpfun"
            v.pop("liquidity", None)
        return out

    cw.sources.any_dex_pairs = curve
    assert run(cw.on_trade("zelda89", parse_fomo_trade(tx(), W, "BC"))) == "gate_bonding_curve"
    assert not db.open_positions()


# ------------------------------------------------------------ GitHub-repo upgrade (2026-11-13)
def test_sell_fraction_from_trader_balance():
    from copytrade import sell_fraction

    s = parse_fomo_trade(tx(usdc_delta=4.0, tok_delta=-200.0), W)  # trader held 500, sold 200
    assert s["pre_token_amount"] == pytest.approx(500.0)
    assert sell_fraction(s) == pytest.approx(0.4)
    assert sell_fraction({"token_amount": 5}) == 1.0  # unknown balance -> full exit


def test_partial_trader_sell_closes_same_share_of_copy(db, cfg):
    cw, n = _cw(db, cfg)
    _kv(db, "copy.size_usd", 20.0)
    run(cw.on_trade("zelda89", parse_fomo_trade(tx(), W, "PB")))
    cw.sources.price = 0.02
    bal0 = db.last_balance()
    act = run(cw.on_trade("zelda89", parse_fomo_trade(tx(usdc_delta=4.0, tok_delta=-200.0), W, "PS1")))
    assert act == "partial_copy"
    open_ = db.open_positions()
    assert len(open_) == 1 and open_[0]["size_usd"] == pytest.approx(12.0)
    part = db.query_one("SELECT * FROM positions WHERE exit_reason='trader_partial'")
    assert part["size_usd"] == pytest.approx(8.0) and part["pnl_usd"] > 0 and part["status"] == "closed"
    assert db.last_balance() == pytest.approx(bal0 + part["pnl_usd"])
    assert "продадох 40% от копието" in n.sent[-1]["text"]
    assert "r=savipww" in n.sent[-1]["buttons"][0]["url"]
    # the rest goes when the trader dumps the bag
    assert run(cw.on_trade("zelda89", parse_fomo_trade(tx(usdc_delta=10.0, tok_delta=-500.0), W, "PS2"))) == "closed_copy"
    assert not db.open_positions()


def test_partial_sells_flag_off_closes_whole_copy(db, cfg):
    cw, n = _cw(db, cfg)
    _kv(db, "copy.follow_partial_sells", False)
    run(cw.on_trade("zelda89", parse_fomo_trade(tx(), W, "FB")))
    assert run(cw.on_trade("zelda89", parse_fomo_trade(tx(usdc_delta=4.0, tok_delta=-200.0), W, "FS"))) == "closed_copy"
    assert not db.open_positions()


def test_tiny_remainder_is_closed_fully(db, cfg):
    cw, n = _cw(db, cfg)  # default copy size $10 in tests
    _kv(db, "copy.partial_min_usd", 9.0)
    run(cw.on_trade("zelda89", parse_fomo_trade(tx(), W, "TB")))
    assert run(cw.on_trade("zelda89", parse_fomo_trade(tx(usdc_delta=4.0, tok_delta=-200.0), W, "TS"))) == "closed_copy"


def test_top10_gate_off_by_default_and_fails_closed_when_on(db, cfg, monkeypatch):
    import agents.scanner as scanner

    cw, n = _cw(db, cfg)
    calls = []
    monkeypatch.setattr(scanner, "_top10", lambda *a: calls.append(a) or None)
    assert run(cw.on_trade("zelda89", parse_fomo_trade(tx(), W, "H1"))) == "copied"
    assert calls == []  # off: no RPC reads
    db.execute("UPDATE positions SET status='closed'")
    _kv(db, "copy.max_top10_pct", 30)
    assert run(cw.on_trade("zelda89", parse_fomo_trade(tx(), W, "H2"))) == "gate_no_holders"
    monkeypatch.setattr(scanner, "_top10", lambda *a: 55.0)
    assert run(cw.on_trade("zelda89", parse_fomo_trade(tx(), W, "H3"))) == "gate_top10"
    monkeypatch.setattr(scanner, "_top10", lambda *a: 12.0)
    assert run(cw.on_trade("zelda89", parse_fomo_trade(tx(), W, "H4"))) == "copied"


def test_gate_skip_writes_counterfactual(db, cfg):
    import util

    _kv(db, "copy.gate_followup_min", 0)
    cw, n = _cw(db, cfg)
    cw.sources.liq = 100
    assert run(cw.on_trade("zelda89", parse_fomo_trade(tx(), W, "CF"))) == "gate_low_liq"
    rows = [json.loads(l) for l in (util.LOG_DIR / "gate_counterfactual.jsonl").read_text().splitlines()]
    assert rows[-1]["reason"] == "gate_low_liq" and rows[-1]["mint"] == MINT and rows[-1]["price"] == 0.01


def test_backtest_concentration_audit():
    c = backtest.concentration({"A": 90.0, "B": 10.0, "C": 5.0, "D": -20.0})
    assert c["top1_share_pct"] == pytest.approx(105.9) and c["pnl_ex_top3"] == pytest.approx(-20.0)
    assert backtest.concentration({"A": -5.0})["top1_share_pct"] is None


def test_risk_stop_basis_applies_to_copies(db, cfg):
    """Shared risk.stop_basis=market is enough; copy.stop_basis need not be set."""
    cfg.set("risk.stop_pct", "1", who="test")
    _kv(db, "risk.stop_basis", "market")
    cw, n = _cw(db, cfg)
    other = "So11111111111111111111111111111111111111113"
    run(cw.on_trade("zelda89", parse_fomo_trade(tx(mint=other), W, "B3")))
    pos = db.open_positions()[0]
    assert pos["stop_price"] == pytest.approx(0.01 * 0.99)  # 1% under market, not 1.015 fill
    assert pos["entry_price"] == pytest.approx(0.01 * 1.015)


def test_copy_disabled_skips_new_entries(db, cfg):
    cw, n = _cw(db, cfg)
    _kv(db, "copy.enabled", False)
    assert run(cw.on_trade("zelda89", parse_fomo_trade(tx(), W, "OFF"))) == "disabled"
    assert not db.open_positions()

"""Quiet mode, wake summary, PAPER stop & sell all, owner lock on new commands."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone

import pytest

import dashboard_data
import session
from telegram import TelegramNotifier
from util import iso_utc

OWNER = 7659480276
STRANGER = 111222333
NOW = datetime(2026, 11, 11, 20, 0, tzinfo=timezone.utc)


class FakeNotifier(TelegramNotifier):
    """Real command/quiet logic; captures what would be sent instead of calling Telegram."""

    def __init__(self, db, cfg):
        super().__init__(token="x", owner_chat_id=OWNER, db=db, cfg=cfg)
        self.sent: list[dict] = []

    def send_sync(self, text, buttons=None, chat_id=None):
        self.sent.append({"text": text, "buttons": buttons})
        return True

    def _answer_callback(self, callback_id):
        return None


class FakeSources:
    def __init__(self, prices):
        self.prices = prices

    def pumpswap_pairs(self, mints):
        return {m: {"priceUsd": str(self.prices[m]), "liquidity": {"usd": 50000}} for m in mints if m in self.prices}


def _msg(text, chat=OWNER):
    return {"update_id": 1, "message": {"chat": {"id": chat}, "from": {"id": chat}, "text": text}}


def _cb(data, chat=OWNER):
    return {"update_id": 2, "callback_query": {"id": "c1", "data": data, "from": {"id": chat}, "message": {"chat": {"id": chat}}}}


def _open(db, mint, entry=1.0, size=47.5, name=None, mode="PAPER"):
    return db.open_position(mint=mint, entry_price=entry, size_usd=size, stop_price=entry * 0.94, mode=mode, name=name or mint)


def run(coro):
    return asyncio.run(coro)


# ------------------------------------------------------------ quiet queue
def test_quiet_holds_trade_pushes_in_db(db, cfg):
    db.add_balance(200.0)
    n = FakeNotifier(db, cfg)
    run(n._handle_update(_msg("/quiet")))
    assert session.is_quiet(db)
    assert len(n.sent) == 1 and "Тих режим" in n.sent[0]["text"]  # command reply goes through
    # agent pushes (not command replies) are held
    assert run(n.send("PAPER · Buy ABC · $47.50", buttons=[{"text": "Buy on FOMO", "url": "https://fomo.family/x"}], mint="M1")) is True
    assert run(n.send("PAPER · Sold ABC · +$3.00", mint="M1")) is True
    assert len(n.sent) == 1
    held = db.queued_pushes()
    assert [h["text"] for h in held] == ["PAPER · Buy ABC · $47.50", "PAPER · Sold ABC · +$3.00"]


def test_critical_alert_bypasses_quiet(db, cfg):
    n = FakeNotifier(db, cfg)
    session.start_quiet(db, cfg)
    run(n.send("desk-health: restarted main.py", critical=True))
    assert n.sent and "restarted" in n.sent[-1]["text"]
    assert db.queued_pushes() == []


def test_quiet_hours_auto_wake(db, cfg):
    db.add_balance(200.0)
    n = FakeNotifier(db, cfg)
    real_now = datetime.now(timezone.utc)
    session.start_quiet(db, cfg, hours=2, now=real_now - timedelta(hours=3))
    assert session.due_auto_wake(db, now=real_now)
    assert run(n.check_auto_wake()) is True
    assert not session.is_quiet(db)
    assert len(n.sent) == 1 and "Обобщение" in n.sent[0]["text"]


def test_quiet_command_with_hours(db, cfg):
    n = FakeNotifier(db, cfg)
    run(n._handle_update(_msg("/quiet 8")))
    st = session.quiet_state(db)
    assert st["on"] and st["until"]


# ------------------------------------------------------------ wake summary
def test_wake_sends_one_summary(db, cfg):
    db.add_balance(200.0, ts=iso_utc(NOW - timedelta(hours=5)))
    n = FakeNotifier(db, cfg)
    session.start_quiet(db, cfg, now=NOW - timedelta(hours=4))
    # two trades closed during quiet: one win, one stop
    p1 = _open(db, "MintWIN", name="WIN")
    db.update_position(p1, status="closed", closed_at=iso_utc(NOW - timedelta(hours=2)), pnl_usd=30.0, exit_reason="trail")
    p2 = _open(db, "MintLOSS", name="LOSS")
    db.update_position(p2, status="closed", closed_at=iso_utc(NOW - timedelta(hours=1)), pnl_usd=-4.0, exit_reason="stop")
    db.add_balance(226.0, ts=iso_utc(NOW - timedelta(minutes=30)))
    _open(db, "MintOPEN", name="OPEN")
    run(n.send("held 1", mint="MintWIN"))
    run(n.send("held 2", mint="MintLOSS"))
    run(n._handle_update(_msg("/wake")))
    assert len(n.sent) == 1, "wake must send exactly ONE message"
    t = n.sent[0]["text"]
    assert "$200.00 → $226.00" in t
    assert "×1.13" in t
    assert "Сделки: 2" in t and "зелени 1" in t and "червени 1" in t and "стопове 1" in t
    assert "WIN +$30.00" in t
    assert "Още отворени: 1" in t and "OPEN" in t
    assert "Задържани известия: 2" in t
    assert "#g=" in t or "fomo-desk-dashboard" in t
    assert "r=savipww" in t
    assert not session.is_quiet(db)
    assert db.queued_pushes() == []


# ------------------------------------------------------------ sell all
def test_sellall_requires_confirm_then_closes_and_pauses(db, cfg):
    db.add_balance(200.0)
    _open(db, "MintA", entry=1.0, size=47.5, name="AAA")
    _open(db, "MintB", entry=2.0, size=47.5, name="BBB")
    n = FakeNotifier(db, cfg)
    n.sources = FakeSources({"MintA": 1.2, "MintB": 1.8})
    run(n._handle_update(_msg("/sellall")))
    # nothing closed yet: only a confirm prompt with buttons
    assert len(db.open_positions()) == 2
    btns = n.sent[-1]["buttons"]
    assert any(b.get("callback") == "sellall:yes" for b in btns)
    run(n._handle_update(_cb("sellall:yes")))
    assert db.open_positions() == []
    rows = [dict(r) for r in db.query("SELECT * FROM positions ORDER BY id")]
    assert all(r["status"] == "closed" and r["exit_reason"] == "sellall" for r in rows)
    assert rows[0]["exit_price"] == 1.2 and rows[1]["exit_price"] == 1.8
    assert rows[0]["pnl_usd"] > 0 and rows[1]["pnl_usd"] < 0
    expected = round(200.0 + rows[0]["pnl_usd"] + rows[1]["pnl_usd"], 2)
    assert db.last_balance() == pytest.approx(expected)
    assert cfg.is_paused("executor")
    assert "пауза" in n.sent[-1]["text"]
    run(n._handle_update(_msg("/resume")))
    assert not cfg.is_paused("executor")


def test_start_sellall_deep_link_still_needs_confirm(db, cfg):
    db.add_balance(200.0)
    _open(db, "MintA", name="AAA")
    n = FakeNotifier(db, cfg)
    n.sources = FakeSources({"MintA": 1.1})
    run(n._handle_update(_msg("/start sellall")))
    assert len(db.open_positions()) == 1
    assert "Спри и продай всичко?" in n.sent[-1]["text"]
    run(n._handle_update(_cb("sellall:no")))
    assert len(db.open_positions()) == 1
    assert not cfg.is_paused("executor")


def test_sellall_no_price_keeps_position_open(db, cfg):
    db.add_balance(200.0)
    _open(db, "MintA", name="AAA")
    n = FakeNotifier(db, cfg)
    n.sources = FakeSources({})
    run(n._handle_update(_cb("sellall:yes")))
    assert len(db.open_positions()) == 1
    assert db.last_balance() == 200.0
    assert "няма цена" in n.sent[-1]["text"]


def test_sellall_never_touches_live(db, cfg):
    db.add_balance(200.0)
    _open(db, "MintL", name="LIVEONE", mode="LIVE")
    res = session.sell_all_paper(db, cfg, {"MintL": {"priceUsd": "2"}})
    assert res["closed"] == [] and len(res["live"]) == 1
    assert len(db.open_positions()) == 1


def test_executor_paused_after_sellall_drops_new_entries(db, cfg):
    from agents import executor
    from bus import Bus

    db.add_balance(200.0)
    session.sell_all_paper(db, cfg, {})
    bus = Bus()

    async def go():
        stop = asyncio.Event()
        await bus.executor.put({"mint": "MintNEW", "pair": {"priceUsd": "1", "liquidity": {"usd": 90000}}})
        task = asyncio.create_task(executor.run_agent(db, cfg, bus, stop_event=stop))
        await asyncio.sleep(0.3)
        stop.set()
        await asyncio.wait_for(task, 5)

    run(go())
    assert db.open_positions() == []


# ------------------------------------------------------------ owner lock
@pytest.mark.parametrize("text", ["/quiet", "/quiet 4", "/wake", "/sellall", "/start sellall", "/resume"])
def test_owner_lock_on_new_commands(db, cfg, text):
    db.add_balance(200.0)
    _open(db, "MintA", name="AAA")
    cfg.pause("executor")
    n = FakeNotifier(db, cfg)
    n.sources = FakeSources({"MintA": 1.5})
    run(n._handle_update(_msg(text, chat=STRANGER)))
    run(n._handle_update(_cb("sellall:yes", chat=STRANGER)))
    assert n.sent == []
    assert not session.is_quiet(db)
    assert len(db.open_positions()) == 1
    assert cfg.is_paused("executor")


# ------------------------------------------------------------ dashboard feed
def test_dashboard_feed_has_video_blocks_and_no_secrets(db, cfg, tmp_path):
    db.add_balance(200.0, ts=iso_utc(NOW - timedelta(hours=2)))
    p = _open(db, "MintWIN", name="WIN")
    db.update_position(p, status="closed", closed_at=iso_utc(NOW - timedelta(hours=1)), pnl_usd=12.5, exit_reason="trail", exit_price=1.3)
    db.add_balance(212.5, ts=iso_utc(NOW - timedelta(hours=1)))
    for i in range(400):
        db.add_equity(200 + i * 0.01, 200, 0, ts=iso_utc(NOW - timedelta(minutes=400 - i)))
    conn = dashboard_data.connect_ro(db.path)
    try:
        data = dashboard_data.collect(conn)
    finally:
        conn.close()
    assert len(data["equity_series"]) <= dashboard_data.MAX_SERIES
    s = data["session"]
    assert s["trades"] == 1 and s["wins"] == 1 and s["multiplier"] == pytest.approx(1.0625)
    assert s["best"][0]["fomo_url"].endswith("&r=savipww&source=share_link")
    assert data["controls"]["sellall_url"] in (None, "https://t.me/xeo_fomo_bot?start=sellall")
    text = json.dumps(data)
    for bad in ("7659480276", "ghp_", "github_pat_", "TG_TOKEN", "APP_TOKEN", "VAPID", "JUPITER_API_KEY"):
        assert bad not in text

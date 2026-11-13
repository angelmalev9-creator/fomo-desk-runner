"""Hard LIVE gate: win rate over closed PAPER trades, plus the existing night rule."""

from __future__ import annotations

import json

import pytest

import dashboard_data
import live_gate
from telegram import TelegramNotifier
from util import iso_utc


OWNER = 7659480276


class FakeNotifier(TelegramNotifier):
    def __init__(self, db, cfg):
        super().__init__(token="x", owner_chat_id=OWNER, db=db, cfg=cfg)
        self.sent: list[dict] = []

    def send_sync(self, text, buttons=None, chat_id=None):
        self.sent.append({"text": text, "buttons": buttons})
        return True

    def _answer_callback(self, callback_id):
        return None


def _msg(text, chat=OWNER):
    return {"update_id": 1, "message": {"chat": {"id": chat}, "from": {"id": chat}, "text": text}}


def _close(db, mint, pnl, mode="PAPER", status="closed", source="momentum"):
    pid = db.open_position(
        mint=mint, entry_price=1.0, size_usd=20.0, stop_price=0.99, mode=mode, name=mint, source=source
    )
    db.update_position(pid, status=status, closed_at=iso_utc(), pnl_usd=pnl, exit_reason="stop", exit_price=1.0)
    return pid


def _nights(db, n=1):
    for i in range(n):
        nid = db.start_night("PAPER", 200.0)
        db.end_night(nid, 200.0)


def _wins_and_losses(db, wins, losses, prefix="T"):
    for i in range(wins):
        _close(db, f"{prefix}W{i}", 1.0)
    for i in range(losses):
        _close(db, f"{prefix}L{i}", -1.0)


# ---------------------------------------------------------------- scoring
def test_win_is_strictly_positive_pnl_after_fees(db, cfg):
    _close(db, "WIN", 0.01)
    _close(db, "EVEN", 0.0)
    _close(db, "LOSS", -0.50)
    _close(db, "LIVEW", 5.0, mode="LIVE")
    _close(db, "SKIP", 3.0, status="cancelled")
    info = live_gate.evaluate(db, cfg)
    # only the PAPER closed rows with a pnl: WIN, EVEN, LOSS (LIVE and cancelled excluded)
    assert info["closed"] == 3 and info["wins"] == 1
    assert info["win_rate_pct"] == pytest.approx(33.3)
    assert info["ok"] is False and info["nights_ok"] is False and info["gate_ok"] is False


def test_counts_all_agents_and_nights(db, cfg):
    _close(db, "A", 1.0, source="copy")
    _close(db, "B", 1.0, source="dip")
    _close(db, "C", 1.0, source=None)  # sniper
    _close(db, "D", -1.0, source="momentum")
    info = live_gate.evaluate(db, cfg)
    assert info["closed"] == 4 and info["wins"] == 3


def test_needs_at_least_20_closed_even_if_rate_is_perfect(db, cfg):
    _nights(db)
    _wins_and_losses(db, 19, 0)
    info = live_gate.evaluate(db, cfg)
    assert info["nights_ok"] is True
    assert info["closed"] == 19 and info["win_rate_pct"] == 100.0
    assert info["trades_ok"] is False and info["ok"] is False
    text = live_gate.refuse_live(db, cfg)
    assert "LIVE е заключен" in text and "19" in text and "100.0%" in text and "20" in text


def test_20_trades_at_85_percent_passes_with_a_night(db, cfg):
    _nights(db)
    _wins_and_losses(db, 17, 3)  # 85%
    info = live_gate.evaluate(db, cfg)
    assert info["closed"] == 20 and info["wins"] == 17
    assert info["win_rate_pct"] == 85.0
    assert info["ok"] is True
    assert live_gate.refuse_live(db, cfg) is None


def test_20_trades_at_80_percent_fails(db, cfg):
    _nights(db)
    _wins_and_losses(db, 16, 4)
    info = live_gate.evaluate(db, cfg)
    assert info["win_rate_pct"] == 80.0 and info["ok"] is False and info["nights_ok"] is True
    assert "80.0%" in live_gate.refuse_live(db, cfg)


def test_night_gate_message_unchanged_when_nights_missing(db, cfg):
    _wins_and_losses(db, 20, 0)
    assert live_gate.refuse_live(db, cfg) == "LIVE needs at least 1 full PAPER night in the db first."


def test_thresholds_cannot_be_lowered_below_floors(db, cfg):
    db.kv_set("setting.live.min_win_rate_pct", json.dumps(10.0))
    db.kv_set("setting.live.min_closed_trades", json.dumps(1))
    rate, n = live_gate.thresholds(cfg, db)
    assert rate == 85.0 and n == 20
    # raising them is allowed (stricter)
    db.kv_set("setting.live.min_win_rate_pct", json.dumps(90.0))
    db.kv_set("setting.live.min_closed_trades", json.dumps(25))
    rate, n = live_gate.thresholds(cfg, db)
    assert rate == 90.0 and n == 25


# ---------------------------------------------------------------- telegram + Config.set
def test_live_command_without_a_night_keeps_existing_message(db, cfg):
    import asyncio

    n = FakeNotifier(db, cfg)
    asyncio.run(n._handle_update(_msg("live")))
    assert n.sent[-1]["text"] == "LIVE needs at least 1 full PAPER night in the db first."
    assert cfg.get("desk.mode") == "PAPER"
    assert n._awaiting_live is False


def test_live_command_locked_when_win_rate_fails(db, cfg):
    import asyncio

    _nights(db)
    _wins_and_losses(db, 3, 25)  # 10.7% of 28
    n = FakeNotifier(db, cfg)
    asyncio.run(n._handle_update(_msg("live")))
    text = n.sent[-1]["text"]
    assert "LIVE е заключен" in text
    assert "10.7%" in text and "3 от 28" in text and "85%" in text and "20" in text
    assert cfg.get("desk.mode") == "PAPER"
    assert n._awaiting_live is False
    # typing LIVE itself is also refused (no confirm step was opened)
    asyncio.run(n._handle_update(_msg("LIVE")))
    # without awaiting, "LIVE" is an unknown command — mode stays PAPER
    assert cfg.get("desk.mode") == "PAPER"


def test_confirmed_live_refused_if_gate_breaks_after_prompt(db, cfg):
    import asyncio

    _nights(db)
    _wins_and_losses(db, 17, 3)
    n = FakeNotifier(db, cfg)
    asyncio.run(n._handle_update(_msg("live")))
    assert n.sent[-1]["text"] == "Type LIVE to confirm."
    assert n._awaiting_live is True
    # a losing trade lands before the owner types LIVE
    _close(db, "LATE", -1.0)
    asyncio.run(n._handle_update(_msg("LIVE")))
    assert "LIVE е заключен" in n.sent[-1]["text"]
    assert "81.0%" in n.sent[-1]["text"]  # 17/21
    assert cfg.get("desk.mode") == "PAPER"


def test_confirmed_live_allowed_when_all_gates_pass(db, cfg):
    import asyncio

    _nights(db)
    _wins_and_losses(db, 18, 2)
    n = FakeNotifier(db, cfg)
    asyncio.run(n._handle_update(_msg("live")))
    asyncio.run(n._handle_update(_msg("LIVE")))
    assert n.sent[-1]["text"] == "Mode is LIVE."
    assert cfg.get("desk.mode") == "LIVE"


def test_set_mode_live_is_also_refused(db, cfg):
    with pytest.raises(ValueError, match="LIVE needs at least 1 full PAPER night"):
        cfg.set("desk.mode", "LIVE", who="test")
    assert cfg.get("desk.mode") == "PAPER"
    _nights(db)
    _wins_and_losses(db, 1, 20)
    with pytest.raises(ValueError, match="LIVE е заключен"):
        cfg.set("mode", "LIVE", who="test")
    assert cfg.get("desk.mode") == "PAPER"


def test_dashboard_includes_live_gate_line(db, cfg):
    _nights(db)
    _wins_and_losses(db, 3, 25)
    conn = dashboard_data.connect_ro(db.path)
    try:
        data = dashboard_data.collect(conn)
    finally:
        conn.close()
    gate = data["desk4"]["live_gate"]
    assert gate["line"] == "LIVE gate: 10.7% / 85% · 28/20 trades"
    assert gate["ok"] is False and gate["closed"] == 28 and gate["wins"] == 3
    assert "7659480276" not in json.dumps(data)

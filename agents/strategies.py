"""Four-agent PAPER desk: sleeves + two new strategy agents (Momentum, Dip Hunter).

Agents and their position `source`:
  Sniper     -> scanner/executor path (source NULL or 'scanner'): migration pairs, auditor 8/8 + analyst
  Momentum   -> 'momentum' (NEW): volume breakout that breaks its 1h high
  Dip Hunter -> 'dip' (NEW): buys a sharp wick (~-30% off the recent high) that is bouncing
  Quiet Breakout -> 'quiet': only calm coins (median 1m range <= quiet.max_noise_pct, so a 1% stop is
               not pure noise) that close above their 30m high on >= 2x volume
  Whale Tail -> 'copy': copywatch on the four winnable FOMO traders

Each agent trades only inside its own sleeve (default 25% of the PAPER stack); one agent's stop
never blocks another agent. PAPER only: nothing here places an order, signs, swaps or touches a
key. FOMO links come only from links.py. Every data gap fails closed (no trade).

Data (all free, read-only): Jupiter token lists/stats (x-api-key, ~1 rps, shared limiter),
DexScreener pairs, GeckoTerminal 1m candles (2.2 s gap), RugCheck report (1.5 s gap).
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from typing import Any

import links
import session
from agents.risk import stop_price_at_entry
from fields import dex_float, rugcheck_market
from util import agent_logger, as_float, iso_utc, utcnow

log = agent_logger("strategies")

AGENT_ORDER = ("sniper", "momentum", "dip", "quiet", "whale")
AGENT_LABEL = {"sniper": "Sniper", "momentum": "Momentum", "dip": "Dip Hunter", "quiet": "Quiet Breakout",
               "whale": "Whale Tail"}
AGENT_SUB = {"sniper": "fresh migrations", "momentum": "volume breakouts", "dip": "−30% wicks",
             "quiet": "calm breakouts", "whale": "smart wallets"}
MARKET_AGENTS = ("momentum", "dip", "quiet")  # agents fed by MarketAgents.cycle()
QUIET_MAX_CHASE_PCT = 3.0  # live price may be at most this far above the signal candle close
QUIET_MIGRATION_POOL = 50  # recent migrations (age >= quiet.min_age_min, <= 16 h) added to the universe
QUIET_MIGRATION_MAX_AGE_H = 16

# Not memes: never traded by Momentum / Dip Hunter.
NON_MEME = {
    "So11111111111111111111111111111111111111112",  # SOL
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",  # USDC
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",  # USDT
    "cbbtcf3aa214zXHbiAZQwf4122FBYbraNdFqgw4iMij",  # cbBTC
    "3NZ9JMVBmGAqocybic2c7LQCJScmgsAZ6vQqTDzcqmJh",  # WBTC
    "7vfCXTUXx5WJV5JADk17DUJ4ksgau7utNKj4b963voxs",  # ETH (Wormhole)
    "J1toso1uCk3RLmjorhTtrVwY9HJ7X8V9yYac6Y7kGCPn",  # JitoSOL
    "mSoLzYCxHdYgdzU16g5QSh3i5K3z3KZK7ytfqcJm7So",  # mSOL
    "jupSoLaHXQiZZTSfEWMTRRgpnyFm8f6sZdosWBjx93v",  # JupSOL
    "bSo13r4TkiE4KumL71LsHTPpL2euBYLFx6h9HP3piy1",  # bSOL
    "2b1kV6DkPAnxd5ixfnxCpjxmKwqjjaYmCZfHsFu24GXo",  # PYUSD
    "USD1ttGY1N17NEEHLmELoaybftRBUSErhqYiQzvEmuB",  # USD1
}

# Ticker tape (real prices, Jupiter). Unknown mints just drop out.
TICKER_MINTS = [
    "JUPyiwrYJFskUPiHa7hkeR8VUtAeFoSYbKedZNsDvCN",  # JUP
    "pumpCmXqMfrsAkQ5r49WcJnRayYRqmXz6ae8H7H9Dfn",  # PUMP
    "6p6xgHyF7AeE6TZkSmFsko444wqoP15icUSqi2jfGiPN",  # TRUMP
    "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263",  # BONK
    "EKpQGSJtjMFqKZ9KQanSqYXRcF8fBopzLHYxdM65zcjm",  # WIF
    "7GCihgDB8fe6KNjn2MYtkzZcRjQy3t9GHdC8uHYmW2hr",  # POPCAT
    "9BB6NFEcjBCtnNLFko2FqVQBq8HHM13kCyYcdQbgpump",  # FARTCOIN
]

DEFAULTS: dict[str, Any] = {
    # sleeves (all four agents)
    "agents.sleeves_enabled": True,
    "agents.sleeve_pct": 25.0,  # share of the PAPER stack each agent may have open
    "agents.max_position_pct": 26.0,  # hard cap for any single position, % of stack
    # shared market loop for Momentum + Dip Hunter
    "agents.enabled": True,
    "agents.poll_s": 60,
    "agents.min_liquidity_usd": 8000.0,  # same floor as the scanner
    "agents.max_mcap_usd": 300_000_000.0,
    "agents.max_top10_pct": 30.0,  # Jupiter audit.topHoldersPercentage; missing -> skip
    "agents.max_dev_pct": 5.0,  # RugCheck creatorBalance / supply; missing -> skip
    "agents.min_lp_locked_pct": 90.0,  # RugCheck market LP locked/burned %; missing -> skip
    "agents.max_checks_per_cycle": 6,  # deep checks (candles + RugCheck) per minute
    "agents.alert_buys": True,  # Telegram buy alert (respects quiet mode)
    "agents.cooldown_h": 6.0,  # do not re-enter the same coin within this window (per agent)
    "agents.ticker_enabled": True,
    "agents.summary_min": 10.0,  # one 'Scanned N pairs' activity row per agent per N minutes
    # Momentum
    "momentum.enabled": True,
    "momentum.size_usd": 20.0,
    "momentum.min_m5_pct": 5.0,
    "momentum.max_m5_pct": 40.0,  # already parabolic -> no chase
    "momentum.vol_spike_x": 3.0,  # 5m volume vs the 1h average 5m volume
    "momentum.min_buy_ratio": 0.55,  # 5m buy volume / total
    "momentum.min_organic_score": 25.0,
    "momentum.require_1h_high": True,
    "momentum.max_per_hour": 3,
    "momentum.tp_pct": 40.0,  # profit target (0 = off)
    "momentum.fade_exit": True,  # 'volume faded' exit
    "momentum.fade_ratio": 0.30,  # m5 volume below 30% of entry m5 volume
    "momentum.fade_min_age_min": 10,
    # Dip Hunter
    "dip.enabled": True,
    "dip.size_usd": 20.0,
    "dip.min_drop_pct": 30.0,  # wick low at least this far under the lookback high
    "dip.lookback_min": 60,
    "dip.min_bounce_pct": 3.0,  # price back above the wick low by this much (bids stepping in)
    "dip.max_recovered_pct": 60.0,  # skip if more than 60% of the drop is already recovered
    "dip.min_buy_ratio": 0.50,
    "dip.max_liq_drop_1h_pct": 20.0,
    "dip.max_holder_drop_1h_pct": 10.0,
    "dip.max_per_hour": 3,
    "dip.tp_pct": 30.0,
    "dip.fade_exit": False,
    "dip.fade_ratio": 0.30,
    "dip.fade_min_age_min": 10,
    # Quiet Breakout (backtest candidate C, /workspace/strat): desk exits only, no TP / fade
    "quiet.enabled": True,
    "quiet.max_noise_pct": 1.2,  # median (high-low)/open of the last 30 1m candles, %
    "quiet.vol_x": 2.0,  # breakout candle volume vs the prior lookback average
    "quiet.min_vol30_usd": 5000.0,  # traded in the last 30 candles
    "quiet.lookback_min": 30,  # breakout above the high of this many prior candles
    "quiet.min_age_min": 30,
    "quiet.max_per_hour": 2,
    "quiet.tp_pct": 0.0,
    "quiet.fade_exit": False,
    "quiet.fade_ratio": 0.30,
    "quiet.fade_min_age_min": 10,
    # existing paths: extra exits stay OFF (backtest: TP ladders hurt the copy path)
    "sniper.tp_pct": 0.0,
    "sniper.fade_exit": False,
    "sniper.fade_ratio": 0.30,
    "sniper.fade_min_age_min": 10,
    "whale.tp_pct": 0.0,
    "whale.fade_exit": False,
    "whale.fade_ratio": 0.30,
    "whale.fade_min_age_min": 10,
}


def setting(cfg, key: str) -> Any:
    """kv override `setting.<key>` (live, no restart) else DEFAULTS."""
    default = DEFAULTS.get(key)
    db = getattr(cfg, "db", None)
    if db is not None:
        try:
            raw = db.kv_get("setting." + key)
        except Exception:
            raw = None
        if raw is not None:
            try:
                val = json.loads(raw)
                if isinstance(default, bool):
                    return bool(val)
                return type(default)(val) if default is not None else val
            except Exception:
                return default
    return default


# ------------------------------------------------------------------ sleeves
def agent_of(pos: dict) -> str:
    src = str(pos.get("source") or "").lower()
    if src == "copy":
        return "whale"
    if src in MARKET_AGENTS:
        return src
    return "sniper"


def sleeve_usd(stack: float, cfg) -> float:
    return max(0.0, float(stack) * float(setting(cfg, "agents.sleeve_pct")) / 100.0)


def sleeve_check(db, cfg, agent: str, size: float, stack: float | None = None) -> str | None:
    """None when `agent` may open `size` USD now, else a short reason. Other agents never count."""
    stack = float(session.current_stack(db, cfg) if stack is None else stack)
    if stack <= 0:
        return "no stack"
    # Cash cap: four sleeves may sum to more than 100%, but open notional can never exceed the stack.
    used_all = sum(float(p.get("size_usd") or 0) for p in db.open_positions())
    free = stack - used_all
    if size > free + 1e-9:
        return f"no free stack (${max(free, 0):.2f})"
    if not bool(setting(cfg, "agents.sleeves_enabled")):
        return None
    cap = float(setting(cfg, "agents.max_position_pct")) / 100.0 * stack
    if size > cap + 1e-9:
        return f"size ${size:.2f} > {setting(cfg, 'agents.max_position_pct'):g}% cap"
    used = sum(float(p.get("size_usd") or 0) for p in db.open_positions() if agent_of(p) == agent)
    room = sleeve_usd(stack, cfg) - used
    if size > room + 1e-9:
        return f"{AGENT_LABEL[agent]} sleeve full (${max(room, 0):.2f} free of ${sleeve_usd(stack, cfg):.2f})"
    return None


# ------------------------------------------------------------------ pure signal checks
def _f(d: dict | None, *path: str) -> float | None:
    cur: Any = d
    for k in path:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(k)
    return as_float(cur)


def _buy_ratio(stats: dict | None) -> float | None:
    b, s = _f(stats, "buyVolume"), _f(stats, "sellVolume")
    if b is None or s is None or b + s <= 0:
        return None
    return b / (b + s)


def universe_ok(tok: dict, cfg) -> str | None:
    if not isinstance(tok, dict) or not tok.get("id"):
        return "no data"
    if tok["id"] in NON_MEME:
        return "not a meme"
    liq = _f(tok, "liquidity")
    if liq is None or liq < float(setting(cfg, "agents.min_liquidity_usd")):
        return "low liquidity"
    mcap = _f(tok, "mcap")
    if mcap is None or mcap > float(setting(cfg, "agents.max_mcap_usd")):
        return "mcap"
    return None


def momentum_prefilter(tok: dict, cfg) -> str | None:
    """Cheap Jupiter-stats screen before spending candle/RugCheck calls."""
    s5, s1 = tok.get("stats5m") or {}, tok.get("stats1h") or {}
    m5 = _f(s5, "priceChange")
    if m5 is None:
        return "no m5"
    if m5 < float(setting(cfg, "momentum.min_m5_pct")):
        return "no momentum"
    if m5 > float(setting(cfg, "momentum.max_m5_pct")):
        return f"already +{m5:.0f}% in 5m (no chase)"
    ratio = _buy_ratio(s5)
    if ratio is None or ratio < float(setting(cfg, "momentum.min_buy_ratio")):
        return "sellers lead"
    v5 = (_f(s5, "buyVolume") or 0) + (_f(s5, "sellVolume") or 0)
    v1 = (_f(s1, "buyVolume") or 0) + (_f(s1, "sellVolume") or 0)
    if v1 <= 0 or v5 < float(setting(cfg, "momentum.vol_spike_x")) * (v1 / 12.0):
        return "no volume spike"
    org = _f(tok, "organicScore")
    if org is None or org < float(setting(cfg, "momentum.min_organic_score")):
        return "low organic score"
    return None


def momentum_signal(tok: dict, price: float | None, candles: list[list[float]], cfg, now_ts: float) -> tuple[str | None, str]:
    """(reason_to_skip | None, note). Needs a new 1h high from free 1m candles (fail closed)."""
    pre = momentum_prefilter(tok, cfg)
    if pre:
        return pre, ""
    if price is None or price <= 0:
        return "no price", ""
    s5 = tok.get("stats5m") or {}
    s1 = tok.get("stats1h") or {}
    v5 = (_f(s5, "buyVolume") or 0) + (_f(s5, "sellVolume") or 0)
    v1 = (_f(s1, "buyVolume") or 0) + (_f(s1, "sellVolume") or 0)
    spike = v5 / (v1 / 12.0) if v1 > 0 else 0.0
    ratio = _buy_ratio(s5) or 0.0
    note = f"volume {spike:.1f}x · buys {ratio:.0%}"
    if bool(setting(cfg, "momentum.require_1h_high")):
        prior = [c for c in candles if now_ts - 3600 <= c[0] < now_ts - 120]
        # GeckoTerminal skips minutes without trades: need a few candles AND history reaching back ~1h.
        if len(prior) < 5 or not any(c[0] <= now_ts - 2880 for c in candles):
            return "no candles (fail closed)", note
        high = max(c[2] for c in prior)
        if price <= high:
            return f"under the 1h high ({(price / high - 1) * 100:+.1f}%)", note
        note = "broke the 1h high · " + note
    return None, note


def dip_prefilter(tok: dict, cfg) -> str | None:
    s5, s1 = tok.get("stats5m") or {}, tok.get("stats1h") or {}
    h1 = _f(s1, "priceChange")
    if h1 is None or h1 > -float(setting(cfg, "dip.min_drop_pct")) * 0.4:
        return "no dip"
    m5 = _f(s5, "priceChange")
    if m5 is None or m5 <= 0:
        return "still falling"
    ratio = _buy_ratio(s5)
    if ratio is None or ratio < float(setting(cfg, "dip.min_buy_ratio")):
        return "no bids"
    liq_ch = _f(s1, "liquidityChange")
    if liq_ch is None or liq_ch < -float(setting(cfg, "dip.max_liq_drop_1h_pct")):
        return "liquidity leaving"
    hold_ch = _f(s1, "holderChange")
    if hold_ch is None or hold_ch < -float(setting(cfg, "dip.max_holder_drop_1h_pct")):
        return "holders leaving"
    return None


def dip_signal(tok: dict, price: float | None, candles: list[list[float]], cfg, now_ts: float) -> tuple[str | None, str]:
    pre = dip_prefilter(tok, cfg)
    if pre:
        return pre, ""
    if price is None or price <= 0:
        return "no price", ""
    look = int(setting(cfg, "dip.lookback_min")) * 60
    win = [c for c in candles if c[0] >= now_ts - look]
    if len(win) < 5 or not any(c[0] <= now_ts - look * 0.8 for c in candles):
        return "no candles (fail closed)", ""
    high = max(c[2] for c in win)
    recent = [c for c in win if c[0] >= now_ts - 600]
    if not recent or high <= 0:
        return "no candles (fail closed)", ""
    low = min(c[3] for c in recent)
    drop = (1 - low / high) * 100
    if drop < float(setting(cfg, "dip.min_drop_pct")):
        return f"wick only -{drop:.0f}%", ""
    bounce = (price / low - 1) * 100 if low > 0 else 0
    if bounce < float(setting(cfg, "dip.min_bounce_pct")):
        return "no bounce yet", ""
    recovered = (price - low) / (high - low) * 100 if high > low else 100
    if recovered > float(setting(cfg, "dip.max_recovered_pct")):
        return "already recovered", ""
    ratio = _buy_ratio(tok.get("stats5m")) or 0
    return None, f"−{drop:.0f}% wick, bids stepping in · buys {ratio:.0%}"


def quiet_prefilter(tok: dict, cfg) -> str | None:
    """Cheap Jupiter-stats screen: enough recent volume and an up move in the last 5m."""
    s5, s1 = tok.get("stats5m") or {}, tok.get("stats1h") or {}
    v1 = (_f(s1, "buyVolume") or 0) + (_f(s1, "sellVolume") or 0)
    if v1 < float(setting(cfg, "quiet.min_vol30_usd")):
        return "thin volume"
    m5 = _f(s5, "priceChange")
    if m5 is None:
        return "no m5"
    if m5 <= 0:
        return "no up move"
    return None


def _noise_pct(candles: list[list[float]]) -> float | None:
    r = sorted((c[2] - c[3]) / c[1] * 100.0 for c in candles if c[1] > 0)
    if not r:
        return None
    n = len(r)
    return r[n // 2] if n % 2 else (r[n // 2 - 1] + r[n // 2]) / 2.0


def quiet_signal(tok: dict, price: float | None, candles: list[list[float]], cfg, now_ts: float,
                 migrated_ts: float | None = None) -> tuple[str | None, str]:
    """(reason_to_skip | None, note). Calm coin closing above its lookback high on a volume burst.

    Uses only COMPLETED 1m candles (GeckoTerminal skips minutes without trades). Fail closed."""
    pre = quiet_prefilter(tok, cfg)
    if pre:
        return pre, ""
    if price is None or price <= 0:
        return "no price", ""
    look = int(setting(cfg, "quiet.lookback_min"))
    done = [c for c in candles if c[0] <= now_ts - 60]
    if len(done) < look + 1:
        return "no candles (fail closed)", ""
    sig = done[-1]
    if sig[0] < now_ts - 180:
        return "no fresh candle", ""
    min_age = float(setting(cfg, "quiet.min_age_min"))
    if migrated_ts is not None:
        if (now_ts - migrated_ts) / 60.0 < min_age:
            return "too young", ""
    elif (sig[0] - done[0][0]) / 60.0 < min_age:
        return "too young (short history)", ""
    prior = done[-1 - look:-1]
    noise = _noise_pct(prior[-30:])
    if noise is None:
        return "no candles (fail closed)", ""
    if noise > float(setting(cfg, "quiet.max_noise_pct")):
        return f"noisy {noise:.1f}%", ""
    high = max(c[2] for c in prior)
    if sig[4] <= high:
        return f"under the {look}m high", ""
    avg_v = sum(c[5] for c in prior) / len(prior)
    spike = sig[5] / avg_v if avg_v > 0 else 0.0
    if avg_v <= 0 or spike < float(setting(cfg, "quiet.vol_x")):
        return "no volume burst", ""
    vol30 = sum(c[5] for c in done[-30:])
    if vol30 < float(setting(cfg, "quiet.min_vol30_usd")):
        return "thin volume", ""
    if sig[4] > 0 and price > sig[4] * (1 + QUIET_MAX_CHASE_PCT / 100.0):
        return f"ran away +{(price / sig[4] - 1) * 100:.0f}% (no chase)", ""
    return None, f"broke the {look}m high · noise {noise:.1f}% · volume {spike:.1f}x"


def quiet_size(stack: float, cfg) -> float | None:
    """Position size for Quiet Breakout: agents.max_position_pct of the stack; None if below the min ticket."""
    size = float(stack) * float(setting(cfg, "agents.max_position_pct")) / 100.0
    min_ticket = float(cfg.get("executor.min_ticket_usd") or 47.5)
    if size + 1e-9 < min_ticket:
        return None
    return round(size, 2)


def rug_check(tok: dict, report: dict | None, pool: str | None, cfg) -> tuple[str | None, str]:
    """Free rug screen (Jupiter audit + RugCheck). Any missing field -> skip."""
    audit = tok.get("audit") or {}
    if audit.get("isSus") is True:
        return "Jupiter flags it suspicious", ""
    if audit.get("mintAuthorityDisabled") is not True:
        return "mint not revoked", ""
    if audit.get("freezeAuthorityDisabled") is not True:
        return "freeze not revoked", ""
    top = as_float(audit.get("topHoldersPercentage"))
    if top is None:
        return "no data: top 10", ""
    if top >= float(setting(cfg, "agents.max_top10_pct")):
        return f"top 10 hold {top:.0f}%", ""
    if not isinstance(report, dict) or not report:
        return "no data: RugCheck", ""
    if report.get("rugged") is True:
        return "RugCheck: rugged", ""
    market = rugcheck_market(report, pool)
    locked = as_float(((market or {}).get("lp") or {}).get("lpLockedPct")) if market else None
    if locked is None:
        return "no data: LP", ""
    if locked < float(setting(cfg, "agents.min_lp_locked_pct")):
        return f"LP only {locked:.0f}% locked", ""
    supply = as_float(((report.get("token") or {}).get("supply")))
    decimals = as_float(((report.get("token") or {}).get("decimals")))
    bal = as_float(report.get("creatorBalance"))
    if supply is None or bal is None or not supply:
        return "no data: dev", ""
    # creatorBalance is in raw units like token.supply
    dev_pct = bal / supply * 100.0
    if dev_pct >= float(setting(cfg, "agents.max_dev_pct")):
        return f"dev holds {dev_pct:.1f}%", ""
    lp_word = "LP burned" if locked >= 99.5 else f"LP {locked:.0f}% locked"
    del decimals
    return None, f"{lp_word} · dev holds {dev_pct:.1f}% · top 10 {top:.0f}%"


# ------------------------------------------------------------------ extra exits (Risk)
def meta_key(pid: int) -> str:
    return f"agentpos.{pid}"


def extra_exit(pos: dict, price: float | None, vol_m5: float | None, cfg, db, now: datetime) -> str | None:
    """'target' or 'faded' for a position, per agent settings. None = hold."""
    if price is None:
        return None
    agent = agent_of(pos)
    entry = float(pos.get("entry_price") or 0)
    if entry <= 0:
        return None
    tp = float(setting(cfg, f"{agent}.tp_pct") or 0)
    if tp > 0 and price >= entry * (1 + tp / 100.0):
        return "target"
    if bool(setting(cfg, f"{agent}.fade_exit")) and vol_m5 is not None:
        try:
            meta = json.loads(db.kv_get(meta_key(int(pos["id"]))) or "{}")
        except Exception:
            meta = {}
        v0 = as_float(meta.get("entry_vol_m5"))
        if not v0:
            return None
        from agents.risk import parse_ts

        opened = parse_ts(pos.get("opened_at")) or now
        if now - opened < timedelta(minutes=float(setting(cfg, f"{agent}.fade_min_age_min"))):
            return None
        if vol_m5 < v0 * float(setting(cfg, f"{agent}.fade_ratio")):
            return "faded"
    return None


# ------------------------------------------------------------------ status for the dashboard
def set_status(db, agent: str, **kw: Any) -> None:
    try:
        db.kv_set(f"agent.{agent}.status", json.dumps({"ts": iso_utc(), **kw}, ensure_ascii=False))
    except Exception:
        pass


# ------------------------------------------------------------------ the agent loop
class MarketAgents:
    """One polite loop that feeds Momentum and Dip Hunter (and the ticker tape)."""

    def __init__(self, db, cfg, bus, sources, notifier=None) -> None:
        self.db, self.cfg, self.bus, self.sources, self.notifier = db, cfg, bus, sources, notifier
        self.skip_logged: dict[str, float] = {}
        self.rot: dict[str, int] = {}

    # -- helpers
    def _recent_entries(self, agent: str, hours: float) -> list[dict]:
        since = iso_utc(utcnow() - timedelta(hours=hours))
        return [dict(r) for r in self.db.query(
            "SELECT * FROM positions WHERE source=? AND opened_at>=?", (agent, since))]

    def _blocked(self, agent: str, mint: str) -> str | None:
        if any(p["mint"] == mint for p in self.db.open_positions()):
            return "already open"
        if self.db.is_skipped(mint):
            return "skipped"
        if session.entries_paused(self.cfg) or self.cfg.is_paused(agent):
            return "paused"
        if len(self._recent_entries(agent, 1.0)) >= int(setting(self.cfg, f"{agent}.max_per_hour")):
            return "hourly limit"
        cool = float(setting(self.cfg, "agents.cooldown_h"))
        if any(p["mint"] == mint for p in self._recent_entries(agent, cool)):
            return "cooldown"
        return None

    def _log_skip(self, agent: str, tok: dict, reason: str) -> None:
        key = f"{agent}:{tok.get('id')}"
        now = utcnow().timestamp()
        if now - self.skip_logged.get(key, 0) < 6 * 3600:
            return
        self.skip_logged[key] = now
        sym = tok.get("symbol") or tok.get("id", "")[:6]
        self.db.add_activity(kind="skipped", mint=tok.get("id"), agent=agent, title=f"Skipped ${sym}",
                             subtitle=f"{AGENT_LABEL[agent]} · {reason}", value="skip")

    async def _ticker(self) -> None:
        if not bool(setting(self.cfg, "agents.ticker_enabled")):
            return
        try:
            toks = await asyncio.to_thread(self.sources.jupiter_tokens, TICKER_MINTS)
        except Exception as e:
            log.warning("ticker failed: %s", type(e).__name__)
            return
        by = {t.get("id"): t for t in toks}
        out = []
        for m in TICKER_MINTS:
            t = by.get(m)
            px = as_float((t or {}).get("usdPrice"))
            if not t or px is None:
                continue
            out.append({"mint": m, "symbol": str(t.get("symbol") or "").lstrip("$").upper(), "price": px,
                        "ch24": as_float((t.get("stats24h") or {}).get("priceChange"))})
        if out:
            self.db.kv_set("ticker", json.dumps({"ts": iso_utc(), "items": out}))

    async def cycle(self) -> dict[str, int]:
        cfg, db = self.cfg, self.db
        stats = {"universe": 0, "momentum": 0, "dip": 0, "quiet": 0, "checked": 0, "bought": 0}
        await self._ticker()
        if not bool(setting(cfg, "agents.enabled")):
            return stats
        toks: dict[str, dict] = {}
        for cat, iv in (("toptrending", "5m"), ("toptraded", "1h"), ("toptrending", "1h")):
            try:
                for t in await asyncio.to_thread(self.sources.jupiter_trending, cat, iv, 50):
                    if t.get("id"):
                        toks.setdefault(t["id"], t)
            except Exception as e:
                log.warning("jupiter %s/%s failed: %s", cat, iv, type(e).__name__)
        if bool(setting(cfg, "quiet.enabled")):
            mig = self._recent_migrations()
            missing = [m for m in mig if m not in toks]
            if missing:
                try:
                    for t in await asyncio.to_thread(self.sources.jupiter_tokens, missing):
                        if t.get("id"):
                            toks.setdefault(t["id"], t)
                except Exception as e:
                    log.warning("jupiter migrations failed: %s", type(e).__name__)
        uni = [t for t in toks.values() if universe_ok(t, cfg) is None]
        stats["universe"] = len(uni)
        cycle_ts = iso_utc()
        live: dict[str, dict[str, Any]] = {}
        per_agent: dict[str, list[dict]] = {}
        for agent, pre in (("momentum", momentum_prefilter), ("dip", dip_prefilter), ("quiet", quiet_prefilter)):
            on = bool(setting(cfg, f"{agent}.enabled")) and bool(setting(cfg, "agents.enabled"))
            reasons: dict[str, int] = {}
            per_agent[agent] = []
            if on:
                for t in uni:
                    why = pre(t, cfg)
                    if why is None:
                        per_agent[agent].append(t)
                    else:
                        key = why.split(" (")[0] if not why.startswith("already +") else "already pumped"
                        reasons[key] = reasons.get(key, 0) + 1
            stats[agent] = len(per_agent[agent])
            live[agent] = {"enabled": on, "scanning": len(uni) if on else 0, "candidates": len(per_agent[agent]),
                           "checked": 0, "bought": 0, "cycle_ts": cycle_ts,
                           "top_reasons": sorted(reasons.items(), key=lambda kv: -kv[1])[:3], "last": None}
            set_status(db, agent, **{k: v for k, v in live[agent].items()}, phase="checking")
        # fair share of the deep-check budget: round-robin over the market agents; each agent's
        # list is rotated across cycles so every candidate gets a deep check eventually.
        cap = int(setting(cfg, "agents.max_checks_per_cycle"))
        lists = {}
        for agent in MARKET_AGENTS:
            lst = per_agent.get(agent, [])
            off = self.rot.get(agent, 0) % len(lst) if lst else 0
            lists[agent] = lst[off:] + lst[:off]
        cands: list[tuple[str, dict]] = []
        for i in range(max((len(v) for v in lists.values()), default=0)):
            for agent in MARKET_AGENTS:
                if i < len(lists[agent]):
                    cands.append((agent, lists[agent][i]))
        cands = cands[:cap]
        for agent in MARKET_AGENTS:
            taken = sum(1 for a, _ in cands if a == agent)
            self.rot[agent] = self.rot.get(agent, 0) + taken

        def note_last(agent: str, tok: dict, verdict: str, reason: str) -> None:
            sym = str(tok.get("symbol") or tok.get("id", "")[:6]).lstrip("$")
            live[agent]["last"] = {"symbol": "$" + sym, "mint": tok.get("id"), "verdict": verdict, "reason": reason,
                                   "ts": iso_utc()}
            set_status(db, agent, **live[agent], phase="checking")

        pairs: dict = {}
        if cands:
            try:
                pairs = await asyncio.to_thread(self.sources.any_dex_pairs, list({t["id"] for _, t in cands}))
            except Exception as e:
                log.warning("dex pairs failed: %s", type(e).__name__)
                cands = []
        for agent, tok in cands:
            mint = tok["id"]
            blocked = self._blocked(agent, mint)
            if blocked:
                note_last(agent, tok, "skip", blocked)
                continue
            pair = pairs.get(mint)
            price = dex_float(pair, "priceUsd") if pair else None
            liq = dex_float(pair, "liquidity.usd") if pair else None
            if pair is None or price is None or liq is None or liq < float(setting(cfg, "agents.min_liquidity_usd")):
                note_last(agent, tok, "skip", "no DEX pair / low liquidity")
                continue
            pool = pair.get("pairAddress")
            candles = await asyncio.to_thread(self.sources.gecko_ohlcv, pool, 90)
            stats["checked"] += 1
            live[agent]["checked"] += 1
            if agent == "quiet":
                why, note = quiet_signal(tok, price, candles, cfg, utcnow().timestamp(), self._migrated_ts(mint))
            else:
                fn = momentum_signal if agent == "momentum" else dip_signal
                why, note = fn(tok, price, candles, cfg, utcnow().timestamp())
            if why:
                log.info("%s pass %s %s: %s", agent, tok.get("symbol"), mint, why)
                note_last(agent, tok, "pass", why)
                continue
            try:
                report = await asyncio.to_thread(self.sources.rugcheck_report_polite, mint)
            except Exception:
                report = None
            bad, rug_note = rug_check(tok, report, pool, cfg)
            if bad:
                log.info("%s skip %s %s: %s", agent, tok.get("symbol"), mint, bad)
                self._log_skip(agent, tok, bad)
                note_last(agent, tok, "skip", bad)
                continue
            if agent == "quiet":
                qs = quiet_size(session.current_stack(db, cfg), cfg)
                if qs is None:
                    self._log_skip(agent, tok, "below min ticket")
                    note_last(agent, tok, "skip", "below min ticket")
                    continue
                size = qs
            else:
                size = float(setting(cfg, f"{agent}.size_usd"))
            full = sleeve_check(db, cfg, agent, size)
            if full:
                self._log_skip(agent, tok, full)
                note_last(agent, tok, "skip", full)
                continue
            if await self.open_paper(agent, tok, pair, price, size, f"{note} · {rug_note}"):
                stats["bought"] += 1
                live[agent]["bought"] += 1
                note_last(agent, tok, "bought", note)
        done_ts = iso_utc()
        for agent, st in live.items():
            set_status(db, agent, **st, phase="waiting", done_ts=done_ts)
            self._summary(agent, st)
        return stats

    def _recent_migrations(self) -> list[str]:
        """Mints migrated between quiet.min_age_min and QUIET_MIGRATION_MAX_AGE_H ago, newest first."""
        now = utcnow()
        lo = iso_utc(now - timedelta(hours=QUIET_MIGRATION_MAX_AGE_H))
        hi = iso_utc(now - timedelta(minutes=float(setting(self.cfg, "quiet.min_age_min"))))
        try:
            rows = self.db.query(
                "SELECT mint FROM coins WHERE migrated_at>=? AND migrated_at<=? ORDER BY migrated_at DESC LIMIT ?",
                (lo, hi, QUIET_MIGRATION_POOL))
        except Exception:
            return []
        return [r["mint"] for r in rows if r["mint"]]

    def _migrated_ts(self, mint: str) -> float | None:
        try:
            coin = self.db.get_coin(mint) or {}
        except Exception:
            return None
        raw = coin.get("migrated_at")
        if not raw:
            return None
        try:
            return datetime.fromisoformat(str(raw).replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None

    def _summary(self, agent: str, st: dict) -> None:
        """At most one 'scan' activity row per agent per agents.summary_min (default 10 min)."""
        if not st.get("enabled"):
            return
        now = utcnow().timestamp()
        key = f"summary:{agent}"
        if now - self.skip_logged.get(key, 0) < float(setting(self.cfg, "agents.summary_min")) * 60:
            return
        self.skip_logged[key] = now
        top = ", ".join(f"{k} {v}" for k, v in (st.get("top_reasons") or [])[:2])
        last = st.get("last") or {}
        tail = f" · last {last.get('symbol')}: {last.get('reason')}" if last else ""
        self.db.add_activity(kind="scan", agent=agent, title=f"Scanned {st.get('scanning', 0)} pairs",
                             subtitle=f"{AGENT_LABEL[agent]} · {st.get('candidates', 0)} candidates, "
                                      f"{st.get('checked', 0)} deep-checked" + (f" · {top}" if top else "") + tail,
                             value="scan")

    async def open_paper(self, agent: str, tok: dict, pair: dict, price: float, size: float, why: str) -> bool:
        db, cfg = self.db, self.cfg
        mint = tok["id"]
        name = (pair.get("baseToken") or {}).get("name") or tok.get("name") or mint[:6]
        symbol = (pair.get("baseToken") or {}).get("symbol") or tok.get("symbol")
        db.upsert_coin(mint=mint, name=name, symbol=symbol)
        stop_pct = float(cfg.get("risk.stop_pct") or 1)
        entry = float(price)
        pid = db.open_position(mint=mint, entry_price=entry, size_usd=size, peak_price=entry,
                               stop_price=stop_price_at_entry(price, entry, stop_pct, cfg), mode="PAPER", status="open",
                               name=name, source=agent, copy_handle=None)
        db.kv_set(meta_key(pid), json.dumps({"agent": agent, "entry_vol_m5": dex_float(pair, "volume.m5"),
                                             "why": why, "pool": pair.get("pairAddress")}))
        stack = session.current_stack(db, cfg)
        link = links.coin_url(mint, "solana")
        db.add_signal(mint=mint, price=entry, size_usd=size, stack_pct=round(size / stack * 100, 2) if stack else None,
                      link=link, mode="PAPER", status="bought", reason=AGENT_LABEL[agent])
        db.add_activity(kind="bought", mint=mint, agent=agent, title=f"Bought ${symbol or name}",
                        subtitle=f"{AGENT_LABEL[agent]} · {why}", value=f"${size:.2f} · PAPER")
        db.add_price(mint, entry, dex_float(pair, "volume.m5"))
        log.info("%s bought %s %s size=%s entry=%s why=%s", agent, symbol, mint, size, entry, why)
        if self.bus is not None:
            await self.bus.risk.put({"mint": mint, "event": "filled"})
        if self.notifier is not None and bool(setting(cfg, "agents.alert_buys")):
            trail = float(cfg.get("risk.trail_pct") or 12)
            tp = float(setting(cfg, f"{agent}.tp_pct") or 0)
            text = (f"PAPER · {AGENT_LABEL[agent]} купи ${symbol or name} · ${size:.2f} "
                    f"({size / stack * 100 if stack else 0:.1f}% от стека)\n"
                    f"{why}\n"
                    f"Изход: стоп -{stop_pct:g}% · трейл {trail:g}%" + (f" · цел +{tp:g}%" if tp else "") + "\n"
                    "Нищо не е купено с реални пари. Реална покупка има само ако натиснеш бутона и купиш във FOMO.")
            try:
                await self.notifier.send(text, buttons=[{"text": "Купи във FOMO", "url": link}], mint=mint)
            except Exception as e:
                log.warning("buy alert failed: %s", type(e).__name__)
        return True


async def run_agent(db, cfg, bus, sources=None, notifier=None, stop_event: asyncio.Event | None = None) -> None:
    ma = MarketAgents(db, cfg, bus, sources, notifier)
    stop = stop_event or asyncio.Event()
    while not stop.is_set():
        for hb in MARKET_AGENTS:
            db.heartbeat(hb)
        if sources is not None:
            try:
                res = await ma.cycle()
                log.info("cycle %s", res)
            except Exception as e:
                log.warning("cycle failed: %s", type(e).__name__)
        for hb in MARKET_AGENTS:
            db.heartbeat(hb)
        try:
            await asyncio.wait_for(stop.wait(), timeout=float(setting(cfg, "agents.poll_s")))
        except asyncio.TimeoutError:
            pass

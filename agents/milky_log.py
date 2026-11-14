"""Video setups (@milkybids dump-bounce) — RECORD ONLY, PAPER research log.

This agent NEVER opens positions, never sends buy alerts, never touches the stack, the risk
settings, signals or the executor. It only writes rows to `milky_setups` and one kv status key.
Switch: kv setting.milky.log_enabled (default true).

Rules (same as /workspace/strat/milky.py backtest):
  * coin = a migration the desk saw in the last 48 h; ATH = highest level held for 3 closes in a
    row, set within 24 h of migration and >= 3x the first price ('actively pumped').
  * E1: first close <= ATH*0.35 (-65%) while the next open is still >= ATH*0.20 (-80%).
  * E2: after touching -65% (never closing under -80%): first swing low L1, a bounce closing
    >= L1*1.15, a >=5% pullback whose lows stay above L1, then a close above the prior candle high.
  * setups must trigger within 24 h after the ATH. One E1 and one E2 row per coin at most.
  * filters recorded (not gating the log, except the loosest 'pattern' one):
      strict_entry  entry mcap >= $4M  and vol24 >= $800k
      loose_entry   entry mcap >= $2M  and vol24 >= $130k
      strict_ath    ATH mcap   >= $4M  and vol24 >= $800k
      loose_ath     ATH mcap   >= $2M  and vol24 >= $130k
      pattern       ATH mcap   >= $300k and vol24 >= $130k   (minimum to be logged)
  * 'holders increasing' cannot be checked (no free holder history) and is not applied.
Hypothetical outcomes (filled in later, final after 6 h): max up / max down over 1 h and 3 h from
the trigger close, and what X1 (desk exits: 1% market stop, lock 8->4.5, trail 20/12, 3 h timeout)
and X2 (10% stop, same trail, INFO ONLY — not deployable under the 1% rule) would have made on a
$59 ticket after FOMO fees and +0.5% / -1% slippage. Nothing is bought.

Data: DexScreener pairs (batch 30) for cheap prefilter + GeckoTerminal 1m candles through the
shared, cached, rate-limited fetch in sources.py (max MILKY_GECKO_PER_CYCLE calls per cycle).
Every gap fails closed (no row).
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from typing import Any

import fees
from util import agent_logger, as_float, iso_utc, utcnow

log = agent_logger("milky")

T, O, H, L, C, V = range(6)
CYCLE_S = 120
MILKY_GECKO_PER_CYCLE = 3
MAX_AGE_H = 48
TRACK_AFTER_TRIGGER_H = 6
TICKET = 59.0
ENTRY_SLIP, EXIT_SLIP = 0.005, 0.01
FILTERS = {
    "strict_entry": (4e6, 8e5, "entry"),
    "loose_entry": (2e6, 1.3e5, "entry"),
    "strict_ath": (4e6, 8e5, "ath"),
    "loose_ath": (2e6, 1.3e5, "ath"),
    "pattern": (3e5, 1.3e5, "ath"),
}
PAIRS_TTL_S = 600
PAIRS_GAP_S = 1.0
PREFILTER_MIN_VOL24 = 1.3e5
PREFILTER_MIN_FDV = 6e4  # ATH >= $300k after a -80% max drop


def _ts(raw) -> float | None:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _iso(ts: float) -> str:
    return iso_utc(datetime.fromtimestamp(ts, tz=timezone.utc))


def clean(bars):
    out = []
    for b in bars:
        hi_body, lo_body = max(b[O], b[C]), min(b[O], b[C])
        out.append([b[T], b[O], b[H] if b[H] <= hi_body * 2 else hi_body,
                    b[L] if b[L] >= lo_body * 0.5 else lo_body, b[C], b[V]])
    return out


def vol24(bars, i) -> float:
    t_lo = bars[i][T] - 86400
    return sum(b[V] for b in bars[: i + 1] if b[T] > t_lo)


def find_setups(bars, created_ts: float, supply: float, pump_x: float = 3.0) -> list[dict[str, Any]]:
    """E1/E2 trigger candles with the filter variants they pass. Pure; bars oldest first."""
    out = []
    if len(bars) < 10 or supply <= 0:
        return out
    p0 = bars[0][O]
    for kind in ("E1", "E2"):
        ath, ath_t = 0.0, None
        state, l1, bounced, pull_ok, peak = "watch", None, False, False, 0.0
        hit = None
        for i in range(len(bars) - 1):
            b = bars[i]
            sustained = min(x[C] for x in bars[max(0, i - 2):i + 1])
            if sustained > ath and (b[T] - created_ts) <= 86400:
                ath, ath_t = sustained, b[T]
                state, l1, bounced, pull_ok = "watch", None, False, False
                continue
            if ath_t is None:
                continue
            if b[T] - ath_t > 86400 or state == "dead":
                break
            if ath < p0 * pump_x:
                continue
            lo65, lo80 = ath * 0.35, ath * 0.20
            if state == "watch":
                if b[C] <= lo65:
                    if b[C] < lo80 or (kind == "E1" and bars[i + 1][O] < lo80):
                        state = "dead"
                        continue
                    if kind == "E1":
                        hit = i
                        break
                    state, l1 = "armed", b[L]
            elif state == "armed":
                if b[C] < lo80:
                    state = "dead"
                    continue
                if not bounced:
                    l1 = min(l1, b[L])
                    if b[C] >= l1 * 1.15:
                        bounced, peak = True, b[H]
                    continue
                peak = max(peak, b[H])
                if b[L] <= l1:
                    state = "dead"
                    continue
                if not pull_ok and b[L] <= peak * 0.95:
                    pull_ok = True
                    continue
                if pull_ok and b[C] > bars[i - 1][H]:
                    hit = i
                    break
        if hit is None:
            continue
        v24 = vol24(bars, hit)
        entry_px = bars[hit + 1][O] if kind == "E1" else bars[hit][C]  # same as the backtest
        mc_entry, mc_ath = entry_px * supply, ath * supply
        passed = [name for name, (mc, v, on) in FILTERS.items()
                  if (mc_entry if on == "entry" else mc_ath) >= mc and v24 >= v]
        if "pattern" not in passed:
            continue
        out.append({"entry_kind": kind, "i": hit, "trigger_ts": bars[hit][T], "trigger_price": bars[hit][C],
                    "ath_price": ath, "ath_ts": ath_t, "mcap_at_trigger": mc_entry, "ath_mcap": mc_ath,
                    "vol24_usd": v24, "filters": passed})
    return out


def _sim_exit(bars, i_entry: int, stop_pct: float, entry_fill: float, mkt: float):
    """Desk exit rules on 1m candles (stop checked first inside a candle). Returns (price, reason, done)."""
    stop = mkt * (1 - stop_pct / 100)
    peak, trail_on, t0 = entry_fill, False, bars[i_entry][T]
    for j in range(i_entry, len(bars)):
        b = bars[j]
        line = stop
        if peak >= entry_fill * 1.08:
            line = max(line, entry_fill * 1.045)
        trail = peak * 0.88 if trail_on else 0.0
        eff = max(line, trail)
        if b[L] <= eff:
            px = (b[O] if (b[O] <= eff and j > i_entry) else min(eff, b[O])) * (1 - EXIT_SLIP)
            reason = "trail" if trail > line else ("lock" if line > entry_fill else "stop")
            return px, reason, True
        peak = max(peak, b[H])
        if peak >= entry_fill * 1.20:
            trail_on = True
        if (b[T] - t0) >= 3 * 3600 and b[C] < entry_fill * 1.05:
            return b[C], "timeout", True
    return bars[-1][C], "open", False


def outcome(bars, trigger_ts: float, now_ts: float) -> dict[str, Any] | None:
    """Hypothetical result of buying the next candle open after the trigger. None if no candle yet."""
    idx = next((k for k, b in enumerate(bars) if b[T] > trigger_ts), None)
    if idx is None:
        return None
    ref = next(b[C] for b in reversed(bars) if b[T] <= trigger_ts) if any(b[T] <= trigger_ts for b in bars) else bars[idx][O]
    res: dict[str, Any] = {}
    for label, secs in (("1h", 3600), ("3h", 3 * 3600)):
        win = [b for b in bars[idx:] if b[T] - trigger_ts <= secs]
        if win:
            res[f"max_up_{label}_pct"] = round((max(b[H] for b in win) / ref - 1) * 100, 1)
            res[f"max_down_{label}_pct"] = round((min(b[L] for b in win) / ref - 1) * 100, 1)
        res[f"{label}_complete"] = now_ts - trigger_ts >= secs
    mkt = bars[idx][O]
    fill = mkt * (1 + ENTRY_SLIP)
    for name, stop in (("x1", 1.0), ("x2_info", 10.0)):
        px, reason, done = _sim_exit(bars, idx, stop, fill, mkt)
        pnl, fee = fees.pnl_usd(TICKET, fill, px)
        res[name] = {"exit_reason": reason, "pnl_usd": pnl, "fees_usd": fee, "move_pct": round((px / fill - 1) * 100, 1),
                     "done": done}
    return res


class MilkyLogger:
    def __init__(self, db, cfg, sources) -> None:
        self.db, self.cfg, self.sources = db, cfg, sources
        self.bars: dict[str, list[list[float]]] = {}
        self.meta: dict[str, dict[str, Any]] = {}
        self.rot = 0
        self._pairs_cache: tuple[float, dict[str, dict]] = (0.0, {})

    def enabled(self) -> bool:
        from agents.strategies import setting

        return bool(setting(self.cfg, "milky.log_enabled"))

    def _recent_migrations(self) -> list[dict]:
        lo = iso_utc(utcnow() - timedelta(hours=MAX_AGE_H))
        try:
            return [dict(r) for r in self.db.query(
                "SELECT mint, symbol, migrated_at FROM coins WHERE migrated_at>=? ORDER BY migrated_at DESC LIMIT 600",
                (lo,))]
        except Exception:
            return []

    def _pending(self) -> list[dict]:
        try:
            return [dict(r) for r in self.db.query("SELECT * FROM milky_setups WHERE outcome_final=0")]
        except Exception:
            return []

    async def _pairs(self, mints: list[str]) -> dict[str, dict]:
        """DexScreener prefilter, refreshed every PAIRS_TTL_S (pending outcome coins always included)."""
        now = utcnow().timestamp()
        ts, cached = self._pairs_cache
        missing = [m for m in mints if m not in cached]
        if now - ts < PAIRS_TTL_S and not missing:
            return cached
        todo = mints if now - ts >= PAIRS_TTL_S else missing
        out: dict[str, dict] = {} if now - ts >= PAIRS_TTL_S else dict(cached)
        for k in range(0, len(todo), 30):
            try:
                out.update(await asyncio.to_thread(self.sources.any_dex_pairs, todo[k:k + 30]))
            except Exception as e:
                log.warning("dex pairs failed: %s", type(e).__name__)
                break
            await asyncio.sleep(PAIRS_GAP_S)
        for m in todo:
            out.setdefault(m, {})
        self._pairs_cache = (now if now - ts >= PAIRS_TTL_S else ts, out)
        return out

    def history_complete(self, mint: str) -> bool:
        """Detection needs candles from (near) migration: the ATH and the 3x pump check use them."""
        meta, bars = self.meta.get(mint) or {}, self.bars.get(mint)
        if not bars:
            return False
        return bool(meta.get("history_done")) or bars[0][T] <= (meta.get("created") or 0) + 1800

    async def _candles(self, mint: str, pool: str) -> list[list[float]] | None:
        have = self.bars.get(mint)
        meta = self.meta.get(mint) or {}
        if not have:
            got = await asyncio.to_thread(self.sources.gecko_ohlcv, pool, 1000, token=mint)
        elif not self.history_complete(mint):  # page back toward migration (one call per cycle)
            got = await asyncio.to_thread(self.sources.gecko_ohlcv, pool, 1000, token=mint, before_ts=have[0][T])
            if not got or min(b[0] for b in got) >= have[0][T]:
                meta["history_done"] = True  # nothing older exists
        else:
            got = await asyncio.to_thread(self.sources.gecko_ohlcv, pool, 60, token=mint)
        if not got:
            return have
        merged = {b[0]: b for b in (have or [])}
        for b in got:
            merged[b[0]] = b
        bars = clean([merged[k] for k in sorted(merged)])
        self.bars[mint] = bars
        return bars

    async def cycle(self) -> dict[str, int]:
        st = {"coins": 0, "candidates": 0, "fetched": 0, "new_setups": 0, "outcomes": 0}
        if not self.enabled():
            self._status(st, enabled=False)
            return st
        migs = {m["mint"]: m for m in self._recent_migrations() if m.get("mint")}
        pending = self._pending()
        for p in pending:
            migs.setdefault(p["mint"], {"mint": p["mint"], "symbol": p.get("symbol"), "migrated_at": None})
        st["coins"] = len(migs)
        pairs = await self._pairs(list(migs))
        cands = []
        pend_mints = {p["mint"] for p in pending}
        for mint, m in migs.items():
            pair = pairs.get(mint)
            if not pair:
                continue
            price = as_float(pair.get("priceUsd"))
            fdv = as_float(pair.get("fdv")) or as_float(pair.get("marketCap"))
            v24 = as_float((pair.get("volume") or {}).get("h24")) or 0.0
            if not price or not fdv:
                continue
            self.meta[mint] = {"pool": pair.get("pairAddress"), "supply": fdv / price,
                               "symbol": ((pair.get("baseToken") or {}).get("symbol")) or m.get("symbol"),
                               "created": _ts(m.get("migrated_at")) or ((as_float(pair.get("pairCreatedAt")) or 0) / 1000)}
            if mint in pend_mints or (v24 >= PREFILTER_MIN_VOL24 and fdv >= PREFILTER_MIN_FDV):
                cands.append(mint)
        st["candidates"] = len(cands)
        # pending outcomes first, then rotate through the rest
        cands.sort(key=lambda m: m not in pend_mints)
        head = [m for m in cands if m in pend_mints]
        rest = [m for m in cands if m not in pend_mints]
        if rest:
            off = self.rot % len(rest)
            rest = rest[off:] + rest[:off]
        budget = MILKY_GECKO_PER_CYCLE
        try:
            from sources import gecko_recent_429

            if gecko_recent_429():  # Quiet Breakout has priority on the shared GeckoTerminal budget
                budget = 1
        except Exception:
            pass
        st["gecko_budget"] = budget
        order = (head + rest)[:budget]
        self.rot += max(0, len(order) - len(head))
        now_ts = utcnow().timestamp()
        for mint in order:
            meta = self.meta[mint]
            bars = await self._candles(mint, meta["pool"])
            if not bars:
                continue
            st["fetched"] += 1
            if not self.history_complete(mint):
                st["outcomes"] += self._update_outcomes(mint, bars, now_ts)
                continue
            for s in find_setups(bars, meta["created"], meta["supply"]):
                if self._insert(mint, meta, s):
                    st["new_setups"] += 1
            st["outcomes"] += self._update_outcomes(mint, bars, now_ts)
        self._status(st, enabled=True)
        return st

    def _insert(self, mint, meta, s) -> bool:
        try:
            cur = self.db.query(
                "SELECT id FROM milky_setups WHERE mint=? AND entry_kind=?", (mint, s["entry_kind"]))
            if cur:
                return False
            self.db.execute(
                "INSERT OR IGNORE INTO milky_setups (mint, symbol, pool, entry_kind, trigger_ts, trigger_price, "
                "ath_price, ath_ts, mcap_at_trigger, ath_mcap, vol24_usd, filters, detected_at, outcome, outcome_final) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,NULL,0)",
                (mint, meta.get("symbol"), meta.get("pool"), s["entry_kind"], _iso(s["trigger_ts"]), s["trigger_price"],
                 s["ath_price"], _iso(s["ath_ts"]), round(s["mcap_at_trigger"], 2), round(s["ath_mcap"], 2),
                 round(s["vol24_usd"], 2), json.dumps(s["filters"]), iso_utc()))
            log.info("setup %s %s %s filters=%s (log only)", s["entry_kind"], meta.get("symbol"), mint, s["filters"])
            return True
        except Exception as e:
            log.warning("insert failed: %s", type(e).__name__)
            return False

    def _update_outcomes(self, mint, bars, now_ts) -> int:
        n = 0
        try:
            rows = [dict(r) for r in self.db.query(
                "SELECT id, trigger_ts FROM milky_setups WHERE mint=? AND outcome_final=0", (mint,))]
        except Exception:
            return 0
        for r in rows:
            t = _ts(r["trigger_ts"])
            if t is None:
                continue
            res = outcome(bars, t, now_ts)
            if res is None:
                continue
            final = now_ts - t >= TRACK_AFTER_TRIGGER_H * 3600
            self.db.execute("UPDATE milky_setups SET outcome=?, outcome_final=? WHERE id=?",
                            (json.dumps(res), 1 if final else 0, r["id"]))
            n += 1
        return n

    def _status(self, st, enabled) -> None:
        try:
            self.db.kv_set("agent.milky.status", json.dumps({"ts": iso_utc(), "enabled": enabled, **st}))
        except Exception:
            pass


async def run_agent(db, cfg, sources=None, stop_event: asyncio.Event | None = None) -> None:
    ml = MilkyLogger(db, cfg, sources)
    stop = stop_event or asyncio.Event()
    while not stop.is_set():
        if sources is not None:
            try:
                res = await ml.cycle()
                log.info("cycle %s", res)
            except Exception as e:
                log.warning("cycle failed: %s", type(e).__name__)
        try:
            await asyncio.wait_for(stop.wait(), timeout=CYCLE_S)
        except asyncio.TimeoutError:
            pass

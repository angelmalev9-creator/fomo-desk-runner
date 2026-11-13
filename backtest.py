"""30-day copy-trade backtest, PAPER research only (no orders, no keys).

Variant A "следвай трейдъра": $10 per copied buy, sell when the trader sells
  (pro-rata to the share of his tracked position he sold).
Variant B "правилата на бюрото": same entries, exit on the desk stop/trail/timeout
  or when the trader sells, whichever is first. Price path = free GeckoTerminal
  1-minute candles when --ohlcv is given (cached), else only the real trade
  prices seen in the data (sparse, marked as approximate).
Fills use the trader's real on-chain prices with a slippage pad (buy higher,
sell lower) and the FOMO fee schedule from fees.py. Runs on partial data.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

import fees
from copytrade import HISTORY_DB
from util import LOG_DIR

CAVEAT = (
    "Внимание (selection bias): тези трейдъри са избрани, защото СЕГА са в топ класацията за последните "
    "30 дни, т.е. миналото им по дефиниция е добро. Това НЕ предсказва следващите 30 дни. Освен това: "
    "виждаме само Solana сделките им (не Base/BNB/Robinhood Chain), копието влиза секунди по-късно от тях, "
    "а при тънки монети реалното приплъзване може да е много над подложката."
)


@dataclass
class Rules:
    size_usd: float = 10.0
    slippage_pct: float = 1.5
    stop_pct: float = 6.0
    trail_start_pct: float = 20.0
    trail_pct: float = 12.0
    timeout_h: float = 3.0
    timeout_min_gain_pct: float = 5.0
    fee_discount: float = 0.10


@dataclass
class Lot:
    handle: str
    mint: str
    t: int
    entry: float  # padded
    size: float
    remaining: float = 1.0  # fraction still held (variant A)
    closes: list = field(default_factory=list)  # (fraction, exit_price, t, reason)


def load_trades(db_path: Path | str = HISTORY_DB, json_files: Iterable[Path] = ()) -> list[dict]:
    out: list[dict] = []
    p = Path(db_path)
    if p.exists():
        conn = sqlite3.connect(str(p))
        conn.row_factory = sqlite3.Row
        try:
            out = [dict(r) for r in conn.execute("SELECT * FROM trades ORDER BY block_time")]
        except sqlite3.OperationalError:
            out = []
        conn.close()
    seen = {t["signature"] for t in out}
    for f in json_files:
        d = json.loads(Path(f).read_text(encoding="utf-8"))
        for t in d.get("trades") or []:
            if t["signature"] in seen:
                continue
            bt = t.get("block_time")
            if bt is None and t.get("time_utc"):
                bt = int(datetime.fromisoformat(t["time_utc"]).timestamp())
            out.append({**t, "handle": d.get("handle"), "wallet": d.get("wallet"), "block_time": bt})
    out.sort(key=lambda t: t["block_time"] or 0)
    return out


def _pnl(size: float, entry: float, exit_: float, disc: float) -> tuple[float, float]:
    return fees.pnl_usd(size, entry, exit_, disc)


def variant_a(trades: list[dict], r: Rules) -> dict[str, Any]:
    pad = r.slippage_pct / 100.0
    held: dict[tuple, float] = defaultdict(float)  # trader's tracked token amount
    lots: dict[tuple, list[Lot]] = defaultdict(list)
    all_lots: list[Lot] = []
    for t in trades:
        key = (t["handle"], t["mint"])
        if t["side"] == "buy":
            lot = Lot(t["handle"], t["mint"], t["block_time"], t["price_usd"] * (1 + pad), r.size_usd)
            lots[key].append(lot)
            all_lots.append(lot)
            held[key] += t["token_amount"]
        else:
            if held[key] <= 0:
                continue  # bought before the window: nothing of ours to sell
            frac = min(1.0, t["token_amount"] / held[key])
            held[key] = max(0.0, held[key] - t["token_amount"])
            px = t["price_usd"] * (1 - pad)
            for lot in lots[key]:
                if lot.remaining <= 1e-9:
                    continue
                part = lot.remaining * frac if held[key] > 0 else lot.remaining
                lot.remaining -= part
                lot.closes.append((part, px, t["block_time"], "trader_sold"))
    return _summarize(all_lots, trades, r)


def _last_prices(trades: list[dict]) -> dict[str, tuple[float, int]]:
    last: dict[str, tuple[float, int]] = {}
    for t in trades:
        last[t["mint"]] = (t["price_usd"], t["block_time"])
    return last


def _summarize(lots: list[Lot], trades: list[dict], r: Rules) -> dict[str, Any]:
    last = _last_prices(trades)
    per: dict[str, dict] = defaultdict(lambda: {"buys": 0, "closed": 0, "open": 0, "invested": 0.0,
                                                "realized": 0.0, "unrealized": 0.0, "fees": 0.0, "wins": 0})
    for lot in lots:
        s = per[lot.handle]
        s["buys"] += 1
        s["invested"] += lot.size
        lot_pnl = 0.0
        for frac, px, _t, _why in lot.closes:
            pnl, fee = _pnl(lot.size * frac, lot.entry, px, r.fee_discount)
            s["realized"] += pnl
            s["fees"] += fee
            lot_pnl += pnl
        if lot.remaining > 1e-6:
            s["open"] += 1
            mark = last.get(lot.mint, (lot.entry, 0))[0] * (1 - r.slippage_pct / 100.0)
            pnl, fee = _pnl(lot.size * lot.remaining, lot.entry, mark, r.fee_discount)
            s["unrealized"] += pnl
        else:
            s["closed"] += 1
            if lot_pnl > 0:
                s["wins"] += 1
    _concentration(lots, trades, r, per)
    total = {"buys": 0, "closed": 0, "open": 0, "invested": 0.0, "realized": 0.0, "unrealized": 0.0, "fees": 0.0, "wins": 0}
    for s in per.values():
        for k in total:
            total[k] += s[k]
    for s in list(per.values()) + [total]:
        for k in ("invested", "realized", "unrealized", "fees"):
            s[k] = round(s[k], 2)
        s["pnl"] = round(s["realized"] + s["unrealized"], 2)
        s["win_rate_pct"] = round(100.0 * s["wins"] / s["closed"], 1) if s["closed"] else None
    return {"per_trader": dict(per), "total": total}


def concentration(per_mint: dict[str, float]) -> dict[str, Any]:
    """Share of P&L from the best coins, and what is left without them.

    From DeeKalshiWay/solana-pumpfun-bot (BLOG_NO_REAL_EDGE.md, tools/concentration_audit.py):
    a trader whose profit vanishes without his top 1-3 coins has a lottery ticket, not an edge.
    """
    vals = sorted(per_mint.values(), reverse=True)
    tot = sum(vals)
    top1 = vals[0] if vals else 0.0
    top3 = sum(vals[:3])
    return {
        "coins": len(vals),
        "top1_share_pct": round(top1 / tot * 100, 1) if tot > 0 else None,
        "top3_share_pct": round(top3 / tot * 100, 1) if tot > 0 else None,
        "pnl_ex_top1": round(tot - top1, 2),
        "pnl_ex_top3": round(tot - top3, 2),
    }


def _concentration(lots: list[Lot], trades: list[dict], r: Rules, per: dict) -> None:
    last = _last_prices(trades)
    by: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for lot in lots:
        v = 0.0
        for frac, px, _t, _why in lot.closes:
            v += _pnl(lot.size * frac, lot.entry, px, r.fee_discount)[0]
        if lot.remaining > 1e-6:
            mark = last.get(lot.mint, (lot.entry, 0))[0] * (1 - r.slippage_pct / 100.0)
            v += _pnl(lot.size * lot.remaining, lot.entry, mark, r.fee_discount)[0]
        by[lot.handle][lot.mint] += v
    for h, pm in by.items():
        per[h]["concentration"] = concentration(pm)


# ---------------------------------------------------------------- variant B
PricePath = Callable[[str, int, int], list[tuple[int, float, float, float]]]  # (t, low, high, close)


def trade_price_path(trades: list[dict]) -> PricePath:
    by_mint: dict[str, list[tuple[int, float]]] = defaultdict(list)
    for t in trades:
        by_mint[t["mint"]].append((t["block_time"], t["price_usd"]))

    def path(mint: str, t0: int, t1: int):
        return [(t, p, p, p) for t, p in by_mint.get(mint, []) if t0 < t <= t1]

    return path


def variant_b(trades: list[dict], r: Rules, path: PricePath | None = None) -> dict[str, Any]:
    path = path or trade_price_path(trades)
    pad = r.slippage_pct / 100.0
    sells: dict[tuple, list[dict]] = defaultdict(list)
    for t in trades:
        if t["side"] == "sell":
            sells[(t["handle"], t["mint"])].append(t)
    end_t = max((t["block_time"] for t in trades), default=0)
    lots: list[Lot] = []
    reasons: dict[str, int] = defaultdict(int)
    for t in trades:
        if t["side"] != "buy":
            continue
        entry = t["price_usd"] * (1 + pad)
        lot = Lot(t["handle"], t["mint"], t["block_time"], entry, r.size_usd)
        trader_exit = next((s for s in sells[(t["handle"], t["mint"])] if s["block_time"] > t["block_time"]), None)
        horizon = trader_exit["block_time"] if trader_exit else end_t
        stop = entry * (1 - r.stop_pct / 100.0)
        peak, trail_on = entry, False
        exit_px, exit_t, why = None, None, None
        for ct, low, high, close in path(t["mint"], t["block_time"], horizon):
            if low <= stop:
                exit_px, exit_t, why = (stop if high >= stop else close), ct, "stop"  # gap down: fill at the real price
                break
            peak = max(peak, high)
            if peak >= entry * (1 + r.trail_start_pct / 100.0):
                trail_on = True
            line = peak * (1 - r.trail_pct / 100.0)
            if trail_on and low <= line:
                exit_px, exit_t, why = (line if high >= line else close), ct, "trail"
                break
            if ct - t["block_time"] >= r.timeout_h * 3600 and close < entry * (1 + r.timeout_min_gain_pct / 100.0):
                exit_px, exit_t, why = close, ct, "timeout"
                break
        if exit_px is None and trader_exit:
            exit_px, exit_t, why = trader_exit["price_usd"], trader_exit["block_time"], "trader_sold"
        if exit_px is not None:
            lot.remaining = 0.0
            lot.closes.append((1.0, exit_px * (1 - pad), exit_t, why))
            reasons[why] += 1
        else:
            reasons["open"] += 1
        lots.append(lot)
    out = _summarize(lots, trades, r)
    out["exit_reasons"] = dict(reasons)
    return out


# ---------------------------------------------------------------- free OHLCV (GeckoTerminal)
class GeckoOhlcv:
    """Free public GeckoTerminal candles, cached in copy_history.db. 30 calls/min max."""

    BASE = "https://api.geckoterminal.com/api/v2/networks/solana"

    def __init__(self, db_path: Path | str = HISTORY_DB, gap_s: float = 2.5) -> None:
        import httpx

        self.conn = sqlite3.connect(str(db_path))
        self.conn.execute("CREATE TABLE IF NOT EXISTS ohlcv (mint TEXT, t INTEGER, low REAL, high REAL, close REAL, PRIMARY KEY(mint,t))")
        self.conn.execute("CREATE TABLE IF NOT EXISTS ohlcv_done (mint TEXT, t0 INTEGER, t1 INTEGER)")
        self.http = httpx.Client(timeout=20.0, headers={"accept": "application/json"})
        self.gap = gap_s
        self._last = 0.0
        self.pools: dict[str, str | None] = {}

    def _get(self, url: str, params: dict | None = None) -> Any:
        for i in range(4):
            w = self.gap - (time.monotonic() - self._last)
            if w > 0:
                time.sleep(w)
            self._last = time.monotonic()
            try:
                r = self.http.get(url, params=params)
            except Exception:
                time.sleep(5)
                continue
            if r.status_code == 429:
                time.sleep(20)
                continue
            if r.status_code != 200:
                return None
            return r.json()
        return None

    def pool(self, mint: str) -> str | None:
        if mint not in self.pools:
            d = self._get(f"{self.BASE}/tokens/{mint}/pools", {"page": 1}) or {}
            data = d.get("data") or []
            self.pools[mint] = (data[0].get("attributes") or {}).get("address") if data else None
        return self.pools[mint]

    def path(self, mint: str, t0: int, t1: int):
        done = self.conn.execute("SELECT 1 FROM ohlcv_done WHERE mint=? AND t0<=? AND t1>=?", (mint, t0, t1)).fetchone()
        if not done:
            pool = self.pool(mint)
            if pool:
                before = t1 + 60
                while before > t0:
                    d = self._get(f"{self.BASE}/pools/{pool}/ohlcv/minute",
                                  {"aggregate": 1, "before_timestamp": before, "limit": 1000, "currency": "usd", "token": mint}) or {}
                    rows = ((d.get("data") or {}).get("attributes") or {}).get("ohlcv_list") or []
                    if not rows:
                        break
                    self.conn.executemany("INSERT OR IGNORE INTO ohlcv VALUES (?,?,?,?,?)",
                                          [(mint, int(x[0]), float(x[3]), float(x[2]), float(x[4])) for x in rows])
                    before = min(int(x[0]) for x in rows)
                    if len(rows) < 1000:
                        break
            self.conn.execute("INSERT INTO ohlcv_done VALUES (?,?,?)", (mint, t0, t1))
            self.conn.commit()
        return [(t, lo, hi, c) for t, lo, hi, c in self.conn.execute(
            "SELECT t, low, high, close FROM ohlcv WHERE mint=? AND t>? AND t<=? ORDER BY t", (mint, t0, t1))]


def run(db_path=HISTORY_DB, json_files=(), rules: Rules | None = None, ohlcv: bool = False) -> dict[str, Any]:
    r = rules or Rules()
    trades = load_trades(db_path, json_files)
    handles = sorted({t["handle"] for t in trades})
    path = GeckoOhlcv(db_path).path if ohlcv else None
    report = {
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "trades_used": len(trades),
        "traders": handles,
        "window": [trades[0]["time_utc"] if trades else None, trades[-1]["time_utc"] if trades else None],
        "rules": r.__dict__,
        "price_path_b": "GeckoTerminal 1m candles (free)" if ohlcv else "only real trade prices in the data (approximate)",
        "A_follow_trader": variant_a(trades, r),
        "B_desk_rules": variant_b(trades, r, path),
        "caveat": CAVEAT,
    }
    return report


def text_report(rep: dict[str, Any]) -> str:
    lines = [f"Бектест копиране · ${rep['rules']['size_usd']:.0f} на покупка · подложка {rep['rules']['slippage_pct']}% · такси FOMO",
             f"Сделки: {rep['trades_used']} · трейдъри: {', '.join(rep['traders']) or '—'}"]
    for key, title in (("A_follow_trader", "A) Излизам, когато трейдърът продаде"),
                       ("B_desk_rules", "B) Правилата на бюрото (стоп/трейл/таймаут) или продажба на трейдъра")):
        v = rep[key]
        tot = v["total"]
        lines.append(f"\n{title}")
        for h, s in sorted(v["per_trader"].items(), key=lambda kv: -kv[1]["pnl"]):
            lines.append(f"  {h}: {s['buys']} покупки · P&L ${s['pnl']:+.2f} (реализ. ${s['realized']:+.2f}, отворени {s['open']}) · печеливши {s['win_rate_pct']}%")
            c = s.get("concentration") or {}
            if c:
                lines.append(f"      концентрация: топ 1 монета {c['top1_share_pct']}% · топ 3 {c['top3_share_pct']}% · без топ 3: ${c['pnl_ex_top3']:+.2f}")
        lines.append(f"  ОБЩО: вложени ${tot['invested']:.2f} · P&L ${tot['pnl']:+.2f} · такси ${tot['fees']:.2f}")
        if "exit_reasons" in v:
            lines.append(f"  изходи: {v['exit_reasons']}")
    lines.append(f"\nЦени за B: {rep['price_path_b']}")
    lines.append("\n" + rep["caveat"])
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(HISTORY_DB))
    ap.add_argument("--json", nargs="*", default=[], help="extra wallet_history_*.json files")
    ap.add_argument("--ohlcv", action="store_true", help="use free GeckoTerminal 1m candles for variant B")
    ap.add_argument("--slippage", type=float, default=1.5)
    ap.add_argument("--out", default=str(LOG_DIR / "backtest_report"))
    a = ap.parse_args(argv)
    rep = run(a.db, [Path(j) for j in a.json], Rules(slippage_pct=a.slippage), ohlcv=a.ohlcv)
    Path(a.out + ".json").write_text(json.dumps(rep, indent=1, ensure_ascii=False), encoding="utf-8")
    txt = text_report(rep)
    Path(a.out + ".txt").write_text(txt, encoding="utf-8")
    print(txt)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

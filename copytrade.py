"""Copy-trading core (PAPER only): parse FOMO trades from public Solana txs.

Read-only. Nothing here places an order, signs anything, calls a swap/execute
endpoint, or touches a private key. Coin links come only from links.py.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from util import ROOT

FOMO_SIGNER = "AgmLJBMDCqWynYnQiPCuj9ewsNNsBJXyzoUhD9LJzN51"
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
# getTransaction must carry maxSupportedTransactionVersion or v0 txs fail with -32015.
TX_OPTS = {"maxSupportedTransactionVersion": 1, "encoding": "jsonParsed", "commitment": "confirmed"}
# Committed public seed (FOMO handles/wallets). Fall back to the historical
# logs/raw path so a local box checkout that still keeps samples there works.
_WALLETS_DATA = ROOT / "data" / "fomo_wallets.json"
_WALLETS_LOGS = ROOT / "logs" / "raw" / "fomo_wallets.json"
WALLETS_PATH = _WALLETS_DATA if _WALLETS_DATA.is_file() else _WALLETS_LOGS
HISTORY_DB = ROOT / "copy_history.db"

DEFAULTS: dict[str, Any] = {
    "copy.enabled": True,
    "copy.size_usd": 10.0,
    "copy.slippage_pct": 1.5,  # PAPER pad: buy higher, sell lower
    "copy.max_age_min": 10,  # never copy a buy older than this (box freezes)
    "copy.min_usdc": 1.0,  # ignore dust trades by the trader
    "copy.poll_s": 90,
    "copy.ws_recycle_s": 300,  # public RPC ws drops ~6 min; recycle before that
    "copy.ws_url": "wss://api.mainnet-beta.solana.com",
    "copy.top_n": 10,
    "copy.resolve_per_day": 2,
    "copy.only_winnable": False,  # when True, copy only traders marked winnable=1
    # Entry gate (meme-strategy upgrade 2026-11-12). Every check fails closed on missing data.
    "copy.gate_enabled": True,  # master switch; false = old behaviour (price + same-mint + stack only)
    "copy.min_liquidity_usd": 8000.0,  # same floor as the scanner; thin pools = fake fills
    "copy.max_chase_pct": 5.0,  # skip when the market is already this far above the trader's fill
    "copy.max_m5_drop_pct": 10.0,  # skip when the coin is dumping (DexScreener priceChange.m5)
    "copy.min_m5_buy_ratio": 0.40,  # skip when m5 buys/(buys+sells) is below this (sell pressure)
    "copy.max_open": 5,  # max open copy positions at once (pile-up cap; 0 = no cap)
    "copy.max_open_per_trader": 2,  # max open copies from one trader (0 = no cap)
    # "fill" = stop 1% under our padded PAPER fill (old, default). "market" = 1% under the market
    # price at entry. Off by default: switching changes what the stop means; owner decides.
    "copy.stop_basis": "fill",
    # GitHub-repo upgrade 2026-11-13 ----------------------------------------------------------
    # Pro-rata exits: trader sells 40% -> we sell 40% of the copy (ChainBuff/open-sol-bot sell_pct).
    # Backtest (4 winnable traders): prints +$1,453 vs +$1,287, 1m candles +$264.55 vs +$264.44.
    "copy.follow_partial_sells": True,
    "copy.partial_full_above": 0.90,  # a sell of >=90% of the trader's bag closes the whole copy
    "copy.partial_min_usd": 2.0,  # never leave a copy remainder smaller than this
    # Top-holder check before a copy (chainstacklabs/pumpfun-bonkfun-bot trading/checks/top_holders.py).
    # 0 = off: cannot be backtested on our history (no holder snapshots). Fails closed when on.
    "copy.max_top10_pct": 0.0,
    # Counterfactual log of gate skips, followed up after N minutes
    # (DeeKalshiWay/solana-pumpfun-bot analyzer/counterfactual.py idea). Pure logging.
    "copy.gate_followup_min": 10,
}


def entry_gate(pair: dict | None, trade: dict, cfg, open_positions: list[dict] | None = None,
               handle: str | None = None) -> tuple[str, str] | None:
    """Return (action, note) when a copy buy should be skipped, else None.

    Pure read of a DexScreener pair dict. Missing data -> skip (fail closed).
    """
    if not bool(setting(cfg, "copy.gate_enabled")):
        return None
    from fields import dex_float  # local import keeps copytrade import-light

    opens = [p for p in (open_positions or []) if p.get("source") == "copy"]
    max_open = int(setting(cfg, "copy.max_open") or 0)
    if max_open > 0 and len(opens) >= max_open:
        return "gate_max_open", f"Вече има {len(opens)} отворени копия (лимит {max_open}) → пропускам."
    per = int(setting(cfg, "copy.max_open_per_trader") or 0)
    if per > 0 and handle and sum(1 for p in opens if p.get("copy_handle") == handle) >= per:
        return "gate_trader_pileup", f"{handle} вече има {per} отворени копия → пропускам."

    liq = dex_float(pair, "liquidity.usd")
    min_liq = float(setting(cfg, "copy.min_liquidity_usd") or 0)
    if min_liq > 0 and liq is None and str((pair or {}).get("dexId") or "").lower() == "pumpfun":
        return "gate_bonding_curve", "Монетата е още на pump.fun кривата (няма пул/ликвидност) → пропускам (fail closed)."
    if min_liq > 0 and (liq is None or liq < min_liq):
        return "gate_low_liq", f"Ликвидност {'няма данни' if liq is None else f'${liq:,.0f}'} < ${min_liq:,.0f} → пропускам."

    price = dex_float(pair, "priceUsd")
    ref = float(trade.get("price_usd") or 0)
    chase = float(setting(cfg, "copy.max_chase_pct") or 0)
    if chase > 0 and price and ref > 0 and price > ref * (1 + chase / 100.0):
        return "gate_chase", f"Цената е +{(price / ref - 1) * 100:.1f}% над входа на трейдъра (> {chase:g}%) → закъснели сме."

    drop = float(setting(cfg, "copy.max_m5_drop_pct") or 0)
    if drop > 0:
        m5 = dex_float(pair, "priceChange.m5")
        if m5 is None:
            return "gate_no_m5", "Няма 5-мин. ценова промяна (fail closed) → пропускам."
        if m5 <= -drop:
            return "gate_dumping", f"Монетата пада {m5:.1f}% за 5 мин. → пропускам."

    ratio_min = float(setting(cfg, "copy.min_m5_buy_ratio") or 0)
    if ratio_min > 0:
        buys = dex_float(pair, "txns.m5.buys")
        sells = dex_float(pair, "txns.m5.sells")
        if buys is None or sells is None or buys + sells <= 0:
            return "gate_no_flow", "Няма данни за покупки/продажби за 5 мин. (fail closed) → пропускам."
        ratio = buys / (buys + sells)
        if ratio < ratio_min:
            return "gate_sell_pressure", f"Продавачите водят: {ratio:.0%} покупки за 5 мин. (< {ratio_min:.0%}) → пропускам."
    return None


def setting(cfg, key: str) -> Any:
    """copy.* settings: DB kv override (if any) else DEFAULTS."""
    default = DEFAULTS.get(key)
    db = getattr(cfg, "db", None)
    if db is not None:
        try:
            raw = db.kv_get("setting." + key)
        except Exception:
            raw = None
        if raw is not None:
            try:
                return type(default)(json.loads(raw)) if default is not None else json.loads(raw)
            except Exception:
                return default
    return default


def copyable_traders(db, cfg) -> list[dict]:
    """Active traders that CopyWatcher may follow.

    When copy.only_winnable is on, a trader must also have winnable=1.
    Daily refresh must not promote anyone until they are scored winnable.
    """
    rows = db.query("SELECT handle, wallet, active, winnable FROM copy_traders WHERE active=1 AND wallet IS NOT NULL")
    only = bool(setting(cfg, "copy.only_winnable"))
    out = []
    for r in rows:
        if only and int(r["winnable"] or 0) != 1:
            continue
        out.append(dict(r))
    return out


def can_activate(cfg, row: dict | None) -> bool:
    """False when only_winnable is on and the row is not already scored winnable."""
    if not bool(setting(cfg, "copy.only_winnable")):
        return True
    if not row:
        return False
    try:
        return int(row.get("winnable") or 0) == 1
    except (TypeError, ValueError):
        return False


def _ui(b: dict) -> float:
    amt = b.get("uiTokenAmount") or {}
    v = amt.get("uiAmount")
    if v is None:
        v = amt.get("uiAmountString")
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def signers_of(tx: dict) -> list[str]:
    keys = (((tx or {}).get("transaction") or {}).get("message") or {}).get("accountKeys") or []
    out = []
    for k in keys:
        if isinstance(k, dict) and k.get("signer"):
            out.append(k.get("pubkey"))
    return out


def parse_fomo_trade(tx: dict | None, wallet: str, signature: str | None = None) -> dict | None:
    """Same rule as wallet_history_zelda89.json.

    FOMO trade = tx co-signed by the FOMO signer; side/amount/price from the
    owner's USDC and token balance deltas. Returns None for anything else.
    """
    if not tx or not isinstance(tx, dict):
        return None
    meta = tx.get("meta") or {}
    if meta.get("err"):
        return None
    if FOMO_SIGNER not in signers_of(tx):
        return None
    pre = {b["mint"]: _ui(b) for b in meta.get("preTokenBalances") or [] if b.get("owner") == wallet}
    post = {b["mint"]: _ui(b) for b in meta.get("postTokenBalances") or [] if b.get("owner") == wallet}
    d = {m: post.get(m, 0.0) - pre.get(m, 0.0) for m in set(pre) | set(post)}
    usdc = d.pop(USDC_MINT, 0.0)
    d = {m: v for m, v in d.items() if abs(v) > 0}
    if not d or abs(usdc) < 0.01:
        return None
    mint, q = max(d.items(), key=lambda kv: abs(kv[1]))
    side = "buy" if q > 0 else "sell"
    bt = tx.get("blockTime")
    sig = signature
    if sig is None:
        sigs = ((tx.get("transaction") or {}).get("signatures")) or []
        sig = sigs[0] if sigs else None
    return {
        "time_utc": datetime.fromtimestamp(bt, timezone.utc).isoformat() if bt else None,
        "block_time": bt,
        "signature": sig,
        "mint": mint,
        "side": side,
        "token_amount": abs(q),
        "usdc": abs(usdc),
        "price_usd": abs(usdc) / abs(q),
        # Trader's token balance before this tx: lets a sell be copied pro-rata
        # (idea from ChainBuff/open-sol-bot app/trading/trading/copytrade.py, sell_pct).
        "pre_token_amount": pre.get(mint, 0.0),
    }


def sell_fraction(trade: dict) -> float:
    """Share of the trader's position this sell closed (1.0 when unknown)."""
    pre = float(trade.get("pre_token_amount") or 0)
    q = float(trade.get("token_amount") or 0)
    if pre <= 0 or q <= 0:
        return 1.0
    return max(0.0, min(1.0, q / pre))


def load_seed_wallets(path: Path | None = None) -> dict[str, str]:
    """handle -> wallet from data/fomo_wallets.json (or logs/raw fallback)."""
    p = path or WALLETS_PATH
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}
    out = {}
    for h, v in (data.get("wallets") or {}).items():
        w = v.get("wallet") if isinstance(v, dict) else v
        if w:
            out[h] = w
    return out


def source_label(handle: str | None) -> str | None:
    return f"копие от {handle}" if handle else None

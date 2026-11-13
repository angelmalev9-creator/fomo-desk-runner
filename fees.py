"""FOMO fee and PnL arithmetic for paper and live fills (STEP 11).

Solana fee schedule, each side of a trade:
  $5 to $47.50     -> 2% of the ticket
  $47.50 to $190   -> flat $0.95
  above $190       -> 0.50% of the ticket
Then minus fees_ref_discount (default 10% off through the referral).
Gas is 0: FOMO pays it.

At $47.50, 2% equals $0.95; at $190, 0.50% equals $0.95, so the
brackets meet. Tickets at exactly $47.50 or $190 use the flat $0.95
bracket (equivalent to the adjacent percentage).
"""

from __future__ import annotations

from util import as_float, round_cents

MIN_TICKET = 47.50
FLAT_FEE = 0.95
FLAT_UNTIL = 190.0
LOW_RATE = 0.02
HIGH_RATE = 0.005
DEFAULT_DISCOUNT = 0.10


def ticket_fee(amount_usd: float, ref_discount: float = DEFAULT_DISCOUNT) -> float:
    """Fee charged on one side (buy or sell) after the referral discount."""
    amount = as_float(amount_usd, 0.0) or 0.0
    if amount <= 0:
        return 0.0
    if amount < MIN_TICKET:
        raw = amount * LOW_RATE
    elif amount <= FLAT_UNTIL:
        raw = FLAT_FEE
    else:
        raw = amount * HIGH_RATE
    discount = as_float(ref_discount, 0.0) or 0.0
    discount = max(0.0, min(1.0, discount))
    return round_cents(raw * (1.0 - discount))


def round_trip_fees(
    size_usd: float,
    entry_price: float,
    exit_price: float,
    ref_discount: float = DEFAULT_DISCOUNT,
) -> float:
    """Fees on both sides. Sell notional is marked at the exit price."""
    entry = as_float(entry_price, 0.0) or 0.0
    exit_ = as_float(exit_price, 0.0) or 0.0
    size = as_float(size_usd, 0.0) or 0.0
    buy = ticket_fee(size, ref_discount)
    if entry > 0:
        sell_notional = size * (exit_ / entry)
    else:
        sell_notional = size
    sell = ticket_fee(sell_notional, ref_discount)
    return round_cents(buy + sell)


def pnl_usd(
    size_usd: float,
    entry_price: float,
    exit_price: float,
    ref_discount: float = DEFAULT_DISCOUNT,
) -> tuple[float, float]:
    """Return (pnl_after_fees, fees_usd)."""
    entry = as_float(entry_price, 0.0) or 0.0
    exit_ = as_float(exit_price, 0.0) or 0.0
    size = as_float(size_usd, 0.0) or 0.0
    if entry <= 0:
        gross = 0.0
    else:
        gross = size * ((exit_ - entry) / entry)
    fees = round_trip_fees(size, entry, exit_, ref_discount)
    return round_cents(gross - fees), fees

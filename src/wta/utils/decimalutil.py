"""Exchange precision helpers. Never use float for order fields.

Rules (Overview §8): quantities round DOWN to stepSize decimals; BUY prices round DOWN and
SELL prices round UP to tickSize decimals (both are conservative for a maker order).
"""

from __future__ import annotations

from decimal import ROUND_DOWN, ROUND_UP, Decimal

from wta.models import MarketInfo, Side


def D(x: object) -> Decimal:
    """Safe Decimal constructor (floats go through str to avoid binary artefacts)."""
    if isinstance(x, Decimal):
        return x
    if isinstance(x, float):
        return Decimal(repr(x))
    return Decimal(str(x))


def quantize_down(value: Decimal, decimals: int) -> Decimal:
    return value.quantize(Decimal(1).scaleb(-decimals), rounding=ROUND_DOWN)


def quantize_up(value: Decimal, decimals: int) -> Decimal:
    return value.quantize(Decimal(1).scaleb(-decimals), rounding=ROUND_UP)


def tick(market: MarketInfo) -> Decimal:
    return market.tick


def round_price(market: MarketInfo, price: Decimal, side: Side) -> Decimal:
    if side == "BUY":
        return quantize_down(price, market.tick_decimals)
    return quantize_up(price, market.tick_decimals)


def round_price_nearest(market: MarketInfo, price: Decimal) -> Decimal:
    return price.quantize(market.tick)


def round_qty(market: MarketInfo, qty: Decimal) -> Decimal:
    q = quantize_down(qty, market.step_decimals)
    return q if q > 0 else Decimal(0)


def notional(qty: Decimal, price: Decimal) -> Decimal:
    return qty * price


def check_min_notional(market: MarketInfo, qty: Decimal, price: Decimal) -> bool:
    """True when the order satisfies minQty and minNotional after rounding."""
    q = round_qty(market, qty)
    if q <= 0 or q < market.min_qty:
        return False
    return q * price >= market.min_notional


def fmt(value: Decimal | None, decimals: int | None = None) -> str:
    """Human formatting for reason strings (thousands separators)."""
    if value is None:
        return "n/a"
    if decimals is None:
        decimals = 0 if abs(value) >= 1000 else 4
    return f"{value:,.{decimals}f}"


def pct(value: Decimal | float | None, decimals: int = 2) -> str:
    if value is None:
        return "n/a"
    return f"{Decimal(str(value)) * 100:.{decimals}f}%"

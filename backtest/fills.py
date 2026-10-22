"""Pessimistic fill model for backtests (PRD-05 §3.2): pure functions, no I/O, no clock.

``wta.exchange.backtest.BacktestExchange`` applies these rules; they live here so every rule can be
read and tested on its own. All prices and quantities are ``Decimal``.

Candle driver (1-minute bars). The engine sees a synthetic book per bar (``synthetic_quotes``:
mid = close, spread = the market's median observed spread, unlimited depth at best: we are tiny).

* Latency: every order acts one step later. An order placed while bar ``k`` is current (the
  engine saw bar ``k``'s synthetic book) is looked at no earlier than bar ``k + 1``, and each bar at
  most once. The exchange enforces this; the functions below assume the bar they get is such a
  later bar.
* Resting LIMIT (not crossing when it was placed): BUY fills when ``bar.l <= price``, at
  ``min(price, bar.o)`` (gap-through: the bar opened below the limit); SELL fills when
  ``bar.h >= price``, at ``max(price, bar.o)``. Full quantity (unlimited depth at best).
* MARKET: fills at ``close x (1 + spread/2 + slippage)`` (BUY) or ``close x (1 - spread/2 -
  slippage)`` (SELL), ``slippage = 0.02 %``, rounded to the tick against us (``taker_price``).
* LIMIT that was crossing when it was placed (``best_ask <= price`` for a BUY), on its first
  (one-step-later) bar: the taker price above when it respects the limit; else, if the order still
  crosses that bar's synthetic book (BUY: ``ask <= limit``; only the slippage breaks it), at its
  limit price -- a limit order never pays more than its limit, and the limit is the worst price it
  allows; else it no longer crosses and rests: later bars use the resting rule (taker fee kept).
* STOP_LIMIT triggers when ``bar.l <= stop`` (stop below the market, e.g. a stop-loss SELL) or
  ``bar.h >= stop`` (stop above, e.g. a breakout BUY); the direction is fixed at placement like the
  PaperExchange's. It then acts in its trigger bar as a LIMIT at its limit price, conservatively
  (SELL; a BUY mirrors it with ``high`` and ``+``):
    - the bar never traded below the limit (``low >= limit``)     -> all at the limit;
    - it traded below by at most 2x the slippage allowance          -> all at ``limit - one tick``
      (never below the bar's low);
    - it traded below by more than that (stress)                    -> 50 % at the limit and the
      rest at the bar's low.
  No fill price is better than where the market was when the stop fired (the stop, or the open
  when the bar gapped through it): when the bar opened beyond the limit, the "at the limit" legs
  are filled at the open instead. The order enters the book at its trigger and takes liquidity in
  that same bar, so every STOP_LIMIT fill pays the taker fee.
  STOP_MARKET fills in its trigger bar at the taker price of the worse of trigger price and close.
* Fee: maker when the order was not crossing when it was placed, else taker, for every fill of
  the order (pessimistic); MARKET and STOP_* fills are always taker.

Book driver (recorded snapshots): the PaperExchange matching (walk the opposite levels), but a
fill takes at most ``level_fraction`` (80 %) of a level's quantity (``walk_levels``) and only a
snapshot newer than the one the order was placed on can fill it.
"""

from __future__ import annotations

import statistics
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, replace
from decimal import Decimal
from typing import Literal

from wta.models import BookSnapshot, Candle, Level, OrderType, Side
from wta.utils.decimalutil import quantize_down, quantize_up

D0 = Decimal(0)
DEFAULT_SLIPPAGE = Decimal("0.0002")  # 0.02 % (candle driver, MARKET / crossing LIMIT)
DEFAULT_LEVEL_FRACTION = Decimal("0.8")  # book driver: at most 80 % of a level's quantity
STRESS_GAP_MULTIPLE = Decimal(2)  # STOP_LIMIT: gap beyond the limit > 2 x slippage -> stress fill
STRESS_FILL_FRACTION = Decimal("0.5")  # ... 50 % at the limit, the rest at the bar extreme

StopDirection = Literal["down", "up"]
FillRule = Literal[
    "none",  # nothing happened in this bar
    "limit",  # resting LIMIT filled at its own price
    "gap_through",  # resting LIMIT filled at the bar open (the bar opened through the limit)
    "taker",  # MARKET / crossing LIMIT / STOP_MARKET at close x (1 +/- spread/2 +/- slippage)
    "taker_limit",  # crossing LIMIT still crossing, but the slippage breaks its limit: at the limit
    "rest",  # crossing LIMIT that no longer crosses on its first bar: it rests from now on
    "stop_wait",  # STOP_* not triggered
    "stop_at_limit",  # STOP_LIMIT triggered, the bar never went beyond the limit
    "stop_tick",  # STOP_LIMIT triggered, small gap beyond the limit: limit -/+ one tick
    "stop_stress",  # STOP_LIMIT triggered, gap > 2 x slippage: 50 % at limit, rest at low/high
]

_RESOLUTION_SECONDS = {"D": 86_400, "1D": 86_400, "W": 604_800, "1W": 604_800}


# ---------------------------------------------------------------- small helpers


def bar_seconds(resolution: str) -> int:
    """Length of a bar: ``"1"`` -> 60, ``"15"`` -> 900, ``"60"`` -> 3600, ``"1D"``/``"D"`` -> 86400."""
    key = resolution.strip().upper()
    if key in _RESOLUTION_SECONDS:
        return _RESOLUTION_SECONDS[key]
    try:
        minutes = int(key)
    except ValueError:
        raise ValueError(f"unknown candle resolution {resolution!r}") from None
    if minutes <= 0:
        raise ValueError(f"unknown candle resolution {resolution!r}")
    return minutes * 60


def tick_size(tick_decimals: int) -> Decimal:
    return Decimal(1).scaleb(-tick_decimals)


def round_against(price: Decimal, side: Side, tick_decimals: int) -> Decimal:
    """Round a fill price to the tick against us: a BUY pays up, a SELL receives down."""
    return quantize_up(price, tick_decimals) if side == "BUY" else quantize_down(price, tick_decimals)


def is_crossing(
    side: Side, price: Decimal | None, best_bid: Decimal | None, best_ask: Decimal | None
) -> bool:
    """Would a LIMIT at ``price`` trade at once (BUY: ``best_ask <= price``; SELL: ``best_bid >= price``)?"""
    if price is None:
        return False
    if side == "BUY":
        return best_ask is not None and best_ask <= price
    return best_bid is not None and best_bid >= price


def fee_is_maker(crossing_at_placement: bool) -> bool:
    """Maker fee when the order was not crossing when it entered the book, else taker."""
    return not crossing_at_placement


def median_spread(books: Iterable[BookSnapshot]) -> Decimal | None:
    """Median ``spread_pct`` of the two-sided books in ``books`` (``None`` when there are none)."""
    spreads = sorted(s for s in (b.spread_pct for b in books) if s is not None and s >= 0)
    return statistics.median(spreads) if spreads else None


def synthetic_quotes(close: Decimal, spread_pct: Decimal, tick_decimals: int) -> tuple[Decimal, Decimal]:
    """``(bid, ask)`` of the candle driver's synthetic book: mid = close, ``ask - bid ~ spread``.

    The half spread is rounded UP to the tick, so for a close on the tick grid the mid is exactly
    the close and the spread is never narrower than ``spread_pct``. A spread that rounds to zero
    becomes one tick (``ask = bid + tick``).
    """
    tick = tick_size(tick_decimals)
    half = quantize_up(close * spread_pct / 2, tick_decimals)
    bid = quantize_down(close - half, tick_decimals)
    ask = quantize_up(close + half, tick_decimals)
    if ask <= bid:
        ask = bid + tick
    return bid, ask


def taker_price(
    side: Side,
    close: Decimal,
    spread_pct: Decimal,
    slippage: Decimal = DEFAULT_SLIPPAGE,
    tick_decimals: int | None = None,
) -> Decimal:
    """MARKET / crossing price: ``close x (1 + spread/2 + slippage)`` (BUY) or ``x (1 - ...)`` (SELL).

    With ``tick_decimals`` the price is rounded to the tick against us.
    """
    half = spread_pct / 2
    raw = close * (1 + half + slippage) if side == "BUY" else close * (1 - half - slippage)
    return raw if tick_decimals is None else round_against(raw, side, tick_decimals)


def limit_fill_price(side: Side, price: Decimal, bar: Candle) -> Decimal | None:
    """Resting LIMIT in a later bar: BUY ``low <= price`` -> ``min(price, open)``;
    SELL ``high >= price`` -> ``max(price, open)``. ``None`` when the bar does not reach it."""
    if side == "BUY":
        return min(price, bar.o) if bar.l <= price else None
    return max(price, bar.o) if bar.h >= price else None


def default_stop_direction(side: Side) -> StopDirection:
    """Without a reference price a SELL stop is a stop-loss (down), a BUY stop a breakout (up)."""
    return "down" if side == "SELL" else "up"


def stop_triggered(direction: StopDirection, stop: Decimal, bar: Candle) -> bool:
    """``down``: ``bar.l <= stop``; ``up``: ``bar.h >= stop``."""
    return bar.l <= stop if direction == "down" else bar.h >= stop


def stop_trigger_price(direction: StopDirection, stop: Decimal, bar: Candle) -> Decimal:
    """Where the market was when the stop fired: the stop, or the open when the bar gapped through it."""
    return min(stop, bar.o) if direction == "down" else max(stop, bar.o)


@dataclass(frozen=True)
class FillLeg:
    """One fill of an order: ``qty`` at ``price``, maker or taker fee."""

    price: Decimal
    qty: Decimal
    is_maker: bool


def stop_limit_legs(
    side: Side,
    limit: Decimal,
    qty: Decimal,
    bar: Candle,
    tick_decimals: int,
    step_decimals: int,
    slippage: Decimal = DEFAULT_SLIPPAGE,
    trigger: Decimal | None = None,
) -> tuple[tuple[FillLeg, ...], FillRule]:
    """Fills of a STOP_LIMIT that triggered in ``bar`` (all taker).

    SELL (BUY is the mirror image with ``high`` and ``+``):
    ``low >= limit`` -> all at ``limit``; ``(limit - low) / limit > 2 x slippage`` -> 50 % at
    ``limit`` and the rest at ``low`` (stress); otherwise all at ``max(limit - tick, low)``.
    With ``trigger`` (where the market was when the stop fired) no leg is better than it: a SELL
    leg is at most ``trigger``, a BUY leg at least ``trigger`` (the bar gapped through the limit).
    """
    tick = tick_size(tick_decimals)
    if side == "SELL":
        extreme = bar.l
        beyond = limit - extreme  # how far the market went below our sell limit
        conservative = max(limit - tick, extreme)
    else:
        extreme = bar.h
        beyond = extreme - limit  # how far the market went above our buy limit
        conservative = min(limit + tick, extreme)

    def leg(price: Decimal, leg_qty: Decimal) -> FillLeg:
        if trigger is not None:
            price = min(price, trigger) if side == "SELL" else max(price, trigger)
        return FillLeg(price, leg_qty, False)

    if beyond <= 0:
        return (leg(limit, qty),), "stop_at_limit"
    if beyond / limit > STRESS_GAP_MULTIPLE * slippage:
        first = quantize_down(qty * STRESS_FILL_FRACTION, step_decimals)
        legs = [leg(limit, first), leg(extreme, qty - first)]
        return tuple(x for x in legs if x.qty > 0), "stop_stress"
    return (leg(conservative, qty),), "stop_tick"


# ---------------------------------------------------------------- candle driver: one order, one bar


@dataclass(frozen=True)
class CandleOrder:
    """What the candle model needs to know about an open order (state between bars)."""

    side: Side
    type: OrderType
    qty: Decimal  # remaining quantity
    price: Decimal | None = None  # limit price (LIMIT / STOP_LIMIT)
    stop_price: Decimal | None = None
    stop_direction: StopDirection | None = None  # None: ``default_stop_direction(side)``
    triggered: bool = False  # STOP_* only: already triggered
    crossing: bool = False  # crossing when it was placed (STOP_LIMIT: triggered) -> taker fee
    first_pass: bool = True  # a crossing LIMIT not looked at yet (its taker attempt is due)

    @property
    def is_stop(self) -> bool:
        return self.type in ("STOP_LIMIT", "STOP_MARKET")


@dataclass(frozen=True)
class CandleMatch:
    """Result of ``match_candle``: the fills, the order's state afterwards and the rule that fired."""

    order: CandleOrder
    legs: tuple[FillLeg, ...] = ()
    rule: FillRule = "none"
    triggered_now: bool = False  # a STOP_* triggered in this bar
    cancel_remainder: bool = False  # MARKET / STOP_MARKET: what did not fill is canceled

    @property
    def filled_qty(self) -> Decimal:
        return sum((leg.qty for leg in self.legs), D0)


def match_candle(
    order: CandleOrder,
    bar: Candle,
    *,
    spread_pct: Decimal,
    tick_decimals: int,
    step_decimals: int,
    slippage: Decimal = DEFAULT_SLIPPAGE,
) -> CandleMatch:
    """Apply the candle rules (module docstring) to one open order and one later bar."""
    if order.qty <= 0:
        return CandleMatch(order=order)
    if order.is_stop and not order.triggered:
        return _match_stop_trigger(order, bar, spread_pct, tick_decimals, step_decimals, slippage)
    if order.type in ("MARKET", "STOP_MARKET"):
        px = taker_price(order.side, bar.c, spread_pct, slippage, tick_decimals)
        done = replace(order, qty=D0, first_pass=False)
        return CandleMatch(
            order=done, legs=(FillLeg(px, order.qty, False),), rule="taker", cancel_remainder=True
        )
    return _match_limit(order, bar, spread_pct, tick_decimals, slippage)


def _match_limit(
    order: CandleOrder, bar: Candle, spread_pct: Decimal, tick_decimals: int, slippage: Decimal
) -> CandleMatch:
    limit = order.price
    if limit is None:  # a LIMIT always has a price (validated by the exchange)
        return CandleMatch(order=order)
    if order.crossing and order.first_pass:
        done = replace(order, qty=D0, first_pass=False)
        px = taker_price(order.side, bar.c, spread_pct, slippage, tick_decimals)
        if (px <= limit) if order.side == "BUY" else (px >= limit):
            return CandleMatch(order=done, legs=(FillLeg(px, order.qty, False),), rule="taker")
        bid, ask = synthetic_quotes(bar.c, spread_pct, tick_decimals)
        if is_crossing(order.side, limit, bid, ask):  # only the slippage breaks the limit
            return CandleMatch(order=done, legs=(FillLeg(limit, order.qty, False),), rule="taker_limit")
        return CandleMatch(order=replace(order, first_pass=False), rule="rest")
    px = limit_fill_price(order.side, limit, bar)
    if px is None:
        return CandleMatch(order=replace(order, first_pass=False))
    leg = FillLeg(px, order.qty, fee_is_maker(order.crossing))
    rule: FillRule = "limit" if px == limit else "gap_through"
    return CandleMatch(order=replace(order, qty=D0, first_pass=False), legs=(leg,), rule=rule)


def _match_stop_trigger(
    order: CandleOrder,
    bar: Candle,
    spread_pct: Decimal,
    tick_decimals: int,
    step_decimals: int,
    slippage: Decimal,
) -> CandleMatch:
    stop = order.stop_price
    direction = order.stop_direction or default_stop_direction(order.side)
    if stop is None or not stop_triggered(direction, stop, bar):
        return CandleMatch(order=order, rule="stop_wait")
    trigger = stop_trigger_price(direction, stop, bar)
    if order.type == "STOP_MARKET":
        ref = min(trigger, bar.c) if order.side == "SELL" else max(trigger, bar.c)
        px = taker_price(order.side, ref, spread_pct, slippage, tick_decimals)
        done = replace(order, qty=D0, triggered=True, crossing=True, first_pass=False)
        return CandleMatch(
            order=done,
            legs=(FillLeg(px, order.qty, False),),
            rule="taker",
            triggered_now=True,
            cancel_remainder=True,
        )
    limit = order.price
    if limit is None:  # a STOP_LIMIT always has a price (validated by the exchange)
        return CandleMatch(order=order, rule="stop_wait")
    legs, rule = stop_limit_legs(
        order.side, limit, order.qty, bar, tick_decimals, step_decimals, slippage, trigger=trigger
    )
    filled = sum((leg.qty for leg in legs), D0)
    after = replace(order, qty=order.qty - filled, triggered=True, crossing=True, first_pass=False)
    return CandleMatch(order=after, legs=legs, rule=rule, triggered_now=True)


# ---------------------------------------------------------------- book driver


def opposite_levels(book: BookSnapshot, side: Side) -> list[Level]:
    """Levels a ``side`` order trades against, best first (asks ascending / bids descending)."""
    if side == "BUY":
        return sorted(book.asks, key=lambda lv: lv.price)
    return sorted(book.bids, key=lambda lv: lv.price, reverse=True)


def walk_levels(
    levels: Iterable[Level],
    remaining: Decimal,
    price_ok: Callable[[Decimal], bool],
    taken: Mapping[Decimal, Decimal] | None = None,
    level_fraction: Decimal = DEFAULT_LEVEL_FRACTION,
    step_decimals: int = 8,
) -> list[tuple[Decimal, Decimal]]:
    """Take liquidity level by level (best first) while ``price_ok``: ``[(level price, qty)]``.

    A level offers at most ``level_fraction x qty`` minus what earlier fills already ``taken``
    from it in this snapshot; quantities are rounded down to the step.
    """
    taken = taken or {}
    out: list[tuple[Decimal, Decimal]] = []
    for level in levels:
        if remaining <= 0 or not price_ok(level.price):
            break
        available = level.qty * level_fraction - taken.get(level.price, D0)
        qty = quantize_down(min(remaining, available), step_decimals)
        if qty <= 0:
            continue
        remaining -= qty
        out.append((level.price, qty))
    return out

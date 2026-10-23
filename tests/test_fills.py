"""PRD-05 §3.2 pessimistic fill model (``backtest/fills.py``) and ``BacktestExchange``.

Pure rules first (later-bar limit, gap-through, MARKET / crossing price with spread + slippage,
STOP_LIMIT trigger + conservative tick + 2x-gap stress partial, fee selection, the book driver's
80 % level cap, synthetic book shape), then the exchange: latency on both drivers, end-to-end runs
on a few bars, balances, state round trip, and end-to-end runs over SyntheticFeed data recorded
into a temporary Store (there is no recorded Wallex data on this machine).

Markets/fees come from conftest: BTCUSDT (tick 2, step 6, maker 0.10 %, taker 0.13 %),
USDTTMN (tick 0, step 2, maker 0.25 %, taker 0.30 %); both equal the fee snapshot.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path

import pytest

from backtest.fills import (
    DEFAULT_LEVEL_FRACTION,
    DEFAULT_SLIPPAGE,
    CandleOrder,
    FillLeg,
    bar_seconds,
    fee_is_maker,
    is_crossing,
    limit_fill_price,
    match_candle,
    median_spread,
    opposite_levels,
    round_against,
    stop_limit_legs,
    stop_trigger_price,
    stop_triggered,
    synthetic_quotes,
    taker_price,
    walk_levels,
)
from wta.data.store import Store
from wta.data.synthetic import SyntheticFeed, SyntheticParams
from wta.exchange.backtest import (
    DEFAULT_SPREAD_PCT,
    SYNTHETIC_DEPTH_QTY,
    BacktestExchange,
    apply_fee_snapshot,
    make_synthetic_book,
    observed_spreads,
)
from wta.exchange.base import MarketDataBus
from wta.exchange.markets import snapshot_reference
from wta.exchange.paper import PaperExchange
from wta.models import Balance, BookSnapshot, Candle, Fill, Level, MarketInfo, OrderRequest
from wta.utils.decimalutil import D, quantize_down, quantize_up
from wta.utils.timeutil import SimClock

from .conftest import T0

USDT_MAKER, USDT_TAKER = D("0.001"), D("0.0013")
TMN_MAKER, TMN_TAKER = D("0.0025"), D("0.003")
START = {"TMN": D("1000000000"), "USDT": D("10000"), "BTC": D("1")}
BAR0 = int(T0)  # 2026-10-22T10:00:00Z, a minute boundary
BTC_SPREAD = D("0.0008")  # half spread 0.04 %, + slippage 0.02 % => taker at close x (1 +/- 0.0006)


def bar(i: int, o: str, h: str, low: str, c: str, symbol: str = "BTCUSDT") -> Candle:
    """The ``i``-th 1-minute bar after BAR0."""
    return Candle(symbol=symbol, resolution="1", ts=BAR0 + 60 * i, o=D(o), h=D(h), l=D(low), c=D(c), v=D("1"))


def flat(i: int, price: str, symbol: str = "BTCUSDT") -> Candle:
    return bar(i, price, price, price, price, symbol)


def req(
    cid: str,
    side: str,
    type_: str,
    qty: str,
    price: str | None = None,
    stop: str | None = None,
    symbol: str = "BTCUSDT",
) -> OrderRequest:
    return OrderRequest(
        client_id=cid,
        symbol=symbol,
        side=side,  # type: ignore[arg-type]
        type=type_,  # type: ignore[arg-type]
        qty=D(qty),
        price=None if price is None else D(price),
        stop_price=None if stop is None else D(stop),
        strategy="S3",
        purpose="entry",
        decision_id=f"d_{cid}",
    )


async def bal(ex: PaperExchange, asset: str) -> Balance:
    return (await ex.get_balances()).get(asset, Balance(asset=asset, free=D("0")))


def expected_totals(
    start: dict[str, Decimal], fills: list[Fill], markets: dict[str, MarketInfo]
) -> dict[str, Decimal]:
    """Free + locked per asset implied by ``start`` and the fills (fee in the asset received)."""
    out = dict(start)
    for f in fills:
        m = markets[f.symbol]
        if f.side == "BUY":
            out[m.quote] = out.get(m.quote, D("0")) - f.qty * f.price
            out[m.base] = out.get(m.base, D("0")) + f.qty - f.fee
        else:
            out[m.base] = out.get(m.base, D("0")) - f.qty
            out[m.quote] = out.get(m.quote, D("0")) + f.qty * f.price - f.fee
    return out


async def assert_balances_consistent(ex: PaperExchange, start: dict[str, Decimal]) -> None:
    """Totals match the fills exactly; nothing negative; nothing locked without open orders."""
    balances = await ex.get_balances()
    markets = await ex.get_markets()
    want = expected_totals(start, await ex.get_my_trades(), markets)
    for asset, amount in want.items():
        got = balances.get(asset, Balance(asset=asset, free=D("0")))
        assert got.total == amount, asset
    for b in balances.values():
        assert b.free >= 0 and b.locked >= 0, b
    if not await ex.get_open_orders():
        assert all(b.locked == 0 for b in balances.values())


@pytest.fixture
def clock() -> SimClock:
    return SimClock(T0)


@pytest.fixture
def ex(markets: dict[str, MarketInfo], clock: SimClock) -> BacktestExchange:
    return BacktestExchange(
        markets,
        clock,
        driver="candles",
        spreads={"BTCUSDT": BTC_SPREAD, "USDTTMN": D("0.00014")},
        initial_balances=dict(START),
    )


def step(ex: BacktestExchange, clock: SimClock, candle: Candle) -> list[Fill]:
    """Runner step: the bar closes, it becomes current, the matching pass runs at its close time."""
    now = float(candle.ts + 60)
    clock.set(now)
    ex.set_bar(candle.symbol, candle)
    return ex.on_tick(now)


# ================================================================ pure rules


def test_bar_seconds() -> None:
    assert [bar_seconds(r) for r in ("1", "15", "60", "1D", "D")] == [60, 900, 3600, 86400, 86400]
    for bad in ("x", "0", "-5"):
        with pytest.raises(ValueError):
            bar_seconds(bad)


def test_resting_limit_needs_the_bar_to_reach_the_price() -> None:
    assert limit_fill_price("BUY", D("100"), bar(1, "101", "102", "100.5", "101")) is None
    assert limit_fill_price("BUY", D("100"), bar(1, "101", "102", "100", "101")) == D("100")  # touch fills
    assert limit_fill_price("SELL", D("100"), bar(1, "99", "99.5", "98", "99")) is None
    assert limit_fill_price("SELL", D("100"), bar(1, "99", "100", "98", "99")) == D("100")


def test_gap_through_fills_at_the_open() -> None:
    # BUY 100: the bar opened at 99, below the limit -> min(price, open)
    assert limit_fill_price("BUY", D("100"), bar(1, "99", "99.5", "98", "99")) == D("99")
    # SELL 100: the bar opened at 101, above the limit -> max(price, open)
    assert limit_fill_price("SELL", D("100"), bar(1, "101", "102", "100.5", "101")) == D("101")


def test_taker_price_adds_half_spread_and_slippage_against_us() -> None:
    assert DEFAULT_SLIPPAGE == D("0.0002")
    assert taker_price("BUY", D("100"), D("0.001")) == D("100.0700")  # 100 x (1 + 0.0005 + 0.0002)
    assert taker_price("SELL", D("100"), D("0.001")) == D("99.9300")
    # rounded to the tick against us: BUY up, SELL down
    assert taker_price("BUY", D("100"), D("0.001"), tick_decimals=1) == D("100.1")
    assert taker_price("SELL", D("100"), D("0.001"), tick_decimals=1) == D("99.9")
    assert round_against(D("10.01"), "BUY", 0) == D("11") and round_against(D("10.99"), "SELL", 0) == D("10")
    assert taker_price("BUY", D("82000"), BTC_SPREAD, D("0.0005")) == D("82000") * D("1.0009")


def test_market_order_fills_at_close_with_spread_and_slippage_taker() -> None:
    order = CandleOrder(side="BUY", type="MARKET", qty=D("0.5"))
    m = match_candle(
        order,
        bar(1, "81000", "83000", "80000", "82000"),
        spread_pct=BTC_SPREAD,
        tick_decimals=2,
        step_decimals=6,
    )
    assert m.legs == (FillLeg(D("82049.20"), D("0.5"), False),)  # 82000 x 1.0006, taker
    assert (m.rule, m.cancel_remainder, m.order.qty) == ("taker", True, D("0"))
    sell = match_candle(
        CandleOrder(side="SELL", type="MARKET", qty=D("0.5")),
        bar(1, "81000", "83000", "80000", "82000"),
        spread_pct=BTC_SPREAD,
        tick_decimals=2,
        step_decimals=6,
    )
    assert sell.legs == (FillLeg(D("81950.80"), D("0.5"), False),)  # 82000 x 0.9994


def test_crossing_limit_takes_at_the_taker_price_or_rests() -> None:
    crossing = CandleOrder(side="BUY", type="LIMIT", qty=D("0.1"), price=D("82100"), crossing=True)
    kw = {"spread_pct": BTC_SPREAD, "tick_decimals": 2, "step_decimals": 6}
    m = match_candle(crossing, bar(1, "82000", "82010", "81990", "82000"), **kw)
    assert m.legs == (FillLeg(D("82049.20"), D("0.1"), False),) and m.rule == "taker"

    # close 82060: the synthetic ask 82092.83 still crosses the 82100 limit, only the slippage
    # breaks it (82060 x 1.0006 = 82109.24): a limit never pays more than its limit -> at 82100
    m = match_candle(crossing, flat(1, "82060"), **kw)
    assert m.legs == (FillLeg(D("82100"), D("0.1"), False),) and m.rule == "taker_limit"
    assert m.order.qty == 0

    # close 82100: ask 82132.84 > limit, it no longer crosses: no fill, it rests
    m = match_candle(crossing, flat(1, "82100"), **kw)
    assert (m.legs, m.rule, m.order.first_pass) == ((), "rest", False)
    # later bars use the resting rule, still with the taker fee (it was crossing at placement)
    m2 = match_candle(m.order, bar(2, "82150", "82200", "82080", "82120"), **kw)
    assert m2.legs == (FillLeg(D("82100"), D("0.1"), False),) and m2.rule == "limit"

    # SELL mirror: limit 81900 crossing; close 81940 -> bid 81907.22 >= limit, taker 81890.83 < limit
    sell = CandleOrder(side="SELL", type="LIMIT", qty=D("0.1"), price=D("81900"), crossing=True)
    m = match_candle(sell, flat(1, "81940"), **kw)
    assert m.legs == (FillLeg(D("81900"), D("0.1"), False),) and m.rule == "taker_limit"
    m = match_candle(sell, flat(1, "82000"), **kw)
    assert m.legs == (FillLeg(D("81950.80"), D("0.1"), False),) and m.rule == "taker"


def test_resting_limit_match_is_maker_with_full_quantity() -> None:
    order = CandleOrder(side="BUY", type="LIMIT", qty=D("3.5"), price=D("100"))
    kw = {"spread_pct": D("0.001"), "tick_decimals": 2, "step_decimals": 6}
    assert match_candle(order, bar(1, "101", "102", "100.01", "101"), **kw).legs == ()
    m = match_candle(order, bar(1, "101", "102", "99", "101"), **kw)
    assert m.legs == (FillLeg(D("100"), D("3.5"), True),) and m.rule == "limit"
    gap = match_candle(order, bar(1, "98", "99", "97", "98"), **kw)
    assert gap.legs == (FillLeg(D("98"), D("3.5"), True),) and gap.rule == "gap_through"


def test_stop_trigger_conditions() -> None:
    b = bar(1, "101", "103", "99", "100")
    assert stop_triggered("down", D("99"), b) and not stop_triggered("down", D("98.99"), b)
    assert stop_triggered("up", D("103"), b) and not stop_triggered("up", D("103.01"), b)
    assert stop_trigger_price("down", D("100"), b) == D("100")
    assert stop_trigger_price("down", D("102"), b) == D("101")  # opened below the stop: fired at the open
    assert stop_trigger_price("up", D("100"), b) == D("101")


def test_stop_limit_waits_then_fills_at_the_limit() -> None:
    order = CandleOrder(side="SELL", type="STOP_LIMIT", qty=D("1"), price=D("99.50"), stop_price=D("100.00"))
    kw = {"spread_pct": D("0.001"), "tick_decimals": 2, "step_decimals": 6}
    m = match_candle(order, bar(1, "101", "101", "100.01", "100.5"), **kw)
    assert (m.legs, m.rule, m.triggered_now, m.order) == ((), "stop_wait", False, order)
    # low 99.60 never went below the 99.50 limit: everything at the limit, taker
    m = match_candle(order, bar(1, "101", "101", "99.60", "99.80"), **kw)
    assert m.legs == (FillLeg(D("99.50"), D("1"), False),)
    assert (m.rule, m.triggered_now, m.order.triggered, m.order.qty) == ("stop_at_limit", True, True, D("0"))


def test_stop_limit_below_the_limit_fills_one_tick_worse() -> None:
    order = CandleOrder(side="SELL", type="STOP_LIMIT", qty=D("1"), price=D("99.50"), stop_price=D("100.00"))
    kw = {"spread_pct": D("0.001"), "tick_decimals": 2, "step_decimals": 6}
    # low 99.48: 0.02 below the limit = 0.020 % <= 2 x 0.02 % -> conservative: limit - one tick
    m = match_candle(order, bar(1, "101", "101", "99.48", "99.6"), **kw)
    assert m.legs == (FillLeg(D("99.49"), D("1"), False),) and m.rule == "stop_tick"
    # never worse than the bar's own low
    legs, rule = stop_limit_legs(
        "SELL", D("100000"), D("1"), bar(1, "100100", "100100", "99999.5", "100000"), 0, 2
    )
    assert legs == (FillLeg(D("99999.5"), D("1"), False),) and rule == "stop_tick"


def test_stop_limit_gap_beyond_twice_the_slippage_is_a_stress_fill() -> None:
    order = CandleOrder(
        side="SELL", type="STOP_LIMIT", qty=D("1.000001"), price=D("99.50"), stop_price=D("100.00")
    )
    kw = {"spread_pct": D("0.001"), "tick_decimals": 2, "step_decimals": 6}
    # low 99.00: 0.50 / 99.50 = 0.50 % > 0.04 % -> 50 % at the limit, the rest at the low
    m = match_candle(order, bar(1, "101", "101", "99.00", "99.2"), **kw)
    assert m.legs == (FillLeg(D("99.50"), D("0.500000"), False), FillLeg(D("99.00"), D("0.500001"), False))
    assert m.rule == "stop_stress" and m.filled_qty == D("1.000001") and m.order.qty == 0
    # exactly at 2 x slippage is still the conservative rule
    edge = D("99.50") * (1 - 2 * DEFAULT_SLIPPAGE)  # 99.4602
    assert (
        stop_limit_legs("SELL", D("99.50"), D("1"), bar(1, "100", "100", str(edge), "99.5"), 4, 6)[1]
        == "stop_tick"
    )


def test_buy_stop_limit_mirrors_the_sell_rules() -> None:
    order = CandleOrder(side="BUY", type="STOP_LIMIT", qty=D("2"), price=D("100.50"), stop_price=D("100.00"))
    kw = {"spread_pct": D("0.001"), "tick_decimals": 2, "step_decimals": 6}
    assert match_candle(order, bar(1, "99", "99.99", "98", "99.5"), **kw).rule == "stop_wait"
    assert match_candle(order, bar(1, "99", "100.40", "98", "100.3"), **kw).legs == (
        FillLeg(D("100.50"), D("2"), False),
    )
    assert match_candle(order, bar(1, "99", "100.52", "98", "100.3"), **kw).legs == (
        FillLeg(D("100.51"), D("2"), False),
    )
    m = match_candle(order, bar(1, "99", "102", "98", "101"), **kw)
    assert m.legs == (FillLeg(D("100.50"), D("1"), False), FillLeg(D("102"), D("1"), False))
    assert m.rule == "stop_stress"


def test_stop_limit_gapped_through_never_fills_better_than_the_open() -> None:
    kw = {"spread_pct": D("0.001"), "tick_decimals": 2, "step_decimals": 6}
    # the bar opened at 99, below both the 100 stop and the 99.50 limit: the stop fired at 99, so
    # the stress leg "at the limit" is filled at the open instead, the rest at the low (taker)
    order = CandleOrder(side="SELL", type="STOP_LIMIT", qty=D("1"), price=D("99.50"), stop_price=D("100"))
    m = match_candle(order, bar(1, "99", "99.40", "98.5", "99"), **kw)
    assert m.legs == (FillLeg(D("99"), D("0.500000"), False), FillLeg(D("98.5"), D("0.500000"), False))
    assert (m.rule, m.triggered_now, m.order.triggered, m.order.qty) == ("stop_stress", True, True, D("0"))
    # a small gap: limit - one tick (99.49) is still better than the 99.485 open -> the open
    m = match_candle(order, bar(1, "99.485", "99.49", "99.48", "99.48"), **kw)
    assert m.legs == (FillLeg(D("99.485"), D("1"), False),) and m.rule == "stop_tick"
    # BUY mirror: a breakout stop 100 / limit 100.50 and a bar that opened at 101
    buy = CandleOrder(side="BUY", type="STOP_LIMIT", qty=D("2"), price=D("100.50"), stop_price=D("100"))
    m = match_candle(buy, bar(1, "101", "101.5", "100.9", "101.2"), **kw)
    assert m.legs == (FillLeg(D("101"), D("1"), False), FillLeg(D("101.5"), D("1"), False))
    # without a trigger cap, the plain PRD legs
    legs, rule = stop_limit_legs("SELL", D("99.50"), D("1"), bar(1, "99", "99.4", "98.5", "99"), 2, 6)
    assert legs == (FillLeg(D("99.50"), D("0.5"), False), FillLeg(D("98.5"), D("0.5"), False))
    assert rule == "stop_stress"


def test_stop_market_fills_at_the_worse_of_trigger_and_close() -> None:
    kw = {"spread_pct": D("0.001"), "tick_decimals": 2, "step_decimals": 6}
    order = CandleOrder(side="SELL", type="STOP_MARKET", qty=D("1"), stop_price=D("100"))
    m = match_candle(order, bar(1, "101", "101", "99", "100.5"), **kw)  # close above the trigger
    assert m.legs == (FillLeg(taker_price("SELL", D("100"), D("0.001"), tick_decimals=2), D("1"), False),)
    m = match_candle(order, bar(1, "101", "101", "98", "98.5"), **kw)
    assert m.legs[0].price == taker_price("SELL", D("98.5"), D("0.001"), tick_decimals=2)
    assert m.cancel_remainder and m.triggered_now


def test_fee_selection() -> None:
    assert fee_is_maker(False) and not fee_is_maker(True)
    assert is_crossing("BUY", D("100"), D("99"), D("100")) and not is_crossing(
        "BUY", D("99.99"), D("99"), D("100")
    )
    assert is_crossing("SELL", D("99"), D("99"), D("100")) and not is_crossing(
        "SELL", D("99.01"), D("99"), D("100")
    )
    assert not is_crossing("BUY", D("100"), D("99"), None) and not is_crossing("BUY", None, D("1"), D("1"))
    kw = {"spread_pct": D("0.001"), "tick_decimals": 2, "step_decimals": 6}
    b = bar(1, "100", "101", "98", "100")
    resting = CandleOrder(side="BUY", type="LIMIT", qty=D("1"), price=D("99"))
    crossed = CandleOrder(
        side="BUY", type="LIMIT", qty=D("1"), price=D("99"), crossing=True, first_pass=False
    )
    assert match_candle(resting, b, **kw).legs[0].is_maker is True
    assert match_candle(crossed, b, **kw).legs[0].is_maker is False
    assert (
        match_candle(CandleOrder(side="SELL", type="MARKET", qty=D("1")), b, **kw).legs[0].is_maker is False
    )
    stop = CandleOrder(side="SELL", type="STOP_LIMIT", qty=D("1"), price=D("98.5"), stop_price=D("99"))
    assert match_candle(stop, b, **kw).legs[0].is_maker is False  # a triggered stop takes: taker


def test_book_walk_takes_at_most_80_percent_of_each_level() -> None:
    assert DEFAULT_LEVEL_FRACTION == D("0.8")
    book = BookSnapshot(
        symbol="BTCUSDT",
        ts=T0,
        source="rest",
        bids=[Level(price=D("99"), qty=D("1")), Level(price=D("98"), qty=D("1"))],
        asks=[Level(price=D("101"), qty=D("1")), Level(price=D("100"), qty=D("1"))],  # unsorted on purpose
    )
    asks = opposite_levels(book, "BUY")
    assert [lv.price for lv in asks] == [D("100"), D("101")]
    assert [lv.price for lv in opposite_levels(book, "SELL")] == [D("99"), D("98")]
    assert walk_levels(asks, D("2"), lambda p: p <= D("101")) == [(D("100"), D("0.8")), (D("101"), D("0.8"))]
    assert walk_levels(asks, D("2"), lambda p: p <= D("100")) == [(D("100"), D("0.8"))]
    # liquidity our earlier fills took from this snapshot is not offered again
    assert walk_levels(asks, D("2"), lambda p: p <= D("101"), {D("100"): D("0.5")}) == [
        (D("100"), D("0.3")),
        (D("101"), D("0.8")),
    ]
    assert walk_levels(asks, D("2"), lambda p: True, {D("100"): D("0.8")}) == [(D("101"), D("0.8"))]
    # configurable fraction; quantities rounded down to the step
    assert walk_levels(asks, D("2"), lambda p: True, level_fraction=D("1")) == [
        (D("100"), D("1")),
        (D("101"), D("1")),
    ]
    thin = [Level(price=D("100"), qty=D("0.333"))]
    assert walk_levels(thin, D("1"), lambda p: True, step_decimals=2) == [(D("100"), D("0.26"))]


def test_synthetic_quotes_mid_is_the_close() -> None:
    # Overview §2.1 USDTTMN: mid 266,739, spread 0.014 % -> 266,720 / 266,758
    assert synthetic_quotes(D("266739"), D("0.00014"), 0) == (D("266720"), D("266758"))
    bid, ask = synthetic_quotes(D("82000"), BTC_SPREAD, 2)
    assert (bid, ask, (bid + ask) / 2) == (D("81967.20"), D("82032.80"), D("82000"))
    assert synthetic_quotes(D("100"), D("0"), 0) == (D("100"), D("101"))  # never a locked book


def test_make_synthetic_book_shape(markets: dict[str, MarketInfo]) -> None:
    candle = bar(3, "266700", "266800", "266650", "266739", symbol="USDTTMN")
    book = make_synthetic_book("USDTTMN", candle, D("0.00014"), markets["USDTTMN"])
    assert book.source == "backtest" and book.symbol == "USDTTMN"
    assert book.ts == float(candle.ts + 60)  # known at the bar's close
    assert len(book.bids) == len(book.asks) == 1
    assert (book.best_bid, book.best_ask, book.mid) == (D("266720"), D("266758"), D("266739"))
    assert book.best_bid_qty == book.best_ask_qty == SYNTHETIC_DEPTH_QTY  # unlimited depth at best
    assert book.spread_pct is not None and book.spread_pct >= D("0.00014")
    assert make_synthetic_book("USDTTMN", candle, D("0.00014"), markets["USDTTMN"], ts=123.0).ts == 123.0
    btc = make_synthetic_book("BTCUSDT", flat(0, "82000"), BTC_SPREAD, markets["BTCUSDT"])
    assert (btc.best_bid, btc.best_ask) == (D("81967.20"), D("82032.80"))
    assert (
        BacktestExchange.make_synthetic_book("BTCUSDT", flat(0, "82000"), BTC_SPREAD, markets["BTCUSDT"])
        == btc
    )


def test_median_spread() -> None:
    def book(bid: str, ask: str | None) -> BookSnapshot:
        asks = [] if ask is None else [Level(price=D(ask), qty=D("1"))]
        return BookSnapshot(
            symbol="X", ts=T0, source="rest", bids=[Level(price=D(bid), qty=D("1"))], asks=asks
        )

    assert median_spread([]) is None
    assert median_spread([book("99", None)]) is None  # one-sided books do not count
    books = [book("99.95", "100.05"), book("99.9", "100.1"), book("99.85", "100.15"), book("1", None)]
    assert median_spread(books) == D("0.002")


# ================================================================ exchange: construction, fees, spreads


def test_is_a_paper_exchange_for_the_engine(ex: BacktestExchange) -> None:
    assert isinstance(ex, PaperExchange) and ex.name == "backtest" and ex.driver == "candles"


def test_invalid_arguments(markets: dict[str, MarketInfo], clock: SimClock) -> None:
    with pytest.raises(ValueError):
        BacktestExchange(markets, clock, driver="ticks")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        BacktestExchange(markets, clock, driver="books")  # needs books()
    with pytest.raises(ValueError):
        BacktestExchange(markets, clock, level_fraction=D("0"))
    with pytest.raises(ValueError):
        BacktestExchange(markets, clock, slippage=D("-0.1"))


async def test_fees_come_from_the_fee_snapshot(markets: dict[str, MarketInfo], clock: SimClock) -> None:
    odd = {
        s: m.model_copy(update={"maker_fee": D("0.05"), "taker_fee": D("0.06")}) for s, m in markets.items()
    }
    got = await BacktestExchange(odd, clock).get_markets()
    assert (got["BTCUSDT"].maker_fee, got["BTCUSDT"].taker_fee) == (USDT_MAKER, USDT_TAKER)
    assert (got["USDTTMN"].maker_fee, got["USDTTMN"].taker_fee) == (TMN_MAKER, TMN_TAKER)
    snap = {
        "default_by_quote": {"USDT": {"maker": "0.002", "taker": "0.004"}},
        "symbols": {"BTCUSDT": {"maker": "0", "taker": "0.001"}},
    }
    custom = apply_fee_snapshot(odd, snap)
    assert (custom["BTCUSDT"].maker_fee, custom["BTCUSDT"].taker_fee) == (D("0"), D("0.001"))
    assert (custom["ETHUSDT"].maker_fee, custom["ETHUSDT"].taker_fee) == (D("0.002"), D("0.004"))
    kept = await BacktestExchange(odd, clock, use_fee_snapshot=False).get_markets()
    assert kept["BTCUSDT"].maker_fee == D("0.05")


async def test_from_snapshot_needs_no_api(clock: SimClock) -> None:
    ex = BacktestExchange.from_snapshot(clock, symbols=["USDTTMN", "BTCUSDT", "NOPE"])
    got = await ex.get_markets()
    assert sorted(got) == ["BTCUSDT", "USDTTMN"]
    assert (got["USDTTMN"].maker_fee, got["BTCUSDT"].taker_fee) == (TMN_MAKER, USDT_TAKER)
    assert await ex.get_balances() == {"TMN": Balance(asset="TMN", free=D("5000000"))}


def test_spread_sources(markets: dict[str, MarketInfo], clock: SimClock) -> None:
    extra = MarketInfo(symbol="ZZZTMN", base="ZZZ", quote="TMN", tick_decimals=0, step_decimals=1)
    ex = BacktestExchange({**markets, "ZZZTMN": extra}, clock, spreads={"BTCUSDT": D("0.0009")})
    assert (ex.spread_source("BTCUSDT"), ex.spread_for("BTCUSDT")) == ("observed", D("0.0009"))
    ref = snapshot_reference()["spreads"]
    assert (ex.spread_source("BTCTMN"), ex.spread_for("BTCTMN")) == ("reference", ref["BTCTMN"])
    assert (ex.spread_source("ZZZTMN"), ex.spread_for("ZZZTMN")) == ("default", DEFAULT_SPREAD_PCT)
    ex.set_bar("BTCUSDT", flat(0, "82000"), spread_pct=D("0.002"))
    assert (ex.spread_source("BTCUSDT"), ex.spread_for("BTCUSDT")) == ("bar", D("0.002"))
    with pytest.raises(ValueError):
        ex.set_bar("NOPE", flat(0, "1", symbol="NOPE"))
    with pytest.raises(ValueError):
        ex.set_bar("BTCUSDT", flat(0, "1", symbol="ETHUSDT"))


def test_synthetic_books_follow_the_current_bars(markets: dict[str, MarketInfo], clock: SimClock) -> None:
    provided = {"ETHUSDT": flat(0, "2440", symbol="ETHUSDT")}
    ex = BacktestExchange(markets, clock, bars=lambda: provided, spreads={"ETHUSDT": D("0.0005")})
    ex.set_bar("BTCUSDT", flat(0, "82000"), spread_pct=BTC_SPREAD)
    books = ex.synthetic_books()
    assert sorted(books) == ["BTCUSDT", "ETHUSDT"]
    assert (books["BTCUSDT"].best_bid, books["BTCUSDT"].best_ask) == (D("81967.20"), D("82032.80"))
    assert books["ETHUSDT"].mid == D("2440") and books["ETHUSDT"].source == "backtest"
    provided["BTCUSDT"] = flat(1, "83000")  # the provider wins over set_bar
    assert ex.current_bar("BTCUSDT") == flat(1, "83000")


async def test_empty_data_is_harmless(
    markets: dict[str, MarketInfo], clock: SimClock, tmp_path: Path
) -> None:
    store = Store(tmp_path / "empty.sqlite")
    try:
        assert observed_spreads(store, ["BTCUSDT", "USDTTMN"]) == {}
        assert store.load_candles("BTCUSDT", "1") == []
    finally:
        store.close()
    ex = BacktestExchange(markets, clock, initial_balances=dict(START))
    assert ex.synthetic_books() == {} and ex.on_tick() == []
    await ex.place_order(req("b1", "BUY", "LIMIT", "0.01", price="80000"))  # no bar yet: still valid
    assert ex.on_tick() == []
    await assert_balances_consistent(ex, START)


# ================================================================ exchange: candle driver


async def test_candle_limit_fills_only_on_a_later_bar(ex: BacktestExchange, clock: SimClock) -> None:
    assert step(ex, clock, flat(0, "82000")) == []
    order = await ex.place_order(req("b1", "BUY", "LIMIT", "0.01", price="81950"))
    assert (order.exchange_id, order.raw["exchange"], order.raw["driver"]) == (
        "bt-o-1",
        "backtest",
        "candles",
    )
    lock = D("0.01") * D("81950") * (1 + USDT_TAKER)
    assert await bal(ex, "USDT") == Balance(asset="USDT", free=D("10000") - lock, locked=lock)

    # same bar again (the engine ticks faster than bars): the order may not fill on its own bar,
    # even though this bar's low would reach it
    ex.set_bar("BTCUSDT", bar(0, "82000", "82000", "81000", "82000"))
    assert ex.on_tick() == []
    assert step(ex, clock, bar(1, "82000", "82050", "81960", "82010")) == []  # low above 81950
    fills = step(ex, clock, bar(2, "81990", "82000", "81900", "81950"))
    assert [(f.price, f.qty, f.is_maker, f.fee, f.fee_asset, f.trade_id) for f in fills] == [
        (D("81950"), D("0.01"), True, D("0.01") * USDT_MAKER, "BTC", "bt-1")
    ]
    assert fills[0].ts == clock.now()
    o = await ex.get_order("b1")
    assert o.status == "FILLED" and o.raw["fill_rule"] == "limit"
    assert await bal(ex, "USDT") == Balance(asset="USDT", free=D("10000") - D("819.50"))
    await assert_balances_consistent(ex, START)


async def test_candle_latency_one_step(ex: BacktestExchange, clock: SimClock) -> None:
    step(ex, clock, flat(0, "82000"))
    await ex.place_order(req("m1", "BUY", "MARKET", "0.01"))
    assert (await ex.get_order("m1")).status == "NEW"  # never at placement
    fills = step(ex, clock, flat(1, "82500"))
    assert [f.price for f in fills] == [D("82549.50")]  # the NEXT bar's close, not 82000's


async def test_candle_gap_through_sell(ex: BacktestExchange, clock: SimClock) -> None:
    step(ex, clock, flat(0, "82000"))
    await ex.place_order(req("s1", "SELL", "LIMIT", "0.01", price="82100"))
    fills = step(ex, clock, bar(1, "82200", "82300", "82150", "82250"))
    assert [(f.price, f.is_maker) for f in fills] == [(D("82200"), True)]  # opened above the limit
    assert (await ex.get_order("s1")).raw["fill_rule"] == "gap_through"
    await assert_balances_consistent(ex, START)


async def test_candle_market_buy_price_fee_and_balances(ex: BacktestExchange, clock: SimClock) -> None:
    step(ex, clock, flat(0, "82000"))
    await ex.place_order(req("m1", "BUY", "MARKET", "0.01"))
    fills = step(ex, clock, bar(1, "82100", "82600", "82050", "82500"))
    f = fills[0]
    assert (f.price, f.qty, f.is_maker, f.fee) == (D("82549.50"), D("0.01"), False, D("0.01") * USDT_TAKER)
    o = await ex.get_order("m1")
    assert o.status == "FILLED" and o.avg_price == D("82549.50")
    assert await bal(ex, "USDT") == Balance(asset="USDT", free=D("10000") - D("825.495"))
    assert await bal(ex, "BTC") == Balance(asset="BTC", free=D("1.01") - D("0.000013"))
    await assert_balances_consistent(ex, START)


async def test_candle_crossing_limit_taker_then_resting_taker_fee(
    ex: BacktestExchange, clock: SimClock
) -> None:
    step(ex, clock, flat(0, "82000"))  # synthetic ask 82032.80
    await ex.place_order(req("x1", "BUY", "LIMIT", "0.01", price="82100"))  # crossing
    fills = step(ex, clock, flat(1, "82000"))
    assert [(f.price, f.is_maker) for f in fills] == [(D("82049.20"), False)]

    await ex.place_order(req("x2", "BUY", "LIMIT", "0.01", price="82100"))  # crossing (ask 82032.80)
    # ask 82132.84 > limit on its first bar: it rests; the same bar again (the engine ticks faster
    # than bars) is not looked at a second time, although its low would reach the limit
    assert step(ex, clock, bar(2, "82100", "82150", "82050", "82100")) == []
    assert ex.on_tick() == []
    assert (await ex.get_order("x2")).status == "NEW"
    fills = step(ex, clock, bar(3, "82150", "82200", "82080", "82120"))
    assert [(f.price, f.is_maker, f.fee) for f in fills] == [(D("82100"), False, D("0.01") * USDT_TAKER)]
    assert (await ex.get_order("x2")).raw["fill_rule"] == "limit"
    await assert_balances_consistent(ex, START)


async def test_candle_order_at_the_ask_fills_next_bar_at_its_limit(
    ex: BacktestExchange, clock: SimClock
) -> None:
    # OrderManager crosses at exactly the opposite best: a BUY at the synthetic ask. In a flat
    # market the taker price (ask + slippage) breaks that limit, but the order still crosses the
    # next bar's book: it fills there, at its limit (the worst price it allows), as a taker.
    step(ex, clock, flat(0, "82000"))
    ask = ex.synthetic_books()["BTCUSDT"].best_ask
    bid = ex.synthetic_books()["BTCUSDT"].best_bid
    assert (bid, ask) == (D("81967.20"), D("82032.80"))
    await ex.place_order(req("tb", "BUY", "LIMIT", "0.01", price=str(ask)))
    fills = step(ex, clock, flat(1, "82000"))
    assert [(f.price, f.qty, f.is_maker) for f in fills] == [(ask, D("0.01"), False)]
    assert (await ex.get_order("tb")).raw["fill_rule"] == "taker_limit"
    await ex.place_order(req("ts", "SELL", "LIMIT", "0.01", price=str(bid)))
    fills = step(ex, clock, flat(2, "82000"))
    assert [(f.price, f.qty, f.is_maker) for f in fills] == [(bid, D("0.01"), False)]
    await assert_balances_consistent(ex, START)


async def test_candle_stop_limit_stress_partial(ex: BacktestExchange, clock: SimClock) -> None:
    step(ex, clock, flat(0, "82000"))
    await ex.place_order(req("st", "SELL", "STOP_LIMIT", "0.5", price="80900", stop="81000"))
    assert (await ex.get_order("st")).raw["stop_direction"] == "down"
    assert step(ex, clock, bar(1, "81900", "81950", "81050", "81100")) == []  # low above the stop
    fills = step(ex, clock, bar(2, "81100", "81150", "80000", "80200"))
    assert [(f.price, f.qty, f.is_maker) for f in fills] == [
        (D("80900"), D("0.25"), False),
        (D("80000"), D("0.25"), False),
    ]
    o = await ex.get_order("st")
    assert o.status == "FILLED" and o.raw["triggered"] is True and o.raw["fill_rule"] == "stop_stress"
    proceeds = D("0.25") * D("80900") + D("0.25") * D("80000")
    assert await bal(ex, "USDT") == Balance(asset="USDT", free=D("10000") + proceeds * (1 - USDT_TAKER))
    assert await bal(ex, "BTC") == Balance(asset="BTC", free=D("0.5"))
    await assert_balances_consistent(ex, START)


async def test_candle_stop_limit_conservative_tick(ex: BacktestExchange, clock: SimClock) -> None:
    step(ex, clock, flat(0, "82000"))
    await ex.place_order(req("st", "SELL", "STOP_LIMIT", "0.5", price="80900", stop="81000"))
    fills = step(ex, clock, bar(1, "81100", "81200", "80890", "80950"))
    assert [(f.price, f.qty) for f in fills] == [(D("80899.99"), D("0.5"))]
    assert (await ex.get_order("st")).raw["fill_rule"] == "stop_tick"


async def test_candle_stop_does_not_trigger_on_its_own_bar(ex: BacktestExchange, clock: SimClock) -> None:
    step(ex, clock, flat(0, "82000"))
    await ex.place_order(req("st", "SELL", "STOP_LIMIT", "0.5", price="80900", stop="81000"))
    ex.set_bar("BTCUSDT", bar(0, "82000", "82000", "79000", "82000"))  # same bar, revised low
    assert ex.on_tick() == []
    assert (await ex.get_order("st")).raw["triggered"] is False


async def test_candle_buy_stress_fill_is_cut_to_the_funds(
    markets: dict[str, MarketInfo], clock: SimClock
) -> None:
    start = {"USDT": D("1000")}
    ex = BacktestExchange(markets, clock, spreads={"BTCUSDT": BTC_SPREAD}, initial_balances=dict(start))
    step(ex, clock, flat(0, "82000"))
    await ex.place_order(req("bo", "BUY", "STOP_LIMIT", "0.012", price="82200", stop="82100"))
    lock = D("0.012") * D("82200") * (1 + USDT_TAKER)
    assert await bal(ex, "USDT") == Balance(asset="USDT", free=D("1000") - lock, locked=lock)
    fills = step(ex, clock, bar(1, "82000", "90000", "81990", "89000"))
    # stress: 0.006 at 82200, then 0.006 at 90000 would cost more than the account holds
    affordable = quantize_down((D("1000") - D("0.006") * D("82200")) / D("90000"), 6)
    assert [(f.price, f.qty) for f in fills] == [(D("82200"), D("0.006")), (D("90000"), affordable)]
    o = await ex.get_order("bo")
    assert o.status == "CANCELED" and o.raw["cancel_reason"] == "insufficient_funds"
    assert o.filled_qty == D("0.006") + affordable
    usdt = await bal(ex, "USDT")
    assert usdt.locked == 0 and usdt.free == D("1000") - D("493.2") - affordable * D("90000") >= 0
    await assert_balances_consistent(ex, start)


async def test_candle_on_fill_callback_and_my_trades(markets: dict[str, MarketInfo], clock: SimClock) -> None:
    seen: list[Fill] = []
    ex = BacktestExchange(
        markets, clock, spreads={"BTCUSDT": BTC_SPREAD}, initial_balances=dict(START), on_fill=seen.append
    )
    step(ex, clock, flat(0, "82000"))
    await ex.place_order(req("m1", "SELL", "MARKET", "0.1"))
    fills = step(ex, clock, flat(1, "82000"))
    assert seen == fills == await ex.get_my_trades("BTCUSDT")
    assert fills[0].price == D("81950.80")


async def test_candle_state_round_trip_is_deterministic(
    markets: dict[str, MarketInfo], clock: SimClock
) -> None:
    series = [
        flat(0, "82000"),
        bar(1, "82000", "82100", "81900", "82050"),
        bar(2, "82050", "82060", "81000", "81100"),
        bar(3, "81100", "81500", "80500", "81400"),
        bar(4, "81400", "82600", "81300", "82500"),
        bar(5, "82500", "82700", "82400", "82600"),
    ]

    async def run(restart_at: int | None) -> tuple[list[Fill], dict]:
        c = SimClock(T0)
        bt = BacktestExchange(markets, c, spreads={"BTCUSDT": BTC_SPREAD}, initial_balances=dict(START))
        fills: list[Fill] = []
        for i, candle in enumerate(series):
            if i == restart_at:
                saved = json.loads(json.dumps(bt.state()))
                bt = BacktestExchange(markets, c, spreads={"BTCUSDT": BTC_SPREAD}, state=saved)
            fills += step(bt, c, candle)
            if i == 0:
                await bt.place_order(req("b1", "BUY", "LIMIT", "0.01", price="81500"))
                await bt.place_order(req("s1", "SELL", "STOP_LIMIT", "0.2", price="80900", stop="81000"))
                await bt.place_order(
                    req("x1", "BUY", "LIMIT", "0.01", price="82040")
                )  # crossing (ask 82032.80)
            if i == 2:
                await bt.place_order(req("t1", "SELL", "LIMIT", "0.05", price="82400"))
        return fills, bt.state()

    straight = await run(None)
    assert straight == await run(None)  # deterministic
    assert straight == await run(2)  # restart from state() mid-run changes nothing
    assert straight == await run(3)
    assert {f.order_client_id for f in straight[0]} == {"b1", "s1", "x1", "t1"}


async def test_load_state_rejects_another_driver(markets: dict[str, MarketInfo], clock: SimClock) -> None:
    candles = BacktestExchange(markets, clock)
    with pytest.raises(ValueError):
        BacktestExchange(markets, clock, driver="books", books=dict, state=candles.state())


# ================================================================ exchange: book driver


class Feed:
    """``books()`` for the book driver; every ``set`` is a newer snapshot."""

    def __init__(self) -> None:
        self.books: dict[str, BookSnapshot] = {}
        self._ts = T0

    def __call__(self) -> dict[str, BookSnapshot]:
        return dict(sorted(self.books.items()))

    def set(self, bids: list[tuple[str, str]], asks: list[tuple[str, str]], symbol: str = "BTCUSDT") -> None:
        self._ts += 2
        self.books[symbol] = BookSnapshot(
            symbol=symbol,
            ts=self._ts,
            source="rest",
            bids=[Level(price=D(p), qty=D(q)) for p, q in bids],
            asks=[Level(price=D(p), qty=D(q)) for p, q in asks],
        )


@pytest.fixture
def feed() -> Feed:
    return Feed()


@pytest.fixture
def bx(markets: dict[str, MarketInfo], clock: SimClock, feed: Feed) -> BacktestExchange:
    return BacktestExchange(markets, clock, driver="books", books=feed, initial_balances=dict(START))


async def test_books_next_snapshot_only_and_80_percent_cap(bx: BacktestExchange, feed: Feed) -> None:
    feed.set(bids=[("82800", "1")], asks=[("82900", "0.01"), ("82901", "0.01")])
    await bx.place_order(req("b1", "BUY", "LIMIT", "0.02", price="82900"))  # crossing
    assert bx.on_tick() == []  # the snapshot it was placed on cannot fill it (PaperExchange would)
    feed.set(bids=[("82800", "1")], asks=[("82900", "0.01"), ("82901", "0.01")])
    fills = bx.on_tick()
    assert [(f.price, f.qty, f.is_maker) for f in fills] == [(D("82900"), D("0.008"), False)]  # 80 % of 0.01
    assert bx.on_tick() == []  # the rest of that level stays taken in this snapshot
    feed.set(bids=[("82800", "1")], asks=[("82900", "0.02")])  # the level grew by 0.01
    fills = bx.on_tick()
    # crossing at placement: the remainder is a TAKER too (PRD-05 §3.2), at the level price; the
    # 0.008 taken before still counts against the level: 80 % of 0.02 - 0.008 = 0.008
    assert [(f.price, f.qty, f.is_maker) for f in fills] == [(D("82900"), D("0.008"), False)]
    assert fills[0].fee == D("0.008") * D("0.0013")  # BTCUSDT taker fee (maker would be 0.10 %)
    o = await bx.get_order("b1")
    assert o.status == "PARTIALLY_FILLED" and o.remaining_qty == D("0.004")
    await assert_balances_consistent(bx, START)


async def test_books_resting_limit_is_maker_at_its_limit(bx: BacktestExchange, feed: Feed) -> None:
    feed.set(bids=[("82800", "1")], asks=[("83000", "1")])
    await bx.place_order(req("b1", "BUY", "LIMIT", "0.005", price="82900"))  # resting
    feed.set(bids=[("82800", "1")], asks=[("82850", "1")])  # the market came down through the limit
    fills = bx.on_tick()
    assert [(f.price, f.qty, f.is_maker) for f in fills] == [(D("82900"), D("0.005"), True)]


async def test_books_a_repeated_level_is_not_offered_again(bx: BacktestExchange, feed: Feed) -> None:
    # a 0.01 ask level shown unchanged on two snapshots: recorded snapshots never show our own fills
    feed.set(bids=[("82800", "1")], asks=[("82900", "0.01"), ("83100", "1")])
    await bx.place_order(req("b1", "BUY", "LIMIT", "0.03", price="82900"))  # crossing
    for _ in range(3):
        feed.set(bids=[("82800", "1")], asks=[("82900", "0.01"), ("83100", "1")])
        bx.on_tick()
    o = await bx.get_order("b1")
    assert o.filled_qty == D("0.008")  # 80 % of the level once, not 0.008 per snapshot (was 0.024)
    feed.set(bids=[("82800", "1")], asks=[("82900", "0.009"), ("83100", "1")])  # shrank: nothing new
    assert bx.on_tick() == []
    feed.set(bids=[("82800", "1")], asks=[("83100", "1")])  # the level is gone ...
    assert bx.on_tick() == []
    feed.set(bids=[("82800", "1")], asks=[("82900", "0.01"), ("83100", "1")])  # ... and back: fresh
    assert [f.qty for f in bx.on_tick()] == [D("0.008")]
    await assert_balances_consistent(bx, START)


async def test_books_an_order_open_across_a_recording_gap_never_fills(
    markets: dict[str, MarketInfo], clock: SimClock, feed: Feed
) -> None:
    ex = BacktestExchange(markets, clock, driver="books", books=feed, max_gap_s=30.0, initial_balances=dict(START))
    feed.set(bids=[("82800", "1")], asks=[("83000", "1")])
    await ex.place_order(req("rest", "BUY", "LIMIT", "0.005", price="82900"))  # resting
    for _ in range(25):  # 50 s of continuous snapshots: a long rest is no gap
        feed.set(bids=[("82800", "1")], asks=[("83000", "1")])
        assert ex.on_tick() == []
    feed.set(bids=[("82800", "1")], asks=[("82850", "1")])
    assert [f.qty for f in ex.on_tick()] == [D("0.005")]  # 52 s after placement, consecutive snapshots
    await ex.place_order(req("gap", "BUY", "LIMIT", "0.005", price="82900"))
    feed.set(bids=[("82800", "1")], asks=[("83000", "1")])
    assert ex.on_tick() == []
    feed._ts += 6 * 3600  # a 6-hour hole in the recording ...
    feed.set(bids=[("82000", "1")], asks=[("82100", "1")])  # ... after which the ask is below the limit
    assert ex.on_tick() == [] and ex.on_tick() == []  # was: filled 21,600 s after placement
    o = await ex.get_order("gap")
    assert o.status == "NEW" and ex.gap_blocked == 1 and o.raw["recording_gap_s"] == pytest.approx(21602.0)
    feed.set(bids=[("82000", "1")], asks=[("82100", "1")])
    assert ex.on_tick() == []  # never again (the OrderManager re-prices or cancels it)
    restored = BacktestExchange(markets, clock, driver="books", books=feed, max_gap_s=30.0, state=ex.state())
    feed.set(bids=[("82000", "1")], asks=[("82100", "1")])
    assert restored.on_tick() == [] and restored.gap_blocked == 1
    # an untriggered stop still triggers after a gap (at the post-gap price), then fills normally
    await ex.cancel_order("gap")
    feed.set(bids=[("82800", "1")], asks=[("82900", "1")])
    await ex.place_order(req("stop", "SELL", "STOP_LIMIT", "0.001", price="80000", stop="81000"))
    feed._ts += 3600
    feed.set(bids=[("80500", "1")], asks=[("80600", "1")])
    assert ex.on_tick() == []  # triggers on this snapshot
    feed.set(bids=[("80500", "1")], asks=[("80600", "1")])
    assert [f.qty for f in ex.on_tick()] == [D("0.001")]
    with pytest.raises(ValueError, match="max_gap_s"):
        BacktestExchange(markets, clock, driver="books", books=feed, max_gap_s=0)


async def test_books_level_fraction_is_configurable(
    markets: dict[str, MarketInfo], clock: SimClock, feed: Feed
) -> None:
    full = BacktestExchange(
        markets, clock, driver="books", books=feed, level_fraction=D("1"), initial_balances=dict(START)
    )
    feed.set(bids=[("82800", "1")], asks=[("82900", "0.01")])
    await full.place_order(req("b1", "BUY", "LIMIT", "0.01", price="82900"))
    feed.set(bids=[("82800", "1")], asks=[("82900", "0.01")])
    assert [f.qty for f in full.on_tick()] == [D("0.01")]


async def test_books_market_walks_capped_levels_and_cancels_the_rest(
    bx: BacktestExchange, feed: Feed
) -> None:
    feed.set(bids=[("82800", "1")], asks=[("82900", "0.01"), ("82950", "0.01")])
    await bx.place_order(req("m1", "BUY", "MARKET", "0.02"))
    feed.set(bids=[("82800", "1")], asks=[("82900", "0.01"), ("82950", "0.01")])
    fills = bx.on_tick()
    assert [(f.price, f.qty, f.is_maker) for f in fills] == [
        (D("82900"), D("0.008"), False),
        (D("82950"), D("0.008"), False),
    ]
    o = await bx.get_order("m1")
    assert o.status == "CANCELED" and o.filled_qty == D("0.016")
    await assert_balances_consistent(bx, START)


async def test_books_stop_fills_on_a_snapshot_after_the_trigger(bx: BacktestExchange, feed: Feed) -> None:
    feed.set(bids=[("82800", "1")], asks=[("82900", "1")])
    await bx.place_order(req("st", "SELL", "STOP_LIMIT", "0.5", price="81900", stop="82000"))
    feed.set(bids=[("81990", "1")], asks=[("82100", "1")])
    assert bx.on_tick() == []  # triggers on this snapshot
    assert (await bx.get_order("st")).raw["triggered"] is True
    assert bx.on_tick() == []  # same snapshot: no fill yet
    feed.set(bids=[("81950", "1")], asks=[("82050", "1")])
    fills = bx.on_tick()
    assert [(f.price, f.qty, f.is_maker) for f in fills] == [(D("81950"), D("0.5"), False)]
    await assert_balances_consistent(bx, START)


# ================================================================ end to end on SyntheticFeed data


def record_synthetic(store: Store, markets: dict[str, MarketInfo], symbols: list[str], minutes: int) -> None:
    """Record ``minutes`` of the deterministic synthetic feed (books every 2 s + 1-min candles)."""
    ref = snapshot_reference()
    bus = MarketDataBus()
    bus.on_book(store.add_book)
    clock = SimClock(T0)
    feed = SyntheticFeed(
        markets=markets,
        symbols=symbols,
        bus=bus,
        reference_mids=ref["mids"],
        reference_spreads=ref["spreads"],
        clock=clock,
        params=SyntheticParams(seed=11, step_s=2.0),
        on_candle=store.upsert_candles,
    )
    now = T0
    feed.publish(now)
    for _ in range(minutes * 30):
        feed.step(2.0)
        now += 2.0
        feed.publish(now)
    store.flush()


@pytest.fixture
def recorded(markets: dict[str, MarketInfo], tmp_path: Path) -> Iterator[Store]:
    store = Store(tmp_path / "wta.sqlite")
    record_synthetic(store, markets, ["BTCUSDT", "USDTTMN"], minutes=12)
    yield store
    store.close()


async def run_candles(
    store: Store, markets: dict[str, MarketInfo]
) -> tuple[BacktestExchange, list[list[Fill]], Decimal]:
    """Maker-first buy of 10 USDT at the bid of bar 0, then a MARKET sell of what we got."""
    candles = store.load_candles("USDTTMN", "1")
    clock = SimClock(T0)
    ex = BacktestExchange(markets, clock, spreads=observed_spreads(store, ["USDTTMN"]))
    per_bar: list[list[Fill]] = []
    limit = D("0")
    sold = False
    for i, candle in enumerate(candles):
        per_bar.append(step(ex, clock, candle))
        if i == 0:
            limit = ex.synthetic_books()["USDTTMN"].best_bid or D("0")
            await ex.place_order(req("buy", "BUY", "LIMIT", "10", price=str(limit), symbol="USDTTMN"))
        elif not sold and (await ex.get_order("buy")).status == "FILLED":
            usdt = (await bal(ex, "USDT")).free
            await ex.place_order(req("sell", "SELL", "MARKET", str(quantize_down(usdt, 2)), symbol="USDTTMN"))
            sold = True
    return ex, per_bar, limit


async def test_end_to_end_candles_on_recorded_synthetic_data(
    recorded: Store, markets: dict[str, MarketInfo]
) -> None:
    candles = recorded.load_candles("USDTTMN", "1")
    assert len(candles) >= 10
    spreads = observed_spreads(recorded, ["USDTTMN", "BTCUSDT", "ETHUSDT"])
    assert sorted(spreads) == ["BTCUSDT", "USDTTMN"]  # no books for ETHUSDT: left out
    assert D("0.0001") < spreads["USDTTMN"] < D("0.0002")

    ex, per_bar, limit = await run_candles(recorded, markets)
    assert ex.spread_source("USDTTMN") == "observed"
    assert per_bar[0] == []
    buy_bar = next(i for i, fills in enumerate(per_bar) if any(f.order_client_id == "buy" for f in fills))
    # the first LATER bar whose low reaches the limit fills it, at min(limit, open), as a maker
    first_reach = next(i for i, c in enumerate(candles) if i > 0 and c.l <= limit)
    assert buy_bar == first_reach
    buy = per_bar[buy_bar][0]
    assert (buy.price, buy.qty, buy.is_maker) == (min(limit, candles[buy_bar].o), D("10"), True)
    assert buy.fee == D("10") * TMN_MAKER
    # the MARKET sell is placed after that bar and fills on the next one: close x (1 - half - slip)
    sell = per_bar[buy_bar + 1][0]
    half = spreads["USDTTMN"] / 2
    expected = quantize_down(candles[buy_bar + 1].c * (1 - half - DEFAULT_SLIPPAGE), 0)
    assert (sell.order_client_id, sell.price, sell.qty, sell.is_maker) == ("sell", expected, D("9.97"), False)
    await assert_balances_consistent(ex, {"TMN": D("5000000")})
    tmn = await bal(ex, "TMN")
    assert tmn.free == D("5000000") - D("10") * buy.price + D("9.97") * sell.price * (1 - TMN_TAKER)

    # deterministic: the same data gives the same fills and the same state
    ex2, per_bar2, _ = await run_candles(recorded, markets)
    assert per_bar2 == per_bar and ex2.state() == ex.state()


async def test_end_to_end_books_on_recorded_synthetic_data(
    recorded: Store, markets: dict[str, MarketInfo]
) -> None:
    clock = SimClock(T0)
    current: dict[str, BookSnapshot] = {}
    ex = BacktestExchange(
        markets, clock, driver="books", books=lambda: dict(current), initial_balances=dict(START)
    )
    snapshots = list(recorded.iter_books("BTCUSDT"))
    assert len(snapshots) > 100
    by_ts: dict[float, BookSnapshot] = {}
    fills: list[Fill] = []
    limit = D("0")
    for i, book in enumerate(snapshots):
        current["BTCUSDT"] = book
        by_ts[book.ts] = book
        clock.set(book.ts)
        new = ex.on_tick(book.ts, dict(current))
        fills += new
        if i == 0:
            assert new == []
            limit = quantize_up((book.best_ask or D("0")) * D("1.001"), 2)
            await ex.place_order(req("b1", "BUY", "LIMIT", "0.003", price=str(limit)))
    assert fills, "the crossing order should fill on the recorded books"
    assert fills[0].ts == snapshots[1].ts  # next snapshot, not the one it was placed on
    for f in fills:
        book = by_ts[f.ts]
        assert f.price <= limit
        if not f.is_maker:  # taker fills take level prices, at most 80 % of the level
            level = next(lv for lv in book.asks if lv.price == f.price)
            assert f.qty <= level.qty * D("0.8")
    first_tick = [f for f in fills if f.ts == snapshots[1].ts]
    assert all(not f.is_maker for f in first_tick) and len(first_tick) >= 2  # walked several levels
    o = await ex.get_order("b1")
    assert o.filled_qty == sum((f.qty for f in fills), D("0")) <= D("0.003")
    await assert_balances_consistent(ex, START)

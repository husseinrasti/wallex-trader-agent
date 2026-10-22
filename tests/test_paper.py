"""PaperExchange (PRD-02 §9): validation, locks, next-tick latency and every fill rule.

Books come from a small mutable ``Feed``: a test sets the book it wants, then calls ``on_tick``.
Each ``Feed.set`` stamps a newer ``ts`` so the exchange sees a fresh snapshot (liquidity our
fills took from an unchanged snapshot is not offered again). Markets/fees come from conftest:
BTCUSDT (tick 2, step 6, maker 0.10%, taker 0.13%, min notional 1 USDT),
BTCTMN (tick 0, step 6, maker 0.25%, taker 0.30%, min notional 50,000 TMN), SOLTMN (no MARKET).
"""

from __future__ import annotations

import json
from decimal import Decimal

import pytest

from wta.exchange.base import Exchange, OrderRejected
from wta.exchange.paper import PaperExchange
from wta.execution.positions import valuate
from wta.models import Balance, BookSnapshot, Fill, Level, MarketInfo, OrderRequest
from wta.utils.decimalutil import D
from wta.utils.timeutil import SimClock

USDT_MAKER, USDT_TAKER = D("0.001"), D("0.0013")
TMN_MAKER, TMN_TAKER = D("0.0025"), D("0.003")
START = {"TMN": D("1000000000"), "USDT": D("10000"), "BTC": D("1")}

LevelsIn = list[tuple[str, str]]


class Feed:
    """The ``books`` callable handed to the exchange; tests swap books between ticks."""

    def __init__(self, t0: float) -> None:
        self.books: dict[str, BookSnapshot] = {}
        self._ts = t0

    def __call__(self) -> dict[str, BookSnapshot]:
        return dict(sorted(self.books.items()))

    def set(self, symbol: str, bids: LevelsIn, asks: LevelsIn) -> BookSnapshot:
        self._ts += 1
        book = BookSnapshot(
            symbol=symbol,
            ts=self._ts,
            source="rest",
            bids=[Level(price=D(p), qty=D(q)) for p, q in bids],
            asks=[Level(price=D(p), qty=D(q)) for p, q in asks],
        )
        self.books[symbol] = book
        return book


def req(
    cid: str,
    symbol: str,
    side: str,
    type_: str,
    qty: str,
    price: str | None = None,
    stop: str | None = None,
) -> OrderRequest:
    return OrderRequest(
        client_id=cid,
        symbol=symbol,
        side=side,  # type: ignore[arg-type]
        type=type_,  # type: ignore[arg-type]
        qty=D(qty),
        price=None if price is None else D(price),
        stop_price=None if stop is None else D(stop),
        strategy="S1",
        purpose="entry",
        decision_id=f"d_{cid}",
    )


async def bal(ex: PaperExchange, asset: str) -> Balance:
    return (await ex.get_balances()).get(asset, Balance(asset=asset, free=D("0")))


@pytest.fixture
def feed(t0: float) -> Feed:
    return Feed(t0)


@pytest.fixture
def ex(markets: dict[str, MarketInfo], feed: Feed, sim_clock: SimClock) -> PaperExchange:
    return PaperExchange(markets, feed, sim_clock, initial_balances=dict(START))


def usdt_book(feed: Feed, bid: str = "82800", ask: str = "82900") -> None:
    feed.set("BTCUSDT", bids=[(bid, "1")], asks=[(ask, "1")])


# ---------------------------------------------------------------- basics


async def test_interface_defaults_and_markets(markets, feed, sim_clock):
    ex = PaperExchange(markets, feed, sim_clock)
    assert isinstance(ex, Exchange) and ex.name == "paper"
    assert await ex.get_markets() == markets
    assert await ex.get_balances() == {"TMN": Balance(asset="TMN", free=D("5000000"))}
    ex2 = PaperExchange(markets, feed, sim_clock, initial_equity_tmn=D("2500000"))
    assert (await ex2.get_balances())["TMN"].free == D("2500000")


async def test_balances_are_sorted_and_include_locks(ex, feed):
    usdt_book(feed)
    await ex.place_order(req("s1", "BTCUSDT", "SELL", "LIMIT", "0.25", price="90000"))
    balances = await ex.get_balances()
    assert list(balances) == ["BTC", "TMN", "USDT"]
    assert balances["BTC"] == Balance(asset="BTC", free=D("0.75"), locked=D("0.25"))


# ---------------------------------------------------------------- LIMIT: maker / taker / latency


async def test_resting_limit_buy_fills_as_maker_at_own_price(ex, feed):
    usdt_book(feed)
    order = await ex.place_order(req("b1", "BTCUSDT", "BUY", "LIMIT", "0.01", price="82850"))
    assert order.status == "NEW" and order.filled_qty == 0
    lock = D("0.01") * D("82850") * (1 + USDT_TAKER)
    assert await bal(ex, "USDT") == Balance(asset="USDT", free=D("10000") - lock, locked=lock)

    assert ex.on_tick() == []  # ask 82900 > 82850: nothing crosses

    feed.set("BTCUSDT", bids=[("82700", "1")], asks=[("82840", "1")])
    fills = ex.on_tick()
    assert len(fills) == 1
    f = fills[0]
    assert (f.price, f.qty, f.is_maker) == (D("82850"), D("0.01"), True)  # own limit, not the 82840 ask
    assert (f.fee, f.fee_asset) == (D("0.01") * USDT_MAKER, "BTC")  # fee in the asset received
    o = await ex.get_order("b1")
    assert o.status == "FILLED" and o.avg_price == D("82850") and o.fee_paid == f.fee and o.fee_asset == "BTC"
    assert await bal(ex, "USDT") == Balance(asset="USDT", free=D("10000") - D("828.5"), locked=D("0"))
    assert await bal(ex, "BTC") == Balance(asset="BTC", free=D("1") + D("0.01") - f.fee)


async def test_no_fill_at_placement_only_on_next_tick(ex, feed, sim_clock):
    usdt_book(feed)
    await ex.place_order(req("b1", "BTCUSDT", "BUY", "LIMIT", "0.01", price="82950"))  # crossing
    assert (await ex.get_order("b1")).status == "NEW"
    assert await ex.get_my_trades() == []
    sim_clock.advance(2)
    fills = ex.on_tick()
    assert [(f.price, f.qty, f.is_maker) for f in fills] == [(D("82900"), D("0.01"), False)]
    assert fills[0].ts == sim_clock.now()


async def test_fill_uses_the_next_tick_book_not_the_placement_book(ex, feed):
    usdt_book(feed)
    await ex.place_order(req("b1", "BTCUSDT", "BUY", "LIMIT", "0.01", price="82950"))  # crossing now
    feed.set("BTCUSDT", bids=[("82990", "1")], asks=[("83000", "1")])  # moved away before the tick
    assert ex.on_tick() == []
    feed.set("BTCUSDT", bids=[("82800", "1")], asks=[("82940", "1")])
    fills = ex.on_tick()
    # it entered the book as a taker but found nothing; the order then rests and fills as a maker
    assert [(f.price, f.is_maker, f.fee) for f in fills] == [(D("82950"), True, D("0.01") * USDT_MAKER)]


async def test_crossing_limit_buy_walks_levels_then_rests_as_maker(ex, feed):
    feed.set(
        "BTCUSDT",
        bids=[("82800", "1")],
        asks=[("82900", "0.01"), ("82901", "0.01"), ("82902", "0.01")],
    )
    await ex.place_order(req("b1", "BTCUSDT", "BUY", "LIMIT", "0.025", price="82901.50"))
    fills = ex.on_tick()
    assert [(f.price, f.qty, f.is_maker) for f in fills] == [
        (D("82900"), D("0.01"), False),
        (D("82901"), D("0.01"), False),
    ]
    assert all(f.fee == D("0.01") * USDT_TAKER and f.fee_asset == "BTC" for f in fills)
    o = await ex.get_order("b1")
    assert o.status == "PARTIALLY_FILLED" and o.filled_qty == D("0.02") and o.avg_price == D("82900.5")

    assert ex.on_tick() == []  # same snapshot: the levels we took are gone

    feed.set("BTCUSDT", bids=[("82800", "1")], asks=[("82901", "1")])
    fills = ex.on_tick()
    assert [(f.price, f.qty, f.is_maker) for f in fills] == [(D("82901.50"), D("0.005"), True)]
    o = await ex.get_order("b1")
    assert o.status == "FILLED" and o.filled_qty == D("0.025")
    assert o.fee_paid == D("0.02") * USDT_TAKER + D("0.005") * USDT_MAKER
    spent = D("829") + D("829.01") + D("0.005") * D("82901.50")
    assert await bal(ex, "USDT") == Balance(asset="USDT", free=D("10000") - spent, locked=D("0"))
    assert (await bal(ex, "BTC")).free == D("1") + D("0.025") - o.fee_paid


async def test_resting_limit_sell_fills_as_maker_with_fee_in_quote(ex, feed):
    usdt_book(feed)
    await ex.place_order(req("s1", "BTCUSDT", "SELL", "LIMIT", "0.01", price="83000"))
    assert await bal(ex, "BTC") == Balance(asset="BTC", free=D("0.99"), locked=D("0.01"))
    assert ex.on_tick() == []
    feed.set("BTCUSDT", bids=[("83010", "0.004"), ("83005", "1")], asks=[("83020", "1")])
    fills = ex.on_tick()
    assert [(f.price, f.qty, f.is_maker) for f in fills] == [(D("83000"), D("0.01"), True)]
    assert (fills[0].fee, fills[0].fee_asset) == (D("830") * USDT_MAKER, "USDT")
    assert await bal(ex, "USDT") == Balance(asset="USDT", free=D("10000") + D("830") - D("0.83"))
    assert await bal(ex, "BTC") == Balance(asset="BTC", free=D("0.99"), locked=D("0"))


async def test_crossing_limit_sell_takes_bids_down_to_limit(ex, feed):
    feed.set("BTCUSDT", bids=[("82800", "0.01"), ("82750", "0.005"), ("82650", "1")], asks=[("82900", "1")])
    await ex.place_order(req("s1", "BTCUSDT", "SELL", "LIMIT", "0.02", price="82700"))
    fills = ex.on_tick()
    assert [(f.price, f.qty, f.is_maker) for f in fills] == [
        (D("82800"), D("0.01"), False),
        (D("82750"), D("0.005"), False),
    ]
    assert [f.fee for f in fills] == [D("828") * USDT_TAKER, D("413.75") * USDT_TAKER]
    o = await ex.get_order("s1")
    assert o.status == "PARTIALLY_FILLED" and o.remaining_qty == D("0.005")
    assert await bal(ex, "BTC") == Balance(asset="BTC", free=D("0.98"), locked=D("0.005"))


async def test_orders_share_level_liquidity_in_time_priority(ex, feed):
    feed.set("BTCUSDT", bids=[("82800", "1")], asks=[("82900", "0.01")])
    await ex.place_order(req("b1", "BTCUSDT", "BUY", "LIMIT", "0.01", price="82900"))
    await ex.place_order(req("b2", "BTCUSDT", "BUY", "LIMIT", "0.01", price="82900"))
    fills = ex.on_tick()
    assert [f.order_client_id for f in fills] == ["b1"]
    assert (await ex.get_order("b2")).status == "NEW"


async def test_fill_qty_rounds_down_to_step(ex, feed):
    feed.set("BTCUSDT", bids=[("82800", "1")], asks=[("82900", "0.0123456789")])
    await ex.place_order(req("b1", "BTCUSDT", "BUY", "LIMIT", "0.02", price="82900"))
    fills = ex.on_tick()
    assert [f.qty for f in fills] == [D("0.012345")]


async def test_missing_book_means_no_fill(ex, feed):
    usdt_book(feed)
    await ex.place_order(req("b1", "BTCUSDT", "BUY", "LIMIT", "0.01", price="82950"))
    del feed.books["BTCUSDT"]
    assert ex.on_tick() == []
    assert (await ex.get_order("b1")).status == "NEW"


# ---------------------------------------------------------------- MARKET


async def test_market_buy_walks_within_band_and_cancels_remainder(ex, feed):
    feed.set(
        "BTCUSDT",
        bids=[("82800", "1")],
        asks=[("82900", "0.01"), ("82950", "0.01"), ("85000", "5")],  # 85000 > 82900 x 1.02
    )
    await ex.place_order(req("m1", "BTCUSDT", "BUY", "MARKET", "0.05"))
    lock = D("0.05") * D("82900") * D("1.02") * (1 + USDT_TAKER)
    assert (await bal(ex, "USDT")).locked == lock
    assert (await ex.get_order("m1")).status == "NEW"  # nothing fills at placement

    fills = ex.on_tick()
    assert [(f.price, f.qty, f.is_maker) for f in fills] == [
        (D("82900"), D("0.01"), False),
        (D("82950"), D("0.01"), False),
    ]
    o = await ex.get_order("m1")
    assert o.status == "CANCELED" and o.filled_qty == D("0.02") and o.avg_price == D("82925")
    assert await bal(ex, "USDT") == Balance(asset="USDT", free=D("10000") - D("829") - D("829.5"))
    assert (await bal(ex, "BTC")).free == D("1") + D("0.02") - D("0.02") * USDT_TAKER
    assert await ex.get_open_orders() == []


async def test_market_sell_walks_bids_and_unlocks_unfilled_base(ex, feed):
    feed.set("BTCUSDT", bids=[("82800", "0.01"), ("80000", "1")], asks=[("82900", "1")])
    await ex.place_order(req("m1", "BTCUSDT", "SELL", "MARKET", "0.03"))
    assert (await bal(ex, "BTC")).locked == D("0.03")
    fills = ex.on_tick()
    assert [(f.price, f.qty) for f in fills] == [(D("82800"), D("0.01"))]  # 80000 is > 2% away
    assert (await ex.get_order("m1")).status == "CANCELED"
    assert await bal(ex, "BTC") == Balance(asset="BTC", free=D("0.99"), locked=D("0"))
    assert (await bal(ex, "USDT")).free == D("10000") + D("828") * (1 - USDT_TAKER)


async def test_market_buy_never_pays_above_its_lock_price(ex, feed):
    feed.set("BTCUSDT", bids=[("82800", "1")], asks=[("82900", "1")])
    await ex.place_order(req("m1", "BTCUSDT", "BUY", "MARKET", "0.01"))
    feed.set("BTCUSDT", bids=[("85000", "1")], asks=[("85100", "1")])  # +2.65% before the tick
    assert ex.on_tick() == []
    o = await ex.get_order("m1")
    assert o.status == "CANCELED" and o.filled_qty == 0
    assert await bal(ex, "USDT") == Balance(asset="USDT", free=D("10000"))


async def test_market_orders_disabled_are_rejected(ex, feed):
    feed.set("SOLTMN", bids=[("28000019", "10")], asks=[("28105486", "10")])
    for type_, stop in (("MARKET", None), ("STOP_MARKET", "27000000")):
        with pytest.raises(OrderRejected) as err:
            await ex.place_order(req(f"x_{type_}", "SOLTMN", "SELL", type_, "1", stop=stop))
        assert (err.value.status, err.value.code) == (400, "market_orders_disabled")


# ---------------------------------------------------------------- STOP orders


async def test_stop_limit_sell_triggers_down_then_fills_next_tick(ex, feed):
    usdt_book(feed)  # mid 82850
    await ex.place_order(req("st1", "BTCUSDT", "SELL", "STOP_LIMIT", "0.01", price="81900", stop="82000"))
    o = await ex.get_order("st1")
    assert o.raw["stop_direction"] == "down" and o.raw["triggered"] is False
    assert await bal(ex, "BTC") == Balance(asset="BTC", free=D("0.99"), locked=D("0.01"))  # stop locks

    assert ex.on_tick() == []  # bid 82800 > stop
    feed.set("BTCUSDT", bids=[("81990", "0.004"), ("81950", "1")], asks=[("82010", "1")])
    assert ex.on_tick() == []  # triggers on this tick, may fill only on the next one
    assert (await ex.get_order("st1")).raw["triggered"] is True

    fills = ex.on_tick()
    assert [(f.price, f.qty, f.is_maker) for f in fills] == [
        (D("81990"), D("0.004"), False),  # crossing at trigger -> taker at level prices
        (D("81950"), D("0.006"), False),
    ]
    assert fills[0].fee == D("0.004") * D("81990") * USDT_TAKER and fills[0].fee_asset == "USDT"
    assert (await ex.get_order("st1")).status == "FILLED"
    assert await bal(ex, "BTC") == Balance(asset="BTC", free=D("0.99"), locked=D("0"))


async def test_stop_limit_buy_triggers_up_when_ask_reaches_stop(ex, feed):
    usdt_book(feed)
    await ex.place_order(req("st1", "BTCUSDT", "BUY", "STOP_LIMIT", "0.01", price="84100", stop="84000"))
    assert (await ex.get_order("st1")).raw["stop_direction"] == "up"
    assert (await bal(ex, "USDT")).locked == D("0.01") * D("84100") * (1 + USDT_TAKER)
    feed.set("BTCUSDT", bids=[("83900", "1")], asks=[("83990", "1")])
    assert ex.on_tick() == [] and (await ex.get_order("st1")).raw["triggered"] is False
    feed.set("BTCUSDT", bids=[("83990", "1")], asks=[("84000", "1")])
    assert ex.on_tick() == []  # trigger tick
    fills = ex.on_tick()
    assert [(f.price, f.qty, f.is_maker) for f in fills] == [(D("84000"), D("0.01"), False)]
    assert await bal(ex, "USDT") == Balance(asset="USDT", free=D("10000") - D("840"))


async def test_stop_limit_not_crossing_at_trigger_rests_as_maker(ex, feed):
    usdt_book(feed)
    await ex.place_order(req("st1", "BTCUSDT", "SELL", "STOP_LIMIT", "0.01", price="82050", stop="82000"))
    ex.on_tick()
    feed.set("BTCUSDT", bids=[("81990", "1")], asks=[("82010", "1")])
    ex.on_tick()  # triggers; LIMIT SELL 82050 > bid 81990 -> maker
    assert ex.on_tick() == []
    feed.set("BTCUSDT", bids=[("82060", "1")], asks=[("82070", "1")])
    fills = ex.on_tick()
    assert [(f.price, f.is_maker, f.fee) for f in fills] == [
        (D("82050"), True, D("820.5") * USDT_MAKER),
    ]


async def test_stop_market_sell_triggers_then_walks_book(ex, feed):
    usdt_book(feed)
    await ex.place_order(req("sm1", "BTCUSDT", "SELL", "STOP_MARKET", "0.01", stop="82000"))
    assert (await bal(ex, "BTC")).locked == D("0.01")
    ex.on_tick()
    feed.set("BTCUSDT", bids=[("81990", "0.004"), ("81900", "1")], asks=[("82010", "1")])
    assert ex.on_tick() == []  # trigger tick
    fills = ex.on_tick()
    assert [(f.price, f.qty, f.is_maker) for f in fills] == [
        (D("81990"), D("0.004"), False),
        (D("81900"), D("0.006"), False),
    ]
    assert (await ex.get_order("sm1")).status == "FILLED"


async def test_stop_market_buy_locks_against_the_band_above_the_stop(ex, feed):
    usdt_book(feed)
    await ex.place_order(req("sm1", "BTCUSDT", "BUY", "STOP_MARKET", "0.01", stop="84000"))
    assert (await bal(ex, "USDT")).locked == D("0.01") * D("84000") * D("1.02") * (1 + USDT_TAKER)
    feed.set("BTCUSDT", bids=[("84000", "1")], asks=[("84010", "1")])
    ex.on_tick()
    fills = ex.on_tick()
    assert [(f.price, f.qty) for f in fills] == [(D("84010"), D("0.01"))]
    assert await bal(ex, "USDT") == Balance(asset="USDT", free=D("10000") - D("840.1"))


async def test_stop_without_book_uses_side_default_direction(markets, feed, sim_clock):
    ex = PaperExchange(markets, feed, sim_clock, initial_balances=dict(START))
    await ex.place_order(req("a", "BTCUSDT", "SELL", "STOP_LIMIT", "0.01", price="81900", stop="82000"))
    await ex.place_order(req("b", "BTCUSDT", "BUY", "STOP_LIMIT", "0.01", price="84100", stop="84000"))
    assert (await ex.get_order("a")).raw["stop_direction"] == "down"
    assert (await ex.get_order("b")).raw["stop_direction"] == "up"


# ---------------------------------------------------------------- cancel / locks


async def test_cancel_releases_the_whole_lock(ex, feed):
    usdt_book(feed)
    await ex.place_order(req("b1", "BTCUSDT", "BUY", "LIMIT", "0.01", price="82850"))
    assert (await bal(ex, "USDT")).locked > 0
    o = await ex.cancel_order("b1")
    assert o.status == "CANCELED"
    assert await bal(ex, "USDT") == Balance(asset="USDT", free=D("10000"))
    assert await ex.get_open_orders() == []
    again = await ex.cancel_order("b1")
    assert again == o  # terminal orders come back as they are
    assert await ex.cancel_order("nope") is None


async def test_cancel_after_partial_fill_unlocks_only_the_remainder(ex, feed):
    feed.set("BTCUSDT", bids=[("82800", "1")], asks=[("82900", "0.004")])
    await ex.place_order(req("b1", "BTCUSDT", "BUY", "LIMIT", "0.01", price="82900"))
    ex.on_tick()
    o = await ex.cancel_order("b1")
    assert o.status == "CANCELED" and o.filled_qty == D("0.004")
    assert await bal(ex, "USDT") == Balance(asset="USDT", free=D("10000") - D("0.004") * D("82900"))
    assert ex.on_tick() == []  # canceled orders never fill


async def test_cancel_filled_order_returns_it_unchanged(ex, feed):
    usdt_book(feed)
    await ex.place_order(req("s1", "BTCUSDT", "SELL", "LIMIT", "0.01", price="82800"))
    ex.on_tick()
    before = await bal(ex, "USDT")
    o = await ex.cancel_order("s1")
    assert o.status == "FILLED"
    assert await bal(ex, "USDT") == before


async def test_cancel_untriggered_stop_unlocks_base(ex, feed):
    usdt_book(feed)
    await ex.place_order(req("st1", "BTCUSDT", "SELL", "STOP_LIMIT", "0.5", price="81900", stop="82000"))
    assert (await bal(ex, "BTC")).free == D("0.5")
    await ex.cancel_order("st1")
    assert await bal(ex, "BTC") == Balance(asset="BTC", free=D("1"))


# ---------------------------------------------------------------- rejections


@pytest.mark.parametrize(
    ("order", "code"),
    [
        (req("r", "XYZTMN", "BUY", "LIMIT", "1", price="100"), "unknown_symbol"),
        (req("r", "BTCUSDT", "BUY", "LIMIT", "0.0000001", price="82850"), "qty_precision"),
        (req("r", "BTCUSDT", "BUY", "LIMIT", "0.01", price="82850.123"), "price_precision"),
        (
            req("r", "BTCUSDT", "SELL", "STOP_LIMIT", "0.01", price="81900", stop="82000.001"),
            "price_precision",
        ),
        (req("r", "BTCUSDT", "BUY", "LIMIT", "0.00001", price="82850"), "min_notional"),
        (req("r", "BTCTMN", "BUY", "LIMIT", "0.000001", price="22000000000"), "min_notional"),
        (req("r", "BTCUSDT", "BUY", "LIMIT", "1", price="82850"), "insufficient_balance"),
        (req("r", "BTCUSDT", "SELL", "LIMIT", "1.5", price="82850"), "insufficient_balance"),
        (req("r", "BTCUSDT", "BUY", "MARKET", "0.2"), "insufficient_balance"),
        (req("r", "BTCUSDT", "BUY", "LIMIT", "0.01"), "missing_price"),
        (req("r", "BTCUSDT", "SELL", "STOP_LIMIT", "0.01", price="81900"), "missing_stop_price"),
        (req("r", "BTCUSDT", "SELL", "STOP_MARKET", "0.01"), "missing_stop_price"),
        (req("r", "BTCUSDT", "BUY", "LIMIT", "0", price="82850"), "invalid_quantity"),
        (req("r", "BTCUSDT", "BUY", "LIMIT", "0.01", price="-1"), "invalid_price"),
        (req("r", "ETHUSDT", "BUY", "MARKET", "0.01"), "no_market_price"),
    ],
)
async def test_invalid_orders_are_rejected_without_side_effects(ex, feed, order, code):
    usdt_book(feed)
    before = await ex.get_balances()
    with pytest.raises(OrderRejected) as err:
        await ex.place_order(order)
    assert (err.value.status, err.value.code) == (400, code)
    assert await ex.get_order(order.client_id) is None
    assert await ex.get_balances() == before


async def test_stop_price_on_a_limit_order_is_ignored(ex, feed):
    """The REST client only sends ``stop_Price`` for STOP_* orders, so the exchange never sees it."""
    usdt_book(feed)
    o = await ex.place_order(req("b1", "BTCUSDT", "BUY", "LIMIT", "0.01", price="82850", stop="1.23456"))
    assert o.status == "NEW"


async def test_min_qty_rejection(markets, feed, sim_clock):
    tiny = MarketInfo(
        symbol="ETHTMN", base="ETH", quote="TMN", tick_decimals=0, step_decimals=4, min_qty=D("0.001")
    )
    ex = PaperExchange({**markets, "ETHTMN": tiny}, feed, sim_clock, initial_balances={"TMN": D("1e9")})
    with pytest.raises(OrderRejected) as err:
        await ex.place_order(req("r", "ETHTMN", "BUY", "LIMIT", "0.0009", price="650000000"))
    assert err.value.code == "min_qty"


async def test_duplicate_client_id_is_rejected_even_after_cancel(ex, feed):
    usdt_book(feed)
    await ex.place_order(req("dup", "BTCUSDT", "BUY", "LIMIT", "0.01", price="82850"))
    with pytest.raises(OrderRejected) as err:
        await ex.place_order(req("dup", "BTCUSDT", "BUY", "LIMIT", "0.01", price="82850"))
    assert err.value.code == "duplicate_client_id"
    await ex.cancel_order("dup")
    with pytest.raises(OrderRejected):
        await ex.place_order(req("dup", "BTCUSDT", "BUY", "LIMIT", "0.01", price="82850"))


async def test_locked_funds_are_not_free_for_the_next_order(ex, feed):
    usdt_book(feed)
    await ex.place_order(req("s1", "BTCUSDT", "SELL", "LIMIT", "0.6", price="90000"))
    with pytest.raises(OrderRejected) as err:
        await ex.place_order(req("s2", "BTCUSDT", "SELL", "LIMIT", "0.6", price="90000"))
    assert err.value.code == "insufficient_balance"


# ---------------------------------------------------------------- queries / callback / copies


async def test_open_orders_and_trade_filters(ex, feed, sim_clock):
    usdt_book(feed)
    feed.set("BTCTMN", bids=[("22000000000", "1")], asks=[("22100000000", "1")])
    await ex.place_order(req("u1", "BTCUSDT", "BUY", "LIMIT", "0.01", price="82000"))
    await ex.place_order(req("t1", "BTCTMN", "SELL", "LIMIT", "0.01", price="22000000000"))
    await ex.place_order(req("u2", "BTCUSDT", "SELL", "LIMIT", "0.01", price="82800"))
    assert [o.client_id for o in await ex.get_open_orders()] == ["u1", "t1", "u2"]
    assert [o.client_id for o in await ex.get_open_orders("BTCUSDT")] == ["u1", "u2"]
    sim_clock.advance(2)
    ex.on_tick()
    assert [f.order_client_id for f in await ex.get_my_trades()] == ["t1", "u2"]
    assert [f.order_client_id for f in await ex.get_my_trades("BTCUSDT")] == ["u2"]
    assert await ex.get_my_trades(since_ts=sim_clock.now() + 1) == []
    assert [f.trade_id for f in await ex.get_my_trades()] == ["paper-1", "paper-2"]


async def test_on_fill_callback_gets_every_fill(markets, feed, sim_clock):
    seen: list[Fill] = []
    ex = PaperExchange(markets, feed, sim_clock, initial_balances=dict(START), on_fill=seen.append)
    feed.set("BTCUSDT", bids=[("82800", "1")], asks=[("82900", "0.01"), ("82901", "1")])
    await ex.place_order(req("b1", "BTCUSDT", "BUY", "LIMIT", "0.02", price="82901"))
    fills = ex.on_tick(now=sim_clock.now() + 5)
    assert seen == fills and len(fills) == 2
    assert all(f.ts == sim_clock.now() + 5 for f in fills)


async def test_returned_orders_are_copies(ex, feed):
    usdt_book(feed)
    o = await ex.place_order(req("b1", "BTCUSDT", "BUY", "LIMIT", "0.01", price="82850"))
    o.status = "CANCELED"
    o.reprices = 3
    fresh = await ex.get_order("b1")
    assert fresh.status == "NEW" and fresh.reprices == 0


# ---------------------------------------------------------------- conservation


async def test_value_changes_only_by_fees_at_constant_price(markets, feed, sim_clock):
    """With every trade at one price P, TMN value moves exactly by the fees (valued at P)."""
    p = D("2000000000")
    ex = PaperExchange(markets, feed, sim_clock, initial_balances={"TMN": D("1000000000"), "BTC": D("1")})
    mark = {"BTCTMN": BookSnapshot(symbol="BTCTMN", ts=0, source="rest", bids=[Level(price=p, qty=D(1))])}

    async def value() -> Decimal:
        v = valuate(await ex.get_balances(), mark)
        assert v.complete
        return v.equity_tmn

    start = await value()
    # maker BUY at P (rests, then the ask comes down to P)
    feed.set("BTCTMN", bids=[(str(p - 10), "1")], asks=[(str(p + 10), "1")])
    await ex.place_order(req("mb", "BTCTMN", "BUY", "LIMIT", "0.01", price=str(p)))
    ex.on_tick()
    feed.set("BTCTMN", bids=[(str(p - 10), "1")], asks=[(str(p), "1")])
    ex.on_tick()
    # taker SELL at P, MARKET BUY at P, and a placed-then-canceled order
    feed.set("BTCTMN", bids=[(str(p), "1")], asks=[(str(p), "1")])
    await ex.place_order(req("ts", "BTCTMN", "SELL", "LIMIT", "0.005", price=str(p)))
    await ex.place_order(req("mk", "BTCTMN", "BUY", "MARKET", "0.002"))
    await ex.place_order(req("cx", "BTCTMN", "BUY", "LIMIT", "0.003", price=str(p - 1000)))
    ex.on_tick()
    await ex.cancel_order("cx")

    trades = await ex.get_my_trades()
    assert [(f.order_client_id, f.is_maker) for f in trades] == [("mb", True), ("ts", False), ("mk", False)]
    assert all(f.price == p for f in trades)
    fees_tmn = sum((f.fee * (p if f.fee_asset == "BTC" else 1) for f in trades), D("0"))
    assert fees_tmn == D("0.01") * TMN_MAKER * p + D("0.005") * p * TMN_TAKER + D("0.002") * TMN_TAKER * p
    assert await value() == start - fees_tmn
    assert all(b.locked == 0 for b in (await ex.get_balances()).values())
    assert (await bal(ex, "BTC")).free == D("1") + D("0.01") - D("0.005") + D("0.002") - D("0.000031")


# ---------------------------------------------------------------- persistence / determinism


async def _scenario(ex: PaperExchange, feed: Feed) -> None:
    """A resting partial fill, an untriggered stop, a triggered stop, a filled and a canceled order."""
    feed.set("BTCUSDT", bids=[("82800", "1")], asks=[("82900", "0.004")])
    await ex.place_order(req("part", "BTCUSDT", "BUY", "LIMIT", "0.01", price="82900"))
    await ex.place_order(req("stop", "BTCUSDT", "SELL", "STOP_LIMIT", "0.2", price="80000", stop="80100"))
    await ex.place_order(req("trig", "BTCUSDT", "SELL", "STOP_LIMIT", "0.1", price="82700", stop="82850"))
    await ex.place_order(req("done", "BTCUSDT", "SELL", "LIMIT", "0.01", price="82800"))
    await ex.place_order(req("gone", "BTCUSDT", "BUY", "LIMIT", "0.01", price="80000"))
    await ex.cancel_order("gone")
    ex.on_tick()  # part partially fills, done fills, trig triggers (bid 82800 <= 82850)


async def test_state_round_trip_continues_where_it_stopped(markets, feed, sim_clock):
    ex = PaperExchange(markets, feed, sim_clock, initial_balances=dict(START))
    await _scenario(ex, feed)
    saved = ex.state()
    text = json.dumps(saved, sort_keys=True)  # what the engine writes to kv "paper_state"

    restored = PaperExchange(markets, feed, sim_clock, state=json.loads(text))
    assert restored.state() == saved
    assert restored.tick_count == ex.tick_count == 1
    assert await restored.get_balances() == await ex.get_balances()
    assert await restored.get_open_orders() == await ex.get_open_orders()
    assert await restored.get_my_trades() == await ex.get_my_trades()
    assert (await restored.get_order("trig")).raw["triggered"] is True

    # both continue identically on the next snapshot (the triggered stop fills now)
    feed.set("BTCUSDT", bids=[("82750", "1")], asks=[("82850", "1")])
    sim_clock.advance(2)
    assert ex.on_tick() == restored.on_tick()
    assert ex.state() == restored.state()
    ids = [f.trade_id for f in await restored.get_my_trades()]
    assert len(ids) == len(set(ids))  # trade ids continue, never reused
    with pytest.raises(OrderRejected):  # client ids survive the restart
        await restored.place_order(req("done", "BTCUSDT", "SELL", "LIMIT", "0.01", price="90000"))


async def test_same_inputs_give_identical_state(markets, t0):
    runs = []
    for _ in range(2):
        feed, clock = Feed(t0), SimClock(t0)
        ex = PaperExchange(markets, feed, clock, initial_balances=dict(START))
        await _scenario(ex, feed)
        runs.append(json.dumps(ex.state(), sort_keys=True))
    assert runs[0] == runs[1]


def test_load_state_rejects_unknown_version(markets, feed, sim_clock):
    with pytest.raises(ValueError):
        PaperExchange(markets, feed, sim_clock, state={"version": 99})

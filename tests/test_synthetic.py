"""Deterministic synthetic market feed (offline demo / engine smoke runs).

The feed is not evidence of strategy performance; these tests only make sure it is a *sane*
stand-in for Wallex: reproducible for a seed, well-formed books on the market's tick/step grid,
TMN and USDT books that agree on average, dislocations at roughly the configured rate, per-minute
candles, and a run loop that stops when asked.
"""

from __future__ import annotations

import asyncio
import statistics
from decimal import Decimal

import pytest

from wta.config import Config
from wta.data.synthetic import SyntheticFeed, SyntheticParams
from wta.exchange.base import MarketDataBus
from wta.exchange.markets import load_markets_snapshot, snapshot_reference
from wta.features.engine import cross_features
from wta.models import BookSnapshot, Candle, MarketInfo
from wta.utils.decimalutil import quantize_down
from wta.utils.timeutil import SimClock

from .conftest import T0

COINS = ["BTC", "DOGE", "ETH", "SOL", "XRP"]


@pytest.fixture(scope="module")
def snap_markets() -> dict[str, MarketInfo]:
    return load_markets_snapshot()


def _feed(
    snap_markets: dict[str, MarketInfo],
    params: SyntheticParams | None = None,
    clock: SimClock | None = None,
    on_candle: object = None,
) -> tuple[SyntheticFeed, MarketDataBus, list[BookSnapshot]]:
    ref = snapshot_reference()
    bus = MarketDataBus()
    seen: list[BookSnapshot] = []
    bus.on_book(seen.append)
    feed = SyntheticFeed(
        markets=snap_markets,
        symbols=list(snap_markets),
        bus=bus,
        reference_mids=ref["mids"],
        reference_spreads=ref["spreads"],
        clock=clock or SimClock(T0),
        params=params or SyntheticParams(),
        on_candle=on_candle,  # type: ignore[arg-type]
    )
    return feed, bus, seen


def _simulate(feed: SyntheticFeed, ticks: int, dt: float = 1.0, start: float = T0) -> None:
    feed.publish(start)
    for i in range(1, ticks):
        feed.step(dt)
        feed.publish(start + i * dt)


# ---------------------------------------------------------------- determinism


def test_same_seed_same_books(snap_markets: dict[str, MarketInfo]) -> None:
    a, _, seen_a = _feed(snap_markets, SyntheticParams(seed=11))
    b, _, seen_b = _feed(snap_markets, SyntheticParams(seed=11))
    _simulate(a, 50)
    _simulate(b, 50)
    assert len(seen_a) == 50 * len(snap_markets)  # one book per watchlist symbol per tick
    assert seen_a == seen_b
    c, _, seen_c = _feed(snap_markets, SyntheticParams(seed=12))
    _simulate(c, 50)
    assert seen_c != seen_a


def test_symbols_are_published_in_sorted_order(snap_markets: dict[str, MarketInfo]) -> None:
    feed, bus, seen = _feed(snap_markets)
    assert feed.publish(T0) == len(snap_markets)
    assert [b.symbol for b in seen] == sorted(snap_markets)
    assert bus.snapshot() == {b.symbol: b for b in seen}
    assert bus.source_ok("synthetic", T0, 30)
    assert feed.ticks == 1


# ---------------------------------------------------------------- book shape


def test_books_are_well_formed(snap_markets: dict[str, MarketInfo]) -> None:
    feed, _, seen = _feed(snap_markets, SyntheticParams(seed=3))
    _simulate(feed, 200)
    for b in seen:
        m = snap_markets[b.symbol]
        assert b.source == "synthetic"
        assert len(b.bids) == 10 and len(b.asks) == 10
        bids = [lv.price for lv in b.bids]
        asks = [lv.price for lv in b.asks]
        assert bids == sorted(bids, reverse=True) and len(set(bids)) == 10  # strictly descending
        assert asks == sorted(asks) and len(set(asks)) == 10  # strictly ascending
        assert b.best_bid is not None and b.best_ask is not None and b.best_bid < b.best_ask
        for lv in b.bids + b.asks:
            assert lv.price > 0 and lv.price == lv.price.quantize(m.tick), (b.symbol, lv.price)
            assert lv.qty > 0 and lv.qty == quantize_down(lv.qty, m.step_decimals), (b.symbol, lv.qty)


def test_reference_spreads_are_respected(snap_markets: dict[str, MarketInfo]) -> None:
    ref = snapshot_reference()["spreads"]
    feed, _, seen = _feed(snap_markets)
    feed.publish(T0)
    for b in seen:
        target = ref[b.symbol]
        tick_pct = snap_markets[b.symbol].tick / b.mid  # type: ignore[operator]
        # rounding bids down / asks up to the tick widens the spread by at most two ticks
        assert target <= b.spread_pct <= target + 2 * tick_pct + Decimal("1e-9"), b.symbol  # type: ignore[operator]


def test_book_and_mid_for_unknown_symbols(snap_markets: dict[str, MarketInfo]) -> None:
    feed, _, _ = _feed(snap_markets)
    assert feed.mid("FOOTMN") is None
    assert feed.book("FOOTMN", T0) is None
    assert feed.mid("USDTTMN") == pytest.approx(266739)
    assert feed.mid("BTCUSDT") == pytest.approx(82877)
    assert feed.mid("BTCTMN") == pytest.approx(82877 * 266739)  # no lag at start


# ---------------------------------------------------------------- cross-book consistency


def test_tmn_books_follow_usdt_books_times_the_dollar(snap_markets: dict[str, MarketInfo], cfg: Config) -> None:
    feed, bus, _ = _feed(snap_markets, SyntheticParams(seed=5))
    abs_devs: list[Decimal] = []
    feed.publish(T0)
    for i in range(1, 1001):  # 50 simulated minutes, 3 s ticks
        feed.step(3.0)
        feed.publish(T0 + 3 * i)
        if i % 4:
            continue
        books = bus.snapshot()
        for coin in COINS:
            f = cross_features(coin, books, snap_markets, cfg, T0 + 3 * i)
            assert f is not None
            abs_devs.append(abs(f["dev_mid_pct"]))
            # the book-implied deviation is the model's lag (up to tick rounding of the mids)
            lag = Decimal(repr(feed._lag[coin]))
            assert abs(f["dev_mid_pct"] - lag) < Decimal("0.0005"), coin
    assert statistics.mean(abs_devs) < Decimal("0.001")  # 0.1 %: TMN and USDT books agree on average
    assert statistics.median(abs_devs) < Decimal("0.0005")


def test_dislocation_rate(snap_markets: dict[str, MarketInfo]) -> None:
    """Jumps of 0.36-0.84 % per coin at ~``jump_rate_per_hour``; diffusion steps are ~0.004 %."""
    hours, rate = 20, 1.0
    feed, _, _ = _feed(snap_markets, SyntheticParams(seed=21, jump_rate_per_hour=rate))
    jumps = 0
    max_abs_lag = 0.0
    for _ in range(int(hours * 3600 / 5)):
        before = dict(feed._lag)
        feed.step(5.0)
        for coin, lag in feed._lag.items():
            if abs(lag - before[coin]) > 0.002:
                jumps += 1
            max_abs_lag = max(max_abs_lag, abs(lag))
    expected = rate * hours * len(feed._lag)  # one jump process per coin with a USDT book
    assert 0.6 * expected <= jumps <= 1.4 * expected
    assert max_abs_lag > 0.003  # dislocations big enough to beat S1's 0.43 % lock-in cost appear


def test_no_jumps_when_disabled(snap_markets: dict[str, MarketInfo]) -> None:
    feed, _, _ = _feed(snap_markets, SyntheticParams(seed=21, jump_rate_per_hour=0.0))
    for _ in range(2000):
        before = dict(feed._lag)
        feed.step(5.0)
        assert all(abs(feed._lag[c] - before[c]) < 0.002 for c in COINS)
    assert max(abs(v) for v in feed._lag.values()) < 0.002


# ---------------------------------------------------------------- candles


def test_candles_aggregate_per_minute(snap_markets: dict[str, MarketInfo]) -> None:
    batches: list[list[Candle]] = []
    feed, _, seen = _feed(snap_markets, SyntheticParams(seed=9), on_candle=batches.append)
    times = [T0 + 20 * i for i in range(7)]  # 10:00:00 .. 10:02:00, three publishes per minute
    feed.publish(times[0])
    for t in times[1:]:
        feed.step(20.0)
        feed.publish(t)
    assert len(batches) == len(times)

    sym = "BTCUSDT"
    m = snap_markets[sym]
    pxs = [quantize_down(b.mid, m.tick_decimals) for b in seen if b.symbol == sym]  # type: ignore[arg-type]
    minute0, minute1 = pxs[0:3], pxs[3:6]
    # first publish of a minute: one open candle per symbol
    assert [c.symbol for c in batches[0]] == sorted(snap_markets)
    open0 = next(c for c in batches[2] if c.symbol == sym)
    assert open0.ts == T0 and open0.resolution == "1"
    assert (open0.o, open0.h, open0.l, open0.c) == (minute0[0], max(minute0), min(minute0), minute0[-1])
    assert open0.v > 0
    # the first publish of 10:01 closes 10:00 (same numbers) and opens 10:01
    batch = [c for c in batches[3] if c.symbol == sym]
    assert [c.ts for c in batch] == [T0, T0 + 60]
    assert batch[0] == open0
    assert batch[1].o == batch[1].c == minute1[0]
    closed1 = next(c for c in batches[6] if c.symbol == sym and c.ts == T0 + 60)
    assert (closed1.o, closed1.h, closed1.l, closed1.c) == (minute1[0], max(minute1), min(minute1), minute1[-1])
    first0 = next(c for c in batches[0] if c.symbol == sym)
    assert 0 < first0.v <= open0.v  # volume accumulates within the minute
    assert all(c.ts % 60 == 0 for b in batches for c in b)


def test_trades_are_published(snap_markets: dict[str, MarketInfo]) -> None:
    feed, bus, _ = _feed(snap_markets)
    trades = []
    bus.on_trade(trades.append)
    feed.publish(T0)
    assert [t.symbol for t in trades] == sorted(snap_markets)
    assert all(t.ts == T0 and t.qty > 0 and t.side in ("BUY", "SELL") for t in trades)


# ---------------------------------------------------------------- run loop


async def test_run_honours_stop_event(snap_markets: dict[str, MarketInfo]) -> None:
    clock = SimClock(T0)
    feed, bus, seen = _feed(snap_markets, SyntheticParams(step_s=2.0), clock=clock)
    stop = asyncio.Event()
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)
        clock.advance(seconds)
        if len(sleeps) == 3:
            stop.set()  # shutdown requested while the feed sleeps

    await asyncio.wait_for(feed.run(stop, sleep=fake_sleep), timeout=5)
    assert sleeps == [2.0, 2.0, 2.0]
    assert feed.ticks == 3  # initial publish + two loop publishes; nothing after the stop
    assert sorted({b.ts for b in seen}) == [T0, T0 + 2, T0 + 4]
    assert bus.source_ok("synthetic", T0 + 4, 30)


async def test_run_with_stop_already_set_publishes_once(snap_markets: dict[str, MarketInfo]) -> None:
    feed, _, _ = _feed(snap_markets)
    stop = asyncio.Event()
    stop.set()

    async def never(seconds: float) -> None:  # pragma: no cover - must not be called
        raise AssertionError("slept after stop")

    await feed.run(stop, sleep=never)
    assert feed.ticks == 1

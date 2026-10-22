"""MarketDataBus: freshest book per symbol wins, listeners, snapshots, per-source staleness."""

from __future__ import annotations

from collections.abc import Callable
from decimal import Decimal

from wta.exchange.base import MarketDataBus
from wta.models import BookSnapshot, Trade
from wta.utils.timeutil import SimClock

BookFactory = Callable[..., BookSnapshot]  # the conftest ``make_book`` fixture


def _btc(make_book: BookFactory, ts: float, bid: str = "82845.00", source: str = "rest") -> BookSnapshot:
    return make_book("BTCUSDT", bid, "82909.16", ts=ts, source=source)


# ---------------------------------------------------------------- publish_book


def test_publish_book_stores_and_replaces_with_newer(make_book: BookFactory, sim_clock: SimClock) -> None:
    bus = MarketDataBus()
    t = sim_clock.now()
    first, newer = _btc(make_book, t), _btc(make_book, t + 1, bid="82850.00")
    assert bus.publish_book(first) is True
    assert bus.latest("BTCUSDT") is first
    assert bus.publish_book(newer) is True
    assert bus.latest("BTCUSDT") is newer


def test_publish_book_ignores_an_older_snapshot(make_book: BookFactory, sim_clock: SimClock) -> None:
    bus = MarketDataBus()
    t = sim_clock.now()
    fresh = _btc(make_book, t, source="ws")
    stale = _btc(make_book, t - 2, bid="82000.00", source="rest")  # slow REST poll arrives late
    bus.publish_book(fresh)
    assert bus.publish_book(stale) is False
    assert bus.latest("BTCUSDT") is fresh


def test_publish_book_accepts_an_equally_fresh_snapshot(make_book: BookFactory, sim_clock: SimClock) -> None:
    bus = MarketDataBus()
    t = sim_clock.now()
    bus.publish_book(_btc(make_book, t, source="ws"))
    same_ts = _btc(make_book, t, bid="82846.00", source="rest")
    assert bus.publish_book(same_ts) is True
    assert bus.latest("BTCUSDT") is same_ts


def test_symbols_are_independent(make_book: BookFactory, sim_clock: SimClock) -> None:
    bus = MarketDataBus()
    t = sim_clock.now()
    bus.publish_book(make_book("BTCTMN", "22075699081", "22106074058", ts=t + 10))
    assert bus.publish_book(make_book("USDTTMN", "266720", "266758", ts=t)) is True
    assert bus.latest("USDTTMN") is not None
    assert bus.latest("ETHTMN") is None


# ---------------------------------------------------------------- listeners


def test_book_listeners_are_called_only_for_stored_books(make_book: BookFactory, sim_clock: SimClock) -> None:
    bus = MarketDataBus()
    seen_a: list[BookSnapshot] = []
    seen_b: list[BookSnapshot] = []
    bus.on_book(seen_a.append)
    bus.on_book(seen_b.append)
    t = sim_clock.now()
    fresh, stale = _btc(make_book, t), _btc(make_book, t - 1)
    bus.publish_book(fresh)
    bus.publish_book(stale)
    assert seen_a == [fresh]
    assert seen_b == [fresh]


def test_trade_listeners(sim_clock: SimClock) -> None:
    bus = MarketDataBus()
    seen: list[Trade] = []
    bus.on_trade(seen.append)
    trade = Trade(
        symbol="BTCUSDT", ts=sim_clock.now(), price=Decimal("82850.00"), qty=Decimal("0.001"), side="BUY"
    )
    bus.publish_trade(trade)
    assert seen == [trade]


# ---------------------------------------------------------------- snapshot / age


def test_snapshot_is_sorted_by_symbol(make_book: BookFactory, sim_clock: SimClock) -> None:
    bus = MarketDataBus()
    t = sim_clock.now()
    for symbol in ("USDTTMN", "BTCUSDT", "ETHTMN", "BTCTMN"):
        bus.publish_book(make_book(symbol, "100", "101", ts=t))
    assert list(bus.snapshot()) == ["BTCTMN", "BTCUSDT", "ETHTMN", "USDTTMN"]
    assert list(bus.snapshot(["USDTTMN", "SOLTMN", "BTCTMN"])) == ["BTCTMN", "USDTTMN"]  # unknown skipped


def test_snapshot_is_a_copy(make_book: BookFactory, sim_clock: SimClock) -> None:
    bus = MarketDataBus()
    bus.publish_book(_btc(make_book, sim_clock.now()))
    snap = bus.snapshot()
    snap.clear()
    assert bus.latest("BTCUSDT") is not None


def test_age(make_book: BookFactory, sim_clock: SimClock) -> None:
    bus = MarketDataBus()
    bus.publish_book(_btc(make_book, sim_clock.now()))
    sim_clock.advance(7.5)
    assert bus.age("BTCUSDT", sim_clock.now()) == 7.5
    assert bus.age("ETHUSDT", sim_clock.now()) is None


# ---------------------------------------------------------------- source health


def test_source_ok_goes_stale(make_book: BookFactory, sim_clock: SimClock) -> None:
    bus = MarketDataBus()
    t = sim_clock.now()
    bus.publish_book(_btc(make_book, t, source="ws"))
    assert bus.source_ok("ws", now=t + 30, stale_s=30)  # exactly at the limit is still fresh
    assert not bus.source_ok("ws", now=t + 30.001, stale_s=30)
    assert not bus.source_ok("rest", now=t, stale_s=30)  # never published


def test_stale_book_does_not_refresh_the_source(make_book: BookFactory, sim_clock: SimClock) -> None:
    bus = MarketDataBus()
    t = sim_clock.now()
    bus.publish_book(_btc(make_book, t, source="ws"))
    bus.publish_book(_btc(make_book, t - 60, source="ws"))
    assert bus.sources["ws"].last_update_ts == t


def test_mark_source_failure_and_recovery(make_book: BookFactory, sim_clock: SimClock) -> None:
    bus = MarketDataBus()
    t = sim_clock.now()
    bus.publish_book(_btc(make_book, t, source="rest"))
    bus.mark_source("rest", ok=False)
    bus.mark_source("rest", ok=False)
    assert not bus.source_ok("rest", now=t, stale_s=30)  # fresh data, but the source reported errors
    assert bus.sources["rest"].errors == 2
    bus.mark_source("rest", ok=True, now=t + 100)
    assert bus.source_ok("rest", now=t + 100, stale_s=30)


def test_mark_source_ok_without_time_keeps_the_last_update(sim_clock: SimClock) -> None:
    bus = MarketDataBus()
    t = sim_clock.now()
    bus.mark_source("ws", ok=True, now=t)
    bus.mark_source("ws", ok=True)  # e.g. a ping answer: alive, but no fresh data
    assert bus.sources["ws"].last_update_ts == t
    assert not bus.source_ok("ws", now=t + 31, stale_s=30)

"""Equity in TMN and the portfolio's risk counters (PRD-02 §2, Overview §5).

Covers ``valuate`` / ``asset_price_tmn`` on mixed balances (TMN, USDT, a coin with a TMN book, a
coin with only a USDT book, a coin with no book at all), the derived ratios (drawdown, daily PnL,
total return, coin exposure, USDT share), the 00:00 UTC day rollover, the placement counters used
by the rate caps, and the persistence round trip that lets a restart keep peak equity, day-start
equity, fees, placements and realized PnL.

Every expected number is written out by hand from the literal prices below.
"""

from __future__ import annotations

import json
from decimal import Decimal

import pytest

from wta.execution.positions import (
    PortfolioState,
    Valuation,
    asset_price_tmn,
    quote_to_tmn,
    valuate,
    value_tmn,
)
from wta.models import Balance, BookSnapshot, Fill, MarketInfo
from wta.utils.timeutil import day_start

from .conftest import T0, BookFactory

U_BID = Decimal("266720")  # USDTTMN bid (Overview §2.1)
BTC_TMN_BID = Decimal("22075699081")  # BTCTMN bid
ETH_USDT_BID = Decimal("2440.00")  # ETH has only a USDT book in these fixtures
MIDNIGHT = day_start(T0) + 86400  # 2026-10-23T00:00:00Z


def _bal(asset: str, free: str, locked: str = "0") -> Balance:
    return Balance(asset=asset, free=Decimal(free), locked=Decimal(locked))


@pytest.fixture
def books(make_book: BookFactory) -> dict[str, BookSnapshot]:
    """USDTTMN + BTCTMN + ETHUSDT. No SOL book and no ETHTMN book on purpose."""
    return {
        "USDTTMN": make_book("USDTTMN", "266720", "266758"),
        "BTCTMN": make_book("BTCTMN", "22075699081", "22106074058"),
        "BTCUSDT": make_book("BTCUSDT", "82845.00", "82909.16"),
        "ETHUSDT": make_book("ETHUSDT", "2440.00", "2441.00"),
    }


@pytest.fixture
def mixed() -> dict[str, Balance]:
    """1.2M TMN (200k locked in an order), 5 USDT, 0.0001 BTC, 0.001 ETH, an empty XRP row."""
    return {
        "TMN": _bal("TMN", "1000000", "200000"),
        "USDT": _bal("USDT", "4", "1"),
        "BTC": _bal("BTC", "0.0001"),
        "ETH": _bal("ETH", "0.001"),
        "XRP": _bal("XRP", "0"),
    }


# Hand-computed parts of the mixed portfolio
TMN_PART = Decimal("1200000")
USDT_PART = Decimal("5") * U_BID  # 1,333,600
BTC_PART = Decimal("0.0001") * BTC_TMN_BID  # 2,207,569.9081 (TMN book preferred over BTCUSDT)
ETH_PART = Decimal("0.001") * ETH_USDT_BID * U_BID  # 650,796.8 via bid(ETHUSDT) x bid(USDTTMN)
EQUITY = TMN_PART + USDT_PART + BTC_PART + ETH_PART  # 5,391,966.7081


# ---------------------------------------------------------------- prices and valuation


def test_asset_price_tmn_rules(books: dict[str, BookSnapshot]) -> None:
    assert asset_price_tmn("TMN", books) == Decimal(1)
    assert asset_price_tmn("USDT", books) == U_BID
    # a TMN book wins over the USDT route (BTCUSDT x USDTTMN would give 22,096,418,400)
    assert asset_price_tmn("BTC", books) == BTC_TMN_BID
    assert asset_price_tmn("ETH", books) == Decimal("650796800.0000")  # 2440.00 x 266,720
    assert asset_price_tmn("SOL", books) is None


def test_prices_without_the_dollar_book(books: dict[str, BookSnapshot]) -> None:
    no_dollar = {k: v for k, v in books.items() if k != "USDTTMN"}
    assert asset_price_tmn("USDT", no_dollar) is None
    assert asset_price_tmn("ETH", no_dollar) is None  # USDT route needs USDTTMN
    assert asset_price_tmn("BTC", no_dollar) == BTC_TMN_BID  # direct TMN book still works
    assert asset_price_tmn("TMN", {}) == Decimal(1)


def test_bid_side_is_used(make_book: BookFactory) -> None:
    """Valuation is conservative: what we would get selling now (best bid), never the ask or mid."""
    books = {"USDTTMN": make_book("USDTTMN", "100", "200")}
    assert asset_price_tmn("USDT", books) == Decimal("100")
    one_sided = {"USDTTMN": make_book("USDTTMN", None, "200")}
    assert asset_price_tmn("USDT", one_sided) is None


def test_quote_and_value_conversions(books: dict[str, BookSnapshot]) -> None:
    assert quote_to_tmn("TMN", Decimal("123.45"), books) == Decimal("123.45")
    assert quote_to_tmn("USDT", Decimal("10"), books) == Decimal("2667200")
    assert value_tmn("BTC", Decimal("0.0001"), books) == BTC_PART
    assert value_tmn("SOL", Decimal("1"), books) is None


def test_valuate_mixed_balances(books: dict[str, BookSnapshot], mixed: dict[str, Balance]) -> None:
    v = valuate(mixed, books)
    assert EQUITY == Decimal("5391966.7081")
    assert v.equity_tmn == EQUITY
    assert v.tmn == TMN_PART  # free + locked
    assert v.usdt_tmn_value == USDT_PART == Decimal("1333600")
    assert v.coins_tmn_value == BTC_PART + ETH_PART
    assert v.per_asset_tmn == {"BTC": BTC_PART, "ETH": ETH_PART, "TMN": TMN_PART, "USDT": USDT_PART}
    assert list(v.per_asset_tmn) == sorted(v.per_asset_tmn)  # deterministic order
    assert "XRP" not in v.per_asset_tmn  # zero balances are skipped, not "missing"
    assert v.missing_prices == ()
    assert v.complete and v.usable


def test_valuate_reports_missing_prices(books: dict[str, BookSnapshot], mixed: dict[str, Balance]) -> None:
    mixed["SOL"] = _bal("SOL", "0.5")
    mixed["DOGE"] = _bal("DOGE", "10")
    v = valuate(mixed, books)
    assert v.missing_prices == ("DOGE", "SOL")  # sorted
    assert not v.complete and not v.usable
    assert v.equity_tmn == EQUITY  # the priced part only
    assert "SOL" not in v.per_asset_tmn


def test_valuate_empty() -> None:
    v = valuate({}, {})
    assert v.equity_tmn == 0
    assert v.complete
    assert not v.usable  # zero equity never moves peak/day start


# ---------------------------------------------------------------- refresh and ratios


def _state(balances: dict[str, Balance], initial: str = "5000000") -> PortfolioState:
    ps = PortfolioState(initial_equity_tmn=Decimal(initial))
    ps.set_balances(balances)
    return ps


def _tmn_only(amount: str) -> dict[str, Balance]:
    return {"TMN": _bal("TMN", amount)}


def test_refresh_sets_peak_and_day_start(books: dict[str, BookSnapshot], mixed: dict[str, Balance]) -> None:
    ps = _state(mixed)
    assert ps.equity_tmn is None
    assert ps.drawdown_pct == 0 and ps.daily_pnl_pct == 0 and ps.total_return_pct == 0
    v = ps.refresh(books, T0)
    assert isinstance(v, Valuation)
    assert ps.last_valuation is v and ps.last_refresh_ts == T0
    assert ps.equity_tmn == EQUITY
    assert ps.peak_equity_tmn == EQUITY
    assert ps.day_start_ts == day_start(T0) == T0 - 10 * 3600  # T0 is 10:00 UTC
    assert ps.day_start_equity_tmn == EQUITY
    assert ps.drawdown_pct == 0
    assert ps.daily_pnl_pct == 0
    assert ps.total_return_pct == EQUITY / Decimal("5000000") - 1


def test_ratios_on_mixed_portfolio(books: dict[str, BookSnapshot], mixed: dict[str, Balance]) -> None:
    ps = _state(mixed)
    ps.refresh(books, T0)
    assert ps.coin_exposure_pct == (BTC_PART + ETH_PART) / EQUITY
    assert ps.usdt_share == USDT_PART / EQUITY
    assert ps.coin_exposure_pct == pytest.approx(Decimal("0.53012"), abs=Decimal("0.00001"))
    assert ps.usdt_share == pytest.approx(Decimal("0.24733"), abs=Decimal("0.00001"))
    assert ps.free("TMN") == Decimal("1000000")
    assert ps.total("TMN") == Decimal("1200000")
    assert ps.free("SOL") == 0 and ps.total("SOL") == 0


def test_ratios_are_zero_before_any_valuation() -> None:
    ps = _state({})
    assert ps.coin_exposure_pct == 0
    assert ps.usdt_share == 0


def test_drawdown_daily_and_total_return() -> None:
    ps = _state(_tmn_only("5000000"))
    ps.refresh({}, T0)
    ps.set_balances(_tmn_only("5500000"))  # +10 %: new peak
    ps.refresh({}, T0 + 60)
    assert ps.peak_equity_tmn == Decimal("5500000")
    ps.set_balances(_tmn_only("5225000"))  # -5 % from the peak
    ps.refresh({}, T0 + 120)
    assert ps.peak_equity_tmn == Decimal("5500000")  # peak never moves down
    assert ps.drawdown_pct == Decimal("0.05")  # (5.5M - 5.225M) / 5.5M
    assert ps.daily_pnl_pct == Decimal("0.045")  # 5.225M / 5M - 1 (day start = first refresh)
    assert ps.total_return_pct == Decimal("0.045")
    ps.set_balances(_tmn_only("6000000"))  # above the peak: drawdown back to 0, never negative
    ps.refresh({}, T0 + 180)
    assert ps.drawdown_pct == 0
    assert ps.peak_equity_tmn == Decimal("6000000")


def test_daily_loss_math() -> None:
    ps = _state(_tmn_only("4000000"), initial="5000000")
    ps.refresh({}, T0)
    ps.set_balances(_tmn_only("3900000"))
    ps.refresh({}, T0 + 60)
    assert ps.daily_pnl_pct == Decimal("-0.025")  # exactly the -2.5 % daily loss limit
    assert ps.total_return_pct == Decimal("-0.22")
    assert ps.drawdown_pct == Decimal("0.025")


def test_partial_valuation_does_not_move_peak_or_day_start(
    books: dict[str, BookSnapshot], mixed: dict[str, Balance]
) -> None:
    ps = _state(mixed)
    ps.refresh(books, T0)
    # a coin with no book at all appears, and the priced part rises above the old peak
    mixed2 = dict(mixed, SOL=_bal("SOL", "1"), TMN=_bal("TMN", "9000000"))
    ps.set_balances(mixed2)
    v = ps.refresh(books, T0 + 60)
    assert v.missing_prices == ("SOL",)
    assert ps.last_valuation is v
    assert ps.equity_tmn == v.equity_tmn > EQUITY  # the partial number is still visible
    assert ps.peak_equity_tmn == EQUITY  # ... but the peak did not move
    assert ps.day_start_equity_tmn == EQUITY
    # partial valuation across midnight: the day start waits for a complete valuation
    v = ps.refresh(books, MIDNIGHT + 5)
    assert not v.complete
    assert ps.day_start_ts == day_start(T0)
    assert ps.day_start_equity_tmn == EQUITY


def test_partial_valuation_never_fakes_a_drawdown(
    books: dict[str, BookSnapshot], mixed: dict[str, Balance]
) -> None:
    """Restart case: positions are held but their books have not arrived yet.

    The priced part is far below the peak; using it would read as a ~40 % drawdown and fire the
    kill-switch. Risk ratios use the last *usable* equity instead.
    """
    ps = _state(mixed)
    ps.refresh(books, T0)
    ps.set_balances(_tmn_only("4000000"))
    ps.refresh(books, T0 + 10)
    assert ps.drawdown_pct > 0  # a real loss on a complete valuation is reported
    real_dd = ps.drawdown_pct
    ps.set_balances(mixed)
    only_dollar = {"USDTTMN": books["USDTTMN"]}  # BTC and ETH have no price
    v = ps.refresh(only_dollar, T0 + 20)
    assert v.missing_prices == ("BTC", "ETH")
    assert ps.equity_tmn == TMN_PART + USDT_PART  # partial number, for display
    assert ps.drawdown_pct == real_dd  # unchanged: still the last usable equity
    assert ps.last_complete_equity_tmn == Decimal("4000000")


def test_restart_before_first_complete_valuation_reads_no_drawdown(books: dict[str, BookSnapshot]) -> None:
    ps = _state({"TMN": _bal("TMN", "1000000"), "BTC": _bal("BTC", "0.0001")})
    ps.load_state({"peak_equity_tmn": "5000000", "day_start_equity_tmn": "5000000", "day_start_ts": day_start(T0)})
    ps.refresh({}, T0)  # no BTC price yet: equity reads 1M, which is not a 80 % crash
    assert ps.drawdown_pct == 0
    assert ps.daily_pnl_pct == 0
    assert ps.peak_equity_tmn == Decimal("5000000")


def test_empty_balances_are_not_a_valuation() -> None:
    """An empty balance read (e.g. a failed parse) must not set peak/day start to 0 or read as -100 %."""
    ps = _state({})
    ps.refresh({}, T0)
    assert ps.peak_equity_tmn is None and ps.day_start_equity_tmn is None and ps.day_start_ts is None
    ps.set_balances(_tmn_only("5000000"))
    ps.refresh({}, T0 + 2)
    assert ps.peak_equity_tmn == Decimal("5000000")
    assert ps.day_start_equity_tmn == Decimal("5000000")
    ps.set_balances({})
    ps.refresh({}, T0 + 4)
    assert ps.drawdown_pct == 0
    assert ps.daily_pnl_pct == 0
    assert ps.peak_equity_tmn == Decimal("5000000")


# ---------------------------------------------------------------- day rollover


def test_day_start_resets_at_midnight_utc() -> None:
    ps = _state(_tmn_only("5000000"))
    ps.refresh({}, T0)
    ps.set_balances(_tmn_only("4900000"))
    ps.refresh({}, MIDNIGHT - 1)  # 23:59:59 — still the same UTC day
    assert ps.day_start_ts == day_start(T0)
    assert ps.day_start_equity_tmn == Decimal("5000000")
    assert ps.daily_pnl_pct == Decimal("-0.02")
    ps.set_balances(_tmn_only("4800000"))
    ps.refresh({}, MIDNIGHT)  # 00:00:00 — new day starts from the current equity
    assert ps.day_start_ts == MIDNIGHT
    assert ps.day_start_equity_tmn == Decimal("4800000")
    assert ps.daily_pnl_pct == 0
    assert ps.peak_equity_tmn == Decimal("5000000")  # the peak is not a daily number
    ps.set_balances(_tmn_only("4560000"))
    ps.refresh({}, MIDNIGHT + 3600)
    assert ps.daily_pnl_pct == Decimal("-0.05")
    assert ps.drawdown_pct == Decimal("0.088")


def test_day_start_does_not_go_backwards() -> None:
    """A refresh with an older timestamp (e.g. replayed tick) never rewinds the day start."""
    ps = _state(_tmn_only("5000000"))
    ps.refresh({}, MIDNIGHT + 10)
    ps.set_balances(_tmn_only("4000000"))
    ps.refresh({}, T0)
    assert ps.day_start_ts == MIDNIGHT
    assert ps.day_start_equity_tmn == Decimal("5000000")


# ---------------------------------------------------------------- placements (rate caps)


def test_orders_last_hour_and_today() -> None:
    ps = _state({})
    for ts in (T0 - 3 * 3600, T0 - 3600, T0 - 3599, T0 - 60, T0):
        ps.record_placement(ts)
    assert ps.orders_last_hour(T0) == 3  # the window is (now - 3600, now]
    assert ps.orders_today(T0) == 5  # all since 00:00 UTC
    assert ps.orders_last_hour(T0 + 3600) == 0
    assert ps.orders_today(MIDNIGHT) == 0  # new UTC day
    ps.record_placement(MIDNIGHT)
    assert ps.orders_today(MIDNIGHT) == 1
    assert ps.orders_last_hour(MIDNIGHT) == 1


def test_orders_ignore_future_placements() -> None:
    ps = _state({})
    ps.record_placement(T0 + 10)
    assert ps.orders_last_hour(T0) == 0
    assert ps.orders_today(T0) == 0


def test_placements_are_pruned_after_two_days() -> None:
    ps = _state({})
    ps.record_placement(T0)
    ps.record_placement(T0 + 86400)
    assert ps.placements == [T0, T0 + 86400]
    ps.record_placement(T0 + 2 * 86400)  # cutoff = ts - 2 days: T0 is exactly on it and stays
    assert ps.placements == [T0, T0 + 86400, T0 + 2 * 86400]
    ps.record_placement(T0 + 2 * 86400 + 1)
    assert ps.placements == [T0 + 86400, T0 + 2 * 86400, T0 + 2 * 86400 + 1]


# ---------------------------------------------------------------- fees, realized PnL, balances


def test_fees_and_realized_counters() -> None:
    ps = _state({})
    ps.add_fee_tmn(Decimal("1500"))
    ps.add_fee_tmn(Decimal("250.5"))
    assert ps.fees_paid_tmn_total == Decimal("1750.5")
    ps.add_realized("S1", Decimal("1000"))
    ps.add_realized("S2", Decimal("-300"))
    ps.add_realized("S1", Decimal("200"))
    assert ps.realized_pnl_tmn == {"S1": Decimal("1200"), "S2": Decimal("-300")}
    assert ps.realized_total_tmn == Decimal("900")
    assert _state({}).realized_total_tmn == 0


def test_apply_fill_optimistic(markets: dict[str, MarketInfo]) -> None:
    ps = _state({"USDT": _bal("USDT", "10"), "TMN": _bal("TMN", "100")})
    buy = Fill(
        order_client_id="S3_BTCUSDT_1792663200000_AAAA", symbol="BTCUSDT", side="BUY",
        price=Decimal("80000"), qty=Decimal("0.0001"), fee=Decimal("0.00000013"), fee_asset="BTC", ts=T0,
    )
    ps.apply_fill_optimistic(buy, markets["BTCUSDT"])
    assert ps.free("BTC") == Decimal("0.00009987")  # qty minus the fee taken in BTC
    assert ps.free("USDT") == Decimal("2")  # 10 - 8
    sell = Fill(
        order_client_id="S3_BTCUSDT_1792663260000_BBBB", symbol="BTCUSDT", side="SELL",
        price=Decimal("81000"), qty=Decimal("0.00009987"), fee=Decimal("0.01"), fee_asset="USDT", ts=T0 + 60,
    )
    ps.apply_fill_optimistic(sell, markets["BTCUSDT"])
    assert ps.free("BTC") == 0
    assert ps.free("USDT") == Decimal("2") + Decimal("0.00009987") * Decimal("81000") - Decimal("0.01")
    assert list(ps.balances) == sorted(ps.balances)


def test_set_balances_sorts_assets() -> None:
    ps = _state({"USDT": _bal("USDT", "1"), "BTC": _bal("BTC", "1"), "TMN": _bal("TMN", "1")})
    assert list(ps.balances) == ["BTC", "TMN", "USDT"]


# ---------------------------------------------------------------- persistence (restart)


def _roundtrip(ps: PortfolioState) -> dict:
    return json.loads(json.dumps(ps.to_state()))  # exactly what the store's kv table holds


def test_state_roundtrip_keeps_counters(books: dict[str, BookSnapshot], mixed: dict[str, Balance]) -> None:
    ps = _state(mixed)
    ps.refresh(books, T0)
    ps.add_fee_tmn(Decimal("1234.5678"))
    ps.add_realized("S2", Decimal("-10.5"))
    ps.add_realized("S1", Decimal("99.25"))
    for ts in (T0 - 120, T0 - 60, T0):
        ps.record_placement(ts)

    restored = _state(mixed)
    restored.load_state(_roundtrip(ps))
    assert restored.peak_equity_tmn == ps.peak_equity_tmn == EQUITY
    assert restored.day_start_equity_tmn == EQUITY
    assert restored.day_start_ts == day_start(T0)
    assert restored.fees_paid_tmn_total == Decimal("1234.5678")
    assert restored.placements == [T0 - 120, T0 - 60, T0]
    assert restored.realized_pnl_tmn == {"S1": Decimal("99.25"), "S2": Decimal("-10.5")}
    assert restored.orders_last_hour(T0) == 3
    assert restored.to_state() == ps.to_state()
    assert list(ps.to_state()["realized_pnl_tmn"]) == ["S1", "S2"]


def test_restart_keeps_peak_and_day_start_on_the_same_day(mixed: dict[str, Balance]) -> None:
    ps = _state(_tmn_only("5500000"))
    ps.refresh({}, T0)
    ps.set_balances(_tmn_only("5000000"))
    ps.refresh({}, T0 + 60)
    state = _roundtrip(ps)

    restarted = _state(_tmn_only("5000000"))  # a new process: fresh object, same exchange balances
    restarted.load_state(state)
    restarted.refresh({}, T0 + 600)
    assert restarted.peak_equity_tmn == Decimal("5500000")  # not reset to the current equity
    assert restarted.day_start_equity_tmn == Decimal("5500000")
    assert restarted.drawdown_pct == Decimal("500000") / Decimal("5500000")
    assert restarted.daily_pnl_pct == Decimal("5000000") / Decimal("5500000") - 1


def test_restart_on_a_new_day_resets_only_the_day_start() -> None:
    ps = _state(_tmn_only("5500000"))
    ps.refresh({}, T0)
    restarted = _state(_tmn_only("5000000"))
    restarted.load_state(_roundtrip(ps))
    restarted.refresh({}, MIDNIGHT + 60)
    assert restarted.peak_equity_tmn == Decimal("5500000")
    assert restarted.day_start_ts == MIDNIGHT
    assert restarted.day_start_equity_tmn == Decimal("5000000")


def test_load_state_tolerates_empty_and_null_fields() -> None:
    ps = _state({})
    ps.load_state(None)
    ps.load_state({})
    assert ps.peak_equity_tmn is None and ps.fees_paid_tmn_total == 0 and ps.placements == []
    fresh = _state({})
    fresh.load_state(_roundtrip(_state({})))
    assert fresh.peak_equity_tmn is None
    assert fresh.day_start_equity_tmn is None
    assert fresh.realized_pnl_tmn == {}


def test_load_state_sorts_placements() -> None:
    ps = _state({})
    ps.load_state({"placements": [T0, T0 - 10, "1792663100.5"]})
    assert ps.placements == [1792663100.5, T0 - 10, T0]

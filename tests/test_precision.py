"""Exchange precision rules (Overview §8): BUY prices round down, SELL prices round up to the tick;
quantities always round down to the step; min-notional is checked after rounding."""

from __future__ import annotations

from decimal import Decimal

import pytest

from wta.models import MarketInfo
from wta.utils.decimalutil import (
    D,
    check_min_notional,
    fmt,
    notional,
    pct,
    quantize_down,
    quantize_up,
    round_price,
    round_price_nearest,
    round_qty,
    tick,
)

# ---------------------------------------------------------------- prices


@pytest.mark.parametrize(
    "symbol,price,buy,sell",
    [
        ("BTCTMN", "22075699081.6", "22075699081", "22075699082"),
        ("BTCTMN", "22075699081.01", "22075699081", "22075699082"),
        ("USDTTMN", "266720.5", "266720", "266721"),
        ("BTCUSDT", "82845.009", "82845.00", "82845.01"),
        ("BTCUSDT", "82845.001", "82845.00", "82845.01"),
        ("BTCUSDT", "82909.1666", "82909.16", "82909.17"),
    ],
)
def test_round_price_buy_down_sell_up(
    markets: dict[str, MarketInfo], symbol: str, price: str, buy: str, sell: str
) -> None:
    market = markets[symbol]
    assert str(round_price(market, Decimal(price), "BUY")) == buy
    assert str(round_price(market, Decimal(price), "SELL")) == sell


@pytest.mark.parametrize("symbol,price", [("BTCTMN", "22075699081"), ("BTCUSDT", "82845.00")])
def test_round_price_on_tick_is_unchanged(markets: dict[str, MarketInfo], symbol: str, price: str) -> None:
    market = markets[symbol]
    for side in ("BUY", "SELL"):
        assert str(round_price(market, Decimal(price), side)) == price


def test_round_price_pads_to_tick_decimals(markets: dict[str, MarketInfo]) -> None:
    assert str(round_price(markets["BTCUSDT"], Decimal("82845"), "BUY")) == "82845.00"
    assert str(round_price(markets["BTCTMN"], Decimal("22075699081.0"), "SELL")) == "22075699081"


def test_round_price_nearest(markets: dict[str, MarketInfo]) -> None:
    usdt = markets["BTCUSDT"]
    assert round_price_nearest(usdt, Decimal("82845.006")) == Decimal("82845.01")
    assert round_price_nearest(usdt, Decimal("82845.004")) == Decimal("82845.00")
    assert round_price_nearest(markets["BTCTMN"], Decimal("22075699081.7")) == Decimal("22075699082")


def test_tick(markets: dict[str, MarketInfo]) -> None:
    assert tick(markets["BTCTMN"]) == Decimal("1")
    assert tick(markets["BTCUSDT"]) == Decimal("0.01")


def test_quantize_helpers() -> None:
    assert quantize_down(Decimal("1.239"), 2) == Decimal("1.23")
    assert quantize_up(Decimal("1.231"), 2) == Decimal("1.24")
    assert quantize_up(Decimal("1.230"), 2) == Decimal("1.23")


# ---------------------------------------------------------------- quantities


@pytest.mark.parametrize(
    "symbol,qty,expected",
    [
        ("BTCUSDT", "0.0000129", "0.000012"),
        ("BTCUSDT", "0.1234569", "0.123456"),
        ("BTCUSDT", "0.9999999", "0.999999"),  # never rounds up, even when very close
        ("BTCUSDT", "0.000012", "0.000012"),  # on step: unchanged
        ("USDTTMN", "18.759", "18.75"),
        ("SOLTMN", "1.999", "1.99"),
    ],
)
def test_round_qty_always_down(markets: dict[str, MarketInfo], symbol: str, qty: str, expected: str) -> None:
    assert str(round_qty(markets[symbol], Decimal(qty))) == expected


def test_round_qty_below_one_step_is_zero(markets: dict[str, MarketInfo]) -> None:
    assert round_qty(markets["BTCUSDT"], Decimal("0.0000009")) == 0
    assert round_qty(markets["USDTTMN"], Decimal("0.009")) == 0


# ---------------------------------------------------------------- min notional


@pytest.mark.parametrize(
    "symbol,qty,price,ok",
    [
        # TMN pairs: minNotional 50,000 TMN
        ("USDTTMN", "0.19", "266720", True),  # 50,676.8
        ("USDTTMN", "0.18", "266720", False),  # 48,009.6
        ("BTCTMN", "0.0025", "20000000", True),  # exactly 50,000 -> allowed
        ("BTCTMN", "0.002499", "20000000", False),  # 49,980
        ("BTCTMN", "0.0025009", "19995000", False),  # raw 50,005.5 but rounded qty 0.002500 -> 49,987.5
        # USDT pairs: minNotional 1 USDT
        ("BTCUSDT", "0.000013", "82845.00", True),  # 1.077
        ("BTCUSDT", "0.000012", "82845.00", False),  # 0.994
        ("BTCUSDT", "0.00001", "100000.00", True),  # exactly 1 -> allowed
        ("BTCUSDT", "0.00001", "99999.99", False),  # 0.9999999
        ("BTCUSDT", "0.0000129", "82845.00", False),  # rounds to 0.000012 -> 0.994
    ],
)
def test_check_min_notional(
    markets: dict[str, MarketInfo], symbol: str, qty: str, price: str, ok: bool
) -> None:
    assert check_min_notional(markets[symbol], Decimal(qty), Decimal(price)) is ok


def test_check_min_notional_rejects_zero_and_below_min_qty(markets: dict[str, MarketInfo]) -> None:
    market = markets["BTCUSDT"].model_copy(update={"min_qty": Decimal("0.001")})
    assert check_min_notional(market, Decimal("0.001"), Decimal("82845.00")) is True
    # 0.000999 BTC is worth ~82 USDT (far above minNotional) but is below minQty
    assert check_min_notional(market, Decimal("0.000999"), Decimal("82845.00")) is False
    assert check_min_notional(markets["BTCUSDT"], Decimal("0"), Decimal("82845.00")) is False


def test_notional() -> None:
    assert notional(Decimal("0.19"), Decimal("266720")) == Decimal("50676.80")


# ---------------------------------------------------------------- D() and formatting


@pytest.mark.parametrize(
    "value,expected",
    [
        (0.1, "0.1"),
        (1.1 + 2.2, "3.3000000000000003"),  # the float's shortest repr, not its binary expansion
        (82845.0, "82845.0"),
        (1e-8, "1E-8"),
        (5, "5"),
        ("0.00000001", "0.00000001"),
        ("82845.00", "82845.00"),
    ],
)
def test_D_avoids_binary_artefacts(value: object, expected: str) -> None:
    result = D(value)
    assert isinstance(result, Decimal)
    assert result == Decimal(expected)
    assert result.as_tuple() == Decimal(expected).as_tuple()


def test_D_float_differs_from_raw_decimal_constructor() -> None:
    assert D(0.1) != Decimal(0.1)  # Decimal(0.1) == 0.1000000000000000055511151231257827...


def test_D_returns_decimals_unchanged() -> None:
    value = Decimal("82845.00")
    assert D(value) is value


@pytest.mark.parametrize(
    "value,decimals,expected",
    [
        (Decimal("22075699081"), None, "22,075,699,081"),
        (Decimal("82845.7"), None, "82,846"),  # >= 1000 -> no decimals
        (Decimal("0.123456"), None, "0.1235"),  # < 1000 -> 4 decimals
        (Decimal("-1500"), None, "-1,500"),
        (Decimal("82845.5"), 2, "82,845.50"),
        (None, None, "n/a"),
    ],
)
def test_fmt(value: Decimal | None, decimals: int | None, expected: str) -> None:
    assert fmt(value, decimals) == expected


@pytest.mark.parametrize(
    "value,decimals,expected",
    [
        (Decimal("0.001375"), 2, "0.14%"),
        (Decimal("0.001375"), 3, "0.138%"),
        (Decimal("0.025"), 2, "2.50%"),
        (Decimal("-0.025"), 2, "-2.50%"),
        (0.05, 2, "5.00%"),
        (None, 2, "n/a"),
    ],
)
def test_pct(value: Decimal | float | None, decimals: int, expected: str) -> None:
    assert pct(value, decimals) == expected

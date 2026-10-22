"""Shared pytest fixtures for the whole test-suite.

Only cross-module fixtures live here: a small set of markets, an order-book factory, a simulated
clock and the loaded config. Module-specific fixtures belong in the test module that uses them.
Nothing here touches the network, the wall clock or the developer's ``.env``.

The market precision/fee values below are realistic test values (Overview §2), not exchange truth.
"""

from __future__ import annotations

from collections.abc import Callable
from decimal import Decimal

import pytest

from wta.config import Config, load_config
from wta.models import BookSnapshot, BookSource, Level, MarketInfo
from wta.utils.decimalutil import D
from wta.utils.timeutil import SimClock

# 2026-10-22T10:00:00Z — the simulated "now" used across the suite.
T0: float = 1_792_663_200.0

BookFactory = Callable[..., BookSnapshot]

TMN_FEES = {"maker_fee": Decimal("0.0025"), "taker_fee": Decimal("0.003")}
USDT_FEES = {"maker_fee": Decimal("0.001"), "taker_fee": Decimal("0.0013")}


def _market(symbol: str, base: str, quote: str, tick: int, step: int, **extra: object) -> MarketInfo:
    """MarketInfo with min notional and fees chosen by quote asset (TMN 50,000 / USDT 1)."""
    if quote == "TMN":
        defaults: dict[str, object] = {"min_notional": Decimal("50000"), **TMN_FEES}
    else:
        defaults = {"min_notional": Decimal("1"), **USDT_FEES}
    return MarketInfo(
        symbol=symbol,
        base=base,
        quote=quote,
        tick_decimals=tick,
        step_decimals=step,
        **{**defaults, **extra},
    )


@pytest.fixture
def markets() -> dict[str, MarketInfo]:
    """Six markets covering both quote currencies and a market-orders-disabled book."""
    items = [
        _market("USDTTMN", "USDT", "TMN", tick=0, step=2),
        _market("BTCTMN", "BTC", "TMN", tick=0, step=6),
        _market("BTCUSDT", "BTC", "USDT", tick=2, step=6),
        _market("ETHTMN", "ETH", "TMN", tick=0, step=4),
        _market("ETHUSDT", "ETH", "USDT", tick=2, step=4),
        _market("SOLTMN", "SOL", "TMN", tick=0, step=2, market_orders_enabled=False),
    ]
    return {m.symbol: m for m in items}


def _make_book(
    symbol: str,
    bid: object | None,
    ask: object | None,
    qty: object = "1",
    ts: float = T0,
    levels: int = 1,
    level_step: object = "1",
    source: BookSource = "rest",
) -> BookSnapshot:
    """Book with ``levels`` levels per side, each holding ``qty``.

    Bids step down from ``bid`` and asks step up from ``ask`` by ``level_step`` price units.
    Pass ``bid=None`` or ``ask=None`` for a one-sided book. Numbers go through ``D`` (no floats).
    """
    q, step = D(qty), D(level_step)
    bids = [] if bid is None else [Level(price=D(bid) - i * step, qty=q) for i in range(levels)]
    asks = [] if ask is None else [Level(price=D(ask) + i * step, qty=q) for i in range(levels)]
    return BookSnapshot(symbol=symbol, ts=ts, source=source, bids=bids, asks=asks)


@pytest.fixture
def make_book() -> BookFactory:
    """Factory: ``make_book("BTCUSDT", "82845.00", "82909.16", qty="0.5", ts=T0, levels=3)``."""
    return _make_book


@pytest.fixture
def t0() -> float:
    """The suite's simulated start time (2026-10-22T10:00:00Z) as unix seconds."""
    return T0


@pytest.fixture
def sim_clock() -> SimClock:
    """A fresh simulated clock starting at 2026-10-22T10:00:00Z."""
    return SimClock(T0)


@pytest.fixture
def cfg() -> Config:
    """The repo's ``configs/*.yaml`` with an empty environment (no ``.env`` leakage)."""
    return load_config(env={})

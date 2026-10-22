"""Data models: JSON round-trip with exact Decimals, frozen vs mutable models, and the
derived properties used by strategies and risk (book prices, failed checks, order state)."""

from __future__ import annotations

import inspect
from collections.abc import Callable
from decimal import Decimal

import pytest
from pydantic import BaseModel, ValidationError

import wta.models as models
from wta.models import (
    Balance,
    BookSnapshot,
    Candle,
    DecisionRecord,
    EventRecord,
    Fill,
    HealthRecord,
    Intent,
    Level,
    MarketInfo,
    MarketPlan,
    Order,
    OrderLogRecord,
    OrderRequest,
    OutcomeUpdate,
    Position,
    RegimeCall,
    RiskCheck,
    RiskDecision,
    Trade,
)

BookFactory = Callable[..., BookSnapshot]  # the conftest ``make_book`` fixture

TS = 1_792_663_200.0  # 2026-10-22T10:00:00Z
ISO = "2026-10-22T10:00:00.000Z"
TINY = Decimal("0.00000001")
BIG = Decimal("22075699081")


def _req(qty: Decimal = Decimal("0.5")) -> OrderRequest:
    return OrderRequest(
        client_id="S1_BTCUSDT_1792663200000_A1B2",
        symbol="BTCUSDT",
        side="BUY",
        type="LIMIT",
        qty=qty,
        price=Decimal("82845.00"),
        strategy="S1",
        purpose="entry",
        decision_id="d_1792663200000_S1_BTCUSDT_1",
    )


def _order(qty: Decimal = Decimal("0.5"), filled: Decimal = Decimal("0"), status: str = "NEW") -> Order:
    return Order(req=_req(qty), status=status, filled_qty=filled, created_ts=TS, updated_ts=TS)


def _samples() -> list[BaseModel]:
    """One populated instance of every model, using the tiniest and largest Decimals we meet."""
    level = Level(price=BIG, qty=TINY)
    check = RiskCheck(code="max_order_pct_equity", passed=True, detail="1.2% <= 25%")
    return [
        MarketInfo(symbol="BTCTMN", base="BTC", quote="TMN", tick_decimals=0, step_decimals=8, min_qty=TINY),
        level,
        BookSnapshot(
            symbol="BTCTMN", ts=TS, source="ws", bids=[level], asks=[Level(price=BIG + 1, qty=TINY)]
        ),
        Trade(symbol="BTCTMN", ts=TS, price=BIG, qty=TINY, side="SELL"),
        Candle(symbol="BTCTMN", resolution="15", ts=int(TS), o=BIG, h=BIG + 5, l=BIG - 5, c=BIG, v=TINY),
        Balance(asset="BTC", free=TINY, locked=Decimal("0.5")),
        OrderRequest(
            client_id="S3_BTCUSDT_1792663200000_FFFF",
            symbol="BTCUSDT",
            side="SELL",
            type="STOP_LIMIT",
            qty=TINY,
            price=Decimal("82000.00"),
            stop_price=Decimal("82100.00"),
            strategy="S3",
            purpose="stop",
            decision_id="d_1",
        ),
        Order(
            req=_req(),
            exchange_id="123",
            status="PARTIALLY_FILLED",
            filled_qty=Decimal("0.1"),
            avg_price=Decimal("82845.00"),
            fee_paid=TINY,
            fee_asset="BTC",
            created_ts=TS,
            updated_ts=TS + 1,
            raw={"status": "NEW", "executedQty": "0.1"},
        ),
        Fill(order_client_id="S1_X", symbol="BTCTMN", side="BUY", price=BIG, qty=TINY, fee=TINY, ts=TS),
        Position(
            id="p_S1_BTCTMN_1792663200000_1",
            strategy="S1",
            symbol="BTCTMN",
            base="BTC",
            quote="TMN",
            qty=TINY,
            entry_price=BIG,
            entry_ts=TS,
            stop_price=BIG - 100,
            meta={"mode": "revert"},
        ),
        Intent(
            id="d_1",
            ts=TS,
            strategy="S1",
            symbol="BTCTMN",
            side="BUY",
            order_type="LIMIT",
            qty=TINY,
            notional=BIG,
            price=BIG,
            purpose="entry",
            reason="TMN book cheaper by 0.31% > cost 0.26% + margin",
            features={"dev_pct": "0.0031"},
            expected_edge_pct=Decimal("0.0005"),
        ),
        check,
        RiskDecision(approved=True, checks=[check], adjusted_qty=TINY),
        RegimeCall(regime="trend_up", confidence=0.7, evidence="higher highs"),
        MarketPlan(
            ts=TS,
            regimes={"BTCUSDT": RegimeCall(regime="range", confidence=0.5)},
            enabled_strategies=["S1", "S2"],
            s1_margin_min_pct=Decimal("0.0015"),
            clamped={"size_multiplier": "3"},
        ),
        DecisionRecord(
            kind="intent",
            decision_id="d_1",
            ts=ISO,
            mode="shadow",
            git_commit="abc123",
            config_hash="5751839f7e2d",
            strategy="S1",
            symbol="BTCTMN",
            intent={"qty": "0.00000001"},
        ),
        OutcomeUpdate(decision_id="d_1", ts=ISO, outcome={"pnl_tmn": "1250", "exit_reason": "target"}),
        HealthRecord(
            ts=ISO,
            mode="shadow",
            git_commit="abc123",
            config_hash="5751839f7e2d",
            equity_tmn=BIG,
            drawdown_pct=TINY,
            reasons=["stale_data"],
        ),
        EventRecord(ts=ISO, kind="start", data={"argv": ["run"]}),
        OrderLogRecord(ts=ISO, action="place", client_id="S1_X", symbol="BTCTMN", data={"price": "1"}),
    ]


SAMPLES = _samples()
SAMPLE_IDS = [type(m).__name__ for m in SAMPLES]
MUTABLE_MODELS = (Order, Position)


def _model_classes() -> set[type[BaseModel]]:
    return {
        cls
        for _, cls in inspect.getmembers(models, inspect.isclass)
        if issubclass(cls, BaseModel)
        and cls.__module__ == models.__name__
        and cls not in (models.Frozen, models.Mutable)
    }


# ---------------------------------------------------------------- serialisation


def test_every_model_has_a_round_trip_sample() -> None:
    assert _model_classes() == {type(m) for m in SAMPLES}


@pytest.mark.parametrize("model", SAMPLES, ids=SAMPLE_IDS)
def test_json_round_trip(model: BaseModel) -> None:
    dumped = model.model_dump(mode="json")
    assert type(model).model_validate(dumped) == model
    assert type(model).model_validate_json(model.model_dump_json()) == model


@pytest.mark.parametrize("value", [TINY, BIG, Decimal("82845.00"), Decimal("0.0013")])
def test_decimal_is_preserved_exactly(value: Decimal) -> None:
    level = Level(price=value, qty=value)
    dumped = level.model_dump(mode="json")
    assert isinstance(dumped["price"], str)  # never a float in logs
    back = Level.model_validate(dumped)
    assert back.price == value
    assert back.price.as_tuple() == value.as_tuple()  # same digits and exponent, e.g. "82845.00"


def test_round_trip_keeps_nested_decimals() -> None:
    order = next(m for m in SAMPLES if isinstance(m, Order))
    back = Order.model_validate(order.model_dump(mode="json"))
    assert isinstance(back.req.price, Decimal)
    assert back.req.price.as_tuple() == Decimal("82845.00").as_tuple()
    assert back.fee_paid.as_tuple() == TINY.as_tuple()


# ---------------------------------------------------------------- frozen / mutable


@pytest.mark.parametrize(
    "model", [m for m in SAMPLES if not isinstance(m, MUTABLE_MODELS)], ids=lambda m: type(m).__name__
)
def test_frozen_models_reject_mutation(model: BaseModel) -> None:
    field = next(iter(type(model).model_fields))
    with pytest.raises(ValidationError):
        setattr(model, field, getattr(model, field))


@pytest.mark.parametrize("model", SAMPLES, ids=SAMPLE_IDS)
def test_unknown_fields_are_rejected(model: BaseModel) -> None:
    with pytest.raises(ValidationError):
        type(model).model_validate({**model.model_dump(), "unexpected_field": 1})


def test_order_is_mutable_and_validated_on_assignment() -> None:
    order = _order()
    order.status = "FILLED"
    order.filled_qty = "0.5"  # type: ignore[assignment]
    assert order.status == "FILLED"
    assert order.filled_qty == Decimal("0.5")
    assert isinstance(order.filled_qty, Decimal)
    with pytest.raises(ValidationError):
        order.status = "DONE"  # type: ignore[assignment]
    with pytest.raises(ValidationError):
        order.req.qty = Decimal("1")  # the request inside an order stays frozen


def test_position_is_mutable_and_validated_on_assignment() -> None:
    pos = next(m for m in SAMPLES if isinstance(m, Position)).model_copy(deep=True)
    pos.stop_price = Decimal("22000000000")
    pos.meta["note"] = "moved stop"
    assert pos.stop_price == Decimal("22000000000")
    with pytest.raises(ValidationError):
        pos.qty = "lots"  # type: ignore[assignment]


# ---------------------------------------------------------------- market data properties


def test_market_tick_and_step() -> None:
    tmn = MarketInfo(symbol="BTCTMN", base="BTC", quote="TMN", tick_decimals=0, step_decimals=6)
    usdt = MarketInfo(symbol="BTCUSDT", base="BTC", quote="USDT", tick_decimals=2, step_decimals=6)
    assert tmn.tick == Decimal("1")
    assert usdt.tick == Decimal("0.01")
    assert usdt.step == Decimal("0.000001")


# Overview §2.1 snapshot (2026-10-22): bid, ask, spread as printed in the table.
OVERVIEW_BOOKS = [
    ("USDTTMN", "266720", "266758", "0.014"),
    ("BTCTMN", "22075699081", "22106074058", "0.138"),
    ("BTCUSDT", "82845.00", "82909.16", "0.077"),
    ("ETHTMN", "650660181", "650859142", "0.031"),
    ("SOLTMN", "28000019", "28105486", "0.376"),
]


@pytest.mark.parametrize("symbol,bid,ask,spread_pct", OVERVIEW_BOOKS)
def test_book_spread_matches_overview(
    make_book: BookFactory, symbol: str, bid: str, ask: str, spread_pct: str
) -> None:
    book = make_book(symbol, bid, ask)
    assert book.spread_pct is not None
    assert abs(book.spread_pct * 100 - Decimal(spread_pct)) < Decimal("0.001")


def test_book_properties_btctmn(make_book: BookFactory) -> None:
    book = make_book("BTCTMN", "22075699081", "22106074058", qty="0.25", levels=3)
    assert book.best_bid == Decimal("22075699081")
    assert book.best_ask == Decimal("22106074058")
    assert book.best_bid_qty == Decimal("0.25")
    assert book.best_ask_qty == Decimal("0.25")
    assert book.mid == Decimal("22090886569.5")
    assert book.spread_pct == (book.best_ask - book.best_bid) / book.mid
    assert Decimal("0.001375") < book.spread_pct < Decimal("0.001376")  # ≈ 0.138%
    assert [lv.price for lv in book.bids] == sorted((lv.price for lv in book.bids), reverse=True)
    assert [lv.price for lv in book.asks] == sorted(lv.price for lv in book.asks)


def test_book_properties_without_both_sides(make_book: BookFactory) -> None:
    one_sided = make_book("BTCTMN", "22075699081", None)
    assert one_sided.best_bid == Decimal("22075699081")
    assert one_sided.best_ask is None
    assert one_sided.best_ask_qty is None
    assert one_sided.mid is None
    assert one_sided.spread_pct is None
    zero = make_book("BTCTMN", "0", "0")
    assert zero.mid == 0
    assert zero.spread_pct is None


def test_book_age(make_book: BookFactory) -> None:
    book = make_book("BTCUSDT", "82845.00", "82909.16", ts=TS)
    assert book.age(TS + 12.5) == 12.5


def test_same_levels(make_book: BookFactory) -> None:
    book = make_book("BTCUSDT", "82845.00", "82909.16", qty="0.5", levels=2, ts=TS, source="rest")
    later_ws = make_book("BTCUSDT", "82845.00", "82909.16", qty="0.5", levels=2, ts=TS + 5, source="ws")
    other_qty = make_book("BTCUSDT", "82845.00", "82909.16", qty="0.4", levels=2, ts=TS)
    assert book.same_levels(later_ws)  # ts/source are not part of the comparison
    assert not book.same_levels(other_qty)
    assert not book.same_levels(None)


# ---------------------------------------------------------------- decisions and orders


def test_risk_decision_failed_codes_keep_order() -> None:
    decision = RiskDecision(
        approved=False,
        checks=[
            RiskCheck(code="max_open_orders", passed=True, detail="2 < 8"),
            RiskCheck(code="daily_loss_limit", passed=False, detail="-2.6% < -2.5%"),
            RiskCheck(code="stale_data", passed=False, detail="age 31s > 30s"),
        ],
        veto_reason="daily_loss_limit",
    )
    assert decision.failed_codes == ["daily_loss_limit", "stale_data"]
    assert RiskDecision(approved=True).failed_codes == []


def test_order_remaining_qty() -> None:
    assert _order(Decimal("1"), Decimal("0.4")).remaining_qty == Decimal("0.6")
    assert _order(Decimal("1"), Decimal("0")).remaining_qty == Decimal("1")
    assert _order(Decimal("1"), Decimal("1.2")).remaining_qty == Decimal("0")  # never negative


@pytest.mark.parametrize(
    "status,terminal",
    [
        ("NEW", False),
        ("PARTIALLY_FILLED", False),
        ("UNKNOWN", False),
        ("FILLED", True),
        ("CANCELED", True),
        ("REJECTED", True),
        ("EXPIRED", True),
    ],
)
def test_order_is_terminal(status: str, terminal: bool) -> None:
    order = _order(status=status)
    assert order.is_terminal is terminal
    assert order.client_id == order.req.client_id


def test_balance_total() -> None:
    assert Balance(asset="USDT", free=Decimal("10.5"), locked=Decimal("2.25")).total == Decimal("12.75")

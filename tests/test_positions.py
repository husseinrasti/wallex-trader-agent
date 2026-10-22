"""PositionManager (PRD-02 §2, §4.4): fills -> positions -> realized PnL in TMN -> exit intents.

What is pinned down here:

* A position opens on the first ``entry`` fill; further fills of the same entry decision add to
  it (quantity-weighted average price). Values in/out and fees are kept in TMN, converted at the
  USDTTMN bid *at fill time* for USDT-quoted books.
* Closing fills (exit/stop/leg2/flatten) reduce it, possibly in the other book of the same coin
  (S1 lock-in: buy BTCTMN, sell BTCUSDT -> one position, ``exit_reason="lockin"``). It closes when
  the remainder is at or below the market's ``min_qty`` (dust).
* ``pnl = value out - value in - fees``; a fee taken in the base coin shrinks the quantity instead
  and is not subtracted twice.
* ``check_exits`` emits deterministic exit intents: software stop (only when software stops are on
  or the position has no live server stop), target, time stop; ``exit_pending`` positions skipped.

Markets come from ``configs/markets_snapshot.yaml`` (realistic ``min_qty``/steps).
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest

from wta.exchange.markets import load_markets_snapshot
from wta.execution.positions import ClosedPosition, PortfolioState, PositionManager
from wta.models import BookSnapshot, Fill, Intent, MarketInfo, Order, OrderRequest, Position, Purpose, Side
from wta.utils.ids import make_client_id, make_decision_id, make_position_id

from .conftest import T0, BookFactory

U_BID = Decimal("266720")
BTC_TMN_BID = Decimal("22000000000")


@pytest.fixture
def snap_markets() -> dict[str, MarketInfo]:
    return load_markets_snapshot()


@pytest.fixture
def books(make_book: BookFactory) -> dict[str, BookSnapshot]:
    return {
        "USDTTMN": make_book("USDTTMN", "266720", "266758"),
        "BTCTMN": make_book("BTCTMN", "22000000000", "22030000000"),
        "BTCUSDT": make_book("BTCUSDT", "83000.00", "83050.00"),
        "DOGETMN": make_book("DOGETMN", "48000", "48100"),
    }


@pytest.fixture
def portfolio() -> PortfolioState:
    return PortfolioState(initial_equity_tmn=Decimal("5000000"))


@pytest.fixture
def pm(portfolio: PortfolioState, snap_markets: dict[str, MarketInfo]) -> PositionManager:
    return PositionManager(portfolio, snap_markets)


# ---------------------------------------------------------------- builders


def _req(strategy: str, symbol: str, side: Side, purpose: Purpose, decision_id: str, attempt: int = 0) -> OrderRequest:
    return OrderRequest(
        client_id=make_client_id(strategy, symbol, T0, decision_id, attempt),
        symbol=symbol,
        side=side,
        type="LIMIT",
        qty=Decimal("1"),  # the request size is irrelevant to bookkeeping; fills carry the qty
        price=Decimal("1"),
        strategy=strategy,
        purpose=purpose,
        decision_id=decision_id,
    )


def _fill(req: OrderRequest, price: str, qty: str, ts: float, fee: str = "0", fee_asset: str = "") -> Fill:
    return Fill(
        order_client_id=req.client_id,
        symbol=req.symbol,
        side=req.side,
        price=Decimal(price),
        qty=Decimal(qty),
        fee=Decimal(fee),
        fee_asset=fee_asset,
        ts=ts,
    )


def _intent(
    req: OrderRequest,
    *,
    linked: str | None = None,
    stop: str | None = None,
    features: dict[str, Any] | None = None,
) -> Intent:
    return Intent(
        id=req.decision_id,
        ts=T0,
        strategy=req.strategy,
        symbol=req.symbol,
        side=req.side,
        order_type="LIMIT",
        purpose=req.purpose,
        reason="test intent",
        stop_price=Decimal(stop) if stop is not None else None,
        features=features or {},
        linked_position_id=linked,
    )


def _open(
    pm: PositionManager,
    books: dict[str, BookSnapshot],
    strategy: str,
    symbol: str,
    price: str,
    qty: str,
    *,
    ts: float = T0,
    decision_id: str | None = None,
    stop: str | None = None,
    features: dict[str, Any] | None = None,
) -> Position:
    req = _req(strategy, symbol, "BUY", "entry", decision_id or f"d_entry_{strategy}_{symbol}")
    pos = pm.on_fill(_fill(req, price, qty, ts), req, books, _intent(req, stop=stop, features=features))
    assert isinstance(pos, Position)
    return pos


def _exit(
    pm: PositionManager,
    books: dict[str, BookSnapshot],
    pos: Position,
    symbol: str,
    price: str,
    qty: str,
    *,
    ts: float,
    purpose: Purpose = "exit",
    fee: str = "0",
    fee_asset: str = "",
    features: dict[str, Any] | None = None,
    decision_id: str = "d_exit",
) -> ClosedPosition | Position | None:
    req = _req(pos.strategy, symbol, "SELL", purpose, decision_id)
    return pm.on_fill(
        _fill(req, price, qty, ts, fee, fee_asset), req, books, _intent(req, linked=pos.id, features=features)
    )


# ---------------------------------------------------------------- opening


def test_first_entry_fill_opens_a_position(pm: PositionManager, books: dict[str, BookSnapshot]) -> None:
    req = _req("S3", "BTCUSDT", "BUY", "entry", "d_1792663200000_S3_BTCUSDT_1")
    intent = _intent(
        req,
        stop="80000.00",
        features={"target_price": "86000.00", "time_stop_s": 14400, "regime": "trend_up", "atr_pct": "0.01",
                  "not_copied": "x"},
    )
    pos = pm.on_fill(_fill(req, "82000.00", "0.0001", T0, "0.01066", "USDT"), req, books, intent)
    assert isinstance(pos, Position)
    assert pos.id == make_position_id("S3", "BTCUSDT", T0, 1)
    assert pm.portfolio.positions == {pos.id: pos}
    assert (pos.strategy, pos.symbol, pos.base, pos.quote) == ("S3", "BTCUSDT", "BTC", "USDT")
    assert pos.qty == Decimal("0.0001")
    assert pos.entry_price == Decimal("82000.00")
    assert pos.entry_ts == T0
    assert pos.stop_price == Decimal("80000.00")
    assert pos.target_price == Decimal("86000.00")
    assert pos.time_stop_ts == T0 + 14400
    assert pos.meta["entry_decision_id"] == req.decision_id
    assert pos.meta["side"] == "BUY"
    assert pos.meta["regime"] == "trend_up" and pos.meta["atr_pct"] == "0.01"
    assert "not_copied" not in pos.meta
    assert Decimal(pos.meta["value_in_tmn"]) == Decimal("8.2") * U_BID  # 2,187,104 TMN
    assert Decimal(pos.meta["fees_tmn"]) == Decimal("0.01066") * U_BID
    assert pm.portfolio.fees_paid_tmn_total == Decimal("0.01066") * U_BID


def test_average_price_over_several_fills(pm: PositionManager, books: dict[str, BookSnapshot]) -> None:
    req = _req("S3", "BTCUSDT", "BUY", "entry", "d_avg")
    intent = _intent(req, features={"time_stop_s": 600})
    first = pm.on_fill(_fill(req, "82000.00", "0.0001", T0, "0.01066", "USDT"), req, books, intent)
    # a re-placed order of the same decision (new client id), later fill, no intent passed
    req2 = _req("S3", "BTCUSDT", "BUY", "entry", "d_avg", attempt=1)
    second = pm.on_fill(_fill(req2, "82400.00", "0.0003", T0 + 5, "0.032136", "USDT"), req2, books)
    assert second is first  # same Position object, same id
    assert len(pm.portfolio.positions) == 1
    assert first.qty == Decimal("0.0004")
    # (0.0001 x 82,000 + 0.0003 x 82,400) / 0.0004 = 32.92 / 0.0004
    assert first.entry_price == Decimal("82300")
    assert first.entry_ts == T0 and first.time_stop_ts == T0 + 600  # from the first fill
    assert Decimal(first.meta["value_in_tmn"]) == Decimal("32.92") * U_BID
    assert Decimal(first.meta["fees_tmn"]) == Decimal("0.042796") * U_BID


def test_tmn_quoted_entry_values(pm: PositionManager, books: dict[str, BookSnapshot]) -> None:
    req = _req("S1", "BTCTMN", "BUY", "entry", "d_tmn")
    pos = pm.on_fill(_fill(req, "22000000000", "0.0001", T0, "6600", "TMN"), req, books, _intent(req))
    assert isinstance(pos, Position)
    assert Decimal(pos.meta["value_in_tmn"]) == Decimal("2200000")
    assert Decimal(pos.meta["fees_tmn"]) == Decimal("6600")
    assert pos.stop_price is None and pos.target_price is None and pos.time_stop_ts is None


def test_entry_without_intent_has_no_exit_levels(pm: PositionManager, books: dict[str, BookSnapshot]) -> None:
    req = _req("S1", "BTCTMN", "BUY", "entry", "d_plain")
    pos = pm.on_fill(_fill(req, "22000000000", "0.0001", T0), req, books)
    assert isinstance(pos, Position)
    assert pos.stop_price is None and pos.target_price is None and pos.time_stop_ts is None


def test_ignored_fills(pm: PositionManager, books: dict[str, BookSnapshot]) -> None:
    unknown = _req("S1", "FOOTMN", "BUY", "entry", "d_unknown")
    assert pm.on_fill(_fill(unknown, "1", "1", T0, "1", "TMN"), unknown, books) is None
    assert pm.portfolio.fees_paid_tmn_total == 0  # unknown market: nothing booked
    zero = _req("S1", "BTCTMN", "BUY", "entry", "d_zero")
    assert pm.on_fill(_fill(zero, "22000000000", "0", T0), zero, books) is None
    rebalance = _req("S2", "USDTTMN", "BUY", "rebalance", "d_rebal")
    assert pm.on_fill(_fill(rebalance, "266758", "5", T0, "4001.37", "TMN"), rebalance, books) is None
    assert pm.portfolio.fees_paid_tmn_total == Decimal("4001.37")  # fees always count (fee budget)
    orphan = _req("S3", "BTCUSDT", "SELL", "exit", "d_orphan")
    assert pm.on_fill(_fill(orphan, "83000", "0.0001", T0), orphan, books) is None
    assert pm.portfolio.positions == {}


# ---------------------------------------------------------------- closing and PnL


def test_close_on_exit_with_pnl(pm: PositionManager, books: dict[str, BookSnapshot]) -> None:
    req = _req("S3", "BTCUSDT", "BUY", "entry", "d_s3")
    pos = pm.on_fill(_fill(req, "80000.00", "0.0002", T0, "0.0208", "USDT"), req, books, _intent(req))
    assert isinstance(pos, Position)
    closed = _exit(pm, books, pos, "BTCUSDT", "84000.00", "0.0002", ts=T0 + 900, fee="0.02184", fee_asset="USDT",
                   features={"exit_reason": "target"})
    assert isinstance(closed, ClosedPosition)
    expected = (Decimal("16.8") - Decimal("16") - Decimal("0.0208") - Decimal("0.02184")) * U_BID
    assert closed.pnl_tmn == expected == Decimal("202003.0592")
    assert closed.pnl_pct == expected / (Decimal("16") * U_BID)
    assert closed.hold_s == 900
    assert closed.exit_reason == "target"
    assert closed.closed_ts == T0 + 900
    assert closed.decision_id == "d_s3"  # outcome goes to the entry decision
    assert closed.position.qty == 0
    assert pm.portfolio.positions == {}
    assert pm.portfolio.realized_pnl_tmn == {"S3": expected}
    out = closed.outcome()
    assert Decimal(out["pnl_tmn"]) == expected  # Decimal rendered as a string: no precision lost
    assert out == {
        "pnl_tmn": str(closed.pnl_tmn),
        "pnl_pct": str(closed.pnl_pct),
        "hold_s": 900.0,
        "closed_ts": T0 + 900,
        "exit_reason": "target",
        "position_id": pos.id,
        "strategy": "S3",
        "symbol": "BTCUSDT",
    }


def test_usdt_legs_use_the_dollar_bid_at_fill_time(
    pm: PositionManager, books: dict[str, BookSnapshot], make_book: BookFactory
) -> None:
    req = _req("S3", "BTCUSDT", "BUY", "entry", "d_fx")
    pos = pm.on_fill(_fill(req, "80000.00", "0.0002", T0, "0.0208", "USDT"), req, books, _intent(req))
    assert isinstance(pos, Position)
    later = dict(books, USDTTMN=make_book("USDTTMN", "270000", "270040", ts=T0 + 60))
    req_x = _req("S3", "BTCUSDT", "SELL", "exit", "d_fx_exit")
    closed = pm.on_fill(_fill(req_x, "80000.00", "0.0002", T0 + 60, "0.0208", "USDT"), req_x, later,
                        _intent(req_x, linked=pos.id))
    assert isinstance(closed, ClosedPosition)
    # same USDT price both ways, but the dollar rose 266,720 -> 270,000 TMN between the fills
    vin, vout = Decimal("16") * U_BID, Decimal("16") * Decimal("270000")
    fees = Decimal("0.0208") * U_BID + Decimal("0.0208") * Decimal("270000")
    assert closed.pnl_tmn == vout - vin - fees


def test_lockin_close_through_the_other_book(pm: PositionManager, books: dict[str, BookSnapshot]) -> None:
    """S1 TMN-cheap cycle: buy BTCTMN, sell BTCUSDT -> one position, exit_reason lockin."""
    req = _req("S1", "BTCTMN", "BUY", "entry", "d_s1")
    pos = pm.on_fill(_fill(req, "22000000000", "0.0001", T0, "6600", "TMN"), req, books, _intent(req))
    assert isinstance(pos, Position)
    closed = _exit(pm, books, pos, "BTCUSDT", "83000.00", "0.0001", ts=T0 + 3, purpose="leg2",
                   fee="0.01079", fee_asset="USDT")
    assert isinstance(closed, ClosedPosition)
    assert closed.exit_reason == "lockin"  # from purpose leg2 (no exit_reason feature given)
    assert closed.position.meta["last_exit_symbol"] == "BTCUSDT"
    # value out 8.3 USDT x 266,720 = 2,213,776; in 2,200,000; fees 6,600 + 0.01079 x 266,720
    assert closed.pnl_tmn == Decimal("2213776") - Decimal("2200000") - Decimal("6600") - Decimal("2877.9088")
    assert closed.pnl_tmn == Decimal("4298.0912")
    assert closed.hold_s == 3
    assert pm.portfolio.realized_pnl_tmn == {"S1": Decimal("4298.0912")}


def test_exit_reason_from_intent_features_wins(pm: PositionManager, books: dict[str, BookSnapshot]) -> None:
    pos = _open(pm, books, "S1", "BTCTMN", "22000000000", "0.0001")
    closed = _exit(pm, books, pos, "BTCTMN", "21800000000", "0.0001", ts=T0 + 60, purpose="stop",
                   features={"exit_reason": "time_stop"})
    assert isinstance(closed, ClosedPosition)
    assert closed.exit_reason == "time_stop"


@pytest.mark.parametrize("purpose", ["stop", "flatten"])
def test_exit_reason_defaults_to_purpose(pm: PositionManager, books: dict[str, BookSnapshot], purpose: Purpose) -> None:
    pos = _open(pm, books, "S1", "BTCTMN", "22000000000", "0.0001")
    closed = _exit(pm, books, pos, "BTCTMN", "21800000000", "0.0001", ts=T0 + 60, purpose=purpose)
    assert isinstance(closed, ClosedPosition)
    assert closed.exit_reason == purpose


def test_partial_exits(pm: PositionManager, books: dict[str, BookSnapshot]) -> None:
    pos = _open(pm, books, "S1", "BTCTMN", "22000000000", "0.0004")  # 8.8M TMN in, no fees
    part = _exit(pm, books, pos, "BTCTMN", "22100000000", "0.0001", ts=T0 + 10, decision_id="d_x1")
    assert part is pos  # still open
    assert pos.qty == Decimal("0.0003")
    assert Decimal(pos.meta["value_out_tmn"]) == Decimal("2210000")
    assert pm.portfolio.realized_pnl_tmn == {}
    closed = _exit(pm, books, pos, "BTCTMN", "22200000000", "0.0003", ts=T0 + 20, decision_id="d_x2")
    assert isinstance(closed, ClosedPosition)
    assert closed.pnl_tmn == Decimal("2210000") + Decimal("6660000") - Decimal("8800000")  # 70,000
    assert closed.pnl_pct == Decimal("70000") / Decimal("8800000")


@pytest.mark.parametrize(
    ("sold", "closes"),
    [("99.5", True), ("99", True), ("98", False)],  # remainders 0.5 / 1 (= min_qty) / 2 DOGE
)
def test_dust_remainder_closes_by_min_qty(
    pm: PositionManager, books: dict[str, BookSnapshot], snap_markets: dict[str, MarketInfo], sold: str, closes: bool
) -> None:
    assert snap_markets["DOGETMN"].min_qty == Decimal("1")
    pos = _open(pm, books, "S1", "DOGETMN", "48000", "100")
    result = _exit(pm, books, pos, "DOGETMN", "48100", sold, ts=T0 + 30)
    assert isinstance(result, ClosedPosition) is closes
    assert (pos.id in pm.portfolio.positions) is not closes


def test_dust_remainder_btc(pm: PositionManager, books: dict[str, BookSnapshot]) -> None:
    pos = _open(pm, books, "S1", "BTCTMN", "22000000000", "0.0001")
    closed = _exit(pm, books, pos, "BTCTMN", "22000000000", "0.0000995", ts=T0 + 30)  # 0.0000005 < min_qty
    assert isinstance(closed, ClosedPosition)


def test_a_remainder_below_min_notional_closes_the_position_as_dust(
    pm: PositionManager, books: dict[str, BookSnapshot]
) -> None:
    """0.000002 BTC is 2 steps (> min_qty) but only 44,200 TMN (< 50,000): it can never be sold."""
    pos = _open(pm, books, "S3", "BTCTMN", "22000000000", "0.00005")
    closed = _exit(pm, books, pos, "BTCTMN", "22100000000", "0.000048", ts=T0 + 30)
    assert isinstance(closed, ClosedPosition)
    assert pm.portfolio.positions == {}  # no zombie position left to re-emit exits or hold a slot
    dust_value = Decimal("0.000002") * Decimal("22100000000")
    assert closed.position.meta["dust_qty"] == "0.000002"
    assert Decimal(closed.position.meta["dust_value_tmn"]) == dust_value
    # the dust still on the account counts as value out: 0.000048 + 0.000002 sold/held at 22.1e9
    assert closed.pnl_tmn == Decimal("0.00005") * Decimal("22100000000") - Decimal("1100000")
    assert closed.outcome()["dust_qty"] == "0.000002"
    sellable = _open(pm, books, "S3", "BTCTMN", "22000000000", "0.00005", decision_id="d_e2")
    assert isinstance(_exit(pm, books, sellable, "BTCTMN", "22100000000", "0.000045", ts=T0 + 40), Position)


def test_a_leg_without_a_tmn_price_marks_the_pnl_incomplete(
    pm: PositionManager, books: dict[str, BookSnapshot]
) -> None:
    no_dollar = {k: v for k, v in books.items() if k != "USDTTMN"}
    req = _req("S3", "BTCUSDT", "BUY", "entry", "d_unpriced")
    pos = pm.on_fill(_fill(req, "80000.00", "0.0001", T0), req, no_dollar, _intent(req))
    assert isinstance(pos, Position) and pos.meta["unpriced_legs"] == 1
    closed = _exit(pm, books, pos, "BTCUSDT", "81000.00", "0.0001", ts=T0 + 60)
    assert isinstance(closed, ClosedPosition) and not closed.pnl_complete
    assert closed.outcome()["pnl_complete"] is False
    assert pm.portfolio.realized_pnl_tmn == {}  # a PnL with a 0-valued leg is never booked as real


def test_oversized_exit_clamps_at_zero(pm: PositionManager, books: dict[str, BookSnapshot]) -> None:
    pos = _open(pm, books, "S1", "BTCTMN", "22000000000", "0.0001")
    closed = _exit(pm, books, pos, "BTCTMN", "22000000000", "0.0002", ts=T0 + 30)
    assert isinstance(closed, ClosedPosition)
    assert closed.position.qty == 0


def test_realized_pnl_by_strategy(pm: PositionManager, books: dict[str, BookSnapshot]) -> None:
    a = _open(pm, books, "S1", "BTCTMN", "22000000000", "0.0001", decision_id="d_a")
    b = _open(pm, books, "S3", "DOGETMN", "48000", "100", decision_id="d_b")
    c = _open(pm, books, "S1", "DOGETMN", "48000", "10", decision_id="d_c")
    assert len({a.id, b.id, c.id}) == 3
    _exit(pm, books, a, "BTCTMN", "22100000000", "0.0001", ts=T0 + 1, decision_id="d_xa")  # +10,000
    _exit(pm, books, b, "DOGETMN", "47000", "100", ts=T0 + 2, decision_id="d_xb")  # -100,000
    _exit(pm, books, c, "DOGETMN", "48500", "10", ts=T0 + 3, decision_id="d_xc")  # +5,000
    assert pm.portfolio.realized_pnl_tmn == {"S1": Decimal("15000"), "S3": Decimal("-100000")}
    assert pm.portfolio.realized_total_tmn == Decimal("-85000")
    assert pm.portfolio.positions == {}


def test_base_coin_fee_shrinks_qty_and_is_not_counted_twice(
    pm: PositionManager, books: dict[str, BookSnapshot]
) -> None:
    """BUY whose fee is taken from the received BTC: we hold qty - fee, and sell exactly that."""
    req = _req("S3", "BTCUSDT", "BUY", "entry", "d_basefee")
    pos = pm.on_fill(_fill(req, "80000.00", "0.0001", T0, "0.00000013", "BTC"), req, books, _intent(req))
    assert isinstance(pos, Position)
    assert pos.qty == Decimal("0.00009987")
    assert pos.entry_price == Decimal("80000.00")  # the market fill price, not fee-adjusted
    base_fee_tmn = Decimal("0.00000013") * BTC_TMN_BID  # 2,860 TMN at bid(BTCTMN)
    assert Decimal(pos.meta["fees_in_qty_tmn"]) == base_fee_tmn
    exit_fee = Decimal("0.00009987") * Decimal("81000") * Decimal("0.0013")
    closed = _exit(pm, books, pos, "BTCUSDT", "81000.00", "0.00009987", ts=T0 + 60, fee=str(exit_fee), fee_asset="USDT")
    assert isinstance(closed, ClosedPosition)  # nothing left over: the fee never was ours
    # real cash flow: paid 8 USDT, received 0.00009987 x 81,000 - exit fee
    assert closed.pnl_tmn == (Decimal("0.00009987") * Decimal("81000") - Decimal("8") - exit_fee) * U_BID
    assert pm.portfolio.fees_paid_tmn_total == base_fee_tmn + exit_fee * U_BID  # fee budget sees both


def test_force_close_values_remainder_at_bid(pm: PositionManager, books: dict[str, BookSnapshot]) -> None:
    req = _req("S3", "BTCUSDT", "BUY", "entry", "d_force")
    pos = pm.on_fill(_fill(req, "80000.00", "0.0002", T0, "0.0208", "USDT"), req, books, _intent(req))
    assert isinstance(pos, Position)
    closed = pm.force_close(pos.id, T0 + 7200, "stop", books)
    assert closed.exit_reason == "stop"
    assert closed.hold_s == 7200
    assert closed.position.qty == 0
    # coins valued at bid(BTCTMN) (the TMN book is preferred): 0.0002 x 22,000,000,000
    assert closed.pnl_tmn == Decimal("4400000") - Decimal("16") * U_BID - Decimal("0.0208") * U_BID
    assert pm.portfolio.positions == {}
    assert pm.portfolio.realized_pnl_tmn == {"S3": closed.pnl_tmn}


# ---------------------------------------------------------------- restart and server stops


def test_restart_maps_late_entry_fills_to_the_existing_position(
    portfolio: PortfolioState, snap_markets: dict[str, MarketInfo], books: dict[str, BookSnapshot]
) -> None:
    pm1 = PositionManager(portfolio, snap_markets)
    pos = _open(pm1, books, "S1", "BTCTMN", "22000000000", "0.0001", decision_id="d_restart")
    pm2 = PositionManager(portfolio, snap_markets)  # new process: empty entry map, seq back to 0
    req = _req("S1", "BTCTMN", "BUY", "entry", "d_restart", attempt=1)
    again = pm2.on_fill(_fill(req, "22200000000", "0.0001", T0 + 30), req, books)
    assert again is pos
    assert pos.qty == Decimal("0.0002")
    assert pos.entry_price == Decimal("22100000000")


def test_restart_never_reuses_a_position_id(
    portfolio: PortfolioState, snap_markets: dict[str, MarketInfo], books: dict[str, BookSnapshot]
) -> None:
    pm1 = PositionManager(portfolio, snap_markets)
    first = _open(pm1, books, "S1", "BTCTMN", "22000000000", "0.0001", decision_id="d_one")
    pm2 = PositionManager(portfolio, snap_markets)
    second = _open(pm2, books, "S1", "BTCTMN", "22000000000", "0.0001", decision_id="d_two")  # same ts
    assert first.id != second.id
    assert set(portfolio.positions) == {first.id, second.id}


def test_server_stop_fill_maps_by_client_id(pm: PositionManager, books: dict[str, BookSnapshot]) -> None:
    pos = _open(pm, books, "S3", "BTCUSDT", "80000.00", "0.0002", stop="78000.00")
    stop_req = _req("S3", "BTCUSDT", "SELL", "stop", "d_stop_order")
    pos.stop_order_client_id = stop_req.client_id
    pos.meta = {**pos.meta, "stop_mode": "server"}
    closed = pm.on_fill(_fill(stop_req, "77900.00", "0.0002", T0 + 600), stop_req, books)  # no intent
    assert isinstance(closed, ClosedPosition)
    assert closed.exit_reason == "stop"
    assert closed.pnl_tmn == (Decimal("0.0002") * Decimal("77900") - Decimal("16")) * U_BID


# ---------------------------------------------------------------- exits


def _with_stop_mode(pos: Position, *, mode: str | None, client_id: str | None) -> None:
    pos.stop_order_client_id = client_id
    if mode is not None:
        pos.meta = {**pos.meta, "stop_mode": mode}


def test_software_stop_intent(pm: PositionManager, books: dict[str, BookSnapshot], make_book: BookFactory) -> None:
    pos = _open(pm, books, "S1", "BTCTMN", "22000000000", "0.0001", stop="21800000000")
    assert pm.check_exits(books, T0 + 5, software_stops=True) == []  # bid 22.0e9 > stop
    down = dict(books, BTCTMN=make_book("BTCTMN", "21800000000", "21830000000"))  # bid == stop triggers
    [it] = pm.check_exits(down, T0 + 5, software_stops=True)
    assert it.id == make_decision_id(T0 + 5, "S1", "BTCTMN", 1)
    assert (it.strategy, it.symbol, it.side, it.order_type) == ("S1", "BTCTMN", "SELL", "LIMIT")
    assert it.qty == pos.qty and it.price is None
    assert it.purpose == "stop" and it.taker is True
    assert it.linked_position_id == pos.id
    assert it.features == {"exit_reason": "stop", "bid": "21800000000", "position_id": pos.id}
    assert "stop" in it.reason and pos.id in it.reason
    assert pos.id in pm.portfolio.positions  # check_exits never mutates positions


STOP_CID = "S1_BTCTMN_1792663200000_STOP"


def _stop_order(pos: Position, limit: str = "21700000000", status: str = "NEW") -> Order:
    req = OrderRequest(
        client_id=STOP_CID,
        symbol=pos.symbol,
        side="SELL",
        type="STOP_LIMIT",
        qty=pos.qty,
        price=Decimal(limit),
        stop_price=pos.stop_price,
        strategy=pos.strategy,
        purpose="stop",
        decision_id="d_stop",
    )
    return Order(req=req, status=status, created_ts=T0, updated_ts=T0)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("mode", "client_id", "tracked", "software", "fires"),
    [
        ("server", STOP_CID, True, False, False),  # live server stop does the job
        ("server", STOP_CID, True, True, True),  # software stops forced on
        ("server", STOP_CID, False, False, True),  # its id is not among our open orders: protects nothing
        ("server", None, False, False, True),  # server stop was never placed
        ("software", STOP_CID, True, False, True),
        (None, None, False, False, True),
    ],
)
def test_software_stop_only_without_live_server_stop(
    pm: PositionManager, books: dict[str, BookSnapshot], make_book: BookFactory,
    mode: str | None, client_id: str | None, tracked: bool, software: bool, fires: bool,
) -> None:
    pos = _open(pm, books, "S1", "BTCTMN", "22000000000", "0.0001", stop="21800000000")
    _with_stop_mode(pos, mode=mode, client_id=client_id)
    if tracked:
        pm.portfolio.open_orders[STOP_CID] = _stop_order(pos)
    down = dict(books, BTCTMN=make_book("BTCTMN", "21700000000", "21730000000"))  # bid == the stop limit
    intents = pm.check_exits(down, T0 + 5, software_stops=software)
    assert [i.features["exit_reason"] for i in intents] == (["stop"] if fires else [])


def test_a_stranded_server_stop_limit_does_not_leave_the_position_unprotected(
    pm: PositionManager, books: dict[str, BookSnapshot], make_book: BookFactory
) -> None:
    """The price gapped through the STOP_LIMIT's limit: it triggered but can never fill there."""
    pos = _open(pm, books, "S1", "BTCTMN", "22000000000", "0.0001", stop="21800000000")
    _with_stop_mode(pos, mode="server", client_id=STOP_CID)
    pm.portfolio.open_orders[STOP_CID] = _stop_order(pos, limit="21712800000")  # stop x (1 - 0.4 %)
    at_limit = dict(books, BTCTMN=make_book("BTCTMN", "21712800000", "21730000000"))
    assert pm.check_exits(at_limit, T0 + 5, software_stops=False) == []  # can still fill: leave it
    gapped = dict(books, BTCTMN=make_book("BTCTMN", "21300000000", "21330000000"))
    [it] = pm.check_exits(gapped, T0 + 5, software_stops=False)
    assert (it.purpose, it.taker, it.linked_position_id) == ("stop", True, pos.id)
    assert it.features["stranded_server_stop"] == STOP_CID
    assert "cannot fill" in it.reason


def test_target_exit(pm: PositionManager, books: dict[str, BookSnapshot], make_book: BookFactory) -> None:
    pos = _open(pm, books, "S3", "BTCUSDT", "80000.00", "0.0002", stop="78000.00",
                features={"target_price": "83000.00"})
    [it] = pm.check_exits(books, T0 + 60, software_stops=False)  # bid 83,000.00 >= target
    assert it.purpose == "exit" and it.taker is False
    assert it.features["exit_reason"] == "target"
    assert it.qty == pos.qty
    below = dict(books, BTCUSDT=make_book("BTCUSDT", "82999.99", "83050.00"))
    assert pm.check_exits(below, T0 + 60, software_stops=False) == []


def test_time_stop(pm: PositionManager, books: dict[str, BookSnapshot]) -> None:
    pos = _open(pm, books, "S1", "BTCTMN", "22000000000", "0.0001", features={"time_stop_s": 1200})
    assert pos.time_stop_ts == T0 + 1200
    assert pm.check_exits(books, T0 + 1199.9, software_stops=True) == []
    [it] = pm.check_exits(books, T0 + 1200, software_stops=True)
    assert it.features["exit_reason"] == "time_stop"
    assert it.purpose == "exit" and it.taker is True
    assert "time stop" in it.reason


def test_stop_has_priority_over_time_stop(
    pm: PositionManager, books: dict[str, BookSnapshot], make_book: BookFactory
) -> None:
    _open(pm, books, "S1", "BTCTMN", "22000000000", "0.0001", stop="21800000000", features={"time_stop_s": 60})
    down = dict(books, BTCTMN=make_book("BTCTMN", "21700000000", "21730000000"))
    [it] = pm.check_exits(down, T0 + 600, software_stops=True)
    assert it.features["exit_reason"] == "stop"


def test_exit_pending_and_missing_books_are_skipped(
    pm: PositionManager, books: dict[str, BookSnapshot], make_book: BookFactory
) -> None:
    pending = _open(pm, books, "S1", "BTCTMN", "22000000000", "0.0001", features={"time_stop_s": 1},
                    decision_id="d_pending")
    pending.meta = {**pending.meta, "exit_pending": True}
    _open(pm, books, "S1", "DOGETMN", "48000", "100", features={"time_stop_s": 1}, decision_id="d_nobook")
    no_doge = {k: v for k, v in books.items() if k != "DOGETMN"}
    assert pm.check_exits(no_doge, T0 + 10, software_stops=True) == []
    one_sided = dict(no_doge, DOGETMN=make_book("DOGETMN", None, "48100"))
    assert pm.check_exits(one_sided, T0 + 10, software_stops=True) == []


def test_exit_intents_are_deterministic(pm: PositionManager, books: dict[str, BookSnapshot]) -> None:
    a = _open(pm, books, "S1", "DOGETMN", "48000", "100", features={"time_stop_s": 60}, decision_id="d_a")
    b = _open(pm, books, "S1", "BTCTMN", "22000000000", "0.0001", features={"time_stop_s": 60}, decision_id="d_b")
    now = T0 + 120
    first = pm.check_exits(books, now, software_stops=True, seq_start=5)
    second = pm.check_exits(books, now, software_stops=True, seq_start=5)
    assert first == second
    ordered = sorted([a, b], key=lambda p: p.id)  # positions are walked in id order
    assert [i.linked_position_id for i in first] == [p.id for p in ordered]
    assert [i.id for i in first] == [
        make_decision_id(now, "S1", ordered[0].symbol, 6),
        make_decision_id(now, "S1", ordered[1].symbol, 7),
    ]

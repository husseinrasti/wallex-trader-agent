"""Tests for the Wallex REST client (PRD-01 §5).

Parsers are tested against the documented payloads in ``tests/fixtures/wallex/rest_*.json``; the
HTTP behaviour (retries, idempotent order placement, headers, rate limiting) runs against ``respx``
mocks, so nothing here touches the network. Time is faked: ``FakeTime`` records every sleep and
advances a fake monotonic clock, so backoff and rate-limit delays are asserted exactly.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from wta.exchange import wallex_rest
from wta.exchange.base import OrderRejected, WallexAPIError
from wta.exchange.wallex_rest import (
    TokenBucket,
    WallexREST,
    apply_fees,
    decimals_count,
    json_safe,
    map_status,
    order_body,
    parse_all_depth,
    parse_balances,
    parse_candles,
    parse_depth,
    parse_fees,
    parse_fills,
    parse_markets,
    parse_my_trades,
    parse_order,
    parse_trades,
)
from wta.models import OrderRequest
from wta.utils.timeutil import SimClock

BASE = "https://api.wallex.ir"
KEY = "TESTKEY-9f8e7d6c5b4a3210-DO-NOT-LEAK"
FIX = Path(__file__).parent / "fixtures" / "wallex"
T0 = 1_792_663_200.0  # 2026-10-22T10:00:00Z
CID = "S1_BTCUSDT_1792663200000_A1B2"
ORDER_URL = f"/v1/account/orders/{CID}"

Event = tuple[str, str, str, dict[str, Any]]


def load(name: str) -> Any:
    """A fixture decoded like the client does (fractional numbers as Decimal)."""
    return json.loads((FIX / f"rest_{name}.json").read_text(), parse_float=Decimal)


def reply(name: str, status: int = 200) -> httpx.Response:
    """An HTTP response whose body is the fixture file, byte for byte."""
    return httpx.Response(
        status, content=(FIX / f"rest_{name}.json").read_bytes(), headers={"Content-Type": "application/json"}
    )


def fail(status: int, message: str = "error", headers: dict[str, str] | None = None) -> httpx.Response:
    return httpx.Response(status, json={"success": False, "message": message, "result": {}}, headers=headers)


def limit_buy(**overrides: Any) -> OrderRequest:
    fields: dict[str, Any] = {
        "client_id": CID,
        "symbol": "BTCUSDT",
        "side": "BUY",
        "type": "LIMIT",
        "qty": Decimal("0.000150"),
        "price": Decimal("82845.00"),
        "strategy": "S1",
        "purpose": "entry",
        "decision_id": "d_1792663200000_S1_BTCUSDT_1",
    }
    fields.update(overrides)
    return OrderRequest(**fields)


class FakeTime:
    """Fake monotonic clock + sleep: ``sleep`` records the delay and advances the clock."""

    def __init__(self) -> None:
        self.t = 0.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.t

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.t += seconds


@pytest.fixture
def ft() -> FakeTime:
    return FakeTime()


@pytest.fixture
def events() -> list[Event]:
    return []


@pytest.fixture
def api():
    with respx.mock(base_url=BASE, assert_all_called=False) as router:
        yield router


@pytest.fixture
async def make(ft: FakeTime, events: list[Event]):
    """Factory for clients wired to the fake time; closed after the test."""
    clients: list[WallexREST] = []

    def _make(**kw: Any) -> WallexREST:
        params: dict[str, Any] = {
            "api_key": KEY,
            "rps": 1000,
            "clock": SimClock(T0),
            "sleep": ft.sleep,
            "monotonic": ft.monotonic,
            "on_order_event": lambda *e: events.append(e),
        }
        params.update(kw)
        c = WallexREST(**params)
        clients.append(c)
        return c

    yield _make
    for c in clients:
        await c.close()


@pytest.fixture
def client(make) -> WallexREST:
    return make()


# ---------------------------------------------------------------- parsers: market data


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (0, 0),
        (2, 2),
        ("6", 6),
        ("0.01", 2),
        (Decimal("0.001"), 3),
        ("1e-8", 8),
        ("0.000100", 4),
        (None, None),
    ],
)
def test_decimals_count(value: object, expected: int | None) -> None:
    assert decimals_count(value) == expected


def test_parse_markets_v1_symbols() -> None:
    m = parse_markets(load("markets"))
    assert list(m) == sorted(m)
    btc_tmn, btc_usdt, usdt_tmn = m["BTCTMN"], m["BTCUSDT"], m["USDTTMN"]
    assert (btc_tmn.base, btc_tmn.quote, btc_tmn.tick_decimals, btc_tmn.step_decimals) == ("BTC", "TMN", 0, 6)
    assert (btc_usdt.tick_decimals, btc_usdt.step_decimals) == (2, 6)
    assert (usdt_tmn.tick_decimals, usdt_tmn.step_decimals) == (0, 2)
    assert btc_usdt.tick == Decimal("0.01")
    assert btc_tmn.min_qty == Decimal("0.000001") and btc_tmn.min_notional == Decimal("50000")
    assert btc_usdt.min_notional == Decimal("1")
    assert m["SOLTMN"].market_orders_enabled is False
    assert btc_tmn.market_orders_enabled is True
    # string precisions, a step value "0.01" instead of a count, "true" as a string
    eth = m["ETHUSDT"]
    assert (eth.tick_decimals, eth.step_decimals, eth.market_orders_enabled) == (2, 4, True)
    # defaults when missing: min notional and fees by quote asset
    assert m["XRPTMN"].min_notional == Decimal("50000")
    assert (btc_tmn.maker_fee, btc_tmn.taker_fee) == (Decimal("0.0025"), Decimal("0.003"))
    assert (btc_usdt.maker_fee, btc_usdt.taker_fee) == (Decimal("0.001"), Decimal("0.0013"))
    assert all(isinstance(x.min_qty, Decimal) for x in m.values())


def test_parse_markets_hector_list() -> None:
    m = parse_markets(load("markets_hector"))
    assert set(m) == {"BTCTMN", "BTCUSDT", "USDTTMN"}  # the non-spot market is skipped
    assert (m["BTCUSDT"].tick_decimals, m["BTCUSDT"].step_decimals) == (2, 6)
    assert (m["USDTTMN"].tick_decimals, m["USDTTMN"].step_decimals) == (0, 2)
    assert m["BTCUSDT"].min_notional == Decimal("1")
    assert m["BTCTMN"].min_notional == Decimal("50000")


def test_parse_fees_and_apply_fees_returns_new_dict() -> None:
    fees = parse_fees(load("fee"))
    assert fees["BTCUSDT"] == (Decimal("0.001"), Decimal("0.0013"))
    assert fees["ETHUSDT"] == (Decimal("0.0008"), Decimal("0.0011"))
    markets = parse_markets(load("markets"))
    updated = apply_fees(markets, fees)
    assert updated is not markets
    assert updated["ETHUSDT"].maker_fee == Decimal("0.0008")
    assert markets["ETHUSDT"].maker_fee == Decimal("0.001")  # original untouched
    assert updated["XRPTMN"] is markets["XRPTMN"]  # no fee entry -> default kept


def test_parse_fees_accepts_percent_numbers() -> None:
    fees = parse_fees({"result": {"BTCTMN": {"makerFeeRate": "0.25", "takerFeeRate": 0.3}}})
    assert fees == {"BTCTMN": (Decimal("0.0025"), Decimal("0.003"))}


def test_parse_depth_fixture() -> None:
    book = parse_depth("BTCTMN", load("depth"), ts=T0)
    assert book.source == "rest" and book.ts == T0
    assert book.best_bid == Decimal("22075699081") and book.best_ask == Decimal("22106074058")
    assert book.best_ask_qty == Decimal("0.004512")
    assert [lv.price for lv in book.bids] == sorted((lv.price for lv in book.bids), reverse=True)
    assert [lv.price for lv in book.asks] == sorted(lv.price for lv in book.asks)


def test_parse_depth_sorts_drops_zero_and_truncates() -> None:
    payload = {
        "result": {
            "bid": [{"price": "99", "quantity": "1"}, {"price": "101", "quantity": "2"}, ["100", "0"]],
            "ask": [
                {"price": "105", "quantity": 1},
                {"price": "103", "quantity": "0"},
                ["104", "3"],
                ["106", "1"],
            ],
        }
    }
    book = parse_depth("XTMN", payload, ts=1.0, levels=2)
    assert [(lv.price, lv.qty) for lv in book.bids] == [
        (Decimal("101"), Decimal("2")),
        (Decimal("99"), Decimal("1")),
    ]
    assert [lv.price for lv in book.asks] == [Decimal("104"), Decimal("105")]


def test_parse_all_depth() -> None:
    books = parse_all_depth(load("depth_all"), ts=T0, levels=10)
    assert set(books) == {"BTCTMN", "BTCUSDT", "USDTTMN"}  # the empty book is dropped
    assert books["BTCUSDT"].best_bid == Decimal("82845.00")
    assert books["BTCUSDT"].best_ask == Decimal("82909.16")
    assert len(books["USDTTMN"].bids) == 1  # zero-quantity level dropped


def test_parse_trades_taker_side_and_order() -> None:
    trades = parse_trades("BTCTMN", load("trades"))
    assert [t.ts for t in trades] == sorted(t.ts for t in trades)
    last = trades[-1]
    assert last.ts == T0 + 5
    assert last.side == "SELL"  # isBuyOrder=true: the maker was the buyer
    assert trades[1].side == "BUY" and trades[1].ts == T0 + 3.5
    assert last.price == Decimal("22090000000") and last.qty == Decimal("0.001200")


def test_parse_candles_ok_and_no_data() -> None:
    candles = parse_candles("BTCUSDT", "15", load("udf"))
    assert [c.ts for c in candles] == [1792661400, 1792662300, 1792663200]
    first = candles[0]
    assert (first.o, first.h, first.l, first.c) == (
        Decimal("82790.12"),
        Decimal("82860.0"),
        Decimal("82770.0"),
        Decimal("82851.5"),
    )
    assert first.v == Decimal("1.284311") and first.resolution == "15"
    assert parse_candles("BTCUSDT", "15", {"s": "no_data", "nextTime": 1792600000}) == []
    assert parse_candles("BTCUSDT", "15", {"s": "error", "errmsg": "bad"}) == []


def test_parse_balances_main_wallet() -> None:
    b = parse_balances(load("balances"))
    assert b["TMN"].free == Decimal("4750000") and b["TMN"].locked == Decimal("250000")
    assert b["TMN"].total == Decimal("5000000")
    assert b["USDT"].free == Decimal("1.25")
    assert b["BTC"].free == 0 and b["BTC"].locked == Decimal("0.000150")


def test_parse_balances_detail() -> None:
    b = parse_balances(load("balances_detail"))
    assert b["TMN"].free == Decimal("4750000") and b["TMN"].locked == Decimal("250000")
    assert b["BTC"].free == 0 and b["BTC"].locked == Decimal("0.000150")
    assert set(b) == {"BTC", "TMN", "USDT"}


# ---------------------------------------------------------------- parsers: orders


@pytest.mark.parametrize(
    ("raw", "executed", "orig", "active", "expected"),
    [
        ("NEW", "0", "1", None, "NEW"),
        ("NEW", "0.4", "1", None, "PARTIALLY_FILLED"),
        ("open", "0", "1", None, "NEW"),
        ("ACTIVE", "0", "1", None, "NEW"),
        ("PENDING", "0", "1", None, "NEW"),
        ("PARTIALLY_FILLED", "0.4", "1", None, "PARTIALLY_FILLED"),
        ("PARTIAL", "0.4", "1", None, "PARTIALLY_FILLED"),
        ("FILLED", "1", "1", None, "FILLED"),
        ("DONE", "1", "1", None, "FILLED"),
        ("FINISHED", "1", "1", None, "FILLED"),
        ("FINISHED", "0.4", "1", None, "CANCELED"),
        ("FINISHED", "0", "1", None, "CANCELED"),
        ("CANCELED", "0", "1", None, "CANCELED"),
        ("CANCELLED", "0", "1", None, "CANCELED"),
        ("CANCEL", "0", "1", None, "CANCELED"),
        ("CANCELLED_BY_USER", "0", "1", None, "CANCELED"),
        ("REJECTED", "0", "1", None, "REJECTED"),
        ("EXPIRED", "0", "1", None, "EXPIRED"),
        ("WEIRD_STATE", "0", "1", None, "UNKNOWN"),
        ("CANCELED", "1", "1", None, "FILLED"),  # executed >= orig wins
        (None, "0", "1", True, "NEW"),
        (None, "0", "1", False, "CANCELED"),
        (None, "0", "1", None, "UNKNOWN"),
    ],
)
def test_map_status(raw: str | None, executed: str, orig: str, active: bool | None, expected: str) -> None:
    assert map_status(raw, Decimal(executed), Decimal(orig), active) == expected


def test_parse_order_new_keeps_our_request() -> None:
    req = limit_buy()
    order = parse_order(load("order_new"), req, now=T0 + 1)
    assert order.req is req
    assert order.status == "NEW" and order.filled_qty == 0 and order.avg_price is None
    assert order.created_ts == pytest.approx(T0 + 0.123)
    assert order.raw["clientOrderId"] == CID
    json.dumps(order.raw)  # raw is JSON-safe


def test_parse_order_filled_with_fills() -> None:
    order = parse_order(load("order_filled"), None, now=T0)
    assert order.status == "FILLED"
    assert order.filled_qty == Decimal("0.002000")
    assert order.avg_price == Decimal("22102429623.2")
    assert order.fee_paid == Decimal("0.0000054") and order.fee_asset == "BTC"
    assert order.req.strategy == "unknown"  # our id format, but not created by this process
    assert order.req.purpose == "exit" and order.req.decision_id == ""
    assert order.req.qty == Decimal("0.002000") and order.req.price == Decimal("22106074058")
    fills = parse_fills(load("order_filled"))
    assert [(f.qty, f.is_maker) for f in fills] == [(Decimal("0.001200"), True), (Decimal("0.000800"), False)]
    assert all(f.side == "BUY" and f.order_client_id == "EX_BTCTMN_1792663260000_C3D4" for f in fills)
    assert fills[0].ts == T0 + 60 and fills[0].fee == Decimal("0.000003")
    assert fills[0].trade_id and fills[0].trade_id != fills[1].trade_id


def test_parse_order_finished_partial_is_canceled_with_fill() -> None:
    order = parse_order(load("order_finished_partial"), None, now=T0)
    assert order.status == "CANCELED"
    assert order.filled_qty == Decimal("4.00") and order.remaining_qty == Decimal("6.00")
    assert order.avg_price == Decimal("266760")
    assert order.req.side == "SELL"
    [fill] = parse_fills(load("order_finished_partial"))
    assert fill.side == "SELL" and fill.fee_asset == "TMN" and fill.is_maker is True


def test_parse_order_foreign_gets_synthetic_request() -> None:
    order = parse_order(load("order_canceled"), None, now=T0)
    assert order.status == "CANCELED"
    assert order.req.client_id == "web8812345abc"
    assert order.req.strategy == "foreign"
    assert (order.req.side, order.req.type, order.req.qty) == ("SELL", "LIMIT", Decimal("0.000500"))


def test_parse_order_unknown_status_keeps_raw_and_logs_it(caplog: pytest.LogCaptureFixture) -> None:
    payload = load("order_new")
    payload["result"]["status"] = "SOMETHING_NEW"
    with caplog.at_level("WARNING", logger="wta.exchange.wallex_rest"):
        order = parse_order(payload, None, now=T0)
    assert order.status == "UNKNOWN" and order.raw["status"] == "SOMETHING_NEW"
    assert f"unknown Wallex order status 'SOMETHING_NEW' for {CID}" in caplog.text  # PRD-01 §5: log raw
    assert '"status": "SOMETHING_NEW"' in caplog.text


def test_parse_my_trades_tolerates_shapes_and_filters_since() -> None:
    trades = load("my_trades")["result"]["trades"]
    for payload in ({"result": {"trades": trades}}, {"result": {"latestTrades": trades}}, {"result": trades}):
        fills = parse_my_trades(payload)
        assert [f.ts for f in fills] == [T0 + 60, T0 + 62, T0 + 130]  # ISO, ms and s timestamps
    fills = parse_my_trades(load("my_trades"), since_ts=T0 + 62)
    assert [(f.symbol, f.side, f.is_maker) for f in fills] == [
        ("BTCTMN", "BUY", False),
        ("USDTTMN", "SELL", True),
    ]
    assert (
        parse_my_trades(load("my_trades"), symbol="USDTTMN")[0].order_client_id
        == "S2_USDTTMN_1792663300000_E5F6"
    )


def test_same_fill_has_same_trade_id_from_order_and_my_trades() -> None:
    from_order = {f.trade_id for f in parse_fills(load("order_filled"))}
    from_trades = {f.trade_id for f in parse_my_trades(load("my_trades"), symbol="BTCTMN")}
    assert from_order == from_trades and len(from_order) == 2


def test_order_body_strings_and_stop_price_only_for_stops() -> None:
    assert order_body(limit_buy()) == {
        "symbol": "BTCUSDT",
        "type": "LIMIT",
        "side": "BUY",
        "quantity": "0.000150",
        "price": "82845.00",
        "client_id": CID,
    }
    stop = limit_buy(
        side="SELL", type="STOP_LIMIT", price=Decimal("81000.00"), stop_price=Decimal("81200.00")
    )
    assert order_body(stop)["stop_Price"] == "81200.00"
    with pytest.raises(ValueError):
        order_body(limit_buy(type="STOP_MARKET", price=None))


def test_json_safe() -> None:
    assert json_safe({"a": Decimal("1.50"), "b": [Decimal("2E+2")], "c": None}) == {
        "a": "1.50",
        "b": ["200"],
        "c": None,
    }


# ---------------------------------------------------------------- HTTP: market data + fallbacks


async def test_get_markets_falls_back_to_hector(api: respx.Router, client: WallexREST) -> None:
    v1 = api.get("/v1/markets").mock(return_value=fail(404, "not found"))
    hector = api.get("/hector/web/v1/markets").mock(return_value=reply("markets_hector"))
    markets = await client.get_markets()
    assert v1.call_count == 1 and hector.call_count == 1
    assert set(markets) == {"BTCTMN", "BTCUSDT", "USDTTMN"}


async def test_get_markets_outage_does_not_fall_back_to_hector(api: respx.Router, make) -> None:
    client = make(max_retries=0)
    v1 = api.get("/v1/markets").mock(return_value=httpx.Response(503))
    hector = api.get("/hector/web/v1/markets").mock(return_value=reply("markets_hector"))
    with pytest.raises(WallexAPIError) as info:
        await client.get_markets()  # hector has no minQty/minNotional/isMarketTypeEnable: never on a 5xx
    assert info.value.status == 503 and v1.call_count == 1 and not hector.called
    api.get("/v1/markets").mock(side_effect=httpx.ReadTimeout("timed out"))
    with pytest.raises(WallexAPIError):
        await client.get_markets()
    assert not hector.called


async def test_get_markets_with_fees(api: respx.Router, client: WallexREST) -> None:
    api.get("/v1/markets").mock(return_value=reply("markets"))
    fee = api.get("/v1/account/fee").mock(return_value=reply("fee"))
    markets = await client.get_markets(with_fees=True)
    assert fee.called and markets["ETHUSDT"].taker_fee == Decimal("0.0011")


async def test_get_all_depth_falls_back_and_remembers(api: respx.Router, client: WallexREST) -> None:
    v2 = api.get("/v2/depth/all").mock(return_value=fail(404))
    v1 = api.get("/depth/all").mock(return_value=reply("depth_all"))
    books = await client.get_all_depth()
    assert books["BTCTMN"].best_bid == Decimal("22075699081")
    await client.get_all_depth()
    assert v2.call_count == 1  # not tried again once /depth/all worked
    assert v1.call_count == 2


async def test_public_requests_send_expected_params(api: respx.Router, client: WallexREST) -> None:
    depth = api.get("/v1/depth").mock(return_value=reply("depth"))
    trades = api.get("/v1/trades").mock(return_value=reply("trades"))
    udf = api.get("/v1/udf/history").mock(return_value=reply("udf"))
    book = await client.get_depth("BTCTMN", levels=2)
    assert len(book.bids) == 2 and book.ts == T0
    assert dict(depth.calls.last.request.url.params) == {"symbol": "BTCTMN"}
    assert len(await client.get_trades("BTCTMN")) == 3
    assert dict(trades.calls.last.request.url.params) == {"symbol": "BTCTMN"}
    candles = await client.get_candles("BTCUSDT", "15", 1792661400.7, 1792663200.2)
    assert len(candles) == 3
    assert dict(udf.calls.last.request.url.params) == {
        "symbol": "BTCUSDT",
        "resolution": "15",
        "from": "1792661400",
        "to": "1792663200",
    }


# ---------------------------------------------------------------- HTTP: retries and errors


async def test_429_backoff_honours_retry_after(api: respx.Router, client: WallexREST, ft: FakeTime) -> None:
    route = api.get("/v1/depth").mock(
        side_effect=[fail(429, "too many requests", headers={"Retry-After": "3"}), reply("depth")]
    )
    book = await client.get_depth("BTCTMN")
    assert book.best_ask == Decimal("22106074058")
    assert route.call_count == 2
    assert ft.sleeps == [3.0]


async def test_5xx_retry_then_success(api: respx.Router, client: WallexREST, ft: FakeTime) -> None:
    route = api.get("/v1/depth").mock(side_effect=[httpx.Response(503), httpx.Response(502), reply("depth")])
    await client.get_depth("BTCTMN")
    assert route.call_count == 3
    assert len(ft.sleeps) == 2
    assert 1 <= ft.sleeps[0] < 1.25 and 2 <= ft.sleeps[1] < 2.25  # 1 s, 2 s + small jitter
    assert client.ok and client.consecutive_errors == 0 and client.errors_total == 2
    assert client.last_ok_ts == T0


async def test_timeout_is_retried(api: respx.Router, client: WallexREST, ft: FakeTime) -> None:
    route = api.get("/v1/depth").mock(side_effect=[httpx.ConnectTimeout("timed out"), reply("depth")])
    await client.get_depth("BTCTMN")
    assert route.call_count == 2 and len(ft.sleeps) == 1


async def test_retries_exhausted_raise(api: respx.Router, client: WallexREST, ft: FakeTime) -> None:
    route = api.get("/v1/depth").mock(return_value=httpx.Response(503, text="<html>bad gateway</html>"))
    with pytest.raises(WallexAPIError) as info:
        await client.get_depth("BTCTMN")
    assert info.value.status == 503 and info.value.retryable
    assert route.call_count == client.max_retries + 1 == 5
    assert [int(s) for s in ft.sleeps] == [1, 2, 4, 8]
    assert not client.ok and client.consecutive_errors == 5 and "503" in client.last_error


async def test_success_false_raises_without_retry(
    api: respx.Router, client: WallexREST, ft: FakeTime
) -> None:
    route = api.get("/v1/depth").mock(
        return_value=httpx.Response(200, json={"success": False, "message": "Invalid symbol", "code": 422})
    )
    with pytest.raises(WallexAPIError) as info:
        await client.get_depth("NOPE")
    assert (info.value.status, info.value.code, info.value.message) == (200, "422", "Invalid symbol")
    assert route.call_count == 1 and ft.sleeps == []


async def test_4xx_raises_immediately(api: respx.Router, client: WallexREST, ft: FakeTime) -> None:
    route = api.get("/v1/account/fee").mock(return_value=fail(401, "Unauthenticated"))
    with pytest.raises(WallexAPIError) as info:
        await client.get_fees()
    assert info.value.status == 401 and info.value.message == "Unauthenticated"
    assert route.call_count == 1 and ft.sleeps == []


async def test_missing_api_key_never_sends_account_calls(api: respx.Router, make) -> None:
    route = api.get("/v1/account/balances").mock(return_value=reply("balances"))
    with pytest.raises(WallexAPIError) as info:
        await make(api_key="").get_balances()
    assert info.value.code == "no_api_key" and not route.called


# ---------------------------------------------------------------- HTTP: orders


async def test_place_order_limit_body_exact(
    api: respx.Router, client: WallexREST, events: list[Event]
) -> None:
    post = api.post("/v1/account/orders").mock(return_value=reply("order_new", 201))
    req = limit_buy()
    order = await client.place_order(req)
    sent = json.loads(post.calls.last.request.content)
    assert sent == {
        "symbol": "BTCUSDT",
        "type": "LIMIT",
        "side": "BUY",
        "quantity": "0.000150",
        "price": "82845.00",
        "client_id": CID,
    }
    assert all(isinstance(v, str) for v in sent.values())
    assert order.req is req and order.status == "NEW"
    assert [e[0] for e in events] == ["place", "response"]
    assert events[1][3]["http_status"] == 201
    json.dumps(events)  # JSON-safe


async def test_place_order_stop_limit_sends_stop_price(api: respx.Router, client: WallexREST) -> None:
    post = api.post("/v1/account/orders").mock(return_value=reply("order_new", 201))
    req = limit_buy(side="SELL", type="STOP_LIMIT", price=Decimal("81000.00"), stop_price=Decimal("81200.00"))
    await client.place_order(req)
    sent = json.loads(post.calls.last.request.content)
    assert sent["stop_Price"] == "81200.00" and sent["type"] == "STOP_LIMIT" and sent["price"] == "81000.00"


async def test_place_order_market_omits_price_and_stop(api: respx.Router, client: WallexREST) -> None:
    post = api.post("/v1/account/orders").mock(return_value=reply("order_new", 200))
    await client.place_order(limit_buy(type="MARKET", price=None))
    sent = json.loads(post.calls.last.request.content)
    assert "price" not in sent and "stop_Price" not in sent and sent["type"] == "MARKET"


async def test_place_order_resends_after_timeout_when_not_found(
    api: respx.Router, client: WallexREST, events: list[Event]
) -> None:
    post = api.post("/v1/account/orders").mock(
        side_effect=[httpx.ReadTimeout("timed out"), reply("order_new", 201)]
    )
    get = api.get(ORDER_URL).mock(return_value=fail(404, "order not found"))
    req = limit_buy()
    order = await client.place_order(req)
    assert post.call_count == 2 and get.call_count == 1
    assert order.req is req and order.status == "NEW"
    assert [e[0] for e in events] == ["place", "timeout", "status", "place", "response"]
    assert events[2][3]["found"] is False
    assert json.loads(post.calls[0].request.content) == json.loads(post.calls[1].request.content)


async def test_place_order_returns_existing_after_timeout(api: respx.Router, client: WallexREST) -> None:
    post = api.post("/v1/account/orders").mock(side_effect=httpx.ReadTimeout("timed out"))
    get = api.get(ORDER_URL).mock(return_value=reply("order_new"))
    req = limit_buy()
    order = await client.place_order(req)
    assert post.call_count == 1 and get.call_count == 1
    assert order.req is req and order.client_id == CID and order.status == "NEW"


async def test_place_order_after_5xx_checks_before_resend(api: respx.Router, client: WallexREST) -> None:
    post = api.post("/v1/account/orders").mock(return_value=httpx.Response(502))
    api.get(ORDER_URL).mock(return_value=reply("order_new"))
    order = await client.place_order(limit_buy())
    assert post.call_count == 1 and order.status == "NEW"


async def test_place_order_4xx_rejected_without_retry(
    api: respx.Router, client: WallexREST, ft: FakeTime, events: list[Event]
) -> None:
    post = api.post("/v1/account/orders").mock(return_value=fail(400, "Insufficient balance"))
    get = api.get(ORDER_URL).mock(return_value=reply("order_new"))
    with pytest.raises(OrderRejected) as info:
        await client.place_order(limit_buy())
    assert info.value.status == 400 and info.value.message == "Insufficient balance"
    assert post.call_count == 1 and not get.called and ft.sleeps == []
    assert [e[0] for e in events] == ["place", "error"]


async def test_place_order_duplicate_on_resend_returns_registered_order(
    api: respx.Router, client: WallexREST
) -> None:
    post = api.post("/v1/account/orders").mock(
        side_effect=[httpx.ReadTimeout("timed out"), fail(400, "duplicate client_id")]
    )
    get = api.get(ORDER_URL).mock(side_effect=[fail(404), reply("order_new")])
    order = await client.place_order(limit_buy())
    assert post.call_count == 2 and get.call_count == 2 and order.status == "NEW"


async def test_place_order_duplicate_refusal_with_no_order_found_is_ambiguous(
    api: respx.Router, client: WallexREST, events: list[Event]
) -> None:
    post = api.post("/v1/account/orders").mock(
        side_effect=[httpx.ReadTimeout("timed out"), fail(400, "duplicate client_id")]
    )
    get = api.get(ORDER_URL).mock(return_value=fail(404, "order not found"))  # not indexed yet
    with pytest.raises(WallexAPIError) as info:
        await client.place_order(limit_buy())
    assert not isinstance(info.value, OrderRejected)  # an order may exist: the caller must reconcile
    assert info.value.code == "ambiguous" and info.value.status == 400
    assert post.call_count == 2 and get.call_count == 2
    assert events[-1][0] == "error" and events[-1][3]["ambiguous"] is True


async def test_place_order_again_after_state_unknown_looks_up_first(api: respx.Router, make) -> None:
    client = make(max_retries=0)
    req = limit_buy()
    post = api.post("/v1/account/orders").mock(return_value=httpx.Response(503))
    get = api.get(ORDER_URL).mock(return_value=fail(404))
    with pytest.raises(WallexAPIError):
        await client.place_order(req)  # state unknown
    assert post.call_count == 1 and get.call_count == 1

    get.mock(return_value=reply("order_new"))  # it did register after all
    order = await client.place_order(req)
    assert order.status == "NEW" and post.call_count == 1  # found by the lookup, not sent again

    other = make(max_retries=0)
    other.register_request(req)  # e.g. restored from the store after a restart
    get.mock(return_value=fail(404))
    post.mock(return_value=fail(400, "duplicate client_id"))
    with pytest.raises(WallexAPIError) as info:
        await other.place_order(req)
    assert not isinstance(info.value, OrderRejected) and info.value.code == "ambiguous"


async def test_place_order_unreadable_2xx_is_looked_up_not_rejected(api: respx.Router, client: WallexREST) -> None:
    html = httpx.Response(201, text="<html><body>gateway says ok</body></html>", headers={"Content-Type": "text/html"})
    post = api.post("/v1/account/orders").mock(return_value=html)
    get = api.get(ORDER_URL).mock(return_value=reply("order_new"))
    order = await client.place_order(limit_buy())
    assert order.status == "NEW" and post.call_count == 1 and get.call_count == 1


async def test_unreadable_2xx_get_is_retryable(api: respx.Router, make) -> None:
    client = make(max_retries=0)
    api.get("/v1/markets").mock(return_value=httpx.Response(200, text="<html>proxy</html>"))
    with pytest.raises(WallexAPIError) as info:
        await client.get_markets()
    assert info.value.retryable and info.value.code == "bad_json" and "HTTP 200" in info.value.message


async def test_place_order_decoding_error_is_ambiguous_and_looked_up(
    api: respx.Router, client: WallexREST, events: list[Event]
) -> None:
    def corrupt(request: httpx.Request) -> httpx.Response:
        raise httpx.DecodingError("Error -3 while decompressing data", request=request)

    post = api.post("/v1/account/orders").mock(side_effect=[corrupt, reply("order_new", 201)])
    get = api.get(ORDER_URL).mock(return_value=fail(404))
    order = await client.place_order(limit_buy())
    assert order.status == "NEW" and post.call_count == 2 and get.call_count == 1
    assert [e[0] for e in events] == ["place", "timeout", "status", "place", "response"]
    assert events[1][3]["code"] == "bad_body"


async def test_place_order_gives_up_after_max_retries(api: respx.Router, make, ft: FakeTime) -> None:
    client = make(max_retries=2)
    post = api.post("/v1/account/orders").mock(return_value=httpx.Response(503))
    api.get(ORDER_URL).mock(return_value=fail(404))
    with pytest.raises(WallexAPIError) as info:
        await client.place_order(limit_buy())
    assert not isinstance(info.value, OrderRejected) and info.value.status == 503
    assert post.call_count == 3


async def test_get_order_and_cancel(api: respx.Router, client: WallexREST, events: list[Event]) -> None:
    api.get(ORDER_URL).mock(side_effect=[fail(404), reply("order_new")])
    assert await client.get_order(CID) is None
    order = await client.get_order(CID)
    assert order is not None and order.req.strategy == "unknown"  # not placed by this process

    cancel = api.delete(ORDER_URL).mock(side_effect=[reply("order_canceled"), fail(404)])
    canceled = await client.cancel_order(CID)
    assert canceled is not None and canceled.status == "CANCELED"
    assert cancel.calls.last.request.headers["X-API-Key"] == KEY
    assert await client.cancel_order(CID) is None
    assert [e[0] for e in events] == ["cancel", "cancel_response", "cancel", "cancel_response"]


async def test_get_order_reports_an_unknown_status_with_the_raw_order(
    api: respx.Router, client: WallexREST, events: list[Event]
) -> None:
    payload = load("order_new")
    payload["result"]["status"] = "TRIGGER_PENDING"
    api.get(ORDER_URL).mock(return_value=httpx.Response(200, json=json_safe(payload)))
    order = await client.get_order(CID)
    assert order is not None and order.status == "UNKNOWN"
    action, client_id, symbol, data = events[-1]
    assert (action, client_id, symbol) == ("status", CID, "BTCUSDT")
    assert data["raw_status"] == "TRIGGER_PENDING" and data["order"]["status"] == "TRIGGER_PENDING"


async def test_get_order_attaches_known_request(api: respx.Router, client: WallexREST) -> None:
    req = limit_buy()
    client.register_request(req)
    api.get(ORDER_URL).mock(return_value=reply("order_new"))
    order = await client.get_order(CID)
    assert order is not None and order.req is req


async def test_cancel_refused_returns_current_state(api: respx.Router, client: WallexREST) -> None:
    cid = "EX_BTCTMN_1792663260000_C3D4"
    api.delete(f"/v1/account/orders/{cid}").mock(return_value=fail(400, "order is already finished"))
    api.get(f"/v1/account/orders/{cid}").mock(return_value=reply("order_filled"))
    order = await client.cancel_order(cid)
    assert order is not None and order.status == "FILLED"


async def test_get_open_orders_flattens_mapping(api: respx.Router, client: WallexREST) -> None:
    route = api.get("/v1/account/openOrders").mock(return_value=reply("open_orders"))
    orders = await client.get_open_orders()
    assert [(o.client_id, o.req.strategy) for o in orders] == [(CID, "unknown"), ("abcXYZ123", "foreign")]
    assert orders[1].req.type == "STOP_LIMIT" and orders[1].req.stop_price == Decimal("27100000")
    assert dict(route.calls.last.request.url.params) == {"page": "1", "per_page": "100"}
    only_sol = await client.get_open_orders("SOLTMN")
    assert [o.client_id for o in only_sol] == ["abcXYZ123"]
    assert route.calls.last.request.url.params["symbol"] == "SOLTMN"


async def test_get_open_orders_paginates(
    api: respx.Router, client: WallexREST, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(wallex_rest, "OPEN_ORDERS_PER_PAGE", 2)
    template = load("open_orders")["result"]["additionalProp1"][0]

    def page(request: httpx.Request) -> httpx.Response:
        n = int(request.url.params["page"])
        count = 2 if n < 3 else 1
        items = [{**template, "clientOrderId": f"o{n}_{i}"} for i in range(count)]
        return httpx.Response(200, json=json_safe({"success": True, "result": {"orders": items}}))

    route = api.get("/v1/account/openOrders").mock(side_effect=page)
    orders = await client.get_open_orders()
    assert route.call_count == 3
    assert [o.client_id for o in orders] == ["o1_0", "o1_1", "o2_0", "o2_1", "o3_0"]


async def test_get_my_trades_since(api: respx.Router, client: WallexREST) -> None:
    route = api.get("/v1/account/trades").mock(return_value=reply("my_trades"))
    fills = await client.get_my_trades("BTCTMN", since_ts=T0 + 61)
    assert [f.qty for f in fills] == [Decimal("0.000800")]
    assert dict(route.calls.last.request.url.params) == {"symbol": "BTCTMN"}


# ---------------------------------------------------------------- rate limiting


async def test_token_bucket_spacing_with_fake_clock(ft: FakeTime) -> None:
    bucket = TokenBucket(rate=2, sleep=ft.sleep, monotonic=ft.monotonic)
    waits = [await bucket.acquire() for _ in range(5)]
    assert waits == [0.0, 0.0, 0.5, 0.5, 0.5]  # burst of 2, then one token every 0.5 s
    assert ft.sleeps == [0.5, 0.5, 0.5]
    ft.t += 10  # idle time refills at most `burst` tokens
    assert [await bucket.acquire() for _ in range(3)] == [0.0, 0.0, 0.5]


async def test_client_requests_are_rate_limited(api: respx.Router, make, ft: FakeTime) -> None:
    client = make(rps=2)
    api.get("/v1/depth").mock(return_value=reply("depth"))
    for _ in range(4):
        await client.get_depth("BTCTMN")
    assert ft.sleeps == [0.5, 0.5]


# ---------------------------------------------------------------- headers, accounts, secrets


async def test_auth_headers_only_on_account_calls(api: respx.Router, make) -> None:
    client = make(sub_account_client_id="sub-42")
    depth = api.get("/v1/depth").mock(return_value=reply("depth"))
    detail = api.get("/v1/account/balances-detail").mock(return_value=reply("balances_detail"))
    await client.get_depth("BTCTMN")
    balances = await client.get_balances()
    public, private = depth.calls.last.request.headers, detail.calls.last.request.headers
    assert "x-api-key" not in public and "sub-account-client-id" not in public
    assert private["x-api-key"] == KEY and private["sub-account-client-id"] == "sub-42"
    assert public["user-agent"] == private["user-agent"] == "wta/0.1"
    assert balances["TMN"].free == Decimal("4750000")


async def test_no_sub_account_header_without_config(api: respx.Router, client: WallexREST) -> None:
    route = api.get("/v1/account/balances").mock(return_value=reply("balances"))
    await client.get_balances()
    assert "sub-account-client-id" not in route.calls.last.request.headers


async def test_sub_account_balances_fall_back_to_main_endpoint(api: respx.Router, make) -> None:
    client = make(sub_account_client_id="sub-42")
    detail = api.get("/v1/account/balances-detail").mock(return_value=fail(404))
    main = api.get("/v1/account/balances").mock(return_value=reply("balances"))
    balances = await client.get_balances()
    assert detail.called and main.called
    assert main.calls.last.request.headers["sub-account-client-id"] == "sub-42"
    assert balances["USDT"].free == Decimal("1.25")


@pytest.mark.parametrize(
    "failure",
    [httpx.ReadTimeout("timed out"), httpx.ConnectError("refused"), httpx.Response(503), fail(429), fail(401, "bad key")],
)
async def test_sub_account_balances_never_fall_back_on_other_failures(api: respx.Router, make, failure: Any) -> None:
    client = make(sub_account_client_id="sub-42", max_retries=0)
    kw = {"side_effect": failure} if isinstance(failure, Exception) else {"return_value": failure}
    detail = api.get("/v1/account/balances-detail").mock(**kw)
    main = api.get("/v1/account/balances").mock(return_value=reply("balances"))
    with pytest.raises(WallexAPIError):
        await client.get_balances()  # the main wallet is another account: an error, never its balances
    assert detail.called and not main.called


async def test_sub_account_empty_detail_answer_is_returned_as_is(api: respx.Router, make) -> None:
    client = make(sub_account_client_id="sub-42")
    api.get("/v1/account/balances-detail").mock(return_value=httpx.Response(200, json={"result": {}, "success": True}))
    main = api.get("/v1/account/balances").mock(return_value=reply("balances"))
    assert await client.get_balances() == {}
    assert not main.called


async def test_get_sub_accounts_tries_known_paths(api: respx.Router, client: WallexREST) -> None:
    first = api.get("/sub-accounts").mock(return_value=fail(404))
    second = api.get("/v1/sub-accounts").mock(return_value=reply("sub_accounts"))
    subs = await client.get_sub_accounts()
    assert first.called and second.called
    assert subs[0]["clientId"] == "c0ffee00-1111-2222-3333-444455556666"
    assert second.calls.last.request.headers["x-api-key"] == KEY


async def test_raw_request(api: respx.Router, client: WallexREST) -> None:
    route = api.get("/v1/account/fee").mock(return_value=reply("fee"))
    body = await client.request("GET", "/v1/account/fee")
    assert body["result"]["BTCTMN"]["makerFeeRate"] == "0.0025"
    assert route.calls.last.request.headers["x-api-key"] == KEY  # account path -> authenticated


async def test_api_key_never_leaks(api: respx.Router, client: WallexREST, events: list[Event]) -> None:
    echo = {"success": False, "message": f"invalid api key {KEY}", "result": {"key": KEY}}
    api.post("/v1/account/orders").mock(return_value=httpx.Response(403, json=echo))
    api.get("/v1/account/balances").mock(return_value=httpx.Response(401, json=echo))
    with pytest.raises(OrderRejected) as rejected:
        await client.place_order(limit_buy())
    with pytest.raises(WallexAPIError) as failed:
        await client.get_balances()
    for err in (rejected.value, failed.value):
        assert KEY not in str(err) and KEY not in repr(err)
        assert KEY not in json.dumps(json_safe(err.payload))
        assert "***" in err.message
    assert KEY not in json.dumps(events)
    assert KEY not in client.last_error and KEY not in repr(client)
    assert client.api_key == "***" and client.has_api_key


async def test_key_is_masked_before_a_non_json_body_is_cut(api: respx.Router, make) -> None:
    client = make(max_retries=0)
    text = "x" * (200 - len(KEY) // 2) + KEY + " trailing"  # the 200-char cut falls inside the key
    api.get("/v1/account/fee").mock(return_value=httpx.Response(502, text=text))
    with pytest.raises(WallexAPIError) as info:
        await client.get_fees()
    for leak in (KEY, KEY[: len(KEY) // 2]):
        assert leak not in str(info.value) and leak not in str(info.value.payload) and leak not in client.last_error


async def test_transport_error_message_is_masked(api: respx.Router, make) -> None:
    client = make(max_retries=0)
    api.get("/v1/account/fee").mock(side_effect=httpx.ConnectError(f"refused for {KEY}"))
    with pytest.raises(WallexAPIError) as info:
        await client.get_fees()
    assert info.value.code == "transport" and KEY not in str(info.value)

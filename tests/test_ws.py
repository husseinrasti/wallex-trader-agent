"""WallexWS: pure parsing, backoff rules, and end-to-end runs against a local Centrifugo-like server.

No test touches the network: servers listen on 127.0.0.1 (port 0) or the connection is a fake.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from websockets.asyncio.server import ServerConnection, serve

from wta.exchange.base import MarketDataBus
from wta.exchange.wallex_ws import SessionEnd, WallexWS, build_channels, parse_levels, parse_messages
from wta.models import Level, Trade
from wta.utils.timeutil import SimClock

WS_LOGGER = "wta.exchange.wallex_ws"
CONNECT_REPLY = '{"id":1,"connect":{"client":"c-1","ping":25,"pong":true}}'


def push(channel: str, data: Any) -> str:
    return json.dumps({"push": {"channel": channel, "pub": {"data": data}}})


def depth(levels: list[tuple[str, str]]) -> str:
    """Depth payload as raw JSON numbers (so we can check exact Decimal parsing)."""
    return "[" + ",".join(f'{{"price":{p},"quantity":{q},"sum":1}}' for p, q in levels) + "]"


def depth_frame(symbol: str, kind: str, levels: list[tuple[str, str]]) -> str:
    return f'{{"push":{{"channel":"orderbook:{symbol}@{kind}","pub":{{"data":{depth(levels)}}}}}}}'


def make_client(bus: MarketDataBus | None = None, **kw: Any) -> WallexWS:
    return WallexWS("ws://127.0.0.1:1/connection/websocket", ["BTCUSDT"], bus or MarketDataBus(),
                    clock=kw.pop("clock", SimClock(1000.0)), **kw)


class FakeWS:
    """Minimal connection: ``recv`` serves queued frames, then blocks; ``send`` records."""

    def __init__(self, frames: list[str] | None = None) -> None:
        self.sent: list[str] = []
        self.frames: asyncio.Queue[str] = asyncio.Queue()
        for f in frames or []:
            self.frames.put_nowait(f)

    async def send(self, message: str) -> None:
        self.sent.append(message)

    async def recv(self) -> str:
        return await self.frames.get()

    async def __aenter__(self) -> FakeWS:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None


async def wait_until(cond, timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    while not cond():
        assert time.monotonic() < deadline, "condition not met in time"
        await asyncio.sleep(0.01)


# ---------------------------------------------------------------- pure parsing


def test_parse_messages_splits_multi_message_frames():
    frame = (
        '{}\n{"id":2,"subscribe":{}}\n\n'
        '{"id":3,"error":{"code":102,"message":"unknown channel"}}\n'
        '{"push":{"channel":"public:BTCUSDT@trade","pub":{"data":{"price":82131.25}}}}\n'
        "not json\n[1,2]\n"
    )
    msgs = parse_messages(frame)
    assert len(msgs) == 4
    assert msgs[0] == {}  # ping
    assert msgs[1] == {"id": 2, "subscribe": {}}
    assert msgs[2]["error"]["code"] == 102
    price = msgs[3]["push"]["pub"]["data"]["price"]
    assert isinstance(price, Decimal) and price == Decimal("82131.25")
    assert parse_messages(b'{"id":1,"connect":{}}') == [{"id": 1, "connect": {}}]


def test_depth_prices_are_exact_decimals():
    client = make_client()
    frame = "\n".join([
        depth_frame("BTCTMN", "buyDepth", [("22075699081", "0.000123")]),
        depth_frame("BTCTMN", "sellDepth", [("22106074058.5", "1")]),
    ])
    for msg in parse_messages(frame):
        client.handle_push(msg["push"]["channel"], msg["push"]["pub"]["data"])
    book = client.bus.latest("BTCTMN")
    assert book is not None
    assert book.bids[0].price == Decimal("22075699081") and isinstance(book.bids[0].price, Decimal)
    assert str(book.bids[0].qty) == "0.000123"
    assert str(book.asks[0].price) == "22106074058.5"

    msg = parse_messages(depth_frame("BTCUSDT", "buyDepth", [("82131.25", "0.5")]))[0]
    level = parse_levels(msg["push"]["pub"]["data"], 10, descending=True)[0]
    assert level == Level(price=Decimal("82131.25"), qty=Decimal("0.5"))
    assert str(level.price) == "82131.25"


def test_depth_publishes_only_with_both_sides_and_replaces_each_side():
    clock = SimClock(1000.0)
    bus = MarketDataBus()
    client = make_client(bus, clock=clock, levels=3)
    bids = [("82100", "1"), ("82131.25", "0.5"), ("82000", "0"), ("82050", "2"), ("81900", "3")]
    client.handle_push("orderbook:BTCUSDT@buyDepth", json.loads(depth(bids), parse_float=Decimal))
    assert bus.latest("BTCUSDT") is None  # sell side not seen yet

    clock.set(1001.0)
    asks = [("82200", "1"), ("82140", "0.25"), ("82300", "0")]
    client.handle_push("orderbook:BTCUSDT@sellDepth", json.loads(depth(asks), parse_float=Decimal))
    book = bus.latest("BTCUSDT")
    assert book is not None and book.source == "ws" and book.ts == 1001.0
    assert [lv.price for lv in book.bids] == [Decimal("82131.25"), Decimal("82100"), Decimal("82050")]
    assert [lv.price for lv in book.asks] == [Decimal("82140"), Decimal("82200")]  # zero qty dropped
    assert book.best_bid == Decimal("82131.25") and book.best_ask == Decimal("82140")

    clock.set(1002.0)
    client.handle_push("orderbook:BTCUSDT@buyDepth", [{"price": 82120, "quantity": 7, "sum": 1}])
    book = bus.latest("BTCUSDT")
    assert book is not None and book.ts == 1002.0
    assert book.bids == [Level(price=Decimal("82120"), qty=Decimal("7"))]  # replaced, not merged
    assert [lv.price for lv in book.asks] == [Decimal("82140"), Decimal("82200")]


def test_crossed_book_is_not_published():
    bus = MarketDataBus()
    client = make_client(bus)
    client.handle_push("orderbook:BTCUSDT@buyDepth", [{"price": 100, "quantity": 1}])
    client.handle_push("orderbook:BTCUSDT@sellDepth", [{"price": 99, "quantity": 1}])
    assert bus.latest("BTCUSDT") is None
    assert client.crossed_skipped == 1


def test_trade_side_mapping_and_timestamps():
    clock = SimClock(5000.0)
    bus = MarketDataBus()
    trades: list[Trade] = []
    bus.on_trade(trades.append)
    client = make_client(bus, clock=clock)
    frame = "\n".join([
        '{"push":{"channel":"public:BTCUSDT@trade","pub":{"data":{"isBuyOrder":true,"quantity":0.01,'
        '"price":82131.25,"timestamp":"2026-10-22T10:00:00.250Z"}}}}',
        push("public:BTCUSDT@trade", {"isBuyOrder": False, "quantity": 2, "price": 82140}),
        push("public:BTCUSDT@trade", {"quantity": 1, "price": 1, "timestamp": "garbage"}),
    ])
    for msg in parse_messages(frame):
        client.handle_push(msg["push"]["channel"], msg["push"]["pub"]["data"])

    assert [t.side for t in trades] == ["SELL", "BUY", None]  # isBuyOrder=true: maker bought, taker sold
    assert trades[0].price == Decimal("82131.25") and trades[0].qty == Decimal("0.01")
    assert trades[0].ts == datetime(2026, 10, 22, 10, 0, 0, 250000, tzinfo=UTC).timestamp()
    assert trades[1].ts == 5000.0 and trades[2].ts == 5000.0  # missing / bad timestamp -> clock
    assert trades[1].qty == Decimal("2") and trades[0].symbol == "BTCUSDT"


def test_bad_payloads_are_counted_not_raised():
    client = make_client()
    client.handle_push("orderbook:BTCUSDT@buyDepth", "garbage")
    client.handle_push("orderbook:BTCUSDT@sellDepth", [{"price": "abc", "quantity": 1}])
    client.handle_push("public:BTCUSDT@trade", {"isBuyOrder": True})
    client.handle_push("candle:BTCUSDT@1m", {"o": 1})  # unknown channel: ignored, not an error
    assert client.parse_errors == 3
    assert client.bus.latest("BTCUSDT") is None


async def test_handle_message_ping_connect_ack_and_error(caplog):
    client = WallexWS("ws://127.0.0.1:1/ws", ["BTCUSDT", "ETHUSDT"], MarketDataBus(), clock=SimClock(1.0))
    ws = FakeWS()

    await client.handle_message(ws, {})
    assert ws.sent == ["{}"]  # ping -> pong

    await client.handle_message(ws, json.loads(CONNECT_REPLY))
    assert client.connected
    subs = [json.loads(m) for m in ws.sent[1:]]
    assert [m["id"] for m in subs] == [2, 3, 4, 5, 6, 7]
    assert [m["subscribe"]["channel"] for m in subs] == [
        "orderbook:BTCUSDT@buyDepth", "orderbook:BTCUSDT@sellDepth", "public:BTCUSDT@trade",
        "orderbook:ETHUSDT@buyDepth", "orderbook:ETHUSDT@sellDepth", "public:ETHUSDT@trade",
    ]

    await client.handle_message(ws, {"id": 2, "subscribe": {}})
    with caplog.at_level(logging.ERROR, logger=WS_LOGGER):
        await client.handle_message(ws, {"id": 3, "error": {"code": 103, "message": "permission denied"}})
    assert "subscribe orderbook:BTCUSDT@sellDepth failed" in caplog.text
    assert len(ws.sent) == 7  # acks and errors are not answered


def test_channel_cap_drops_whole_symbols(caplog):
    symbols = [f"C{i:02d}USDT" for i in range(30)]
    kept, dropped = build_channels(symbols, ("buyDepth", "sellDepth", "trade"))
    assert len(kept) == 63 and len(dropped) == 27  # 21 whole symbols fit in 64 channels
    assert kept[-1] == "public:C20USDT@trade" and dropped[0] == "orderbook:C21USDT@buyDepth"

    with caplog.at_level(logging.WARNING, logger=WS_LOGGER):
        client = WallexWS("ws://127.0.0.1:1/ws", symbols, MarketDataBus())
    assert client.channels == kept and client.dropped_channels == dropped
    assert "dropped 27 channels" in caplog.text and "C29USDT" in caplog.text

    try:
        build_channels(["BTCUSDT"], ("marketCap",))
    except ValueError as exc:
        assert "marketCap" in str(exc)
    else:
        raise AssertionError("unknown channel kind must be rejected")


def test_backoff_doubles_caps_and_resets():
    client = make_client(reconnect_max_s=8)
    closed = SessionEnd(3005, "closed")
    assert [client.next_delay(closed, lived_s=1) for _ in range(5)] == [1, 2, 4, 8, 8]
    assert client.next_delay(closed, lived_s=61) == 1  # a long-lived connection resets the backoff
    assert client.next_delay(SessionEnd(None, "max_age"), lived_s=1740) == 0


def test_ok_needs_connection_and_fresh_messages():
    clock = SimClock(100.0)
    client = make_client(clock=clock, stale_s=30)
    assert not client.ok
    client.connected = True
    client.last_msg_ts = 100.0
    assert client.ok
    clock.advance(31)
    assert not client.ok
    assert client.stats()["connected"] is True


# ---------------------------------------------------------------- integration (local server)


async def _handshake(conn: ServerConnection, n_channels: int) -> list[dict[str, Any]]:
    """Read connect, reply, read the subscribes, ack them in ONE multi-message frame."""
    received = [json.loads(await conn.recv())]
    await conn.send(CONNECT_REPLY)
    for _ in range(n_channels):
        received.append(json.loads(await conn.recv()))
    await conn.send("\n".join(json.dumps({"id": m["id"], "subscribe": {}}) for m in received[1:]))
    return received


async def test_run_against_local_server_reconnects_and_resubscribes():
    symbols = ["BTCUSDT", "USDTTMN"]
    expected = {f"orderbook:{s}@buyDepth" for s in symbols} | {f"orderbook:{s}@sellDepth" for s in symbols}
    expected |= {f"public:{s}@trade" for s in symbols}
    bus = MarketDataBus()
    trades: list[Trade] = []
    bus.on_trade(trades.append)
    sessions: list[list[dict[str, Any]]] = []
    pongs: list[str] = []
    resubscribed = asyncio.Event()

    async def handler(conn: ServerConnection) -> None:
        first = not sessions
        received: list[dict[str, Any]] = []
        sessions.append(received)
        received.extend(await _handshake(conn, len(expected)))
        if first:
            await conn.send(depth_frame("BTCUSDT", "buyDepth", [("82131.25", "0.5"), ("82100", "1"), ("82000", "0")]))
            await conn.send(depth_frame("BTCUSDT", "sellDepth", [("82200", "2"), ("82140.5", "0.25")]))
            await conn.send(push("public:BTCUSDT@trade", {"isBuyOrder": True, "quantity": 0.01,
                                                          "price": 82131.25, "timestamp": "2026-10-22T10:00:00Z"}))
            await conn.send("{}")
            pongs.append(await asyncio.wait_for(conn.recv(), 2))
            await conn.close(code=3005, reason="test reconnect")
        else:
            resubscribed.set()
            await conn.wait_closed()

    async with serve(handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        client = WallexWS(f"ws://127.0.0.1:{port}/connection/websocket", symbols, bus, reconnect_max_s=0.05)
        stop = asyncio.Event()
        task = asyncio.create_task(client.run(stop))
        await asyncio.wait_for(resubscribed.wait(), 4)
        assert client.connected and client.ok
        stop.set()
        await asyncio.wait_for(task, 3)

    assert len(sessions) == 2
    for received in sessions:
        assert received[0] == {"id": 1, "connect": {}}
        assert {m["subscribe"]["channel"] for m in received[1:]} == expected
        assert [m["id"] for m in received[1:]] == list(range(2, 2 + len(expected)))
    assert pongs == ["{}"]
    assert client.reconnects >= 1 and client.messages >= 6

    book = bus.latest("BTCUSDT")
    assert book is not None and book.source == "ws"
    assert book.bids == [Level(price=Decimal("82131.25"), qty=Decimal("0.5")),
                         Level(price=Decimal("82100"), qty=Decimal("1"))]
    assert book.asks == [Level(price=Decimal("82140.5"), qty=Decimal("0.25")),
                         Level(price=Decimal("82200"), qty=Decimal("2"))]
    assert len(trades) == 1 and trades[0].side == "SELL" and trades[0].price == Decimal("82131.25")

    assert not client.connected
    assert bus.sources["ws"].ok is False


async def test_non_reconnect_close_code_stops_the_feed_for_good(caplog):
    opened: list[float] = []

    async def handler(conn: ServerConnection) -> None:
        opened.append(time.monotonic())
        await _handshake(conn, 3)
        await conn.close(code=3501, reason="bad request")

    with caplog.at_level(logging.ERROR, logger=WS_LOGGER):
        async with serve(handler, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            client = WallexWS(f"ws://127.0.0.1:{port}/", ["BTCUSDT"], MarketDataBus(), reconnect_max_s=0.05)
            stop = asyncio.Event()
            task = asyncio.create_task(client.run(stop))
            await wait_until(lambda: client.fatal_close is not None)
            await asyncio.sleep(0.5)  # 10x reconnect_max_s: a retrying client would have reconnected
            assert not task.done()  # run() keeps waiting for stop, so a supervisor does not restart it
            stop.set()
            await asyncio.wait_for(task, 3)

    assert len(opened) == 1  # Centrifugo: >= 3500 means "do not reconnect"
    assert client.fatal_close == 3501 and client.stats()["fatal_close"] == 3501
    assert not client.connected and not client.ok
    assert "non-reconnect code 3501" in caplog.text


async def test_retryable_close_code_reconnects():
    opened: list[float] = []
    second = asyncio.Event()

    async def handler(conn: ServerConnection) -> None:
        opened.append(time.monotonic())
        await _handshake(conn, 3)
        if len(opened) == 1:
            await conn.close(code=3499, reason="reconnect please")
        else:
            second.set()
            await conn.wait_closed()

    async with serve(handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        client = WallexWS(f"ws://127.0.0.1:{port}/", ["BTCUSDT"], MarketDataBus(), reconnect_max_s=0.05)
        stop = asyncio.Event()
        task = asyncio.create_task(client.run(stop))
        await asyncio.wait_for(second.wait(), 3)
        stop.set()
        await asyncio.wait_for(task, 3)
    assert len(opened) == 2 and client.fatal_close is None


async def test_proactive_reconnect_when_connection_is_old():
    close_codes: list[int | None] = []
    second = asyncio.Event()

    async def handler(conn: ServerConnection) -> None:
        first = not close_codes and not second.is_set()
        await _handshake(conn, 3)
        if first:
            await conn.wait_closed()
            close_codes.append(conn.close_code)
        else:
            second.set()
            await conn.wait_closed()

    async with serve(handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        client = WallexWS(f"ws://127.0.0.1:{port}/", ["BTCUSDT"], MarketDataBus(),
                          max_conn_age_s=0.3, reconnect_max_s=5)
        stop = asyncio.Event()
        task = asyncio.create_task(client.run(stop))
        await asyncio.wait_for(second.wait(), 3)
        stop.set()
        await asyncio.wait_for(task, 3)

    assert close_codes == [1000]  # the client closed it cleanly, no backoff delay
    assert client.reconnects >= 1


async def test_run_survives_connect_failures_and_stops():
    attempts = 0

    def failing_connect(url: str) -> Any:
        nonlocal attempts
        attempts += 1
        raise OSError("network unreachable")

    bus = MarketDataBus()
    client = WallexWS("wss://api.example.invalid/connection/websocket", ["BTCUSDT"], bus,
                      reconnect_max_s=0.01, connect=failing_connect)
    stop = asyncio.Event()
    task = asyncio.create_task(client.run(stop))
    await wait_until(lambda: attempts >= 3)
    stop.set()
    await asyncio.wait_for(task, 2)  # returned normally, nothing raised
    assert client.reconnects >= 2 and not client.connected
    assert bus.sources["ws"].ok is False and bus.sources["ws"].errors >= 2


async def test_failing_listener_does_not_drop_the_connection():
    bus = MarketDataBus()

    def broken_listener(book: Any) -> None:
        raise RuntimeError("listener bug")

    bus.on_book(broken_listener)
    trades: list[Trade] = []
    bus.on_trade(trades.append)
    fake = FakeWS([
        CONNECT_REPLY,
        depth_frame("BTCUSDT", "buyDepth", [("100", "1")]) + "\n" + depth_frame("BTCUSDT", "sellDepth", [("101", "1")]),
        push("public:BTCUSDT@trade", {"isBuyOrder": False, "quantity": 1, "price": 100}),
    ])
    client = WallexWS("ws://fake/", ["BTCUSDT"], bus, connect=lambda url: fake)
    stop = asyncio.Event()
    task = asyncio.create_task(client.run(stop))
    await wait_until(lambda: len(trades) == 1)
    assert client.reconnects == 0 and client.connected
    assert bus.latest("BTCUSDT") is not None  # stored before the listener raised
    assert fake.sent[0] == '{"id":1,"connect":{}}' and len(fake.sent) == 4
    stop.set()
    await asyncio.wait_for(task, 2)

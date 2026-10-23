"""Wallex WebSocket v2 market-data feed (Centrifugo JSON protocol over plain ``websockets``).

Protocol (docs/WALLEX_API_NOTES.md; v1 socket.io is deprecated, see docs/DECISIONS.md):

* connect to ``wss://api.wallex.ir/connection/websocket`` and send ``{"id":1,"connect":{}}``;
* after the connect reply, send one ``{"id":N,"subscribe":{"channel":...}}`` per channel
  (``orderbook:<SYM>@buyDepth``, ``orderbook:<SYM>@sellDepth``, ``public:<SYM>@trade``; max 64);
* a frame may hold several newline-separated JSON messages;
* ``{}`` is a server ping -> answer ``{}`` at once (the server closes with 3012 after 8 s);
* ``{"push":{"channel":...,"pub":{"data":...}}}`` carries data. A depth push is a FULL snapshot of
  one side (replace, never merge); a trade push is ``{isBuyOrder, quantity, price, timestamp}``.

Books are published to the ``MarketDataBus`` as ``BookSnapshot(source="ws")`` once both sides of a
symbol have been seen on the current connection. ``run()`` reconnects with exponential backoff and
never raises (except ``CancelledError``); the REST poller covers any gap. Close codes 3000-3499 are
retried; a code >= 3500 means "do not reconnect", so the feed stops for good (``fatal_close``) and
``run()`` just waits for its stop event.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Callable, Iterable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, NamedTuple, Protocol
from urllib.parse import urlsplit

from websockets.asyncio.client import connect as ws_connect
from websockets.exceptions import ConnectionClosed, WebSocketException

from wta.exchange.base import MarketDataBus
from wta.models import BookSnapshot, Level, Trade
from wta.utils.decimalutil import D
from wta.utils.timeutil import Clock, SystemClock, parse_iso

log = logging.getLogger(__name__)

SOURCE = "ws"
MAX_CHANNELS = 64  # Centrifugo limit per connection on Wallex
BACKOFF_RESET_AFTER_S = 60.0  # a connection that lived longer than this resets the backoff
FATAL_CLOSE_MIN = 3500  # Centrifugo: close codes >= 3500 must not be retried
CONNECT_ID = 1
CHANNEL_NAMESPACE = {"buyDepth": "orderbook", "sellDepth": "orderbook", "trade": "public"}


class WSConnection(Protocol):
    """The part of a websockets connection this client uses (lets tests inject fakes)."""

    async def send(self, message: str) -> None: ...

    async def recv(self) -> str | bytes: ...


ConnectFn = Callable[[str], AbstractAsyncContextManager[WSConnection]]


def _default_connect(url: str) -> AbstractAsyncContextManager[WSConnection]:
    return ws_connect(url, open_timeout=10, ping_interval=20, ping_timeout=20, close_timeout=5, max_size=2**22)


# ---------------------------------------------------------------- pure helpers


def parse_messages(frame: str | bytes) -> list[dict[str, Any]]:
    """Split one frame into JSON messages (one per line).

    Fractional numbers parse as exact ``Decimal`` (never float); ints stay ints and price/qty
    fields are converted with ``D()`` when the models are built, so every money value is Decimal.

    Undecodable lines and non-object messages are logged and skipped, never raised.
    """
    if isinstance(frame, bytes):
        frame = frame.decode("utf-8", errors="replace")
    out: list[dict[str, Any]] = []
    for line in frame.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line, parse_float=Decimal)
        except json.JSONDecodeError:
            log.warning("ws: undecodable message skipped: %.200s", line)
            continue
        if isinstance(msg, dict):
            out.append(msg)
        else:
            log.debug("ws: non-object message skipped: %.200s", line)
    return out


def channel_name(symbol: str, kind: str) -> str:
    """``("BTCTMN", "buyDepth")`` -> ``"orderbook:BTCTMN@buyDepth"``."""
    return f"{CHANNEL_NAMESPACE[kind]}:{symbol}@{kind}"


def split_channel(channel: str) -> tuple[str, str]:
    """``"orderbook:BTCTMN@buyDepth"`` -> ``("BTCTMN", "buyDepth")``."""
    _, _, rest = channel.partition(":")
    symbol, _, kind = rest.partition("@")
    return symbol, kind


def build_channels(
    symbols: Iterable[str], kinds: Iterable[str], max_channels: int = MAX_CHANNELS
) -> tuple[list[str], list[str]]:
    """All channels for ``symbols`` x ``kinds``, capped at ``max_channels``.

    Whole symbols are dropped (a book with only one side is useless). Returns ``(kept, dropped)``.
    """
    kinds = list(kinds)
    unknown = [k for k in kinds if k not in CHANNEL_NAMESPACE]
    if unknown:
        raise ValueError(f"unsupported channel kinds: {unknown}")
    kept: list[str] = []
    dropped: list[str] = []
    for symbol in dict.fromkeys(s.upper() for s in symbols):  # dedupe, keep order
        chans = [channel_name(symbol, k) for k in kinds]
        if len(kept) + len(chans) <= max_channels:
            kept.extend(chans)
        else:
            dropped.extend(chans)
    return kept, dropped


def unwrap_message(data: Any) -> Any:
    """The live v2 endpoint wraps every channel payload as ``{"message": <payload>}`` (observed on
    2026-10-23: ``{"message": [{price, quantity, sum}, ...]}`` for depth); the docs show the bare payload.
    Both are accepted; a dict that already looks like a trade/level (has ``price``) is left alone."""
    while isinstance(data, dict) and "message" in data and "price" not in data:
        data = data["message"]
    return data


def parse_levels(data: Any, levels: int, descending: bool) -> list[Level]:
    """Depth payload ``[{price, quantity, sum}, ...]`` -> sorted ``Level`` list, zero qty dropped."""
    items = data.values() if isinstance(data, dict) else data
    if not isinstance(items, Iterable) or isinstance(items, str):
        raise TypeError(f"depth data must be a list, got {type(data).__name__}")
    out: list[Level] = []
    for item in items:
        price, qty = D(item["price"]), D(item["quantity"])
        if price > 0 and qty > 0:
            out.append(Level(price=price, qty=qty))
    out.sort(key=lambda lv: lv.price, reverse=descending)
    return out[:levels]


def _safe_url(url: str) -> str:
    """URL without query string or credentials, for logs."""
    parts = urlsplit(url)
    port = f":{parts.port}" if parts.port else ""
    return f"{parts.scheme}://{parts.hostname or ''}{port}{parts.path}"


# ---------------------------------------------------------------- client


@dataclass
class _PartialBook:
    bids: list[Level] | None = None
    asks: list[Level] | None = None


class SessionEnd(NamedTuple):
    """Why one connection ended: close ``code`` (if any) and a short ``reason``."""

    code: int | None
    reason: str  # "closed" | "max_age" | "idle" | "error"


class WallexWS:
    """Depth + trade feed for ``symbols``; publishes to ``bus`` and keeps simple health stats."""

    def __init__(
        self,
        url: str,
        symbols: list[str],
        bus: MarketDataBus,
        clock: Clock | None = None,
        channels: tuple[str, ...] = ("buyDepth", "sellDepth", "trade"),
        levels: int = 10,
        reconnect_max_s: float = 60,
        max_conn_age_s: float = 1740,
        connect: ConnectFn | None = None,
        stale_s: float = 30,
    ) -> None:
        self.url = url
        self.bus = bus
        self.clock: Clock = clock or SystemClock()
        self.levels = levels
        self.reconnect_max_s = max(float(reconnect_max_s), 0.01)  # > 0 so a retry loop can never spin
        self.max_conn_age_s = max_conn_age_s
        self.stale_s = stale_s
        self.idle_timeout_s = 2 * stale_s  # server pings every 25 s, so this long without a message = dead
        self._connect: ConnectFn = connect or _default_connect
        self.channels, self.dropped_channels = build_channels(symbols, channels)
        if self.dropped_channels:
            log.warning(
                "ws: %d-channel cap, dropped %d channels: %s",
                MAX_CHANNELS,
                len(self.dropped_channels),
                ", ".join(self.dropped_channels),
            )

        # stats
        self.connected = False
        self.last_msg_ts: float | None = None
        self.reconnects = 0
        self.messages = 0
        self.parse_errors = 0
        self.crossed_skipped = 0
        self.fatal_close: int | None = None  # close code >= 3500: the feed stopped for good

        self._books: dict[str, _PartialBook] = {}
        self._pending: dict[int, str] = {}  # subscribe id -> channel, until acked
        self._attempt = 0  # consecutive failures, drives the backoff
        self._opened_mono = 0.0
        self._last_recv_mono = 0.0

    # ------------------------------------------------------------ stats

    @property
    def ok(self) -> bool:
        """Connected and heard from the server within ``stale_s``."""
        return (
            self.connected
            and self.last_msg_ts is not None
            and self.clock.now() - self.last_msg_ts <= self.stale_s
        )

    def stats(self) -> dict[str, Any]:
        return {
            "connected": self.connected,
            "ok": self.ok,
            "last_msg_ts": self.last_msg_ts,
            "reconnects": self.reconnects,
            "messages": self.messages,
            "parse_errors": self.parse_errors,
            "crossed_skipped": self.crossed_skipped,
            "channels": len(self.channels),
            "dropped_channels": len(self.dropped_channels),
            "fatal_close": self.fatal_close,
        }

    # ------------------------------------------------------------ run loop

    async def run(self, stop_event: asyncio.Event | None = None) -> None:
        """Connect, subscribe and read until ``stop_event`` is set. Only ``CancelledError`` escapes.

        After a fatal close the feed stays off, but ``run`` still waits for ``stop_event``: returning
        early would only make a supervisor restart the feed and reconnect.
        """
        stop = stop_event or asyncio.Event()
        main = asyncio.create_task(self._reconnect_loop(), name="wallex-ws")
        waiter = asyncio.create_task(stop.wait(), name="wallex-ws-stop")
        try:
            await asyncio.wait({main, waiter}, return_when=asyncio.FIRST_COMPLETED)
            if not waiter.done():  # the reconnect loop ended: a fatal close code
                await waiter
        finally:
            main.cancel()
            waiter.cancel()
            await asyncio.gather(main, waiter, return_exceptions=True)
            self._set_connected(False)

    async def _reconnect_loop(self) -> None:
        while True:
            self._opened_mono = 0.0
            try:
                end = await self._session()
            except Exception as exc:  # a bug must not kill the feed; the REST poller covers the gap
                log.exception("ws: unexpected error: %s", type(exc).__name__)
                end = SessionEnd(None, "error")
            lived = time.monotonic() - self._opened_mono if self._opened_mono else 0.0
            self._set_connected(False)
            if end.code is not None and end.code >= FATAL_CLOSE_MIN:
                self.fatal_close = end.code
                log.error("ws: server closed with non-reconnect code %s; the WebSocket feed stays off "
                          "(REST polling covers the data; restart the process to retry)", end.code)
                return
            delay = self.next_delay(end, lived)
            self.reconnects += 1
            log.info("ws: session ended (%s, code=%s, lived %.0fs); reconnect in %.1fs",
                     end.reason, end.code, lived, delay)
            if delay > 0:
                await asyncio.sleep(delay)

    def next_delay(self, end: SessionEnd, lived_s: float) -> float:
        """Seconds to wait before the next connection attempt (updates the backoff counter)."""
        if lived_s > BACKOFF_RESET_AFTER_S:
            self._attempt = 0
        if end.reason == "max_age":
            return 0.0
        delay = min(self.reconnect_max_s, float(2**self._attempt))
        self._attempt = min(self._attempt + 1, 30)
        return delay

    async def _session(self) -> SessionEnd:
        """One connection from open to close."""
        log.info("ws: connecting to %s (%d channels)", _safe_url(self.url), len(self.channels))
        try:
            async with self._connect(self.url) as ws:
                self._opened_mono = self._last_recv_mono = time.monotonic()
                self._books.clear()  # no snapshot on subscribe: both sides must be fresh on this connection
                self._pending.clear()
                await ws.send(_dumps({"id": CONNECT_ID, "connect": {}}))
                return await self._read_loop(ws)
        except ConnectionClosed as exc:
            code = exc.rcvd.code if exc.rcvd is not None else None
            reason = exc.rcvd.reason if exc.rcvd is not None else ""
            log.warning("ws: connection closed (code=%s %s)", code, reason)
            return SessionEnd(code, "closed")
        except (OSError, TimeoutError, WebSocketException) as exc:
            log.warning("ws: connect failed: %s: %s", type(exc).__name__, exc)
            return SessionEnd(None, "error")

    async def _read_loop(self, ws: WSConnection) -> SessionEnd:
        while True:
            now = time.monotonic()
            age = now - self._opened_mono
            if age >= self.max_conn_age_s:
                log.info("ws: connection is %.0fs old; reconnecting proactively", age)
                return SessionEnd(None, "max_age")
            idle = now - self._last_recv_mono
            if idle >= self.idle_timeout_s:
                log.warning("ws: no message for %.0fs; reconnecting", idle)
                return SessionEnd(None, "idle")
            timeout = min(self.max_conn_age_s - age, self.idle_timeout_s - idle)
            try:
                frame = await asyncio.wait_for(ws.recv(), timeout=timeout)
            except TimeoutError:
                continue  # re-check age / idle
            self._last_recv_mono = time.monotonic()
            self.last_msg_ts = self.clock.now()
            for msg in parse_messages(frame):
                self.messages += 1
                try:
                    await self.handle_message(ws, msg)
                except ConnectionClosed:
                    raise
                except Exception:  # e.g. a failing bus listener: log it, keep the connection
                    log.exception("ws: failed to handle message")

    # ------------------------------------------------------------ message handling

    async def handle_message(self, ws: WSConnection, msg: dict[str, Any]) -> None:
        """Dispatch one protocol message (ping, push, reply or error)."""
        if not msg:  # server ping
            await ws.send("{}")
            return
        if "push" in msg:
            self._on_push(msg["push"])
            return
        msg_id = msg.get("id")
        if "error" in msg:
            channel = self._pending.pop(msg_id, None) if isinstance(msg_id, int) else None
            what = f"subscribe {channel}" if channel else ("connect" if msg_id == CONNECT_ID else f"id={msg_id}")
            log.error("ws: %s failed: %s", what, msg["error"])
            return
        if "connect" in msg:
            info = msg["connect"] if isinstance(msg["connect"], dict) else {}
            log.info("ws: connected (client=%s, ping=%ss)", info.get("client"), info.get("ping"))
            self._set_connected(True)
            await self._subscribe_all(ws)
            return
        if "subscribe" in msg:
            if isinstance(msg_id, int):
                self._pending.pop(msg_id, None)
            return
        log.debug("ws: ignored message: %.200s", msg)

    async def _subscribe_all(self, ws: WSConnection) -> None:
        self._pending.clear()
        for msg_id, channel in enumerate(self.channels, start=CONNECT_ID + 1):
            self._pending[msg_id] = channel
            await ws.send(_dumps({"id": msg_id, "subscribe": {"channel": channel}}))

    def _on_push(self, push: Any) -> None:
        if not isinstance(push, dict):
            return
        channel = str(push.get("channel", ""))
        pub = push.get("pub")
        if isinstance(pub, dict) and "data" in pub:
            self.handle_push(channel, pub["data"])
        elif "unsubscribe" in push:
            log.warning("ws: server unsubscribed %s: %s", channel, push["unsubscribe"])
        elif "disconnect" in push:
            log.warning("ws: server disconnect push: %s", push["disconnect"])
        else:
            log.debug("ws: ignored push on %s", channel)

    def handle_push(self, channel: str, data: Any) -> None:
        """Apply one data push: depth -> book on the bus, trade -> ``bus.publish_trade``.

        Malformed payloads are logged and counted in ``parse_errors``; they never raise.
        """
        symbol, kind = split_channel(channel)
        data = unwrap_message(data)
        try:
            if kind in ("buyDepth", "sellDepth"):
                self._on_depth(symbol, kind, data)
            elif kind == "trade":
                for item in data if isinstance(data, list) else [data]:
                    self.bus.publish_trade(self._parse_trade(symbol, item))
            else:
                log.debug("ws: ignored channel %s", channel)
        except (KeyError, TypeError, ValueError, ArithmeticError) as exc:
            self.parse_errors += 1
            log.warning("ws: bad payload on %s: %s: %s", channel, type(exc).__name__, exc)

    def _on_depth(self, symbol: str, kind: str, data: Any) -> None:
        book = self._books.setdefault(symbol, _PartialBook())
        if kind == "buyDepth":
            book.bids = parse_levels(data, self.levels, descending=True)
        else:
            book.asks = parse_levels(data, self.levels, descending=False)
        if book.bids is None or book.asks is None:
            return  # wait until both sides were seen on this connection
        if book.bids and book.asks and book.bids[0].price >= book.asks[0].price:
            # one side is momentarily stale; a crossed book would fake an arbitrage signal
            self.crossed_skipped += 1
            log.debug("ws: crossed book for %s skipped (bid %s >= ask %s)",
                      symbol, book.bids[0].price, book.asks[0].price)
            return
        self.bus.publish_book(
            BookSnapshot(symbol=symbol, ts=self.clock.now(), source=SOURCE, bids=book.bids, asks=book.asks)
        )

    def _parse_trade(self, symbol: str, item: dict[str, Any]) -> Trade:
        is_buy_order = item.get("isBuyOrder")
        side = None if is_buy_order is None else ("SELL" if is_buy_order else "BUY")  # taker side
        raw_ts = item.get("timestamp")
        try:
            ts = parse_iso(raw_ts) if isinstance(raw_ts, str) else None
        except ValueError:
            ts = None
        return Trade(
            symbol=symbol,
            ts=ts if ts is not None else self.clock.now(),
            price=D(item["price"]),
            qty=D(item["quantity"]),
            side=side,
        )

    # ------------------------------------------------------------ helpers

    def _set_connected(self, flag: bool) -> None:
        """Update ``connected`` and the bus source state (freshness still comes from book pushes)."""
        self.connected = flag
        self.bus.mark_source(SOURCE, flag)


def _dumps(obj: dict[str, Any]) -> str:
    return json.dumps(obj, separators=(",", ":"))

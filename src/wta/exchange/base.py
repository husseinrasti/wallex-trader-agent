"""Exchange interface + in-memory market data bus.

Every mode (live, shadow/paper, backtest, replay) implements ``Exchange``; the engine only
talks to this interface. Market data arrives separately through ``MarketDataBus``.
"""

from __future__ import annotations

import abc
from collections.abc import Callable
from dataclasses import dataclass, field

from wta.models import Balance, BookSnapshot, Fill, MarketInfo, Order, OrderRequest, Trade


class WallexAPIError(Exception):
    """Any non-success answer from Wallex (HTTP error or ``success: false``). Never carries the key."""

    def __init__(self, status: int | None, code: str | None, message: str, payload: object = None):
        super().__init__(f"WallexAPIError(status={status}, code={code}): {message}")
        self.status = status
        self.code = code
        self.message = message
        self.payload = payload

    @property
    def retryable(self) -> bool:
        return self.status is None or self.status == 429 or self.status >= 500


class OrderRejected(WallexAPIError):
    """The exchange answered and refused the order (4xx / validation)."""


class Exchange(abc.ABC):
    name: str = "base"

    @abc.abstractmethod
    async def get_markets(self) -> dict[str, MarketInfo]: ...

    @abc.abstractmethod
    async def get_balances(self) -> dict[str, Balance]: ...

    @abc.abstractmethod
    async def place_order(self, req: OrderRequest) -> Order: ...

    @abc.abstractmethod
    async def get_order(self, client_id: str) -> Order | None: ...

    @abc.abstractmethod
    async def cancel_order(self, client_id: str) -> Order | None: ...

    @abc.abstractmethod
    async def get_open_orders(self, symbol: str | None = None) -> list[Order]: ...

    @abc.abstractmethod
    async def get_my_trades(self, symbol: str | None = None, since_ts: float | None = None) -> list[Fill]: ...

    async def close(self) -> None:  # pragma: no cover - optional
        return None


BookListener = Callable[[BookSnapshot], None]
TradeListener = Callable[[Trade], None]


@dataclass
class _SourceState:
    last_update_ts: float = 0.0
    ok: bool = False
    errors: int = 0


@dataclass
class MarketDataBus:
    """Latest book per symbol from whichever source (ws/rest/...) is fresher.

    Sources publish; consumers read ``latest``/``snapshot`` or register listeners (the recorder).
    """

    books: dict[str, BookSnapshot] = field(default_factory=dict)
    sources: dict[str, _SourceState] = field(default_factory=dict)
    _book_listeners: list[BookListener] = field(default_factory=list)
    _trade_listeners: list[TradeListener] = field(default_factory=list)

    def publish_book(self, book: BookSnapshot) -> bool:
        """Store ``book`` if it is at least as fresh as the current one. Returns True if stored."""
        st = self.sources.setdefault(book.source, _SourceState())
        st.last_update_ts = max(st.last_update_ts, book.ts)
        st.ok = True
        cur = self.books.get(book.symbol)
        if cur is not None and cur.ts > book.ts:
            return False
        self.books[book.symbol] = book
        for cb in list(self._book_listeners):
            cb(book)
        return True

    def publish_trade(self, trade: Trade) -> None:
        for cb in list(self._trade_listeners):
            cb(trade)

    def mark_source(self, source: str, ok: bool, now: float | None = None) -> None:
        st = self.sources.setdefault(source, _SourceState())
        st.ok = ok
        if not ok:
            st.errors += 1
        elif now is not None:
            st.last_update_ts = max(st.last_update_ts, now)

    def source_ok(self, source: str, now: float, stale_s: float) -> bool:
        st = self.sources.get(source)
        return bool(st and st.ok and now - st.last_update_ts <= stale_s)

    def on_book(self, cb: BookListener) -> None:
        self._book_listeners.append(cb)

    def on_trade(self, cb: TradeListener) -> None:
        self._trade_listeners.append(cb)

    def latest(self, symbol: str) -> BookSnapshot | None:
        return self.books.get(symbol)

    def snapshot(self, symbols: list[str] | None = None) -> dict[str, BookSnapshot]:
        keys = sorted(self.books) if symbols is None else sorted(s for s in symbols if s in self.books)
        return {k: self.books[k] for k in keys}

    def age(self, symbol: str, now: float) -> float | None:
        b = self.books.get(symbol)
        return None if b is None else now - b.ts

"""PaperExchange: the shadow-mode ``Exchange`` that fills our orders against live order books.

It has the same interface as ``WallexREST`` (PRD-02 §9) and never touches the network: balances,
orders and fills live in memory, and ``state()`` / ``load_state()`` let a shadow restart continue
exactly where it stopped (the engine keeps the state in the store's kv key ``"paper_state"``).

Validation (``place_order``). Like the real exchange, a bad order raises
``OrderRejected(status=400, code=...)`` and is not registered:

* ``unknown_symbol``          symbol not in the markets list
* ``duplicate_client_id``     the client_id was used before (even by a finished order)
* ``market_orders_disabled``  MARKET / STOP_MARKET on a market with ``market_orders_enabled=False``
* ``missing_price`` / ``missing_stop_price``  LIMIT/STOP_LIMIT without price, STOP_* without stop
* ``invalid_quantity`` / ``invalid_price``    zero or negative values
* ``qty_precision`` / ``price_precision``     more decimals than the market's step / tick
* ``min_qty`` / ``min_notional``              ``qty < min_qty`` or ``qty x price < min_notional``
* ``no_market_price``         a MARKET order on a symbol without a book on the needed side
* ``insufficient_balance``    the free balance cannot cover the lock below

Locks (taken at placement, also for STOP orders, which matches the decision that a resting
stop locks the coins; released on fill, cancel or MARKET remainder cancel):

* BUY  locks quote ``qty x lock_price x (1 + taker fee)`` where ``lock_price`` is the limit price
  (LIMIT, STOP_LIMIT), ``best_ask x 1.02`` (MARKET) or ``max(stop, best_ask) x 1.02``
  (STOP_MARKET). 2% is Wallex's MARKET price band and also caps what a MARKET BUY may pay.
* SELL locks base ``qty``.

Books. ``on_tick(now, books)`` matches against the books it is given (the engine passes exactly the
books it records for replay) and orders placed later in the same tick are validated and priced
against the same snapshot; without ``books`` the ``books()`` callable is read.

Latency (pessimistic). ``on_tick`` is the matching engine and counts ticks. An order placed (or a
stop triggered) during tick ``k`` is looked at no earlier than tick ``k + 1``. A stop therefore
needs one tick to trigger and one more tick to fill.

Matching (``on_tick``), per open order in placement order:

* LIMIT BUY fills when ``best_ask <= price``: it walks the ask levels with ``price <= limit``,
  taking at most each level's quantity (partial fills allowed; the remainder keeps resting).
  SELL is symmetric on the bids.
* Maker or taker is decided when the order *enters the book*: at placement for LIMIT, at the
  trigger for STOP_LIMIT. An order that was crossing then is a taker: on its first matching pass it
  takes liquidity at the level prices with the taker fee. Whatever is left after that pass rests in
  the book, so it (and every order that was not crossing) fills later as a maker, at its own
  limit price, with the maker fee.
* MARKET walks the opposite side at its first matching pass, at level prices, taker fee, within
  2% of the best price (and, for a BUY, up to its lock price). The unfilled remainder is canceled,
  like Wallex does.
* STOP_LIMIT / STOP_MARKET: the trigger direction is fixed at placement by comparing the stop with
  the book's reference price (mid; one side if the other is empty; with no book a SELL stop counts
  as a stop-loss and a BUY stop as a breakout). ``stop <= reference`` triggers when
  ``best_bid <= stop``; ``stop > reference`` triggers when ``best_ask >= stop``. On trigger
  STOP_LIMIT becomes a LIMIT at ``req.price`` and STOP_MARKET becomes a MARKET.
* Liquidity is remembered per book snapshot: the quantity our fills took from a level is not
  available again (to another order or on the next tick) until a newer snapshot of that book
  arrives. Fill quantities are rounded down to the market's step.

Fees. Rate = ``maker_fee`` or ``taker_fee`` of the market. The fee is charged in the asset
received: BUY pays ``qty x rate`` in base (gets ``qty x (1 - rate)`` base for ``qty x price``
quote), SELL pays ``qty x price x rate`` in quote.

Every fill unlocks and transfers balances, updates the ``Order`` (``filled_qty``, ``avg_price``,
``fee_paid``, ``fee_asset``, status, ``updated_ts``), is appended to my-trades with a deterministic
``trade_id`` (``paper-<seq>``) and is passed to ``on_fill``. Determinism: iteration in placement
or sorted order, sequence counters, time only from the injected clock (or ``on_tick(now)``).
Returned ``Order`` objects are copies; mutating them never changes the exchange's state.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Literal

from wta.exchange.base import Exchange, OrderRejected
from wta.models import D0, Balance, BookSnapshot, Fill, Level, MarketInfo, Order, OrderRequest, Side
from wta.utils.decimalutil import D, quantize_down
from wta.utils.timeutil import Clock

STATE_VERSION = 1
MARKET_PRICE_BAND = Decimal("0.02")  # Wallex: MARKET orders may not move the price more than 2%
DEFAULT_INITIAL_EQUITY_TMN = Decimal("5000000")
FIAT = "TMN"

StopDirection = Literal["down", "up"]
FillCallback = Callable[[Fill], None]


@dataclass
class _Working:
    """What the exchange keeps for one order besides the public ``Order``."""

    order: Order
    seq: int  # placement sequence (time priority and iteration order)
    lock_asset: str
    lock_left: Decimal  # still locked for this order, in ``lock_asset``
    eligible_tick: int  # first tick that may trigger or match this order (latency)
    triggered: bool  # False only for a STOP_* order still waiting for its stop
    takes_liquidity: bool  # crossing when it entered the book: next pass fills as taker
    stop_direction: StopDirection | None = None
    cap_price: Decimal | None = None  # MARKET / STOP_MARKET BUY: the highest price it may pay
    quote_sum: Decimal = D0  # sum(qty x price) of its fills, for ``avg_price``

    @property
    def is_market(self) -> bool:
        return self.order.req.type in ("MARKET", "STOP_MARKET")


def _reject(code: str, message: str) -> OrderRejected:
    return OrderRejected(400, code, message)


def _finer_than(value: Decimal, decimals: int) -> bool:
    """True if ``value`` has more decimals than the market allows."""
    return value != quantize_down(value, decimals)


def _crosses(side: Side, level_price: Decimal, limit: Decimal) -> bool:
    """Can an order on ``side`` at ``limit`` trade with a resting level at ``level_price``?"""
    return level_price <= limit if side == "BUY" else level_price >= limit


def _opposite_levels(book: BookSnapshot, side: Side) -> list[Level]:
    """Levels a ``side`` order trades against, best first (asks ascending / bids descending)."""
    if side == "BUY":
        return sorted(book.asks, key=lambda lv: lv.price)
    return sorted(book.bids, key=lambda lv: lv.price, reverse=True)


def _best_opposite(book: BookSnapshot | None, side: Side) -> Decimal | None:
    if book is None:
        return None
    return book.best_ask if side == "BUY" else book.best_bid


def _reference_price(book: BookSnapshot | None) -> Decimal | None:
    """Mid when both sides exist, else the side that exists (stand-in for Wallex's last price)."""
    if book is None:
        return None
    if book.mid is not None:
        return book.mid
    return book.best_bid if book.best_bid is not None else book.best_ask


def _s(value: Decimal | None) -> str | None:
    return None if value is None else format(value, "f")


class PaperExchange(Exchange):
    """In-memory exchange for shadow mode; see the module docstring for the fill rules."""

    name = "paper"

    def __init__(
        self,
        markets: dict[str, MarketInfo],
        books: Callable[[], dict[str, BookSnapshot]],
        clock: Clock,
        initial_balances: dict[str, Decimal] | None = None,
        initial_equity_tmn: Decimal = DEFAULT_INITIAL_EQUITY_TMN,
        state: dict[str, Any] | None = None,
        on_fill: FillCallback | None = None,
    ) -> None:
        self._markets = dict(sorted(markets.items()))
        self._books = books
        self._clock = clock
        self._on_fill = on_fill
        start = initial_balances if initial_balances is not None else {FIAT: initial_equity_tmn}
        self._free: dict[str, Decimal] = {a: D(v) for a, v in sorted(start.items())}
        self._locked: dict[str, Decimal] = {}
        self._orders: dict[str, _Working] = {}  # client_id -> working order, in placement order
        self._trades: list[Fill] = []
        self._tick = 0
        self._order_seq = 0
        self._trade_seq = 0
        # symbol -> (book ts, {(book side, price): qty our fills took from that level})
        self._taken: dict[str, tuple[float, dict[tuple[str, Decimal], Decimal]]] = {}
        self._tick_books: dict[str, BookSnapshot] | None = None  # the current tick's snapshot
        if state is not None:
            self.load_state(state)

    # ------------------------------------------------------------ read side

    @property
    def tick_count(self) -> int:
        """Number of ``on_tick`` calls so far (persisted in the state)."""
        return self._tick

    async def get_markets(self) -> dict[str, MarketInfo]:
        return dict(self._markets)

    async def get_balances(self) -> dict[str, Balance]:
        assets = sorted(set(self._free) | set(self._locked))
        return {
            a: Balance(asset=a, free=self._free.get(a, D0), locked=self._locked.get(a, D0)) for a in assets
        }

    async def get_order(self, client_id: str) -> Order | None:
        w = self._orders.get(client_id)
        return None if w is None else w.order.model_copy(deep=True)

    async def get_open_orders(self, symbol: str | None = None) -> list[Order]:
        return [
            w.order.model_copy(deep=True)
            for w in self._open_working()
            if symbol is None or w.order.req.symbol == symbol
        ]

    async def get_my_trades(self, symbol: str | None = None, since_ts: float | None = None) -> list[Fill]:
        return [
            f
            for f in self._trades
            if (symbol is None or f.symbol == symbol) and (since_ts is None or f.ts >= since_ts)
        ]

    # ------------------------------------------------------------ orders

    async def place_order(self, req: OrderRequest) -> Order:
        """Validate, lock funds and register ``req`` as NEW. It can fill from the next tick on."""
        book = self._current_books().get(req.symbol)
        market = self._validate(req, book)
        lock_asset, lock_amount, cap_price = self._lock_for(req, market, book)
        if self._free.get(lock_asset, D0) < lock_amount:
            raise _reject(
                "insufficient_balance",
                f"{req.client_id}: needs {_s(lock_amount)} {lock_asset} free, "
                f"has {_s(self._free.get(lock_asset, D0))}",
            )
        self._free[lock_asset] = self._free.get(lock_asset, D0) - lock_amount
        self._locked[lock_asset] = self._locked.get(lock_asset, D0) + lock_amount

        now = self._clock.now()
        self._order_seq += 1
        is_stop = req.type in ("STOP_LIMIT", "STOP_MARKET")
        direction = self._stop_direction(req, book) if is_stop else None
        raw: dict[str, Any] = {"exchange": "paper"}
        if is_stop:
            raw.update(stop_direction=direction, triggered=False)
        order = Order(
            req=req,
            exchange_id=f"paper-o-{self._order_seq}",
            status="NEW",
            created_ts=now,
            updated_ts=now,
            raw=raw,
        )
        self._orders[req.client_id] = _Working(
            order=order,
            seq=self._order_seq,
            lock_asset=lock_asset,
            lock_left=lock_amount,
            eligible_tick=self._tick + 1,
            triggered=not is_stop,
            takes_liquidity=req.type == "LIMIT" and self._is_crossing(req.side, req.price, book),
            stop_direction=direction,
            cap_price=cap_price,
        )
        return order.model_copy(deep=True)

    async def cancel_order(self, client_id: str) -> Order | None:
        """Cancel and unlock the remainder. Unknown -> ``None``; finished orders come back as they are."""
        w = self._orders.get(client_id)
        if w is None:
            return None
        if not w.order.is_terminal:
            self._finish(w, "CANCELED", self._clock.now())
        return w.order.model_copy(deep=True)

    # ------------------------------------------------------------ matching

    def _current_books(self) -> dict[str, BookSnapshot]:
        return self._tick_books if self._tick_books is not None else self._books()

    def on_tick(self, now: float | None = None, books: dict[str, BookSnapshot] | None = None) -> list[Fill]:
        """Run one matching pass over all open orders against ``books`` (default ``books()``);
        returns the new fills. Orders placed during the rest of the tick see the same books."""
        now = self._clock.now() if now is None else now
        self._tick += 1
        self._tick_books = books  # None: place_order keeps reading books()
        books = self._books() if books is None else books
        fills: list[Fill] = []
        for w in self._open_working():
            if w.order.is_terminal or w.eligible_tick > self._tick:
                continue  # finished by an on_fill callback, or placed/triggered this tick
            book = books.get(w.order.req.symbol)
            if book is None:
                continue  # no data: nothing can trigger or fill
            if not w.triggered:
                self._check_trigger(w, book, now)
                continue
            new = self._match_market(w, book, now) if w.is_market else self._match_limit(w, book, now)
            fills.extend(new)
            if self._on_fill is not None:
                for f in new:
                    self._on_fill(f)
        return fills

    def _check_trigger(self, w: _Working, book: BookSnapshot, now: float) -> None:
        req = w.order.req
        stop = req.stop_price
        if stop is None:  # validated at placement; kept for type narrowing
            return
        if w.stop_direction == "down":
            hit = book.best_bid is not None and book.best_bid <= stop
        else:
            hit = book.best_ask is not None and book.best_ask >= stop
        if not hit:
            return
        w.triggered = True
        w.eligible_tick = self._tick + 1  # a triggered stop fills no earlier than the next tick
        if req.type == "STOP_LIMIT":
            w.takes_liquidity = self._is_crossing(req.side, req.price, book)
        w.order.raw = {**w.order.raw, "triggered": True, "triggered_ts": now}
        w.order.updated_ts = now

    def _match_limit(self, w: _Working, book: BookSnapshot, now: float) -> list[Fill]:
        req = w.order.req
        limit = req.price
        if limit is None:  # validated at placement
            return []
        taker = w.takes_liquidity
        w.takes_liquidity = False  # what is left after this pass rests in the book (maker)
        takes = self._walk(w, book, lambda price: _crosses(req.side, price, limit))
        if taker:
            return [self._apply_fill(w, price, qty, False, now) for price, qty in takes]
        total = sum((qty for _, qty in takes), D0)
        return [self._apply_fill(w, limit, total, True, now)] if total > 0 else []

    def _match_market(self, w: _Working, book: BookSnapshot, now: float) -> list[Fill]:
        req = w.order.req
        best = _best_opposite(book, req.side)
        takes: list[tuple[Decimal, Decimal]] = []
        if best is not None:
            if req.side == "BUY":
                ceiling = best * (1 + MARKET_PRICE_BAND)
                if w.cap_price is not None:
                    ceiling = min(ceiling, w.cap_price)
                takes = self._walk(w, book, lambda price: price <= ceiling)
            else:
                floor = best * (1 - MARKET_PRICE_BAND)
                takes = self._walk(w, book, lambda price: price >= floor)
        fills = [self._apply_fill(w, price, qty, False, now) for price, qty in takes]
        if not w.order.is_terminal:
            self._finish(w, "CANCELED", now)  # Wallex cancels what a MARKET order cannot fill
        return fills

    def _walk(
        self, w: _Working, book: BookSnapshot, price_ok: Callable[[Decimal], bool]
    ) -> list[tuple[Decimal, Decimal]]:
        """Take liquidity level by level while ``price_ok``; returns ``[(level price, qty)]``."""
        req = w.order.req
        step_decimals = self._markets[req.symbol].step_decimals
        book_side = "ask" if req.side == "BUY" else "bid"
        taken = self._taken_for(book)
        remaining = w.order.remaining_qty
        out: list[tuple[Decimal, Decimal]] = []
        for level in _opposite_levels(book, req.side):
            if remaining <= 0 or not price_ok(level.price):
                break
            key = (book_side, level.price)
            available = level.qty - taken.get(key, D0)
            qty = quantize_down(min(remaining, available), step_decimals)
            if qty <= 0:
                continue
            taken[key] = taken.get(key, D0) + qty
            remaining -= qty
            out.append((level.price, qty))
        return out

    def _taken_for(self, book: BookSnapshot) -> dict[tuple[str, Decimal], Decimal]:
        """Liquidity already taken from this exact snapshot; a newer snapshot starts fresh."""
        current = self._taken.get(book.symbol)
        if current is None or current[0] != book.ts:
            current = (book.ts, {})
            self._taken[book.symbol] = current
        return current[1]

    def _apply_fill(self, w: _Working, price: Decimal, qty: Decimal, is_maker: bool, now: float) -> Fill:
        """Move balances for one fill, update the order, record the trade."""
        order = w.order
        req = order.req
        market = self._markets[req.symbol]
        rate = market.maker_fee if is_maker else market.taker_fee
        if req.side == "BUY":
            spent = qty * price  # quote, from this order's lock
            fee, fee_asset = qty * rate, market.base
            self._unlock_spent(w, spent)
            self._credit(market.base, qty - fee)
        else:
            proceeds = qty * price
            fee, fee_asset = proceeds * rate, market.quote
            self._unlock_spent(w, qty)
            self._credit(market.quote, proceeds - fee)
        w.quote_sum += qty * price
        order.filled_qty = order.filled_qty + qty
        order.avg_price = w.quote_sum / order.filled_qty
        order.fee_paid = order.fee_paid + fee
        order.fee_asset = fee_asset
        order.updated_ts = now
        if order.remaining_qty == 0:
            self._finish(w, "FILLED", now)
        else:
            order.status = "PARTIALLY_FILLED"
        self._trade_seq += 1
        fill = Fill(
            order_client_id=req.client_id,
            symbol=req.symbol,
            side=req.side,
            price=price,
            qty=qty,
            fee=fee,
            fee_asset=fee_asset,
            ts=now,
            is_maker=is_maker,
            trade_id=f"paper-{self._trade_seq}",
        )
        self._trades.append(fill)
        return fill

    # ------------------------------------------------------------ balances

    def _unlock_spent(self, w: _Working, amount: Decimal) -> None:
        """``amount`` of the order's locked funds leaves the account (paid to the counterparty)."""
        self._locked[w.lock_asset] = self._locked.get(w.lock_asset, D0) - amount
        w.lock_left -= amount

    def _credit(self, asset: str, amount: Decimal) -> None:
        self._free[asset] = self._free.get(asset, D0) + amount

    def _finish(self, w: _Working, status: Literal["FILLED", "CANCELED"], now: float) -> None:
        """Terminal status; whatever is still locked for the order goes back to free."""
        if w.lock_left:
            self._locked[w.lock_asset] = self._locked.get(w.lock_asset, D0) - w.lock_left
            self._credit(w.lock_asset, w.lock_left)
            w.lock_left = D0
        w.order.status = status
        w.order.updated_ts = now

    # ------------------------------------------------------------ validation helpers

    def _validate(self, req: OrderRequest, book: BookSnapshot | None) -> MarketInfo:
        cid = req.client_id
        market = self._markets.get(req.symbol)
        if market is None:
            raise _reject("unknown_symbol", f"{cid}: unknown symbol {req.symbol}")
        if cid in self._orders:
            raise _reject("duplicate_client_id", f"{cid}: client_id already used")
        if req.type in ("MARKET", "STOP_MARKET") and not market.market_orders_enabled:
            raise _reject("market_orders_disabled", f"{cid}: MARKET orders are disabled on {req.symbol}")
        if req.type in ("LIMIT", "STOP_LIMIT") and req.price is None:
            raise _reject("missing_price", f"{cid}: {req.type} needs a price")
        if req.type in ("STOP_LIMIT", "STOP_MARKET") and req.stop_price is None:
            raise _reject("missing_stop_price", f"{cid}: {req.type} needs a stop_price")
        if req.qty <= 0:
            raise _reject("invalid_quantity", f"{cid}: quantity must be positive")
        if _finer_than(req.qty, market.step_decimals):
            raise _reject("qty_precision", f"{cid}: quantity has more than {market.step_decimals} decimals")
        # Prices the exchange sees: the limit price (ignored for MARKET) and the stop price, which
        # the REST client only sends for STOP_* orders.
        checked = [req.price] if req.type != "MARKET" else []
        if req.type in ("STOP_LIMIT", "STOP_MARKET"):
            checked.append(req.stop_price)
        for p in (x for x in checked if x is not None):
            if p <= 0:
                raise _reject("invalid_price", f"{cid}: prices must be positive")
            if _finer_than(p, market.tick_decimals):
                raise _reject(
                    "price_precision", f"{cid}: price has more than {market.tick_decimals} decimals"
                )
        if req.qty < market.min_qty:
            raise _reject("min_qty", f"{cid}: quantity below min_qty {_s(market.min_qty)}")
        price = self._notional_price(req, book)
        if req.qty * price < market.min_notional:
            raise _reject("min_notional", f"{cid}: notional below min_notional {_s(market.min_notional)}")
        return market

    @staticmethod
    def _notional_price(req: OrderRequest, book: BookSnapshot | None) -> Decimal:
        """Price used for the min-notional check: limit price, stop price, or the book for MARKET."""
        if req.type in ("LIMIT", "STOP_LIMIT") and req.price is not None:
            return req.price
        if req.type == "STOP_MARKET" and req.stop_price is not None:
            return req.stop_price
        best = _best_opposite(book, req.side)
        if best is None:
            best = req.price
        if best is None:
            raise _reject("no_market_price", f"{req.client_id}: no {req.symbol} book to price a MARKET order")
        return best

    def _lock_for(
        self, req: OrderRequest, market: MarketInfo, book: BookSnapshot | None
    ) -> tuple[str, Decimal, Decimal | None]:
        """``(asset, amount, cap_price)`` to lock at placement (see the module docstring)."""
        if req.side == "SELL":
            return market.base, req.qty, None
        cap_price: Decimal | None = None
        if req.type in ("LIMIT", "STOP_LIMIT"):
            lock_price = req.price
        elif req.type == "MARKET":
            best_ask = _best_opposite(book, "BUY")
            if best_ask is None:
                raise _reject(
                    "no_market_price", f"{req.client_id}: no {req.symbol} asks to price a MARKET BUY"
                )
            lock_price = cap_price = best_ask * (1 + MARKET_PRICE_BAND)
        else:  # STOP_MARKET
            stop = req.stop_price or D0
            best_ask = _best_opposite(book, "BUY") or D0
            lock_price = cap_price = max(stop, best_ask) * (1 + MARKET_PRICE_BAND)
        if lock_price is None:  # validated earlier; kept for type narrowing
            raise _reject("missing_price", f"{req.client_id}: no price to lock funds for")
        return market.quote, req.qty * lock_price * (1 + market.taker_fee), cap_price

    @staticmethod
    def _is_crossing(side: Side, price: Decimal | None, book: BookSnapshot | None) -> bool:
        """Would a LIMIT at ``price`` trade immediately against ``book``?"""
        best = _best_opposite(book, side)
        return price is not None and best is not None and _crosses(side, best, price)

    @staticmethod
    def _stop_direction(req: OrderRequest, book: BookSnapshot | None) -> StopDirection:
        ref = _reference_price(book)
        if ref is None or req.stop_price is None:
            return "down" if req.side == "SELL" else "up"
        return "down" if req.stop_price <= ref else "up"

    def _open_working(self) -> list[_Working]:
        return sorted((w for w in self._orders.values() if not w.order.is_terminal), key=lambda w: w.seq)

    # ------------------------------------------------------------ persistence

    def state(self) -> dict[str, Any]:
        """JSON-able snapshot (Decimals as strings) of balances, orders, trades and counters."""
        assets = sorted(set(self._free) | set(self._locked))
        return {
            "version": STATE_VERSION,
            "tick": self._tick,
            "order_seq": self._order_seq,
            "trade_seq": self._trade_seq,
            "balances": {
                a: {"free": _s(self._free.get(a, D0)), "locked": _s(self._locked.get(a, D0))} for a in assets
            },
            "orders": [
                {
                    "order": w.order.model_dump(mode="json"),
                    "seq": w.seq,
                    "lock_asset": w.lock_asset,
                    "lock_left": _s(w.lock_left),
                    "eligible_tick": w.eligible_tick,
                    "triggered": w.triggered,
                    "takes_liquidity": w.takes_liquidity,
                    "stop_direction": w.stop_direction,
                    "cap_price": _s(w.cap_price),
                    "quote_sum": _s(w.quote_sum),
                }
                for w in sorted(self._orders.values(), key=lambda w: w.seq)
            ],
            "trades": [f.model_dump(mode="json") for f in self._trades],
        }

    def load_state(self, state: dict[str, Any]) -> None:
        """Replace everything with a ``state()`` snapshot (shadow restart)."""
        version = state.get("version", STATE_VERSION)
        if version != STATE_VERSION:
            raise ValueError(f"unsupported paper state version {version!r}")
        self._tick = int(state.get("tick", 0))
        self._order_seq = int(state.get("order_seq", 0))
        self._trade_seq = int(state.get("trade_seq", 0))
        balances = state.get("balances") or {}
        self._free = {a: D(balances[a].get("free") or "0") for a in sorted(balances)}
        self._locked = {a: D(balances[a].get("locked") or "0") for a in sorted(balances)}
        self._orders = {}
        for item in sorted(state.get("orders") or [], key=lambda it: int(it["seq"])):
            order = Order.model_validate(item["order"])
            cap = item.get("cap_price")
            self._orders[order.client_id] = _Working(
                order=order,
                seq=int(item["seq"]),
                lock_asset=str(item["lock_asset"]),
                lock_left=D(item.get("lock_left") or "0"),
                eligible_tick=int(item["eligible_tick"]),
                triggered=bool(item["triggered"]),
                takes_liquidity=bool(item.get("takes_liquidity", False)),
                stop_direction=item.get("stop_direction"),
                cap_price=None if cap is None else D(cap),
                quote_sum=D(item.get("quote_sum") or "0"),
            )
        self._trades = [Fill.model_validate(f) for f in state.get("trades") or []]
        self._taken = {}

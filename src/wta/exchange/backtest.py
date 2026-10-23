"""BacktestExchange (PRD-05 §3): the PaperExchange, driven by recorded books or by candles.

It is a ``PaperExchange`` subclass, so the engine treats it exactly like the shadow exchange
(``on_tick(now, books)`` every fast tick, ``state()`` / ``load_state()`` persisted in kv) and
validation, locks, balances, order bookkeeping and the fee arithmetic are shared. Only matching
differs, and the rules are the pessimistic ones of ``backtest/fills.py`` (PRD-05 §3.2):

* ``driver="books"`` (recorded snapshots, for S1/S2 and realistic fills): PaperExchange matching,
  but a fill takes at most ``level_fraction`` (80 %) of a level's quantity, and an order is matched
  only against a snapshot of its symbol that is NEWER than the one it was placed on (or, for a
  stop, the one it triggered on). Recorded snapshots do not show our own fills and often repeat the
  same level for many seconds, so what our fills took from a level is carried to the next snapshot
  of the symbol while that price level is still shown: the cap is ``level_fraction`` x the level's
  current quantity minus everything we took from it since it appeared (only quantity added to the
  level becomes available again; a level that disappears starts fresh when it comes back).
  Recording gaps (``max_gap_s``, the runner passes ``engine.stale_data_s``): an order that is
  live in the book (a LIMIT/MARKET, or a stop after its trigger) and whose symbol had no snapshot
  for more than ``max_gap_s`` since the previous one it was looked at is never filled again: the
  recorded data has a hole there, and live, the OrderManager would have re-priced or canceled it
  within seconds (``limit_reprice_after_s``); the engine re-prices, crosses or gives it up on the
  post-gap books. An untriggered stop still triggers after a gap (at the post-gap price). Fee:
  an order that was crossing at placement (a STOP_LIMIT: at its trigger) pays the TAKER fee on
  every fill and walks the levels at their prices, also for the remainder the cap left (PRD-05
  §3.2: maker only if not crossing at placement); an order resting when it was placed is filled at
  its limit with the maker fee.
* ``driver="candles"`` (1-minute bars, S2/S3 over 30 days): the current bar per symbol comes from
  ``set_bar(symbol, candle, spread_pct)`` and/or a ``bars`` provider callable. The engine sees the
  synthetic book of ``make_synthetic_book`` (mid = close, spread = the market's median observed
  spread, unlimited depth at best; ``source="backtest"``), which is also what placement validates
  and locks against. ``on_tick`` matches every open order against its symbol's current bar with
  ``backtest.fills.match_candle``, and only when that bar is LATER than the bar the order was
  placed on (every order acts one step later) and later than the last bar it was matched against
  (each bar is looked at once per order, however often the engine ticks within a bar).
  ``on_tick`` does not use its ``books`` argument for matching in this driver.

Fees come from ``configs/fees_snapshot.yaml`` (``wta.exchange.markets.load_fee_snapshot``) so a
backtest never needs API access; pass ``use_fee_snapshot=False`` to keep the markets' own fees.

Spread per symbol (candle driver), first match wins: the ``spread_pct`` given to ``set_bar``,
``spreads`` (the median observed spread; ``observed_spreads(store, ...)`` computes it from recorded
books), the reference spreads of ``configs/markets_snapshot.yaml``, ``DEFAULT_SPREAD_PCT``.
``spread_source(symbol)`` says which one was used, so a report can state it honestly.

Funds. A BUY locks ``qty x limit x (1 + taker fee)`` at placement (PaperExchange). A pessimistic
candle fill can cost more than that (a stop's stress leg at the bar high, a MARKET far from the
placement price): the shortfall is moved from the free balance into the lock first, and when the
account cannot pay, the fill is cut to what it can pay and the rest of the order is canceled.
Balances never go negative.

Determinism: no randomness, no wall clock (time only from ``on_tick(now)`` or the injected clock),
iteration in placement / sorted order. Trade ids are ``bt-<seq>``, order ids ``bt-o-<seq>``.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, Protocol

from backtest.fills import (
    DEFAULT_LEVEL_FRACTION,
    DEFAULT_SLIPPAGE,
    CandleOrder,
    FillLeg,
    bar_seconds,
    match_candle,
    median_spread,
    opposite_levels,
    synthetic_quotes,
    walk_levels,
)
from wta.exchange.markets import (
    default_fees,
    load_fee_snapshot,
    load_markets_snapshot,
    snapshot_reference,
)
from wta.exchange.paper import DEFAULT_INITIAL_EQUITY_TMN, FillCallback, PaperExchange
from wta.models import D0, BookSnapshot, Candle, Fill, Level, MarketInfo, Order, OrderRequest
from wta.utils.decimalutil import D, quantize_down
from wta.utils.timeutil import Clock

if TYPE_CHECKING:
    from wta.exchange.paper import _Working

Driver = Literal["books", "candles"]
SpreadSource = Literal["bar", "observed", "reference", "default"]

BACKTEST_STATE_VERSION = 1
DEFAULT_SPREAD_PCT = Decimal("0.001")  # last resort when nothing better is known
SYNTHETIC_DEPTH_QTY = Decimal("1000000000")  # "unlimited" depth at best for the synthetic book

BarsProvider = Callable[[], Mapping[str, Candle]]


class _BookSource(Protocol):
    def iter_books(
        self, symbol: str, from_ts: float | None = None, to_ts: float | None = None
    ) -> Iterable[BookSnapshot]: ...


# ---------------------------------------------------------------- helpers


def apply_fee_snapshot(
    markets: Mapping[str, MarketInfo], snapshot: Mapping[str, Any] | None = None
) -> dict[str, MarketInfo]:
    """Markets with maker/taker fees from the fee snapshot (default ``configs/fees_snapshot.yaml``):
    the symbol's own entry when present, else the default for its quote currency."""
    snap = dict(load_fee_snapshot() if snapshot is None else snapshot)
    per_symbol = snap.get("symbols") or {}
    out: dict[str, MarketInfo] = {}
    for sym in sorted(markets):
        m = markets[sym]
        maker, taker = default_fees(m.quote, snap)
        own = per_symbol.get(sym)
        if own:
            maker, taker = D(own["maker"]), D(own["taker"])
        out[sym] = m.model_copy(update={"maker_fee": maker, "taker_fee": taker})
    return out


def make_synthetic_book(
    symbol: str,
    candle: Candle,
    spread_pct: Decimal,
    market: MarketInfo,
    ts: float | None = None,
    depth_qty: Decimal = SYNTHETIC_DEPTH_QTY,
) -> BookSnapshot:
    """The candle driver's book for the engine: one level per side around ``mid = close``.

    ``bid/ask = close -/+ spread/2`` on the tick grid (``backtest.fills.synthetic_quotes``), depth
    ``depth_qty`` at best ("unlimited": we are tiny), ``source="backtest"``; ``ts`` defaults to the
    bar's close time (``candle.ts + bar length``), i.e. when the bar is known.
    """
    bid, ask = synthetic_quotes(candle.c, spread_pct, market.tick_decimals)
    depth = quantize_down(depth_qty, market.step_decimals)
    when = float(candle.ts + bar_seconds(candle.resolution)) if ts is None else float(ts)
    return BookSnapshot(
        symbol=symbol,
        ts=when,
        source="backtest",
        bids=[Level(price=bid, qty=depth)] if bid > 0 else [],
        asks=[Level(price=ask, qty=depth)],
    )


def observed_spreads(
    store: _BookSource, symbols: Iterable[str], from_ts: float | None = None, to_ts: float | None = None
) -> dict[str, Decimal]:
    """Median observed spread per symbol from recorded books (symbols without books are left out)."""
    out: dict[str, Decimal] = {}
    for sym in sorted(set(symbols)):
        spread = median_spread(store.iter_books(sym, from_ts, to_ts))
        if spread is not None:
            out[sym] = spread
    return out


def _reference_spreads() -> dict[str, Decimal]:
    try:
        return dict(snapshot_reference()["spreads"])
    except (OSError, KeyError, TypeError, ValueError):  # no snapshot file: no reference spreads
        return {}


@dataclass
class _BtOrder:
    """Backtest bookkeeping per open order, next to PaperExchange's ``_Working``."""

    ref_ts: float | None  # data time it was placed on / last matched against (bar open ts or book ts)
    crossing: bool  # crossing when it entered the book (placement / stop trigger): taker fee for every fill
    seen_ts: float | None = None  # books driver: the snapshot ts it was last looked at (recording gaps)
    gap_blocked: bool = False  # books driver: open across a recording gap, never filled afterwards


@dataclass
class _Bar:
    candle: Candle
    spread_pct: Decimal | None  # set_bar override; None = use the symbol's spread


# ---------------------------------------------------------------- exchange


class BacktestExchange(PaperExchange):
    """PaperExchange with the PRD-05 pessimistic fill model; see the module docstring."""

    name = "backtest"
    make_synthetic_book = staticmethod(make_synthetic_book)

    def __init__(
        self,
        markets: Mapping[str, MarketInfo],
        clock: Clock,
        driver: Driver = "candles",
        books: Callable[[], dict[str, BookSnapshot]] | None = None,
        bars: BarsProvider | None = None,
        spreads: Mapping[str, Decimal] | None = None,
        slippage: Decimal = DEFAULT_SLIPPAGE,
        level_fraction: Decimal = DEFAULT_LEVEL_FRACTION,
        max_gap_s: float | None = None,
        initial_balances: dict[str, Decimal] | None = None,
        initial_equity_tmn: Decimal = DEFAULT_INITIAL_EQUITY_TMN,
        state: dict[str, Any] | None = None,
        on_fill: FillCallback | None = None,
        use_fee_snapshot: bool = True,
        fee_snapshot: Mapping[str, Any] | None = None,
    ) -> None:
        if driver not in ("books", "candles"):
            raise ValueError(f"unknown backtest driver {driver!r} (books | candles)")
        if driver == "books" and books is None:
            raise ValueError("the books driver needs a books() callable")
        if not D0 < D(level_fraction) <= 1:
            raise ValueError("level_fraction must be in (0, 1]")
        if D(slippage) < 0:
            raise ValueError("slippage must not be negative")
        if max_gap_s is not None and max_gap_s <= 0:
            raise ValueError("max_gap_s must be > 0")
        self.driver: Driver = driver
        self.max_gap_s = max_gap_s
        self.gap_blocked = 0  # orders kept from filling across a recording gap
        self.slippage = D(slippage)
        self.level_fraction = D(level_fraction)
        self._bars_provider = bars
        self._bars: dict[str, _Bar] = {}
        self._spreads = {s: D(v) for s, v in sorted((spreads or {}).items())}
        self._reference: dict[str, Decimal] | None = None  # loaded on first use
        self._bt: dict[str, _BtOrder] = {}
        # books driver: what our fills took per (side, price) level, carried across snapshots of a
        # symbol while the level is shown: symbol -> (snapshot ts, {(side, price): qty taken})
        self._level_taken: dict[str, tuple[float, dict[tuple[str, Decimal], Decimal]]] = {}
        fee_markets = apply_fee_snapshot(markets, fee_snapshot) if use_fee_snapshot else dict(markets)
        super().__init__(
            fee_markets,
            books if driver == "books" and books is not None else self.synthetic_books,
            clock,
            initial_balances=initial_balances,
            initial_equity_tmn=initial_equity_tmn,
            state=None,
            on_fill=on_fill,
        )
        if state is not None:
            self.load_state(state)

    @classmethod
    def from_snapshot(
        cls,
        clock: Clock,
        driver: Driver = "candles",
        symbols: Iterable[str] | None = None,
        markets_path: Path | None = None,
        **kwargs: Any,
    ) -> BacktestExchange:
        """Markets from ``configs/markets_snapshot.yaml`` (fees from the fee snapshot), no API."""
        markets = load_markets_snapshot() if markets_path is None else load_markets_snapshot(markets_path)
        if symbols is not None:
            wanted = set(symbols)
            markets = {s: m for s, m in markets.items() if s in wanted}
        return cls(markets, clock, driver=driver, **kwargs)

    # ------------------------------------------------------------ candle driver inputs

    def set_bar(self, symbol: str, candle: Candle, spread_pct: Decimal | None = None) -> None:
        """Make ``candle`` the current bar of ``symbol`` (optionally with its own spread)."""
        if symbol not in self._markets:
            raise ValueError(f"unknown symbol {symbol}")
        if candle.symbol != symbol:
            raise ValueError(f"candle of {candle.symbol} given for {symbol}")
        self._bars[symbol] = _Bar(candle, None if spread_pct is None else D(spread_pct))

    def current_bars(self) -> dict[str, Candle]:
        """Current bar per symbol: ``set_bar`` values, overridden by the ``bars`` provider."""
        out = {s: b.candle for s, b in self._bars.items()}
        if self._bars_provider is not None:
            out.update(self._bars_provider())
        return {s: out[s] for s in sorted(out) if s in self._markets}

    def current_bar(self, symbol: str) -> Candle | None:
        return self.current_bars().get(symbol)

    def spread_source(self, symbol: str) -> SpreadSource:
        """Where ``spread_for(symbol)`` comes from: bar | observed | reference | default."""
        bar = self._bars.get(symbol)
        if bar is not None and bar.spread_pct is not None:
            return "bar"
        if symbol in self._spreads:
            return "observed"
        if symbol in self._reference_spreads():
            return "reference"
        return "default"

    def spread_for(self, symbol: str) -> Decimal:
        """The spread the candle driver uses for ``symbol`` (see the module docstring)."""
        source = self.spread_source(symbol)
        if source == "bar":
            spread = self._bars[symbol].spread_pct
            if spread is not None:
                return spread
        if source == "observed":
            return self._spreads[symbol]
        if source == "reference":
            return self._reference_spreads()[symbol]
        return DEFAULT_SPREAD_PCT

    def _reference_spreads(self) -> dict[str, Decimal]:
        if self._reference is None:
            self._reference = _reference_spreads()
        return self._reference

    def synthetic_books(self) -> dict[str, BookSnapshot]:
        """The engine's books in the candle driver: one synthetic book per symbol with a bar."""
        return {
            sym: make_synthetic_book(sym, bar, self.spread_for(sym), self._markets[sym])
            for sym, bar in self.current_bars().items()
        }

    # ------------------------------------------------------------ orders

    async def place_order(self, req: OrderRequest) -> Order:
        """PaperExchange placement (validation, locks) + the data time it was placed on."""
        ref_ts = self._data_ts(req.symbol)
        await super().place_order(req)
        w = self._orders[req.client_id]
        w.order.exchange_id = f"bt-o-{w.seq}"
        w.order.raw = {**w.order.raw, "exchange": "backtest", "driver": self.driver}
        self._bt[req.client_id] = _BtOrder(ref_ts=ref_ts, crossing=w.takes_liquidity)
        return w.order.model_copy(deep=True)

    def _data_ts(self, symbol: str) -> float | None:
        if self.driver == "candles":
            bar = self.current_bar(symbol)
            return None if bar is None else float(bar.ts)
        book = self._current_books().get(symbol)
        return None if book is None else book.ts

    def _info(self, w: _Working) -> _BtOrder:
        info = self._bt.get(w.order.client_id)
        if info is None:  # e.g. state saved by a plain PaperExchange
            info = self._bt[w.order.client_id] = _BtOrder(ref_ts=None, crossing=w.takes_liquidity)
        return info

    # ------------------------------------------------------------ matching

    def on_tick(self, now: float | None = None, books: dict[str, BookSnapshot] | None = None) -> list[Fill]:
        """One matching pass over all open orders; returns the new fills.

        ``books`` (the engine's snapshot) is what orders placed during the rest of the tick are
        validated against; the books driver also matches against it (default ``books()``). The
        candle driver matches against the current bars.
        """
        now = self._clock.now() if now is None else now
        self._tick += 1
        self._tick_books = books
        if self.driver == "books":
            book_map = self._books() if books is None else books
            bars: dict[str, Candle] = {}
        else:
            book_map = {}
            bars = self.current_bars()
        fills: list[Fill] = []
        for w in self._open_working():
            if w.order.is_terminal or w.eligible_tick > self._tick:
                continue  # finished by an on_fill callback, or placed/triggered this tick
            symbol = w.order.req.symbol
            info = self._info(w)
            if self.driver == "books":
                book = book_map.get(symbol)
                if book is None or (info.ref_ts is not None and book.ts <= info.ref_ts):
                    continue  # no data, or not a newer snapshot than the one it was placed on
                if self._across_gap(w, info, book):
                    continue  # open across a hole in the recording: never filled after it
                new = self._match_book(w, info, book, now)
            else:
                bar = bars.get(symbol)
                if bar is None or (info.ref_ts is not None and bar.ts <= info.ref_ts):
                    continue  # no bar, or not a later bar than the one it was placed on
                new = self._match_bar(w, info, bar, now)
            fills.extend(new)
            if self._on_fill is not None:
                for f in new:
                    self._on_fill(f)
        return fills

    def _across_gap(self, w: _Working, info: _BtOrder, book: BookSnapshot) -> bool:
        """True when ``w`` (live in the book) must not fill on ``book``: its symbol had no snapshot for
        more than ``max_gap_s`` since the last one the order was looked at (see the module docstring)."""
        last = info.seen_ts if info.seen_ts is not None else info.ref_ts
        info.seen_ts = book.ts
        if info.gap_blocked:
            return True
        if self.max_gap_s is None or not w.triggered or last is None or book.ts - last <= self.max_gap_s:
            return False
        info.gap_blocked = True
        self.gap_blocked += 1
        w.order.raw = {**w.order.raw, "recording_gap_s": round(book.ts - last, 3)}
        return True

    def _match_book(self, w: _Working, info: _BtOrder, book: BookSnapshot, now: float) -> list[Fill]:
        if not w.triggered:
            self._check_trigger(w, book, now)
            if w.triggered:
                info.ref_ts = book.ts  # a triggered stop fills on a newer snapshot only
                info.crossing = w.takes_liquidity
            return []
        return self._match_market(w, book, now) if w.is_market else self._match_limit(w, book, now)

    def _match_limit(self, w: _Working, book: BookSnapshot, now: float) -> list[Fill]:
        """PaperExchange's limit match, but an order that was crossing when it entered the book stays
        a taker for every fill (books driver; PaperExchange lets its remainder rest as a maker)."""
        if self.driver == "books" and self._info(w).crossing:
            w.takes_liquidity = True
        return super()._match_limit(w, book, now)

    def _match_bar(self, w: _Working, info: _BtOrder, bar: Candle, now: float) -> list[Fill]:
        req = w.order.req
        market = self._markets[req.symbol]
        before = CandleOrder(
            side=req.side,
            type=req.type,
            qty=w.order.remaining_qty,
            price=req.price,
            stop_price=req.stop_price,
            stop_direction=w.stop_direction,
            triggered=w.triggered,
            crossing=info.crossing,
            first_pass=w.takes_liquidity,
        )
        m = match_candle(
            before,
            bar,
            spread_pct=self.spread_for(req.symbol),
            tick_decimals=market.tick_decimals,
            step_decimals=market.step_decimals,
            slippage=self.slippage,
        )
        w.triggered = m.order.triggered or not m.order.is_stop
        w.takes_liquidity = m.order.first_pass
        info.crossing = m.order.crossing
        info.ref_ts = float(bar.ts)  # each bar is looked at once (the engine ticks faster than bars)
        if m.triggered_now:
            w.order.raw = {**w.order.raw, "triggered": True, "triggered_ts": now}
            w.order.updated_ts = now
        fills: list[Fill] = []
        short = False
        for leg in m.legs:
            fill = self._fill_leg(w, leg, now)
            if fill is not None:
                fills.append(fill)
            if fill is None or fill.qty < leg.qty:
                short = True
                break
        if m.legs:
            w.order.raw = {**w.order.raw, "fill_rule": m.rule}
        if not w.order.is_terminal and (m.cancel_remainder or short):
            if short:
                w.order.raw = {**w.order.raw, "cancel_reason": "insufficient_funds"}
            self._finish(w, "CANCELED", now)
        return fills

    def _fill_leg(self, w: _Working, leg: FillLeg, now: float) -> Fill | None:
        """Apply one candle fill; a BUY that costs more than its lock tops the lock up from free
        funds and is cut to what the account can pay (``None`` when it can pay nothing)."""
        qty = min(leg.qty, w.order.remaining_qty)
        req = w.order.req
        if req.side == "BUY":
            asset = w.lock_asset
            free = self._free.get(asset, D0)
            if qty * leg.price > w.lock_left:
                affordable = quantize_down(
                    (w.lock_left + free) / leg.price, self._markets[req.symbol].step_decimals
                )
                qty = min(qty, affordable)
                if qty <= 0:
                    return None
                shortfall = max(qty * leg.price - w.lock_left, D0)
                self._free[asset] = free - shortfall
                self._locked[asset] = self._locked.get(asset, D0) + shortfall
                w.lock_left += shortfall
        if qty <= 0:
            return None
        return self._apply_fill(w, leg.price, qty, leg.is_maker, now)

    def _walk(
        self, w: _Working, book: BookSnapshot, price_ok: Callable[[Decimal], bool]
    ) -> list[tuple[Decimal, Decimal]]:
        """PaperExchange's level walk, capped at ``level_fraction`` of each level's quantity minus
        what our fills already took from that level (``_carried_taken``)."""
        req = w.order.req
        book_side = "ask" if req.side == "BUY" else "bid"
        taken = self._carried_taken(book)
        already = {price: qty for (side, price), qty in taken.items() if side == book_side}
        takes = walk_levels(
            opposite_levels(book, req.side),
            w.order.remaining_qty,
            price_ok,
            already,
            self.level_fraction,
            self._markets[req.symbol].step_decimals,
        )
        for price, qty in takes:
            taken[(book_side, price)] = taken.get((book_side, price), D0) + qty
        return takes

    def _carried_taken(self, book: BookSnapshot) -> dict[tuple[str, Decimal], Decimal]:
        """Liquidity our fills took from ``book``'s levels: this snapshot's, plus, on a newer snapshot
        of the symbol, what was taken from a level that is still shown at the same price (a recorded
        snapshot never reflects our own fills). A level no longer shown is dropped."""
        current = self._level_taken.get(book.symbol)
        if current is not None and current[0] == book.ts:
            return current[1]
        taken: dict[tuple[str, Decimal], Decimal] = {}
        if current is not None and book.ts > current[0]:
            shown = {("ask", lv.price) for lv in book.asks} | {("bid", lv.price) for lv in book.bids}
            taken = {key: qty for key, qty in sorted(current[1].items()) if key in shown and qty > 0}
        self._level_taken[book.symbol] = (book.ts, taken)
        return taken

    def _apply_fill(self, w: _Working, price: Decimal, qty: Decimal, is_maker: bool, now: float) -> Fill:
        fill = super()._apply_fill(w, price, qty, is_maker, now)
        renamed = fill.model_copy(update={"trade_id": f"bt-{self._trade_seq}"})
        self._trades[-1] = renamed
        return renamed

    # ------------------------------------------------------------ persistence

    def state(self) -> dict[str, Any]:
        """PaperExchange state + the backtest bookkeeping of open orders and the ``set_bar`` bars."""
        out = super().state()
        open_ids = {w.order.client_id for w in self._open_working()}
        out["backtest"] = {
            "version": BACKTEST_STATE_VERSION,
            "driver": self.driver,
            "orders": {
                cid: {"ref_ts": info.ref_ts, "crossing": info.crossing, "seen_ts": info.seen_ts,
                      "gap_blocked": info.gap_blocked}
                for cid, info in sorted(self._bt.items())
                if cid in open_ids
            },
            "gap_blocked": self.gap_blocked,
            "bars": {
                sym: {
                    "candle": bar.candle.model_dump(mode="json"),
                    "spread_pct": None if bar.spread_pct is None else str(bar.spread_pct),
                }
                for sym, bar in sorted(self._bars.items())
            },
        }
        return out

    def load_state(self, state: dict[str, Any]) -> None:
        """Replace everything with a ``state()`` snapshot (a plain PaperExchange state also loads)."""
        super().load_state(state)
        self._level_taken = {}
        bt = state.get("backtest") or {}
        version = bt.get("version", BACKTEST_STATE_VERSION)
        if version != BACKTEST_STATE_VERSION:
            raise ValueError(f"unsupported backtest state version {version!r}")
        saved_driver = bt.get("driver", self.driver)
        if saved_driver != self.driver:
            raise ValueError(f"backtest state was saved by the {saved_driver} driver, not {self.driver}")
        self._bt = {}
        self.gap_blocked = int(bt.get("gap_blocked", 0) or 0)
        for cid, item in sorted((bt.get("orders") or {}).items()):
            ref, seen = item.get("ref_ts"), item.get("seen_ts")
            self._bt[cid] = _BtOrder(
                ref_ts=None if ref is None else float(ref), crossing=bool(item["crossing"]),
                seen_ts=None if seen is None else float(seen), gap_blocked=bool(item.get("gap_blocked", False)),
            )
        self._bars = {}
        for sym, item in sorted((bt.get("bars") or {}).items()):
            spread = item.get("spread_pct")
            self._bars[sym] = _Bar(
                Candle.model_validate(item["candle"]), None if spread is None else D(spread)
            )

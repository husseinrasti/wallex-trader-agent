"""Portfolio state and position bookkeeping (PRD-02 §2).

``PortfolioState`` is the single object the engine owns: balances (exchange truth after every
refresh/reconcile), our strategy-tagged positions, open orders, equity in TMN, peak/drawdown,
day-start equity, fees, placement counters and realized PnL. It is persisted to the store's
``kv`` table so a restart never resets peak equity, daily PnL, fees or order counters.

``PositionManager`` turns fills into position opens/closes with realized PnL in TMN, and emits
deterministic exit intents for software stops, targets and time stops.

Equity (TMN) = TMN total + USDT total x bid(USDTTMN) + sum(coin total x bid(coinTMN)),
falling back to bid(coinUSDT) x bid(USDTTMN) when the coin has no TMN book.

A valuation is *usable* only when every held asset has a price and equity is positive. Peak,
day-start equity and the risk ratios (drawdown, daily PnL, total return) only ever move on usable
valuations: a book that has not arrived yet (restart) or an empty balance read must never look like
a crash, because that would fire the drawdown kill-switch or the daily-loss pause for nothing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from wta.models import D0, Balance, BookSnapshot, Fill, Intent, MarketInfo, Order, OrderRequest, Position
from wta.utils.decimalutil import D, check_min_notional
from wta.utils.ids import make_decision_id, make_position_id
from wta.utils.timeutil import day_start

FIAT = "TMN"
USDT = "USDT"
USDTTMN = "USDTTMN"


def _bid(books: dict[str, BookSnapshot], symbol: str) -> Decimal | None:
    b = books.get(symbol)
    return b.best_bid if b is not None else None


def asset_price_tmn(asset: str, books: dict[str, BookSnapshot]) -> Decimal | None:
    """Conservative (bid-side) TMN price of one unit of ``asset``; None if unknown."""
    if asset == FIAT:
        return Decimal(1)
    u_bid = _bid(books, USDTTMN)
    if asset == USDT:
        return u_bid
    direct = _bid(books, f"{asset}TMN")
    if direct is not None:
        return direct
    via_usdt = _bid(books, f"{asset}USDT")
    if via_usdt is not None and u_bid is not None:
        return via_usdt * u_bid
    return None


def value_tmn(asset: str, qty: Decimal, books: dict[str, BookSnapshot]) -> Decimal | None:
    p = asset_price_tmn(asset, books)
    return None if p is None else qty * p


def quote_to_tmn(quote: str, amount: Decimal, books: dict[str, BookSnapshot]) -> Decimal | None:
    """Convert an amount in a quote currency (TMN or USDT) to TMN at the current USDTTMN bid."""
    return value_tmn(quote, amount, books)


@dataclass(frozen=True)
class Valuation:
    equity_tmn: Decimal
    tmn: Decimal
    usdt_tmn_value: Decimal
    coins_tmn_value: Decimal
    per_asset_tmn: dict[str, Decimal]
    missing_prices: tuple[str, ...] = ()

    @property
    def complete(self) -> bool:
        return not self.missing_prices

    @property
    def usable(self) -> bool:
        """Complete and positive: safe to move peak / day start and to compute risk ratios on."""
        return self.complete and self.equity_tmn > 0


def valuate(balances: dict[str, Balance], books: dict[str, BookSnapshot]) -> Valuation:
    per: dict[str, Decimal] = {}
    missing: list[str] = []
    for asset in sorted(balances):
        total = balances[asset].total
        if total == 0:
            continue
        v = value_tmn(asset, total, books)
        if v is None:
            missing.append(asset)
            continue
        per[asset] = v
    tmn = per.get(FIAT, D0)
    usdt = per.get(USDT, D0)
    coins = sum((v for a, v in per.items() if a not in (FIAT, USDT)), D0)
    return Valuation(tmn + usdt + coins, tmn, usdt, coins, per, tuple(missing))


@dataclass
class PortfolioState:
    initial_equity_tmn: Decimal
    balances: dict[str, Balance] = field(default_factory=dict)
    positions: dict[str, Position] = field(default_factory=dict)
    open_orders: dict[str, Order] = field(default_factory=dict)
    peak_equity_tmn: Decimal | None = None
    day_start_equity_tmn: Decimal | None = None
    day_start_ts: float | None = None
    fees_paid_tmn_total: Decimal = D0
    placements: list[float] = field(default_factory=list)  # placement timestamps (rate limits)
    realized_pnl_tmn: dict[str, Decimal] = field(default_factory=dict)  # by strategy
    last_valuation: Valuation | None = None
    last_refresh_ts: float | None = None
    # equity of the latest *usable* valuation (complete and > 0); not persisted — the first usable
    # refresh after a restart sets it again
    last_complete_equity_tmn: Decimal | None = None

    # ------------------------------------------------------------ equity

    def valuation(self, books: dict[str, BookSnapshot]) -> Valuation:
        return valuate(self.balances, books)

    def refresh(self, books: dict[str, BookSnapshot], now: float) -> Valuation:
        """Recompute equity; update peak and the UTC-day start. Call every fast tick.

        Peak and day start only move on a usable valuation (see the module docstring); a partial
        one is still stored in ``last_valuation`` so callers can see ``missing_prices``.
        """
        val = self.valuation(books)
        self.last_valuation = val
        self.last_refresh_ts = now
        if not val.usable:
            return val  # never move peak/day-start on a partial (or empty) valuation
        eq = val.equity_tmn
        self.last_complete_equity_tmn = eq
        if self.peak_equity_tmn is None or eq > self.peak_equity_tmn:
            self.peak_equity_tmn = eq
        today = day_start(now)
        if self.day_start_ts is None or today > self.day_start_ts:
            self.day_start_ts = today
            self.day_start_equity_tmn = eq
        return val

    @property
    def equity_tmn(self) -> Decimal | None:
        """Equity of the latest valuation (may be partial: check ``last_valuation.missing_prices``)."""
        return self.last_valuation.equity_tmn if self.last_valuation else None

    def _risk_equity(self) -> Decimal | None:
        """Equity the risk ratios use: the latest valuation if usable, else the last usable one."""
        v = self.last_valuation
        if v is not None and v.usable:
            return v.equity_tmn
        return self.last_complete_equity_tmn

    @property
    def drawdown_pct(self) -> Decimal:
        eq, peak = self._risk_equity(), self.peak_equity_tmn
        if eq is None or not peak:
            return D0
        return max((peak - eq) / peak, D0)

    @property
    def daily_pnl_pct(self) -> Decimal:
        eq, start = self._risk_equity(), self.day_start_equity_tmn
        if eq is None or not start:
            return D0
        return eq / start - 1

    @property
    def total_return_pct(self) -> Decimal:
        eq = self._risk_equity()
        if eq is None or not self.initial_equity_tmn:
            return D0
        return eq / self.initial_equity_tmn - 1

    @property
    def coin_exposure_pct(self) -> Decimal:
        v = self.last_valuation
        if v is None or v.equity_tmn == 0:
            return D0
        return v.coins_tmn_value / v.equity_tmn

    @property
    def usdt_share(self) -> Decimal:
        v = self.last_valuation
        if v is None or v.equity_tmn == 0:
            return D0
        return v.usdt_tmn_value / v.equity_tmn

    # ------------------------------------------------------------ balances

    def free(self, asset: str) -> Decimal:
        b = self.balances.get(asset)
        return b.free if b else D0

    def total(self, asset: str) -> Decimal:
        b = self.balances.get(asset)
        return b.total if b else D0

    def set_balances(self, balances: dict[str, Balance]) -> None:
        self.balances = {k: balances[k] for k in sorted(balances)}

    def apply_fill_optimistic(self, fill: Fill, market: MarketInfo) -> None:
        """Optimistic balance update between fill and the next exchange balance refresh."""
        base, quote = market.base, market.quote
        value = fill.qty * fill.price
        sign = 1 if fill.side == "BUY" else -1
        self._adjust(base, sign * fill.qty)
        self._adjust(quote, -sign * value)
        if fill.fee and fill.fee_asset:
            self._adjust(fill.fee_asset, -fill.fee)

    def _adjust(self, asset: str, delta: Decimal) -> None:
        b = self.balances.get(asset, Balance(asset=asset, free=D0, locked=D0))
        self.balances[asset] = Balance(asset=asset, free=b.free + delta, locked=b.locked)
        self.balances = {k: self.balances[k] for k in sorted(self.balances)}

    # ------------------------------------------------------------ counters

    def record_placement(self, ts: float) -> None:
        self.placements.append(ts)
        cutoff = ts - 2 * 86400
        self.placements = [t for t in self.placements if t >= cutoff]

    def orders_last_hour(self, now: float) -> int:
        return sum(1 for t in self.placements if now - 3600 < t <= now)

    def orders_today(self, now: float) -> int:
        start = day_start(now)
        return sum(1 for t in self.placements if start <= t <= now)

    def add_fee_tmn(self, fee_tmn: Decimal) -> None:
        self.fees_paid_tmn_total += fee_tmn

    def add_realized(self, strategy: str, pnl_tmn: Decimal) -> None:
        self.realized_pnl_tmn[strategy] = self.realized_pnl_tmn.get(strategy, D0) + pnl_tmn

    @property
    def realized_total_tmn(self) -> Decimal:
        return sum(self.realized_pnl_tmn.values(), D0)

    def positions_by_strategy(self, strategy: str) -> list[Position]:
        return [p for _, p in sorted(self.positions.items()) if p.strategy == strategy]

    def open_orders_for(self, symbol: str) -> list[Order]:
        return [o for _, o in sorted(self.open_orders.items()) if o.req.symbol == symbol]

    # ------------------------------------------------------------ persistence

    def to_state(self) -> dict[str, Any]:
        """JSON-able durable counters (positions/orders are persisted in their own tables)."""
        return {
            "peak_equity_tmn": str(self.peak_equity_tmn) if self.peak_equity_tmn is not None else None,
            "day_start_equity_tmn": (
                str(self.day_start_equity_tmn) if self.day_start_equity_tmn is not None else None
            ),
            "day_start_ts": self.day_start_ts,
            "fees_paid_tmn_total": str(self.fees_paid_tmn_total),
            "placements": list(self.placements),
            "realized_pnl_tmn": {k: str(v) for k, v in sorted(self.realized_pnl_tmn.items())},
        }

    def load_state(self, state: dict[str, Any] | None) -> None:
        if not state:
            return
        if state.get("peak_equity_tmn") is not None:
            self.peak_equity_tmn = D(state["peak_equity_tmn"])
        if state.get("day_start_equity_tmn") is not None:
            self.day_start_equity_tmn = D(state["day_start_equity_tmn"])
        self.day_start_ts = state.get("day_start_ts")
        self.fees_paid_tmn_total = D(state.get("fees_paid_tmn_total", "0"))
        self.placements = sorted(float(t) for t in state.get("placements", []))
        self.realized_pnl_tmn = {k: D(v) for k, v in (state.get("realized_pnl_tmn") or {}).items()}


# ---------------------------------------------------------------- positions


@dataclass(frozen=True)
class ClosedPosition:
    position: Position
    pnl_tmn: Decimal
    pnl_pct: Decimal
    hold_s: float
    exit_reason: str
    closed_ts: float
    decision_id: str  # the entry decision this outcome belongs to
    pnl_complete: bool = True  # False when a leg could not be valued in TMN (no book at fill time)

    def outcome(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "pnl_tmn": str(self.pnl_tmn),
            "pnl_pct": str(self.pnl_pct),
            "hold_s": round(self.hold_s, 3),
            "closed_ts": self.closed_ts,
            "exit_reason": self.exit_reason,
            "position_id": self.position.id,
            "strategy": self.position.strategy,
            "symbol": self.position.symbol,
        }
        dust = self.position.meta.get("dust_qty")
        if dust:
            out["dust_qty"] = str(dust)
        if not self.pnl_complete:
            out["pnl_complete"] = False
            out["note"] = (
                "a fill could not be valued in TMN (no book): this PnL is incomplete and was not "
                "added to the realized total"
            )
        return out


ENTRY_PURPOSES = frozenset({"entry"})
CLOSING_PURPOSES = frozenset({"exit", "stop", "leg2", "flatten"})


class PositionManager:
    """Opens/closes positions from fills; computes realized PnL in TMN; emits exit intents.

    A position is opened by the first fill of an ``entry`` order and identified by
    ``req.decision_id``-derived id (strategies pass it back as ``linked_position_id``).
    Closing fills (exit/stop/leg2/flatten) reduce its quantity — possibly in another book of the
    same base coin (S1 lock-in leg 2) — and the position closes when the remainder is zero or can
    no longer be sold as an order of its own: at or below the market's minimum quantity, or below
    its minimum notional at the closing price. That remainder stays on the account as *dust*; its
    value at the closing price counts as value out (``meta.dust_qty`` / ``meta.dust_value_tmn``), so
    the PnL does not book coins we still hold as a loss. A dust position would otherwise stay open
    for good, re-emit exits every tick and block a ``MAX_POSITIONS`` slot.

    A leg that cannot be converted to TMN (no USDTTMN / coin book at that moment) is never booked
    as worth 0 silently: the position is marked (``meta.unpriced_legs``), its outcome says
    ``pnl_complete: false`` and its PnL is kept out of the realized total.

    PnL = value out − value in − fees, every leg converted to TMN at fill time. A fee charged in the
    position's base coin (e.g. a BUY whose fee is taken from the received BTC) never reaches our
    balance: it reduces the position quantity instead, and because the smaller quantity already
    lowers "value out", it is not subtracted a second time (``meta.fees_in_qty_tmn``).
    ``entry_price`` is the average fill price weighted by the filled (gross) quantity.
    """

    def __init__(self, portfolio: PortfolioState, markets: dict[str, MarketInfo]) -> None:
        self.portfolio = portfolio
        self.markets = markets
        self._seq = 0
        # entry decision id -> position id, so later fills of the same order map to it
        self._entry_map: dict[str, str] = {}

    def position_id_for(self, req: OrderRequest, ts: float) -> str:
        """Position id for an entry order: the existing one for its decision id, else a new id.

        After a restart ``_entry_map`` is empty, so open positions are also searched by their
        ``meta.entry_decision_id`` — a late fill of a pre-restart entry order adds to its position.
        """
        pid = self._entry_map.get(req.decision_id) or self._position_of_entry(req.decision_id)
        if pid is None:
            self._seq += 1
            pid = make_position_id(req.strategy, req.symbol, ts, self._seq)
            while pid in self.portfolio.positions:  # restart: _seq starts at 0 again
                self._seq += 1
                pid = make_position_id(req.strategy, req.symbol, ts, self._seq)
        self._entry_map[req.decision_id] = pid
        return pid

    def _position_of_entry(self, decision_id: str) -> str | None:
        for pid, pos in sorted(self.portfolio.positions.items()):
            if pos.meta.get("entry_decision_id") == decision_id:
                return pid
        return None

    def _find_position_id(self, req: OrderRequest, fill: Fill) -> str | None:
        """Position a closing fill belongs to when no intent links it explicitly.

        Tries the entry-decision map (and its restart fallback), then a server-side stop order
        recorded on a position (``stop_order_client_id``), so a stop that fires is booked even when
        the caller no longer has the stop's intent.
        """
        pid = self._entry_map.get(req.decision_id) or self._position_of_entry(req.decision_id)
        if pid is not None:
            return pid
        for pid, pos in sorted(self.portfolio.positions.items()):
            if pos.stop_order_client_id is not None and pos.stop_order_client_id == fill.order_client_id:
                return pid
        return None

    def on_fill(
        self,
        fill: Fill,
        req: OrderRequest,
        books: dict[str, BookSnapshot],
        intent: Intent | None = None,
    ) -> ClosedPosition | Position | None:
        """Apply one fill. Returns the (new/updated) Position, a ClosedPosition, or None."""
        market = self.markets.get(req.symbol)
        if market is None or fill.qty <= 0:
            return None
        gross = fill.qty * fill.price
        gross_value = quote_to_tmn(market.quote, gross, books)
        fee_value = value_tmn(fill.fee_asset, fill.fee, books) if fill.fee and fill.fee_asset else D0
        unpriced = gross_value is None or fee_value is None
        gross_tmn, fee_tmn = gross_value or D0, fee_value or D0
        self.portfolio.add_fee_tmn(fee_tmn)
        base_fee = fill.fee if fill.fee and fill.fee_asset == market.base else D0

        linked = intent.linked_position_id if intent is not None else None
        if req.purpose in ENTRY_PURPOSES and linked is None:
            pos = self._open_or_add(fill, req, market, gross_tmn, fee_tmn, base_fee, intent)
            return _mark_unpriced(pos, unpriced)
        pid = linked or self._find_position_id(req, fill)
        if pid is None or pid not in self.portfolio.positions:
            return None  # rebalance / flatten of free balances: no strategy position involved
        if req.purpose in ENTRY_PURPOSES:
            pos = self._open_or_add(fill, req, market, gross_tmn, fee_tmn, base_fee, intent, pid=pid)
            return _mark_unpriced(pos, unpriced)
        _mark_unpriced(self.portfolio.positions[pid], unpriced)
        return self._reduce(pid, fill, req, gross_tmn, fee_tmn, base_fee, intent, books)

    def _open_or_add(
        self,
        fill: Fill,
        req: OrderRequest,
        market: MarketInfo,
        gross_tmn: Decimal,
        fee_tmn: Decimal,
        base_fee: Decimal,
        intent: Intent | None,
        pid: str | None = None,
    ) -> Position:
        pid = pid or self.position_id_for(req, fill.ts)
        pos = self.portfolio.positions.get(pid)
        if pos is None:
            meta: dict[str, Any] = {
                "entry_decision_id": req.decision_id,
                "value_in_tmn": "0",
                "value_out_tmn": "0",
                "fees_tmn": "0",
                "fees_in_qty_tmn": "0",
                "entry_fill_qty": "0",
                "side": fill.side,
            }
            if intent is not None:
                meta.update({k: v for k, v in intent.features.items() if k in _POSITION_META_KEYS})
            pos = Position(
                id=pid,
                strategy=req.strategy,
                symbol=req.symbol,
                base=market.base,
                quote=market.quote,
                qty=D0,
                entry_price=fill.price,
                entry_ts=fill.ts,
                stop_price=intent.stop_price if intent is not None else None,
                target_price=_dec_or_none(intent.features.get("target_price")) if intent else None,
                time_stop_ts=(
                    fill.ts + float(intent.features["time_stop_s"])
                    if intent is not None and intent.features.get("time_stop_s") is not None
                    else None
                ),
                meta=meta,
            )
            self.portfolio.positions[pid] = pos
        meta = dict(pos.meta)
        filled_before = D(meta.get("entry_fill_qty", pos.qty))
        filled_after = filled_before + fill.qty
        pos.entry_price = (pos.entry_price * filled_before + fill.price * fill.qty) / filled_after
        pos.qty = pos.qty + fill.qty - base_fee
        meta["entry_fill_qty"] = str(filled_after)
        meta["value_in_tmn"] = str(D(meta["value_in_tmn"]) + gross_tmn)
        meta["fees_tmn"] = str(D(meta["fees_tmn"]) + fee_tmn)
        if base_fee:
            meta["fees_in_qty_tmn"] = str(D(meta.get("fees_in_qty_tmn", "0")) + fee_tmn)
        pos.meta = meta
        return pos

    def _reduce(
        self,
        pid: str,
        fill: Fill,
        req: OrderRequest,
        gross_tmn: Decimal,
        fee_tmn: Decimal,
        base_fee: Decimal,
        intent: Intent | None,
        books: dict[str, BookSnapshot],
    ) -> Position | ClosedPosition:
        pos = self.portfolio.positions[pid]
        pos.qty = max(pos.qty - fill.qty - base_fee, D0)
        meta = dict(pos.meta)
        meta["value_out_tmn"] = str(D(meta["value_out_tmn"]) + gross_tmn)
        meta["fees_tmn"] = str(D(meta["fees_tmn"]) + fee_tmn)
        if base_fee:
            meta["fees_in_qty_tmn"] = str(D(meta.get("fees_in_qty_tmn", "0")) + fee_tmn)
        meta["last_exit_symbol"] = req.symbol
        pos.meta = meta
        market = self.markets.get(pos.symbol)
        # the price the remainder would sell at in its own market (the closing fill's price when
        # that fill was in the same book; leg 2 trades in the other book, so then its bid)
        price = fill.price if req.symbol == pos.symbol else _bid(books, pos.symbol)
        if pos.qty > 0 and market is not None and _sellable(market, pos.qty, price):
            return pos
        if pos.qty > 0:
            self._book_dust(pos, market, price, books)
        reason = "lockin" if req.purpose == "leg2" else req.purpose
        if intent is not None and intent.features.get("exit_reason"):
            reason = str(intent.features["exit_reason"])
        return self._close(pid, fill.ts, reason)

    @staticmethod
    def _book_dust(
        pos: Position, market: MarketInfo | None, price: Decimal | None, books: dict[str, BookSnapshot]
    ) -> None:
        """The unsellable remainder stays on the account: count its value as value out."""
        value: Decimal | None = None
        if market is not None and price is not None:
            value = quote_to_tmn(market.quote, pos.qty * price, books)
        if value is None:
            value = value_tmn(pos.base, pos.qty, books)
        meta = {**pos.meta, "dust_qty": format(pos.qty, "f")}
        if value is None:
            meta["unpriced_legs"] = int(meta.get("unpriced_legs", 0)) + 1
        else:
            meta["dust_value_tmn"] = str(value)
            meta["value_out_tmn"] = str(D(meta["value_out_tmn"]) + value)
        pos.meta = meta

    def _close(self, pid: str, ts: float, reason: str) -> ClosedPosition:
        pos = self.portfolio.positions.pop(pid)
        vin = D(pos.meta["value_in_tmn"])
        vout = D(pos.meta["value_out_tmn"])
        # base-coin fees already shrank the quantity (and so value out): do not count them twice
        fees = D(pos.meta["fees_tmn"]) - D(pos.meta.get("fees_in_qty_tmn", "0"))
        pnl = vout - vin - fees
        pnl_pct = pnl / vin if vin else D0
        complete = not pos.meta.get("unpriced_legs")
        if complete:
            self.portfolio.add_realized(pos.strategy, pnl)
        return ClosedPosition(
            position=pos,
            pnl_tmn=pnl,
            pnl_pct=pnl_pct,
            hold_s=ts - pos.entry_ts,
            exit_reason=reason,
            closed_ts=ts,
            decision_id=str(pos.meta.get("entry_decision_id", "")),
            pnl_complete=complete,
        )

    def force_close(self, pid: str, ts: float, reason: str, books: dict[str, BookSnapshot]) -> ClosedPosition:
        """Close a position whose coins disappeared (e.g. server stop fired while we were down).

        The remaining quantity is valued at the current bid as the exit value (reconcile then
        corrects balances from the exchange). Without a price the position is still closed but
        marked unpriced (its PnL stays out of the realized total); the Reconciler avoids that by
        not force-closing before the books are there.
        """
        pos = self.portfolio.positions[pid]
        if pos.qty > 0:
            v = value_tmn(pos.base, pos.qty, books)
            _mark_unpriced(pos, v is None)
            pos.meta = {**pos.meta, "value_out_tmn": str(D(pos.meta["value_out_tmn"]) + (v or D0))}
            pos.qty = D0
        return self._close(pid, ts, reason)

    # ------------------------------------------------------------ exits

    def check_exits(
        self,
        books: dict[str, BookSnapshot],
        now: float,
        software_stops: bool,
        seq_start: int = 0,
    ) -> list[Intent]:
        """Deterministic exit intents: software stop (if enabled for the position), target, time stop.

        A position with a live server-side stop (``meta.stop_mode == "server"`` and its
        ``stop_order_client_id`` among our open orders) is not software-stopped here — unless that
        STOP_LIMIT can no longer fill: the bid fell below its limit price (a gap through
        stop x (1 - slippage)). Then the stop exit is emitted anyway; the OrderManager cancels the
        stranded stop (booking any partial fill) before it sells at the bid. A stop id that is not
        among our open orders protects nothing, so the software stop takes over.
        """
        intents: list[Intent] = []
        seq = seq_start
        for pid, pos in sorted(self.portfolio.positions.items()):
            if pos.qty <= 0 or pos.meta.get("exit_pending"):
                continue
            book = books.get(pos.symbol)
            bid = book.best_bid if book else None
            if bid is None:
                continue
            reason: str | None = None
            stop_order = self.portfolio.open_orders.get(pos.stop_order_client_id or "")
            server_stop = (
                pos.meta.get("stop_mode") == "server" and stop_order is not None and not stop_order.is_terminal
            )
            stranded = (
                server_stop
                and stop_order is not None
                and stop_order.req.type == "STOP_LIMIT"
                and stop_order.req.price is not None
                and bid < stop_order.req.price
            )
            hit = pos.stop_price is not None and bid <= pos.stop_price
            if hit and (software_stops or not server_stop or stranded):
                reason = "stop"
            elif pos.target_price is not None and bid >= pos.target_price:
                reason = "target"
            elif pos.time_stop_ts is not None and now >= pos.time_stop_ts:
                reason = "time_stop"
            if reason is None:
                continue
            seq += 1
            purpose = "stop" if reason == "stop" else "exit"
            features: dict[str, Any] = {"exit_reason": reason, "bid": str(bid), "position_id": pid}
            text = _exit_reason_text(pos, reason, bid)
            if reason == "stop" and stranded and stop_order is not None:
                features["stranded_server_stop"] = stop_order.client_id
                text += (
                    f" The server stop {stop_order.client_id} cannot fill: bid {bid} is below its limit "
                    f"{stop_order.req.price}; it is canceled first."
                )
            intents.append(
                Intent(
                    id=make_decision_id(now, pos.strategy, pos.symbol, seq),
                    ts=now,
                    strategy=pos.strategy,
                    symbol=pos.symbol,
                    side="SELL",
                    order_type="LIMIT",
                    qty=pos.qty,
                    price=None,
                    purpose=purpose,
                    reason=text,
                    features=features,
                    linked_position_id=pid,
                    taker=reason in ("stop", "time_stop"),
                )
            )
        return intents


_POSITION_META_KEYS = frozenset(
    {"mode", "leg2_symbol", "cycle", "dev_mid_pct", "entry_dev_pct", "target_dev_pct", "regime", "atr_pct"}
)


def _dec_or_none(v: Any) -> Decimal | None:
    return None if v is None else D(v)


def _sellable(market: MarketInfo, qty: Decimal, price: Decimal | None) -> bool:
    """Can ``qty`` still be sold as an order of its own? Above minQty and, when a price is known,
    at or above minNotional after rounding to the step."""
    if qty <= market.min_qty:
        return False
    return price is None or check_min_notional(market, qty, price)


def _mark_unpriced(pos: Position, unpriced: bool) -> Position:
    """Count a leg that could not be valued in TMN (its PnL becomes incomplete)."""
    if unpriced:
        pos.meta = {**pos.meta, "unpriced_legs": int(pos.meta.get("unpriced_legs", 0)) + 1}
    return pos


def _exit_reason_text(pos: Position, reason: str, bid: Decimal) -> str:
    if reason == "stop":
        return f"{pos.symbol}: bid {bid} <= stop {pos.stop_price} for position {pos.id} -> sell {pos.qty} (stop)."
    if reason == "target":
        return f"{pos.symbol}: bid {bid} >= target {pos.target_price} for position {pos.id} -> sell {pos.qty}."
    return f"{pos.symbol}: time stop reached for position {pos.id} (held since {pos.entry_ts:.0f}) -> sell {pos.qty}."

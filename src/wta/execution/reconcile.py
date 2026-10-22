"""Reconciler — make our in-memory state agree with the exchange (PRD-02 §5).

The exchange is the source of truth. ``Reconciler.run`` is called at startup, every
``engine.reconcile_interval_s`` (5 min) and after an API-error burst. It compares what we *think*
(tracked orders, positions, balances) with what the exchange *says* and repairs our side:

1. Fetch open orders and our own trades of the last 24 h, then balances — **balances last**: a fill
   landing between two reads then shows up only in the balances (logged as drift, booked by the
   OrderManager's next poll), never as a booked fill whose coins the balances do not show yet
   (that would force-close the fresh position as a phantom stop).
2. **Missed fills.** For every order the OrderManager tracks, the exchange's fills beyond the
   quantity we already counted are applied: order fill state, optimistic balances, positions and
   realized PnL. Fills are de-duplicated by ``trade_id`` (or ``(client_id, qty, price, ts)``) and
   matched by quantity, so a fill is never counted twice — not even across runs. A fill whose
   market cannot be valued in TMN yet (no USDTTMN / coin book) is left for the next run: it is
   never booked at 0 TMN.
3. **Untracked open orders.** Ours (``wta.utils.ids.is_ours``) are adopted by the OrderManager with
   the request (and intent) stored when they were placed; foreign ones (manual/UI orders) are
   logged as ``foreign_order`` and **never touched**.
4. **Tracked orders missing from the exchange's open orders** are fetched one by one; their last
   fills are applied and they are finalized with the exchange's final status through
   ``OrderManager.finish_external`` — the same bookkeeping as an order the manager saw end
   itself (a dead server stop hands its position to the software stop; a must-finish order
   re-crosses its remainder).
5. **Positions whose coins are gone** (base balance < qty, e.g. a server stop fired while we were
   down) are closed with ``exit_reason="stop"`` at the current bid — except positions that got a
   fill in this run or still have a live order of ours (their coins may be in flight; the order
   path books them), and positions whose coin has no price yet (retried next run).
6. **Balance drift** above ``risk.reconcile_drift_pct`` of equity (in TMN) for any asset is logged
   as ``reconcile_drift``. Balances are then *always* overwritten with the exchange values.
7. Positions, orders and the portfolio counters are persisted to the store.

Safety rules:

* The reconciler never places or cancels an order on its own. Through the OrderManager it can
  cause two kinds: the protective stop of a position opened by a fill *we only learned about
  here* (``OrderManager.place_stop_for``, after step 5), and the re-placement of a must-finish
  order's remainder when it finalizes that order — exactly what the OrderManager does on its own
  poll.
* A step whose input could not be fetched is skipped, never guessed (no balances -> no position or
  drift checks; no open-orders list -> nothing is adopted or finalized).
* ``run`` never raises on API or storage errors: they are recorded in ``ReconcileReport.errors``.
* Run it from the same task as ``OrderManager.poll`` (never concurrently): both update the same
  order objects.

``startup_restore`` is the startup half of PRD-02 §5: it reloads the persisted counters, flags,
open positions and non-terminal orders, so a restart never resets peak equity, the UTC-day start
equity, the fee total or the order-rate counters.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Protocol, TypeVar

from wta.config import Config
from wta.data.store import Store
from wta.exchange.base import Exchange
from wta.execution.positions import (
    ClosedPosition,
    PortfolioState,
    PositionManager,
    asset_price_tmn,
    quote_to_tmn,
    valuate,
)
from wta.logging_.decision_log import DecisionLog, EventsLog
from wta.models import D0, Balance, BookSnapshot, Fill, Intent, MarketInfo, Order, OrderStatus, Position
from wta.risk.context import EngineFlags
from wta.utils.ids import is_ours
from wta.utils.timeutil import Clock

T = TypeVar("T")

# kv keys of the durable engine state (shared with the engine's periodic save).
KV_PORTFOLIO = "portfolio_state"
KV_FLAGS = "engine_flags"
KV_ORDER_MANAGER = "order_manager_state"

MY_TRADES_LOOKBACK_S = 24 * 3600
# An order placed this recently may not be listed by the exchange yet: do not finalize it.
MISSING_ORDER_GRACE_S = 10.0
# Fees charged in the bought coin leave a balance slightly below the filled quantity; a position
# counts as "coins gone" only when its balance is short by more than this fraction.
POSITION_BALANCE_TOLERANCE = Decimal("0.01")
# Run reasons that write a "reconcile" event only when the run found something.
QUIET_REASONS = frozenset({"scheduled", "startup"})


class OrderManagerLike(Protocol):
    """The part of ``wta.execution.order_manager.OrderManager`` the reconciler uses.

    ``managed`` maps the current client id of each live logical order to its ``ManagedOrder``
    (fields used here: ``intent``, ``order``, ``placed_ts``, ``logical_filled``, ``done``,
    ``done_reason``; ``position_id`` when present). An order manager may also offer
    ``async finish_external(m, status, why, books, now)``; without it the reconciler ends the order
    itself (and hands a dead server stop's position to the software stop).
    """

    managed: dict[str, Any]
    balances_dirty: bool

    def adopt(self, order: Order, intent: Intent | None = None) -> Any: ...

    async def place_stop_for(self, position: Position, books: dict[str, BookSnapshot]) -> Any: ...

    def to_state(self) -> dict[str, Any]: ...

    def load_state(self, state: dict[str, Any], orders: list[Order]) -> None: ...


@dataclass
class ReconcileReport:
    """What one reconcile run found and repaired (the engine logs it and the closed outcomes)."""

    reason: str
    ts: float
    drift: dict[str, dict[str, Decimal]] = field(default_factory=dict)  # asset -> computed/exchange/...
    adopted: list[str] = field(default_factory=list)  # our orders found untracked on the exchange
    foreign: list[str] = field(default_factory=list)  # orders without our client-id pattern
    finalized: list[str] = field(default_factory=list)  # tracked orders given their final status
    missed_fills: int = 0
    closed_positions: list[ClosedPosition] = field(default_factory=list)
    stops_placed: list[str] = field(default_factory=list)  # position ids protected after a recovered entry
    errors: list[str] = field(default_factory=list)
    # bookkeeping for the persist step (not part of the summary)
    _fills: list[Fill] = field(default_factory=list, repr=False)
    _orders: list[Order] = field(default_factory=list, repr=False)
    _recovered_entries: dict[str, str] = field(default_factory=dict, repr=False)  # position id -> entry cid
    _touched_positions: set[str] = field(default_factory=set, repr=False)  # positions a fill reached

    @property
    def ok(self) -> bool:
        return not self.errors

    @property
    def notable(self) -> bool:
        """True when the run found anything at all (worth an ``events.jsonl`` line)."""
        return bool(
            self.drift
            or self.adopted
            or self.foreign
            or self.finalized
            or self.missed_fills
            or self.closed_positions
            or self.stops_placed
            or self.errors
        )

    def summary(self) -> dict[str, Any]:
        """JSON-ready summary for the ``reconcile`` event."""
        return {
            "reason": self.reason,
            "ts": self.ts,
            "drift": {a: {k: str(v) for k, v in d.items()} for a, d in sorted(self.drift.items())},
            "adopted": list(self.adopted),
            "foreign": list(self.foreign),
            "finalized": list(self.finalized),
            "missed_fills": self.missed_fills,
            "closed_positions": [c.outcome() for c in self.closed_positions],
            "stops_placed": list(self.stops_placed),
            "errors": list(self.errors),
        }

    def log_outcomes(self, decision_log: DecisionLog) -> int:
        """Append the decision-log outcome of every position this run closed. Returns the count."""
        for closed in self.closed_positions:
            decision_log.outcome(closed.decision_id, closed.outcome())
        return len(self.closed_positions)


class Reconciler:
    """Compares tracked state with the exchange and repairs our side (see the module docstring)."""

    def __init__(
        self,
        exchange: Exchange,
        portfolio: PortfolioState,
        positions: PositionManager,
        order_manager: OrderManagerLike,
        store: Store | None,
        events_log: EventsLog | None,
        clock: Clock,
        cfg: Config,
    ) -> None:
        self.exchange = exchange
        self.portfolio = portfolio
        self.positions = positions
        self.order_manager = order_manager
        self.store = store
        self.events_log = events_log
        self.clock = clock
        self.cfg = cfg
        self._foreign_logged: set[str] = set()  # one foreign_order event per order per process

    # ------------------------------------------------------------ main entry point

    async def run(
        self, books: dict[str, BookSnapshot], now: float | None = None, reason: str = "scheduled"
    ) -> ReconcileReport:
        """One full reconcile pass. Never raises on API/storage errors (see ``report.errors``)."""
        now = self.clock.now() if now is None else now
        report = ReconcileReport(reason=reason, ts=now)

        # 1. exchange truth; balances last (see the module docstring)
        open_orders = await self._fetch(report, "open orders", self.exchange.get_open_orders)
        trades = await self._fetch(
            report, "my trades", self.exchange.get_my_trades, since_ts=now - MY_TRADES_LOOKBACK_S
        )
        balances = await self._fetch(report, "balances", self.exchange.get_balances)

        # 2. fills we missed on orders we track
        if trades is not None:
            await self._apply_trade_fills(trades, books, now, report)

        # 3. + 4. orders: adopt ours, flag foreign ones, finalize the ones that disappeared
        if open_orders is not None:
            self._adopt_or_flag(open_orders, report)
            await self._finalize_missing(open_orders, books, now, report)

        # 5. + 6. positions without coins, balance drift, exchange balances win
        if balances is not None:
            self._close_positions_without_coins(balances, open_orders, books, now, report)
            self._check_drift(balances, books, report)
            self.portfolio.set_balances(balances)
        await self._protect_recovered_entries(books, report)

        # 7. persist
        self._persist(balances, now, report)
        self._log_summary(report)
        return report

    async def _fetch(
        self, report: ReconcileReport, what: str, fn: Callable[..., Awaitable[T]], **kwargs: Any
    ) -> T | None:
        try:
            return await fn(**kwargs)
        except Exception as exc:  # noqa: BLE001 - reconcile must never take the engine down
            report.errors.append(f"{what}: {exc}")
            return None

    # ------------------------------------------------------------ 2. missed fills

    async def _apply_trade_fills(
        self, trades: list[Fill], books: dict[str, BookSnapshot], now: float, report: ReconcileReport
    ) -> None:
        """Apply my-trades fills of tracked orders that exceed the quantity we already counted."""
        by_order: dict[str, list[Fill]] = {}
        seen: set[str] = set()
        for fill in sorted(trades, key=lambda f: (f.ts, fill_key(f))):
            key = fill_key(fill)
            if not fill.order_client_id or key in seen:
                continue  # unmatched (no client id) or the same trade listed twice
            seen.add(key)
            by_order.setdefault(fill.order_client_id, []).append(fill)

        managed = self.order_manager.managed
        for cid in sorted(by_order):
            m = managed.get(cid)
            if m is None or m.done:
                continue
            fills = unaccounted_fills(by_order[cid], m.order.filled_qty, m.order.req.qty)
            if fills and not self._can_value(m.order, books, report):
                continue
            for fill in fills:
                self._apply_fill(m, fill, books, now, report)
            if m.order.req.qty > 0 and m.order.filled_qty >= m.order.req.qty:
                await self._finalize(m, "FILLED", "fully filled (fills found in my-trades)", books, now, report)

    def _can_value(self, order: Order, books: dict[str, BookSnapshot], report: ReconcileReport) -> bool:
        """True when a fill of ``order`` can be valued in TMN now (quote and base: the fee may be in
        either). Otherwise the fill is left for the next run, with an error in the report."""
        market: MarketInfo | None = self.positions.markets.get(order.req.symbol)
        if market is None:
            return True  # unknown market: on_fill ignores it anyway
        missing = [
            a
            for a in (market.quote, market.base)
            if quote_to_tmn(a, Decimal(1), books) is None
        ]
        if missing:
            report.errors.append(
                f"{order.client_id}: no TMN price for {', '.join(missing)} yet; its fills are booked on a later run"
            )
            return False
        return True

    def _apply_fill(
        self, m: Any, fill: Fill, books: dict[str, BookSnapshot], now: float, report: ReconcileReport
    ) -> None:
        """Apply one recovered fill to the order, the balances and the positions."""
        order: Order = m.order
        filled = order.filled_qty + fill.qty
        order.avg_price = ((order.avg_price or D0) * order.filled_qty + fill.price * fill.qty) / filled
        order.filled_qty = filled
        order.fee_paid = order.fee_paid + fill.fee
        if fill.fee_asset:
            order.fee_asset = fill.fee_asset
        if not order.is_terminal:
            order.status = "FILLED" if filled >= order.req.qty else "PARTIALLY_FILLED"
        order.updated_ts = now
        m.logical_filled = m.logical_filled + fill.qty
        if hasattr(m, "logical_value"):  # OrderManager's ManagedOrder: its outcome's avg price and fee
            m.logical_value = m.logical_value + fill.qty * fill.price
            m.logical_fee = m.logical_fee + fill.fee

        market = self.positions.markets.get(order.req.symbol)
        if market is not None:
            self.portfolio.apply_fill_optimistic(fill, market)
        result = self.positions.on_fill(fill, order.req, books, m.intent)
        if isinstance(result, ClosedPosition):
            report.closed_positions.append(result)
        elif isinstance(result, Position):
            report._touched_positions.add(result.id)
            if order.req.purpose == "entry":
                report._recovered_entries[result.id] = order.client_id

        self.order_manager.balances_dirty = True
        report.missed_fills += 1
        report._fills.append(fill)
        report._orders.append(order)

    # ------------------------------------------------------------ 3. untracked open orders

    def _adopt_or_flag(self, open_orders: list[Order], report: ReconcileReport) -> None:
        from wta.execution.order_manager import intent_from_raw  # (no import cycle at module load)

        managed = self.order_manager.managed
        stored: dict[str, Order] | None = None
        for order in sorted(open_orders, key=lambda o: (o.client_id, o.exchange_id or "")):
            cid = order.client_id
            if cid and cid in managed:
                continue
            if is_ours(cid):
                if stored is None:
                    stored = self._stored_orders(report)
                record = stored.get(cid)
                adopted = with_stored_request(order, record)
                # the write-ahead record keeps the original intent (stop price, linked position)
                self.order_manager.adopt(adopted, intent_from_raw(record) if record is not None else None)
                report.adopted.append(cid)
                report._orders.append(adopted)
                continue
            key = cid or f"exchange:{order.exchange_id or '?'}"
            report.foreign.append(key)
            if key not in self._foreign_logged and self.events_log is not None:
                self._foreign_logged.add(key)
                self.events_log.log(
                    "foreign_order",
                    reason=f"order {key} on {order.req.symbol} does not carry our client-id pattern; "
                    "it is left untouched (not placed by this agent)",
                    data=_order_brief(order),
                )

    def _stored_orders(self, report: ReconcileReport) -> dict[str, Order]:
        if self.store is None:
            return {}
        try:
            return {o.client_id: o for o in self.store.load_orders()}
        except sqlite3.Error as exc:
            report.errors.append(f"store: {exc}")
            return {}

    # ------------------------------------------------------------ 4. tracked orders that disappeared

    async def _finalize_missing(
        self,
        open_orders: list[Order],
        books: dict[str, BookSnapshot],
        now: float,
        report: ReconcileReport,
    ) -> None:
        on_exchange = {o.client_id for o in open_orders}
        managed = self.order_manager.managed
        for cid in sorted(managed):
            m = managed.get(cid)
            if m is None or m.done or m.order.is_terminal or cid in on_exchange:
                continue
            if now - m.placed_ts < MISSING_ORDER_GRACE_S:
                continue  # just placed: the exchange may not list it yet
            try:
                latest = await self.exchange.get_order(cid)
            except Exception as exc:  # noqa: BLE001 - leave the order as it is; retry next run
                report.errors.append(f"get_order {cid}: {exc}")
                continue
            if latest is None:
                await self._finalize(m, "EXPIRED", "not found on the exchange", books, now, report)
                continue
            if latest.filled_qty > m.order.filled_qty:
                if not self._can_value(m.order, books, report):
                    continue  # finalizing now would drop the fill: retry on the next run
                fill = delta_fill(m.order, latest, books, now)
                if fill is None:
                    report.errors.append(f"get_order {cid}: fill of {latest.filled_qty} without a price")
                    continue  # never finalize an order with a fill we could not book
                self._apply_fill(m, fill, books, now, report)
            if latest.is_terminal:
                await self._finalize(
                    m, latest.status, f"exchange status {latest.status}", books, now, report, latest.raw
                )
            # else: the exchange says it is still live (the open-orders list was incomplete);
            # the OrderManager keeps polling it.

    async def _finalize(
        self,
        m: Any,
        status: OrderStatus,
        why: str,
        books: dict[str, BookSnapshot],
        now: float,
        report: ReconcileReport,
        raw: dict[str, Any] | None = None,
    ) -> None:
        """Give a tracked order its final status; the OrderManager does the bookkeeping."""
        order: Order = m.order
        cid = order.client_id
        if raw:
            order.raw = raw
        report.finalized.append(cid)
        report._orders.append(order)
        finish = getattr(self.order_manager, "finish_external", None)
        if callable(finish):
            try:
                await finish(m, status, why, books, now)
            except Exception as exc:  # noqa: BLE001 - reconcile must never take the engine down
                report.errors.append(f"finish {cid}: {exc}")
            return
        # an order manager without finish_external: end the order here
        order.status = status
        order.updated_ts = now
        m.done = True
        m.done_reason = f"reconcile: {why}"
        self.order_manager.managed.pop(cid, None)
        self.portfolio.open_orders.pop(cid, None)
        if status != "FILLED":
            self._release_dead_stop(cid)

    def _release_dead_stop(self, cid: str) -> None:
        """A server stop that ended unfilled protects nothing: its position uses the software stop
        (``PositionManager.check_exits``) from now on."""
        for _, pos in sorted(self.portfolio.positions.items()):
            if pos.stop_order_client_id == cid:
                pos.stop_order_client_id = None
                pos.meta = {**pos.meta, "stop_mode": "software"}

    async def _protect_recovered_entries(
        self, books: dict[str, BookSnapshot], report: ReconcileReport
    ) -> None:
        """A position opened by a recovered entry fill gets its server stop, as on a normal fill."""
        if not self.cfg.execution.use_server_side_stops:
            return  # software stops (PositionManager.check_exits) protect every position
        for pid in sorted(report._recovered_entries):
            pos = self.portfolio.positions.get(pid)
            if pos is None or pos.stop_price is None or pos.stop_order_client_id:
                continue
            if report._recovered_entries[pid] in self.order_manager.managed:
                continue  # entry still working: the OrderManager places the stop when it completes
            try:
                placed = await self.order_manager.place_stop_for(pos, books)
            except Exception as exc:  # noqa: BLE001 - the software stop still protects it
                report.errors.append(f"place_stop_for {pid}: {exc}")
                continue
            if placed is not None:
                report.stops_placed.append(pid)

    # ------------------------------------------------------------ 5. positions whose coins are gone

    def _close_positions_without_coins(
        self,
        balances: dict[str, Balance],
        open_orders: list[Order] | None,
        books: dict[str, BookSnapshot],
        now: float,
        report: ReconcileReport,
    ) -> None:
        """Close long positions whose base coin is no longer on the account (``exit_reason=stop``).

        Positions are grouped by base coin and the balance is allocated to them in turn. Served
        first (and never force-closed): positions with a fill in this run or a live order of ours
        (entry still working, closing order, server stop) — the order path books their coins. Then
        a position whose server stop is still open on the exchange (it certainly holds its coins).
        A position that cannot be covered is force-closed at the current bid; without a price for
        its coin it is left for the next run.
        """
        live = {o.client_id for o in open_orders} if open_orders is not None else set()
        protected = set(report._touched_positions) | self._positions_with_live_orders()
        by_base: dict[str, list[Position]] = {}
        for _, pos in sorted(self.portfolio.positions.items()):
            if pos.qty > 0 and pos.meta.get("side", "BUY") == "BUY":
                by_base.setdefault(pos.base, []).append(pos)
        for base in sorted(by_base):
            bal = balances.get(base)
            left = bal.total if bal is not None else D0
            ordered = sorted(
                by_base[base],
                key=lambda p: (p.id not in protected, p.stop_order_client_id not in live, p.entry_ts, p.id),
            )
            for pos in ordered:
                need = pos.qty * (1 - POSITION_BALANCE_TOLERANCE)
                if left >= need or pos.id in protected:
                    left = max(left - need, D0)
                    continue
                if asset_price_tmn(pos.base, books) is None:
                    report.errors.append(
                        f"position {pos.id}: its {pos.base} is gone but has no TMN price yet; closed on a later run"
                    )
                    continue
                closed = self.positions.force_close(pos.id, now, "stop", books)
                report.closed_positions.append(closed)

    def _positions_with_live_orders(self) -> set[str]:
        """Positions referenced by a live (not done) order the OrderManager tracks."""
        out: set[str] = set()
        for _, m in sorted(self.order_manager.managed.items()):
            if getattr(m, "done", False):
                continue
            pid = getattr(m, "position_id", None)
            intent = getattr(m, "intent", None)
            if pid is None and intent is not None:
                pid = intent.linked_position_id
            if pid is None and m.order.req.purpose == "entry":
                pid = next(
                    (
                        p.id
                        for _, p in sorted(self.portfolio.positions.items())
                        if p.meta.get("entry_decision_id") == m.order.req.decision_id
                    ),
                    None,
                )
            if pid:
                out.add(pid)
        return out

    # ------------------------------------------------------------ 6. balance drift

    def _check_drift(
        self, exchange: dict[str, Balance], books: dict[str, BookSnapshot], report: ReconcileReport
    ) -> None:
        """Record assets whose computed total differs from the exchange by > drift_pct of equity."""
        equity = valuate(exchange, books).equity_tmn
        if equity <= 0:
            equity = self.portfolio.equity_tmn or self.portfolio.initial_equity_tmn
        if equity <= 0:
            return
        limit = self.cfg.risk.reconcile_drift_pct * equity
        for asset in sorted(set(self.portfolio.balances) | set(exchange)):
            computed = self.portfolio.total(asset)
            truth = exchange[asset].total if asset in exchange else D0
            price = asset_price_tmn(asset, books)
            if computed == truth or price is None:
                continue  # equal, or cannot be valued (it is overwritten below either way)
            diff_tmn = abs(computed - truth) * price
            if diff_tmn > limit:
                report.drift[asset] = {
                    "computed": computed,
                    "exchange": truth,
                    "diff_tmn": diff_tmn,
                    "pct_of_equity": (diff_tmn / equity).quantize(Decimal("0.000001")),
                }
        if report.drift and self.events_log is not None:
            self.events_log.log(
                "reconcile_drift",
                reason=f"balance drift above {self.cfg.risk.reconcile_drift_pct:%} of equity for "
                f"{', '.join(sorted(report.drift))}; overwritten with exchange values",
                data={"assets": report.summary()["drift"], "equity_tmn": str(equity)},
            )

    # ------------------------------------------------------------ 7. persist + log

    def _persist(self, balances: dict[str, Balance] | None, now: float, report: ReconcileReport) -> None:
        if self.store is None:
            return
        try:
            for fill in report._fills:
                self.store.add_fill(fill)
            for closed in report.closed_positions:
                self.store.upsert_position(closed.position, open=False, ts=now)
            for _, pos in sorted(self.portfolio.positions.items()):
                self.store.upsert_position(pos, open=True, ts=now)
            touched = {o.client_id: o for o in report._orders}
            for cid in sorted(self.order_manager.managed):
                touched[cid] = self.order_manager.managed[cid].order
            for cid in sorted(touched):
                self.store.upsert_order(touched[cid])
            if balances is not None:
                self.store.add_balances(now, balances)
            persist_state(self.store, now, self.portfolio, order_manager=self.order_manager)
        except sqlite3.Error as exc:
            report.errors.append(f"store: {exc}")

    def _log_summary(self, report: ReconcileReport) -> None:
        # Routine runs that found nothing stay quiet: the periodic ones, and the startup run (the
        # engine puts its summary into the "restart" event instead).
        if self.events_log is None or not (report.notable or report.reason not in QUIET_REASONS):
            return
        self.events_log.log(
            "reconcile",
            reason=(
                f"reconcile ({report.reason}): {report.missed_fills} missed fill(s), "
                f"{len(report.adopted)} adopted, {len(report.foreign)} foreign, "
                f"{len(report.finalized)} finalized, {len(report.closed_positions)} position(s) closed, "
                f"{len(report.drift)} drifted asset(s), {len(report.errors)} error(s)"
            ),
            data=report.summary(),
        )


# ---------------------------------------------------------------- helpers (pure)


def fill_key(fill: Fill) -> str:
    """De-duplication key: the exchange trade id, else ``client_id:qty:price:ts_ms``."""
    if fill.trade_id:
        return fill.trade_id
    return f"{fill.order_client_id}:{fill.qty}:{fill.price}:{int(round(fill.ts * 1000))}"


def unaccounted_fills(fills: list[Fill], known_qty: Decimal, order_qty: Decimal) -> list[Fill]:
    """The part of ``fills`` (oldest first) beyond the ``known_qty`` we already counted.

    A fill straddling the boundary is split (fee pro rata). The result never exceeds the order's
    quantity, so a duplicated trade with a fresh id cannot over-fill an order.
    """
    skip = known_qty
    room = max(order_qty - known_qty, D0) if order_qty > 0 else None
    out: list[Fill] = []
    for fill in fills:
        qty = fill.qty
        if skip >= qty:
            skip -= qty
            continue
        qty -= skip
        skip = D0
        if room is not None:
            qty = min(qty, room)
            room -= qty
        if qty <= 0:
            break
        if qty != fill.qty:
            fill = fill.model_copy(
                update={"qty": qty, "fee": fill.fee * qty / fill.qty, "trade_id": f"{fill_key(fill)}:part"}
            )
        out.append(fill)
    return out


def delta_fill(known: Order, latest: Order, books: dict[str, BookSnapshot], now: float) -> Fill | None:
    """A synthetic fill for ``latest.filled_qty - known.filled_qty`` (from a ``get_order`` answer).

    The price is the incremental average price when both averages are known, else the latest
    average, the limit price or the book mid. None when no price can be derived.
    """
    qty = latest.filled_qty - known.filled_qty
    if qty <= 0:
        return None
    price: Decimal | None = latest.avg_price or known.req.price
    if latest.avg_price is not None and known.avg_price is not None and known.filled_qty > 0:
        price = (latest.avg_price * latest.filled_qty - known.avg_price * known.filled_qty) / qty
    if price is None or price <= 0:
        book = books.get(known.req.symbol)
        price = book.mid if book is not None else None
    if price is None or price <= 0:
        return None
    return Fill(
        order_client_id=known.client_id,
        symbol=known.req.symbol,
        side=known.req.side,
        price=price,
        qty=qty,
        fee=max(latest.fee_paid - known.fee_paid, D0),
        fee_asset=latest.fee_asset or known.fee_asset or "",
        ts=latest.updated_ts or now,
        trade_id=f"{known.client_id}:reconcile:{latest.filled_qty}",
    )


def with_stored_request(order: Order, stored: Order | None) -> Order:
    """Exchange state on top of the request we stored when placing it (strategy, purpose, decision).

    Orders read back from the exchange only carry a synthetic request; the store still has ours.
    """
    if stored is None:
        return order
    return Order(
        req=stored.req,
        exchange_id=order.exchange_id or stored.exchange_id,
        status=order.status,
        filled_qty=order.filled_qty,
        avg_price=order.avg_price,
        fee_paid=order.fee_paid,
        fee_asset=order.fee_asset,
        created_ts=stored.created_ts,
        updated_ts=order.updated_ts,
        reprices=stored.reprices,
        raw=order.raw,
    )


def _order_brief(order: Order) -> dict[str, Any]:
    r = order.req
    return {
        "client_id": r.client_id,
        "exchange_id": order.exchange_id,
        "symbol": r.symbol,
        "side": r.side,
        "type": r.type,
        "qty": str(r.qty),
        "price": str(r.price) if r.price is not None else None,
        "status": order.status,
        "filled_qty": str(order.filled_qty),
    }


# ---------------------------------------------------------------- durable state (startup / save)


def persist_state(
    store: Store,
    now: float,
    portfolio: PortfolioState,
    flags: EngineFlags | None = None,
    order_manager: OrderManagerLike | None = None,
) -> None:
    """Write the durable counters/flags/order-manager state to the store's ``kv`` table."""
    store.kv_set(KV_PORTFOLIO, portfolio.to_state(), now)
    if flags is not None:
        store.kv_set(KV_FLAGS, flags.to_state(), now)
    if order_manager is not None:
        store.kv_set(KV_ORDER_MANAGER, order_manager.to_state(), now)


async def startup_restore(
    store: Store | None,
    portfolio: PortfolioState,
    flags: EngineFlags,
    positions: PositionManager,
    order_manager: OrderManagerLike,
    now: float,
) -> dict[str, Any]:
    """Reload durable state after a (re)start, before the first reconcile (PRD-02 §5).

    * portfolio counters (``kv["portfolio_state"]``): day-start equity, fees, placements, realized PnL;
    * peak equity = max(stored peak, highest row of the ``equity`` table) — never reset;
    * engine flags (``kv["engine_flags"]``): kill/pause/flatten phase survive a restart;
    * open positions (into ``portfolio.positions``, which ``positions`` books into) and
      non-terminal orders (handed to ``OrderManager.load_state``).

    Returns a summary for the ``restart`` event.
    """
    if store is None:
        return {"restored": False}
    portfolio.load_state(store.kv_get(KV_PORTFOLIO))
    flags.load_state(store.kv_get(KV_FLAGS))
    peaks = [p for p in (portfolio.peak_equity_tmn, store.max_peak_equity()) if p is not None]
    if peaks:
        portfolio.peak_equity_tmn = max(peaks)

    open_positions = store.load_open_positions()
    for pos in open_positions:
        portfolio.positions[pos.id] = pos
    portfolio.positions = dict(sorted(portfolio.positions.items()))
    # (``positions`` needs no restore: PositionManager finds a pre-restart entry's position by
    # ``meta.entry_decision_id``, so late fills of that entry add to the restored position.)

    orders = store.load_orders(non_terminal_only=True)
    order_manager.load_state(store.kv_get(KV_ORDER_MANAGER) or {}, orders)
    return {
        "restored": True,
        "ts": now,
        "peak_equity_tmn": str(portfolio.peak_equity_tmn) if portfolio.peak_equity_tmn is not None else None,
        "day_start_equity_tmn": (
            str(portfolio.day_start_equity_tmn) if portfolio.day_start_equity_tmn is not None else None
        ),
        "fees_paid_tmn_total": str(portfolio.fees_paid_tmn_total),
        "orders_today": portfolio.orders_today(now),
        "orders_last_hour": portfolio.orders_last_hour(now),
        "positions": [p.id for p in open_positions],
        "orders": [o.client_id for o in orders],
        "flags": flags.to_state(),
    }

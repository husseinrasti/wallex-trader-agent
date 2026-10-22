"""Order management: approved intents -> exchange orders -> fills -> positions (PRD-02 §4).

One approved ``Intent`` becomes one *logical order* (``ManagedOrder``). A logical order may be
placed several times on the exchange (re-prices, crosses); every placement is a separate exchange
order with a fresh ``client_id``. ``managed`` is keyed by the client id of the current placement.

Submit (§4.1)
  * A vetoed intent is only logged (decision ``kind="veto"``); nothing is sent.
  * Quantity = ``decision.adjusted_qty`` (the RiskManager may have reduced it) or ``intent.qty``,
    rounded down to the market step.
  * First price = ``intent.price`` when the strategy chose one. Otherwise maker-first: BUY at best
    bid + ``maker_offset_ticks`` ticks while that stays below the best ask (else at the best bid),
    SELL mirrored. Taker intents cross at the opposite best. ``round_price``: BUY down, SELL up.
  * ``OrderRequest.decision_id == intent.id`` and ``client_id = make_client_id(strategy, symbol,
    now, intent.id, attempt)``: a fresh id per placement, reproducible under replay.
  * ``OrderRejected`` -> decision record ``"REJECTED: <code> <message>"``, nothing is tracked.
    Any other ``WallexAPIError`` means "state unknown": the order is tracked as ``UNKNOWN`` and
    resolved by ``get_order(client_id)`` on the next polls. It is never re-sent blindly; only after
    ``NOT_FOUND_POLLS`` polls in which the exchange does not know it is it treated as not placed.

Lifecycle (§4.2), driven by ``poll(books, now)``
  * ``get_order`` every ``poll_interval_s`` (resting stop orders every 5 x that). A fill is the
    difference between two snapshots of the same exchange order: qty = filled_new - filled_old,
    price = (avg_new x filled_new - avg_old x filled_old) / qty, fee = fee_new - fee_old.
  * A maker LIMIT not filled after ``limit_reprice_after_s`` is canceled and the remaining logical
    quantity re-placed at the new top, at most ``limit_max_reprices`` times (flatten:
    ``flatten_max_reprices``). After that, exit/stop/leg2/flatten orders cross the spread and keep
    re-crossing at the new opposite best every ``limit_reprice_after_s`` until filled;
    entry/rebalance orders are canceled and given up (``EXPIRED_UNFILLED``).
  * Taker orders re-cross at the new opposite best (entries at most ``limit_max_reprices`` times,
    closing orders until filled).
  * Hard cap: entry/rebalance orders older than ``limit_cancel_after_s`` (from the first
    placement) are canceled (``EXPIRED_UNFILLED``).
  * A remainder below the exchange minimum ends the order (``DUST_REMAINDER``).
  * STOP_LIMIT / STOP_MARKET orders are never re-priced or timed out. A STOP_LIMIT stranded below
    its limit (the price gapped through it) is replaced by the PositionManager's taker stop exit,
    which cancels it first like any closing SELL.

Safety
  * Never a BUY and a SELL of ours open at once in one symbol: such a placement is refused and
    logged. Exception: our resting server stops. They are protective, rest below the market, and
    are always canceled before any other SELL of their position, so they cannot meet our own SELLs;
    the RiskManager's ``SELF_CROSS`` price check still sees them.
  * Before a SELL that closes a position: that position's open BUYs in the same symbol are
    canceled; its server stop is canceled and any fill the stop already had is processed first
    (the position may even be closed -> the SELL is skipped). A closing SELL never exceeds the part
    of the position not already covered by other live closing SELLs: never two SELLs for the same
    coins.
  * ``position.meta["exit_pending"]`` is True while a closing SELL (not the resting stop) is live.

Server-side stops (§4.3)
  * When an entry is done with a fill and its position has ``stop_price``: STOP_LIMIT SELL of
    position.qty, stop = stop_price, limit = stop x (1 - stop_limit_slippage_pct) rounded as a SELL.
    ``position.stop_order_client_id`` is set and ``meta["stop_mode"] = "server"``.
  * Disabled by config, rejected, below the exchange minimum, or the bid already at/below the stop
    -> ``meta["stop_mode"] = "software"`` (``PositionManager.check_exits`` watches the stop) and the
    decision log says why.
  * A STOP_LIMIT intent with purpose ``stop`` (S1 ``lockin_degraded_to_revert``) is placed the same
    way; it also sets the position's stop price and time stop.

Outcomes
  * A fill that closes a position -> ``decision_log.outcome(entry decision id, pnl...)``.
  * Every finished order except a filled entry (whose position reports at close) -> an order-level
    outcome on its own decision id: status, filled qty, average price, fee, done reason.

Restart (§4.5): ``to_state()`` (persist it every tick) + ``load_state(state, orders)`` restore the
live orders; the first poll re-reads each of them by ``client_id``. The last known exchange
snapshot (``ManagedOrder.order``, also kept in the store's ``orders`` table) is the baseline for fill
detection, so fills are never applied twice — also when the Reconciler applies a missed fill to
``order`` directly. The baseline only ever grows: a lagging snapshot (or a cancel reply without
fills) with a lower filled quantity or fee never lowers it, so a later snapshot cannot re-count the
same fill. A fill is applied (positions, balances) before anything is written to the store, and
store errors are logged, never raised: a locked database must not desynchronise memory and fills.

Crash safety of a placement (write-ahead): right before ``place_order`` the request is stored as an
``UNKNOWN`` order that carries its intent (``raw["intent"]``) and committed (``checkpoint``; the
engine also saves its kv state there). If the process dies while the request is in flight, the
restart finds that record and resolves it by client id *with the original intent* (strategy,
purpose, stop price, linked position) instead of adopting an unknown order. A rejected request is
stored as ``REJECTED``.

Adopting (``adopt``): our order found on the exchange without a stored request (the exchange gives a
synthetic one: ``strategy="unknown"``, no decision id) is never re-priced or crossed: we do not know
what it was for, so it is canceled (``ADOPTED_UNKNOWN``). Replacement client ids always carry one of
our prefixes (``EX`` when the strategy name has none), so a restart still recognises them as ours.

Reconciler finalization (``finish_external``): an order whose end the Reconciler learned from the
exchange goes through the same bookkeeping as one the manager saw end itself (software-stop
fallback for a dead server stop, ``exit_pending``, outcome, protective stop after an entry, and a
must-finish order re-crosses its remainder).

The manager is not re-entrant: call it from one task (the engine's fast loop). It never reads the
wall clock: ``submit``/``cancel`` use the injected clock (the engine injects a clock frozen at the
tick's ``now``), ``poll`` uses the ``now`` it is given. Dicts are walked in sorted order so a replay
produces the same calls and log lines.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from wta.config import Config, ExecutionCfg
from wta.data.store import Store
from wta.exchange.base import Exchange, OrderRejected, WallexAPIError
from wta.execution.positions import CLOSING_PURPOSES, ClosedPosition, PortfolioState, PositionManager
from wta.logging_.decision_log import DecisionLog, OrdersLog
from wta.models import (
    D0,
    BookSnapshot,
    DecisionKind,
    Fill,
    Intent,
    MarketInfo,
    Order,
    OrderRequest,
    OrderStatus,
    OrderType,
    Position,
    RiskCheck,
    RiskDecision,
    Side,
)
from wta.utils.decimalutil import D, check_min_notional, round_price, round_qty
from wta.utils.ids import OUR_PREFIXES, is_ours, make_client_id, strategy_prefix
from wta.utils.timeutil import Clock

log = logging.getLogger(__name__)

GIVE_UP_PURPOSES = frozenset({"entry", "rebalance", "probe"})  # never cross the spread to finish
CROSS_PURPOSES = frozenset({"exit", "stop", "leg2", "flatten"})  # must finish: cross after the reprices
STOP_TYPES = frozenset({"STOP_LIMIT", "STOP_MARKET"})
STOP_POLL_FACTOR = 5  # resting stops are polled every 5 x poll_interval_s
NOT_FOUND_POLLS = 3  # consecutive "unknown order" answers before an order counts as not placed

# done reasons
FILLED = "FILLED"
EXPIRED_UNFILLED = "EXPIRED_UNFILLED"
DUST_REMAINDER = "DUST_REMAINDER"
POSITION_CLOSED = "POSITION_CLOSED"
NOT_FOUND = "NOT_FOUND"
CANCELED_FOR_EXIT = "CANCELED_FOR_EXIT"
REPLACE_REJECTED = "REPLACE_REJECTED"
SELF_CROSS_GUARD = "SELF_CROSS_GUARD"
ADOPTED_UNKNOWN = "ADOPTED_UNKNOWN"
ID_FALLBACK_STRATEGY = "EX"  # client-id prefix for re-placements of a strategy without one of ours

# Auto-placed server stops are not risk-evaluated: they can only reduce exposure.
PROTECTIVE_STOP_RISK = RiskDecision(
    approved=True,
    checks=[
        RiskCheck(
            code="PROTECTIVE_STOP",
            passed=True,
            detail="server-side stop for a filled, risk-approved entry; it only reduces exposure",
        )
    ],
)
ADOPTED_RISK = RiskDecision(
    approved=True,
    checks=[RiskCheck(code="ADOPTED", passed=True, detail="our order found on the exchange untracked")],
)


# ---------------------------------------------------------------- data


@dataclass
class ManagedOrder:
    """One logical order: an intent and its current exchange placement."""

    intent: Intent
    decision_id: str  # == intent.id
    order: Order  # current exchange order (last known snapshot; the fill-detection baseline)
    attempt: int = 0  # client-id attempt number of the current placement
    first_ts: float = 0.0  # first placement
    placed_ts: float = 0.0  # current placement
    max_reprices: int = 0
    crossed: bool = False  # current placement crosses the spread (taker)
    logical_filled: Decimal = D0  # filled across all placements
    done: bool = False
    done_reason: str = ""
    is_stop: bool = False
    # bookkeeping (persisted by to_state)
    total_qty: Decimal = D0  # logical quantity to fill
    reprices: int = 0  # re-placements so far (maker re-prices and crosses)
    logical_value: Decimal = D0  # sum(price x qty) of all fills, quote currency
    logical_fee: Decimal = D0
    fee_asset: str | None = None
    position_id: str | None = None  # position opened (entry) or closed (exit/stop/leg2/flatten)
    last_poll_ts: float = 0.0
    misses: int = 0  # consecutive polls in which the exchange did not know the order
    next_action: str = ""  # "reprice" | "cross" | "giveup": what follows once the order is terminal
    next_reason: str = ""
    detail: str = ""  # human detail for the outcome record
    refresh: bool = False  # re-read on the next poll even if terminal (after a restart)
    client_ids: list[str] = field(default_factory=list)  # every placement's client id

    @property
    def client_id(self) -> str:
        return self.order.client_id

    @property
    def symbol(self) -> str:
        return self.intent.symbol

    @property
    def side(self) -> Side:
        return self.intent.side

    @property
    def purpose(self) -> str:
        return self.intent.purpose

    @property
    def closes_position(self) -> bool:
        """A SELL (exit/stop/leg2/flatten) that reduces one of our positions."""
        return self.purpose in CLOSING_PURPOSES and self.side == "SELL" and self.position_id is not None

    @property
    def remaining_qty(self) -> Decimal:
        return max(self.total_qty - self.logical_filled, D0)

    @property
    def avg_price(self) -> Decimal | None:
        return self.logical_value / self.logical_filled if self.logical_filled > 0 else None

    def to_state(self) -> dict[str, Any]:
        return {
            "client_id": self.client_id,
            "intent": self.intent.model_dump(mode="json"),
            "decision_id": self.decision_id,
            "order": self.order.model_dump(mode="json"),
            "attempt": self.attempt,
            "first_ts": self.first_ts,
            "placed_ts": self.placed_ts,
            "max_reprices": self.max_reprices,
            "crossed": self.crossed,
            "logical_filled": str(self.logical_filled),
            "done": self.done,
            "done_reason": self.done_reason,
            "is_stop": self.is_stop,
            "total_qty": str(self.total_qty),
            "reprices": self.reprices,
            "logical_value": str(self.logical_value),
            "logical_fee": str(self.logical_fee),
            "fee_asset": self.fee_asset,
            "position_id": self.position_id,
            "last_poll_ts": self.last_poll_ts,
            "misses": self.misses,
            "next_action": self.next_action,
            "next_reason": self.next_reason,
            "detail": self.detail,
            "client_ids": list(self.client_ids),
        }

    @classmethod
    def from_state(cls, d: dict[str, Any], order: Order) -> ManagedOrder:
        return cls(
            intent=Intent.model_validate(d["intent"]),
            decision_id=str(d["decision_id"]),
            order=order,
            attempt=int(d.get("attempt", 0)),
            first_ts=float(d.get("first_ts", 0.0)),
            placed_ts=float(d.get("placed_ts", 0.0)),
            max_reprices=int(d.get("max_reprices", 0)),
            crossed=bool(d.get("crossed", False)),
            logical_filled=D(d.get("logical_filled", "0")),
            done=bool(d.get("done", False)),
            done_reason=str(d.get("done_reason", "")),
            is_stop=bool(d.get("is_stop", False)),
            total_qty=D(d.get("total_qty", "0")),
            reprices=int(d.get("reprices", 0)),
            logical_value=D(d.get("logical_value", "0")),
            logical_fee=D(d.get("logical_fee", "0")),
            fee_asset=d.get("fee_asset"),
            position_id=d.get("position_id"),
            last_poll_ts=float(d.get("last_poll_ts", 0.0)),
            misses=int(d.get("misses", 0)),
            next_action=str(d.get("next_action", "")),
            next_reason=str(d.get("next_reason", "")),
            detail=str(d.get("detail", "")),
            client_ids=[str(c) for c in d.get("client_ids", [])],
        )


@dataclass
class FillEvent:
    fill: Fill
    managed: ManagedOrder
    result: Position | ClosedPosition | None  # what PositionManager.on_fill returned


@dataclass
class PollResult:
    fills: list[FillEvent] = field(default_factory=list)
    done: list[ManagedOrder] = field(default_factory=list)
    closed: list[ClosedPosition] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class _Ctx:
    """What a decision record needs: the intent, the risk decision and the logged inputs."""

    intent: Intent
    decision: RiskDecision
    features: dict[str, Any]
    plan_snapshot: dict[str, Any] | None = None


def _join(*notes: str) -> str:
    return "; ".join(n for n in notes if n)


# ---------------------------------------------------------------- manager


class OrderManager:
    """Places, tracks, re-prices and cancels our orders; turns fills into positions and outcomes."""

    def __init__(
        self,
        exchange: Exchange,
        portfolio: PortfolioState,
        positions: PositionManager,
        markets: dict[str, MarketInfo],
        cfg: Config,
        clock: Clock,
        decision_log: DecisionLog,
        orders_log: OrdersLog | None = None,
        store: Store | None = None,
        mode: str = "shadow",
    ) -> None:
        self.exchange = exchange
        self.portfolio = portfolio
        self.positions = positions
        self.markets = markets
        self.cfg = cfg
        self.clock = clock
        self.decision_log = decision_log
        self.orders_log = orders_log
        self.store = store
        self.mode = mode
        self.managed: dict[str, ManagedOrder] = {}
        self.balances_dirty = False  # set after any fill; the engine refreshes balances and clears it
        self._carry = PollResult()  # events produced outside poll(); returned by the next poll()
        self._books: dict[str, BookSnapshot] = {}  # latest books (for cancel() outside poll)
        self._exiting: set[str] = set()  # positions whose closing SELL is being submitted right now
        # called right after the write-ahead record of a placement (the engine saves its kv state
        # and commits); None -> only the store is committed
        self.checkpoint: Callable[[], None] | None = None
        self.store_errors = 0

    @property
    def ex(self) -> ExecutionCfg:
        return self.cfg.execution

    def live(self) -> list[ManagedOrder]:
        """Live logical orders in client-id order."""
        return [m for _, m in sorted(self.managed.items()) if not m.done]

    # ------------------------------------------------------------ submit

    async def submit(
        self,
        intent: Intent,
        decision: RiskDecision,
        books: dict[str, BookSnapshot],
        features: dict[str, Any] | None = None,
        plan_snapshot: dict[str, Any] | None = None,
    ) -> ManagedOrder | None:
        """Log a vetoed intent, or place an approved one. Returns the tracked order (or None)."""
        now = self.clock.now()
        self._books = books
        ctx = _Ctx(
            intent, decision, dict(features if features is not None else intent.features), plan_snapshot
        )
        if not decision.approved:
            notes = decision.veto_reason or ("vetoed: " + ", ".join(decision.failed_codes))
            self._record("veto", ctx, notes)
            return None
        market = self.markets.get(intent.symbol)
        if market is None:
            self._record("order", ctx, f"REFUSED: unknown market {intent.symbol}")
            return None
        if intent.order_type in STOP_TYPES:
            return await self._submit_stop_intent(ctx, books, now)
        position: Position | None = None
        if intent.side == "SELL" and intent.purpose in CLOSING_PURPOSES and intent.linked_position_id:
            position = self.portfolio.positions.get(intent.linked_position_id)
            if position is None:
                self._record("order", ctx, f"REFUSED: position {intent.linked_position_id} is not open")
                return None
            self._exiting.add(position.id)
        try:
            return await self._submit_order(ctx, market, position, books, now)
        finally:
            if position is not None:
                self._exiting.discard(position.id)

    async def _submit_order(
        self,
        ctx: _Ctx,
        market: MarketInfo,
        position: Position | None,
        books: dict[str, BookSnapshot],
        now: float,
    ) -> ManagedOrder | None:
        intent, result = ctx.intent, self._carry
        if position is not None:
            await self._cancel_entries_for(position, intent.symbol, books, now, result)
        blocker = self._opposite_open(intent.symbol, intent.side)
        if blocker is not None:
            self._record(
                "order",
                ctx,
                f"REFUSED: {SELF_CROSS_GUARD}: our {blocker.req.side} {blocker.client_id} is open on "
                f"{intent.symbol}; never a BUY and a SELL at once",
            )
            return None
        book = books.get(intent.symbol)
        otype: OrderType = "MARKET" if intent.order_type == "MARKET" else "LIMIT"
        price = self._initial_price(intent, market, book)
        ref_price = price if price is not None else self._taker_price(market, book, intent.side)
        if ref_price is None:
            self._record("order", ctx, f"REFUSED: no {intent.symbol} book to price the order")
            return None
        qty = decision_qty(ctx.decision, intent, ref_price)
        if qty is None:
            self._record("order", ctx, "REFUSED: the intent has no quantity")
            return None
        stop_note = ""
        if position is not None:
            if self._closable_qty(position) <= 0:
                self._record(
                    "order", ctx, f"REFUSED: position {position.id} is already covered by live closing SELLs"
                )
                return None
            if position.stop_order_client_id:
                ok, stop_note = await self._release_stop(position, books, now, result)
                if not ok:
                    self._record("order", ctx, stop_note)
                    return None
                if position.id not in self.portfolio.positions:
                    self._record(
                        "order",
                        ctx,
                        f"SKIPPED: {stop_note}; position {position.id} is closed, nothing left to sell",
                    )
                    return None
            qty = min(qty, self._closable_qty(position))
        qty = round_qty(market, qty)
        if not check_min_notional(market, qty, ref_price):
            self._record(
                "order",
                ctx,
                _join(f"REFUSED: {qty} at {ref_price} is below the {intent.symbol} minimum order", stop_note),
            )
            return None
        req, attempt = self._new_request(intent, qty, price, otype, None, 0, now)
        placed, note = await self._send(req, now, "submit", intent)
        if placed is None:
            self._record("order", ctx, _join(note, stop_note), req=req)
            return None
        mo = ManagedOrder(
            intent=intent,
            decision_id=intent.id,
            order=placed,
            attempt=attempt,
            first_ts=now,
            placed_ts=now,
            max_reprices=self._max_reprices(intent),
            crossed=intent.taker or otype == "MARKET",
            total_qty=qty,
            position_id=position.id if position is not None else intent.linked_position_id,
        )
        self._record("order", ctx, _join(note, stop_note), req=req, order=placed)
        if position is not None:
            self._set_exit_pending(position, True, now)
        self._track(mo, placed, books, now, result)
        return mo

    async def _submit_stop_intent(
        self, ctx: _Ctx, books: dict[str, BookSnapshot], now: float
    ) -> ManagedOrder | None:
        """A strategy's own STOP_LIMIT intent (S1 ``lockin_degraded_to_revert``) for a position."""
        intent = ctx.intent
        pos = self.portfolio.positions.get(intent.linked_position_id or "")
        if pos is None or intent.side != "SELL":
            self._record("order", ctx, "REFUSED: a stop order needs an open linked position (SELL)")
            return None
        if intent.stop_price is not None:
            pos.stop_price = intent.stop_price
        time_stop_s = ctx.features.get("time_stop_s", intent.features.get("time_stop_s"))
        if time_stop_s is not None:
            pos.time_stop_ts = now + float(time_stop_s)
        self._save_position(pos, now)
        if pos.stop_order_client_id:
            ok, note = await self._release_stop(pos, books, now, self._carry)
            if not ok:
                self._record("order", ctx, note)
                return None
            if pos.id not in self.portfolio.positions:
                self._record("order", ctx, f"SKIPPED: {note}; position {pos.id} is closed")
                return None
        return await self._place_stop(pos, books, now, self._carry, ctx)

    # ------------------------------------------------------------ poll

    async def poll(self, books: dict[str, BookSnapshot], now: float) -> PollResult:
        """Read every live order (rate-limited), turn fill deltas into fills, re-price/cross/cancel.

        Also returns the fills/done orders/closed positions produced since the last poll by
        ``submit``/``cancel`` (e.g. a server stop that had filled when it was canceled).
        """
        self._books = books
        result, self._carry = self._carry, PollResult()
        for cid in sorted(self.managed):
            mo = self.managed.get(cid)
            if mo is not None and not mo.done:
                await self._step(mo, books, now, result)
        self._clear_stale_exit_flags(now)
        return result

    def _clear_stale_exit_flags(self, now: float) -> None:
        """``exit_pending`` without a live closing SELL (e.g. the Reconciler finalized the order
        directly) would block every future exit of the position: clear it."""
        for pid, pos in sorted(self.portfolio.positions.items()):
            if pos.meta.get("exit_pending") and pid not in self._exiting and not self._closing_orders(pid):
                self._set_exit_pending(pos, False, now)

    async def _step(
        self, mo: ManagedOrder, books: dict[str, BookSnapshot], now: float, result: PollResult
    ) -> None:
        if mo.symbol not in self.markets:
            result.errors.append(f"{mo.client_id}: market {mo.symbol} is unknown; order not managed")
            return
        if mo.refresh or (not mo.order.is_terminal and self._poll_due(mo, now)):
            mo.refresh = False
            mo.last_poll_ts = now
            snapshot = await self._fetch(mo, now, result)
            if snapshot is not None:
                self._ingest(mo, snapshot, books, now, result)
        if mo.done:
            return
        if mo.order.is_terminal:
            await self._after_terminal(mo, books, now, result)
        else:
            await self._lifecycle(mo, books, now, result)

    def _poll_due(self, mo: ManagedOrder, now: float) -> bool:
        interval = self.ex.poll_interval_s * (STOP_POLL_FACTOR if mo.is_stop else 1)
        return now - mo.last_poll_ts >= interval - 1e-9

    async def _fetch(self, mo: ManagedOrder, now: float, result: PollResult) -> Order | None:
        """``get_order``; after NOT_FOUND_POLLS unknown answers the order counts as gone (CANCELED)."""
        cid = mo.client_id
        try:
            snapshot = await self.exchange.get_order(cid)
        except WallexAPIError as err:
            result.errors.append(f"get_order {cid}: {err}")
            return None
        if snapshot is not None:
            mo.misses = 0
            return snapshot
        mo.misses += 1
        if mo.misses < NOT_FOUND_POLLS:
            return None
        mo.detail = f"{cid} not found on the exchange after {mo.misses} polls"
        self._olog(
            "status", cid, mo.symbol, {"found": False, "misses": mo.misses, "resolution": "not placed"}
        )
        if not mo.next_action and (mo.is_stop or mo.purpose not in CROSS_PURPOSES):
            mo.next_action, mo.next_reason = "giveup", NOT_FOUND
        return mo.order.model_copy(update={"status": "CANCELED", "updated_ts": now})

    async def _lifecycle(
        self, mo: ManagedOrder, books: dict[str, BookSnapshot], now: float, result: PollResult
    ) -> None:
        """Timers of a live (non-terminal) order: re-price, cross, give up, hard cap."""
        if mo.closes_position and mo.position_id not in self.portfolio.positions:
            mo.detail = f"position {mo.position_id} is closed"
            await self._request_cancel(mo, "giveup", POSITION_CLOSED, books, now, result)
            return
        if mo.next_action:  # a cancel was requested earlier and has not completed yet
            await self._request_cancel(mo, mo.next_action, mo.next_reason, books, now, result)
            return
        if mo.is_stop or mo.order.status == "UNKNOWN":
            return  # stops are never re-priced; UNKNOWN orders wait for get_order to resolve them
        giving_up = mo.purpose in GIVE_UP_PURPOSES
        age_total, age = now - mo.first_ts, now - mo.placed_ts
        if giving_up and age_total >= self.ex.limit_cancel_after_s:
            mo.detail = (
                f"hard cap: open {age_total:.0f}s >= limit_cancel_after_s {self.ex.limit_cancel_after_s:g}s"
            )
            await self._request_cancel(mo, "giveup", EXPIRED_UNFILLED, books, now, result)
            return
        if age < self.ex.limit_reprice_after_s:
            return
        if mo.crossed:
            action = "giveup" if giving_up and mo.reprices >= mo.max_reprices else "cross"
        elif mo.reprices < mo.max_reprices:
            action = "reprice"
        else:
            action = "giveup" if giving_up else "cross"
        if action == "giveup":
            what = "re-crosses" if mo.crossed else "re-prices"
            mo.detail = f"not filled after {mo.reprices} {what} (max {mo.max_reprices})"
        await self._request_cancel(
            mo, action, EXPIRED_UNFILLED if action == "giveup" else "", books, now, result
        )

    async def _request_cancel(
        self,
        mo: ManagedOrder,
        action: str,
        reason: str,
        books: dict[str, BookSnapshot],
        now: float,
        result: PollResult,
    ) -> None:
        """Cancel the current placement; once it is terminal, ``action`` decides what follows."""
        mo.next_action, mo.next_reason = action, reason
        if await self._cancel_current(mo, books, now, result):
            await self._after_terminal(mo, books, now, result)

    async def _cancel_current(
        self, mo: ManagedOrder, books: dict[str, BookSnapshot], now: float, result: PollResult
    ) -> bool:
        """Cancel the current placement and ingest the answer (it may show late fills).

        Returns True when the placement is terminal afterwards. An order the exchange does not
        know (cancel and get both answer "not found") is treated as canceled.
        """
        cid = mo.client_id
        self._olog(
            "cancel", cid, mo.symbol, {"source": "order_manager", "why": mo.next_reason or mo.next_action}
        )
        try:
            got = await self.exchange.cancel_order(cid)
            if got is None:
                got = await self.exchange.get_order(cid)
        except WallexAPIError as err:
            result.errors.append(f"cancel {cid}: {err}")
            return False
        if got is None:
            got = mo.order.model_copy(update={"status": "CANCELED", "updated_ts": now})
        self._ingest(mo, got, books, now, result)
        return mo.order.is_terminal

    async def _after_terminal(
        self, mo: ManagedOrder, books: dict[str, BookSnapshot], now: float, result: PollResult
    ) -> None:
        """The current placement is over: finish the logical order or place its remainder again."""
        if mo.done:
            return
        status = mo.order.status
        if mo.is_stop:
            reason = FILLED if status == "FILLED" else (mo.next_reason or f"EXCHANGE_{status}")
            await self._finish(mo, reason, books, now, result)
            return
        remaining = self._remaining(mo)
        if remaining <= 0:
            gone = mo.closes_position and mo.position_id not in self.portfolio.positions
            closed_early = gone and mo.logical_filled < mo.total_qty
            await self._finish(mo, POSITION_CLOSED if closed_early else FILLED, books, now, result)
            return
        action = mo.next_action
        if action == "giveup":
            await self._finish(mo, mo.next_reason or "CANCELED", books, now, result)
            return
        if action not in ("reprice", "cross"):  # the exchange ended it on its own
            if mo.purpose not in CROSS_PURPOSES:
                await self._finish(mo, f"EXCHANGE_{status}", books, now, result)
                return
            mo.detail = f"exchange ended {mo.client_id} as {status}; crossing the remainder"
            action = "cross"
        market = self.markets[mo.symbol]
        book = books.get(mo.symbol)
        if action == "cross":
            price = self._taker_price(market, book, mo.side)
        else:
            price = self._maker_price(market, book, mo.side)
        if price is None:
            mo.next_action = action  # retried on the next poll, when the book is back
            mo.detail = f"no {mo.symbol} book to re-price"
            return
        if not check_min_notional(market, remaining, price):
            mo.detail = f"remainder {remaining} at {price} is below the {mo.symbol} minimum order"
            await self._finish(mo, DUST_REMAINDER, books, now, result)
            return
        if self._opposite_open(mo.symbol, mo.side, exclude=mo.client_id) is not None:
            mo.detail = "an opposite-side order of ours is open on the symbol"
            await self._finish(mo, SELF_CROSS_GUARD, books, now, result)
            return
        await self._replace(mo, remaining, price, action == "cross", books, now, result)

    async def _replace(
        self,
        mo: ManagedOrder,
        qty: Decimal,
        price: Decimal,
        crossing: bool,
        books: dict[str, BookSnapshot],
        now: float,
        result: PollResult,
    ) -> None:
        """Place the remainder again with a new client id (attempt + 1)."""
        old_cid = mo.client_id
        req, attempt = self._new_request(mo.intent, qty, price, "LIMIT", None, mo.attempt + 1, now)
        placed, note = await self._send(req, now, "cross" if crossing else "reprice", mo.intent)
        mo.next_action = mo.next_reason = ""
        if placed is None:
            mo.detail = note
            await self._finish(mo, REPLACE_REJECTED, books, now, result)
            return
        mo.attempt = attempt
        mo.reprices += 1
        mo.crossed = mo.crossed or crossing
        if note:
            mo.detail = note
        self._track(mo, placed, books, now, result, old_client_id=old_cid)

    async def _finish(
        self, mo: ManagedOrder, reason: str, books: dict[str, BookSnapshot], now: float, result: PollResult
    ) -> None:
        """End a logical order: stop tracking, update its position, write outcomes, place a stop."""
        if mo.done:
            return
        mo.done, mo.done_reason = True, reason
        mo.next_action = mo.next_reason = ""
        cid = mo.client_id
        if self.managed.get(cid) is mo:
            del self.managed[cid]
        self.portfolio.open_orders.pop(cid, None)
        self._store_order(mo.order)
        self._olog(
            "status",
            cid,
            mo.symbol,
            {
                "done": True,
                "done_reason": reason,
                "filled_qty": str(mo.logical_filled),
                "reprices": mo.reprices,
            },
        )
        result.done.append(mo)
        pos = self.portfolio.positions.get(mo.position_id) if mo.position_id else None
        if mo.is_stop:
            if pos is not None and pos.stop_order_client_id in mo.client_ids:
                self._use_software_stop(pos, now)
        elif mo.closes_position and pos is not None and not self._closing_orders(pos.id):
            self._set_exit_pending(pos, False, now)
        if mo.decision_id and not (mo.purpose == "entry" and mo.logical_filled > 0):
            self.decision_log.outcome(mo.decision_id, self.order_outcome(mo, now))
        if (
            mo.purpose == "entry"
            and mo.logical_filled > 0
            and pos is not None
            and pos.stop_price is not None
            and not pos.stop_order_client_id
            and pos.id not in self._exiting
        ):
            await self._place_stop(pos, books, now, result)

    async def finish_external(
        self,
        mo: ManagedOrder,
        status: OrderStatus,
        why: str,
        books: dict[str, BookSnapshot],
        now: float,
    ) -> None:
        """The Reconciler learned from the exchange that the current placement ended (``status``).

        Runs exactly what happens when the manager sees an order end by itself (``_after_terminal``
        -> ``_finish``): a dead server stop hands its position to the software stop, ``exit_pending``
        is cleared, the outcome is written, a filled entry gets its protective stop, and a
        must-finish order (exit/stop/leg2/flatten) re-crosses its remainder instead of dropping it.
        Events go to the next ``poll`` result.
        """
        if mo.done:
            return
        self._books = books
        mo.order.status = status
        mo.order.updated_ts = now
        self.portfolio.open_orders.pop(mo.client_id, None)
        mo.detail = _join(mo.detail, f"reconcile: {why}")
        self._olog("status", mo.client_id, mo.symbol, {"source": "reconcile", "status": status, "why": why})
        await self._after_terminal(mo, books, now, self._carry)

    def order_outcome(self, mo: ManagedOrder, now: float) -> dict[str, Any]:
        """Order-level outcome (exits, stops, leg2, flatten, rebalances, unfilled entries)."""
        filled = mo.logical_filled
        if mo.done_reason == FILLED or (filled > 0 and filled >= mo.total_qty):
            status = "FILLED"
        else:
            status = "PARTIALLY_FILLED" if filled > 0 else "UNFILLED"
        avg = mo.avg_price
        out: dict[str, Any] = {
            "kind": "order",
            "status": status,
            "done_reason": mo.done_reason,
            "filled_qty": str(filled),
            "qty": str(mo.total_qty),
            "avg_price": str(avg) if avg is not None else None,
            "fee_paid": str(mo.logical_fee),
            "fee_asset": mo.fee_asset,
            "symbol": mo.symbol,
            "side": mo.side,
            "purpose": mo.purpose,
            "reprices": mo.reprices,
            "crossed": mo.crossed,
            "client_ids": list(mo.client_ids),
            "done_ts": now,
        }
        if mo.position_id:
            out["position_id"] = mo.position_id
        if mo.detail:
            out["detail"] = mo.detail
        return out

    # ------------------------------------------------------------ fills

    def _track(
        self,
        mo: ManagedOrder,
        placed: Order,
        books: dict[str, BookSnapshot],
        now: float,
        result: PollResult,
        old_client_id: str | None = None,
    ) -> None:
        """Make ``placed`` the current placement of ``mo`` (fills already in the answer count)."""
        if old_client_id is not None:
            self.managed.pop(old_client_id, None)
            self.portfolio.open_orders.pop(old_client_id, None)
        # baseline: nothing filled yet, so fills already present in the placement answer are seen
        mo.order = placed.model_copy(
            update={"status": "NEW", "filled_qty": D0, "avg_price": None, "fee_paid": D0}
        )
        mo.placed_ts = mo.last_poll_ts = now
        mo.misses = 0
        mo.client_ids.append(placed.client_id)
        self.managed[placed.client_id] = mo
        self._ingest(mo, placed, books, now, result)

    def _ingest(
        self,
        mo: ManagedOrder,
        snapshot: Order,
        books: dict[str, BookSnapshot],
        now: float,
        result: PollResult,
    ) -> None:
        """Take a fresh exchange snapshot of the current placement; emit the fill delta if any.

        The fill baseline never goes down (see the module docstring), and the fill is applied
        before the order is written to the store.
        """
        base = mo.order
        order = snapshot.model_copy(update={"req": base.req, "reprices": mo.reprices})
        cid = order.client_id
        keep: dict[str, Any] = {}
        if order.filled_qty < base.filled_qty:
            keep = {
                "filled_qty": base.filled_qty,
                "avg_price": base.avg_price,
                "fee_paid": base.fee_paid,
                "fee_asset": base.fee_asset,
            }
        elif order.fee_paid < base.fee_paid:
            keep = {"fee_paid": base.fee_paid, "fee_asset": base.fee_asset or order.fee_asset}
        if keep:
            self._olog(
                "status",
                cid,
                mo.symbol,
                {
                    "baseline_kept": True,
                    "reported_filled": str(order.filled_qty),
                    "known_filled": str(base.filled_qty),
                    "reported_fee": str(order.fee_paid),
                    "known_fee": str(base.fee_paid),
                },
            )
            order = order.model_copy(update=keep)
        fill = self._fill_delta(mo, base, order, now)
        mo.order = order
        if order.is_terminal:
            self.portfolio.open_orders.pop(cid, None)
        else:
            self.portfolio.open_orders[cid] = order
        if fill is not None:
            self._apply_fill(mo, fill, books, now, result)
        self._store_order(order)

    def _fill_delta(self, mo: ManagedOrder, old: Order, new: Order, now: float) -> Fill | None:
        """qty = filled_new - filled_old; price = (avg_new x filled_new - avg_old x filled_old) / qty."""
        qty = new.filled_qty - old.filled_qty
        if qty <= 0:
            return None
        req = new.req
        avg_new = new.avg_price
        if avg_new is None:  # the exchange gave no price: best estimate is our limit or the book
            avg_new = req.price or self._ref_price(req.symbol, req.side) or old.avg_price or D0
            new.avg_price = avg_new
        price = (avg_new * new.filled_qty - (old.avg_price or D0) * old.filled_qty) / qty
        fee = max(new.fee_paid - old.fee_paid, D0)
        market = self.markets[req.symbol]
        fee_asset = new.fee_asset or mo.fee_asset or (market.base if req.side == "BUY" else market.quote)
        mo.fee_asset = fee_asset
        return Fill(
            order_client_id=req.client_id,
            symbol=req.symbol,
            side=req.side,
            price=price,
            qty=qty,
            fee=fee,
            fee_asset=fee_asset,
            ts=now,
            is_maker=None,
            trade_id=f"{req.client_id}@{new.filled_qty}",
        )

    def _apply_fill(
        self, mo: ManagedOrder, fill: Fill, books: dict[str, BookSnapshot], now: float, result: PollResult
    ) -> None:
        """Fill -> balances (optimistic), positions, store, orders log, outcome when a position closes."""
        self.portfolio.apply_fill_optimistic(fill, self.markets[fill.symbol])
        self.balances_dirty = True
        mo.logical_filled += fill.qty
        mo.logical_value += fill.qty * fill.price
        mo.logical_fee += fill.fee
        res = self.positions.on_fill(fill, mo.order.req, books, mo.intent)
        if isinstance(res, ClosedPosition):
            result.closed.append(res)
            if res.decision_id:
                self.decision_log.outcome(res.decision_id, res.outcome())
            if self.store is not None:
                self._store_call(self.store.upsert_position, res.position, open=False, ts=now)
        elif isinstance(res, Position):
            if mo.position_id is None:
                mo.position_id = res.id
            self._save_position(res, now)
        if self.store is not None:
            self._store_call(self.store.add_fill, fill)
        self._olog(
            "fill",
            fill.order_client_id,
            fill.symbol,
            {**fill.model_dump(mode="json"), "decision_id": mo.decision_id, "purpose": mo.purpose},
        )
        result.fills.append(FillEvent(fill=fill, managed=mo, result=res))

    # ------------------------------------------------------------ stops

    async def place_stop_for(self, position: Position, books: dict[str, BookSnapshot]) -> ManagedOrder | None:
        """Place (or return the live) server stop for ``position``; None when a software stop is used.

        Used after an entry completes and by the Reconciler for positions recovered without a stop.
        """
        now = self.clock.now()
        self._books = books
        existing = self.managed.get(position.stop_order_client_id or "")
        if existing is not None and not existing.done:
            return existing
        if position.stop_price is None:
            return None
        return await self._place_stop(position, books, now, self._carry)

    async def _place_stop(
        self,
        pos: Position,
        books: dict[str, BookSnapshot],
        now: float,
        result: PollResult,
        ctx: _Ctx | None = None,
    ) -> ManagedOrder | None:
        """STOP_LIMIT SELL for ``pos`` (or its software fallback, logged with the reason)."""
        market = self.markets.get(pos.symbol)
        if market is None or pos.stop_price is None:
            return None
        stop = round_price(market, pos.stop_price, "SELL")
        given = ctx.intent if ctx is not None else None
        if given is not None and given.price is not None:
            limit = round_price(market, given.price, "SELL")
        else:
            limit = round_price(market, stop * (1 - self.ex.stop_limit_slippage_pct), "SELL")
        want = pos.qty
        if ctx is not None:
            asked = ctx.decision.adjusted_qty if ctx.decision.adjusted_qty is not None else ctx.intent.qty
            if asked is not None:
                want = min(asked, pos.qty)
        qty = round_qty(market, want)
        otype: OrderType = given.order_type if given is not None else "STOP_LIMIT"
        if ctx is None:
            intent = self._auto_stop_intent(pos, stop, limit, qty, now)
            ctx = _Ctx(intent, PROTECTIVE_STOP_RISK, dict(intent.features))
        intent = ctx.intent

        why_not = self._why_no_server_stop(pos, market, books, stop, limit, qty)
        if why_not:
            self._use_software_stop(pos, now)
            self._record("order", ctx, why_not)
            return None
        limit_price = None if otype == "STOP_MARKET" else limit
        req, attempt = self._new_request(intent, qty, limit_price, otype, stop, 0, now)
        placed, note = await self._send(req, now, "server stop", intent)
        if placed is None:
            self._use_software_stop(pos, now)
            self._record("order", ctx, f"server stop rejected -> software stop ({note})", req=req)
            return None
        mo = ManagedOrder(
            intent=intent,
            decision_id=intent.id,
            order=placed,
            attempt=attempt,
            first_ts=now,
            placed_ts=now,
            is_stop=True,
            total_qty=qty,
            position_id=pos.id,
        )
        pos.stop_order_client_id = placed.client_id
        pos.meta = {
            **pos.meta,
            "stop_mode": "server",
            "stops_placed": int(pos.meta.get("stops_placed", 0)) + 1,
        }
        self._save_position(pos, now)
        self._record(
            "order",
            ctx,
            _join(
                f"server stop placed for position {pos.id}: {otype} SELL {qty} stop {stop} limit {limit}",
                note,
            ),
            req=req,
            order=placed,
        )
        self._track(mo, placed, books, now, result)
        return mo

    def _why_no_server_stop(
        self,
        pos: Position,
        market: MarketInfo,
        books: dict[str, BookSnapshot],
        stop: Decimal,
        limit: Decimal,
        qty: Decimal,
    ) -> str:
        """Why a server stop cannot or must not be placed now ('' = place it)."""
        if not self.ex.use_server_side_stops:
            return (
                "software stop (config): execution.use_server_side_stops is false; "
                f"PositionManager.check_exits sells {pos.id} when bid <= {stop}"
            )
        if self._closing_orders(pos.id) or pos.meta.get("exit_pending"):
            return f"no server stop: a closing SELL for {pos.id} is live (never two SELLs) -> software stop"
        book = books.get(pos.symbol)
        bid = book.best_bid if book is not None else None
        if bid is not None and bid <= stop:
            return f"no server stop: bid {bid} is already at/below the stop {stop} -> software stop"
        if not check_min_notional(market, qty, limit):
            return (
                f"no server stop: {qty} at {limit} is below the {pos.symbol} minimum order -> software stop"
            )
        return ""

    def _auto_stop_intent(
        self, pos: Position, stop: Decimal, limit: Decimal, qty: Decimal, now: float
    ) -> Intent:
        n = int(pos.meta.get("stops_placed", 0)) + 1
        base = str(pos.meta.get("entry_decision_id") or pos.id)
        slip = self.ex.stop_limit_slippage_pct
        return Intent(
            id=f"{base}_stop{n}",
            ts=now,
            strategy=pos.strategy,
            symbol=pos.symbol,
            side="SELL",
            order_type="STOP_LIMIT",
            qty=qty,
            price=limit,
            stop_price=stop,
            purpose="stop",
            reason=(
                f"Server-side stop for position {pos.id}: STOP_LIMIT SELL {qty} {pos.base} triggers at "
                f"{stop}, limit {limit} (stop x (1 - {slip}))."
            ),
            features={
                "exit_reason": "stop",
                "position_id": pos.id,
                "stop_price": str(stop),
                "limit": str(limit),
            },
            linked_position_id=pos.id,
        )

    async def _release_stop(
        self, pos: Position, books: dict[str, BookSnapshot], now: float, result: PollResult
    ) -> tuple[bool, str]:
        """Cancel ``pos``'s server stop before another SELL; fills it already had are applied first.

        Returns (ok, note). Not ok means the stop may still be live: the caller must not SELL.
        """
        cid = pos.stop_order_client_id or ""
        mo = self.managed.get(cid)
        if mo is None or mo.done:
            return await self._release_untracked_stop(pos, cid, now)
        before = mo.logical_filled
        mo.next_action, mo.next_reason = "giveup", CANCELED_FOR_EXIT
        if not await self._cancel_current(mo, books, now, result):
            mo.next_action = mo.next_reason = ""  # keep protecting the position
            return False, f"REFUSED: server stop {cid} could not be canceled; not sending a second SELL"
        await self._after_terminal(mo, books, now, result)
        filled = mo.logical_filled - before
        if filled > 0:
            return True, f"server stop {cid} had filled {filled} when it was canceled"
        return True, f"server stop {cid} canceled first"

    async def _release_untracked_stop(self, pos: Position, cid: str, now: float) -> tuple[bool, str]:
        try:
            got = await self.exchange.cancel_order(cid)
        except WallexAPIError as err:
            return False, f"REFUSED: could not cancel server stop {cid}: {err.message}"
        if got is not None and not got.is_terminal:
            return False, f"REFUSED: server stop {cid} is still live after the cancel"
        if got is not None and got.filled_qty > 0:
            return (
                False,
                f"REFUSED: untracked server stop {cid} has fills; the reconciler must book them first",
            )
        self._use_software_stop(pos, now)
        return True, f"untracked server stop {cid} canceled first"

    def _use_software_stop(self, pos: Position, now: float) -> None:
        pos.stop_order_client_id = None
        pos.meta = {**pos.meta, "stop_mode": "software"}
        self._save_position(pos, now)

    # ------------------------------------------------------------ cancel / adopt

    async def cancel(self, client_id: str, reason: str) -> Order | None:
        """Cancel a logical order for good (``done_reason = reason``). Returns the order's last state.

        If the exchange does not confirm the cancel, the order stays tracked and the next poll
        retries it. A client id we do not manage is canceled on the exchange as is.
        """
        now = self.clock.now()
        mo = self.managed.get(client_id)
        if mo is None or mo.done:
            try:
                return await self.exchange.cancel_order(client_id)
            except WallexAPIError as err:
                self._carry.errors.append(f"cancel {client_id}: {err}")
                return None
        await self._request_cancel(mo, "giveup", reason, self._books, now, self._carry)
        return mo.order

    async def cancel_where(self, pred: Callable[[ManagedOrder], bool], reason: str) -> int:
        """Cancel every live logical order matching ``pred``; returns how many are now done."""
        n = 0
        for mo in self.live():
            if pred(mo):
                await self.cancel(mo.client_id, reason)
                n += int(mo.done)
        return n

    def adopt(self, order: Order, intent: Intent | None = None) -> ManagedOrder:
        """Track one of our orders found on the exchange (Reconciler, restart).

        Fills already in ``order`` are taken as booked (the Reconciler books missed fills from the
        trade history); only later fills are applied. The intent is the given one, else the one the
        write-ahead record carries (``order.raw["intent"]``), else it is rebuilt from the order
        request and an ``order`` decision record explains the adoption. An order of unknown origin
        (no decision id, or a strategy without one of our prefixes: the exchange's synthetic
        request) is canceled, never re-priced or crossed (``ADOPTED_UNKNOWN``) — unless it is the
        recorded server stop of one of our positions. An adopted stop of a position becomes that
        position's server stop.
        """
        cid = order.client_id
        existing = self.managed.get(cid)
        if existing is not None and not existing.done:
            return existing
        now = self.clock.now()
        req = order.req
        is_stop = req.type in STOP_TYPES
        if intent is None:
            intent = intent_from_raw(order)
        pos_id = intent.linked_position_id if intent is not None else None
        if pos_id is None:
            pos_id = next(
                (pid for pid, p in sorted(self.portfolio.positions.items()) if p.stop_order_client_id == cid),
                None,
            )
        unknown_origin = intent is None and (
            not req.decision_id or strategy_prefix(req.strategy) not in OUR_PREFIXES
        )
        give_up = unknown_origin and not (is_stop and pos_id is not None)
        if intent is None:
            intent = Intent(
                id=req.decision_id or f"adopted_{cid}",
                ts=now,
                strategy=req.strategy,
                symbol=req.symbol,
                side=req.side,
                order_type=req.type,
                qty=req.qty,
                price=req.price,
                stop_price=req.stop_price,
                purpose="stop" if is_stop else req.purpose,
                reason=f"Adopted by the reconciler: our order {cid} was found on the exchange untracked.",
                linked_position_id=pos_id,
            )
            if not req.decision_id:
                self._record(
                    "order",
                    _Ctx(intent, ADOPTED_RISK, {}),
                    f"adopted untracked order {cid} found on the exchange"
                    + ("; its purpose is unknown: it is canceled" if give_up else ""),
                    req=req,
                    order=order,
                )
        if req.decision_id != intent.id:
            order = order.model_copy(update={"req": req.model_copy(update={"decision_id": intent.id})})
        t = min(order.created_ts, now)
        mo = ManagedOrder(
            intent=intent,
            decision_id=intent.id,
            order=order,
            first_ts=t,
            placed_ts=t,
            max_reprices=self._max_reprices(intent),
            crossed=intent.taker,
            logical_filled=order.filled_qty,
            is_stop=is_stop,
            total_qty=req.qty,
            logical_value=(order.avg_price or D0) * order.filled_qty,
            logical_fee=order.fee_paid,
            fee_asset=order.fee_asset,
            position_id=pos_id if pos_id is not None else intent.linked_position_id,
            client_ids=[cid],
        )
        if give_up:
            mo.next_action, mo.next_reason = "giveup", ADOPTED_UNKNOWN
            mo.detail = (
                f"adopted {cid} without a stored request (strategy {req.strategy!r}, no decision id): "
                "its purpose is unknown, so it is canceled instead of re-priced or crossed"
            )
        pos = self.portfolio.positions.get(mo.position_id or "")
        if is_stop and pos is not None and not pos.stop_order_client_id and not order.is_terminal:
            pos.stop_order_client_id = cid  # e.g. a stop placed right before a crash
            pos.meta = {**pos.meta, "stop_mode": "server"}
            self._save_position(pos, now)
        self.managed[cid] = mo
        if not order.is_terminal:
            self.portfolio.open_orders[cid] = order
        self._store_order(order)
        self._olog(
            "status",
            cid,
            req.symbol,
            {"adopted": True, "status": order.status, "decision_id": intent.id, "give_up": give_up},
        )
        return mo

    # ------------------------------------------------------------ persistence

    def to_state(self) -> dict[str, Any]:
        """JSON-able state of every live logical order; also upserts their orders into the store."""
        managed = []
        for mo in self.live():
            self._store_order(mo.order)
            managed.append(mo.to_state())
        return {"version": 1, "managed": managed}

    def load_state(self, state: dict[str, Any] | None, orders: list[Order]) -> None:
        """Restore live logical orders after a restart; each is re-read on the next poll.

        ``orders`` are the store's orders (``store.load_orders(non_terminal_only=True)``). When the
        store holds a fresher snapshot of an order than ``state`` (fills processed after the last
        ``to_state``), that snapshot is the baseline and its extra fills are counted as booked.
        A non-terminal order of ours missing from ``state`` continues its logical order when one
        has the same decision id (a re-placement after the last ``to_state``), else it is adopted.
        """
        known = {o.client_id: o for o in orders}
        if self.store is not None:
            for o in self.store.load_orders():
                known.setdefault(o.client_id, o)
        register = getattr(self.exchange, "register_request", None)
        for item in sorted((state or {}).get("managed") or [], key=lambda d: str(d["client_id"])):
            cid = str(item["client_id"])
            saved = Order.model_validate(item["order"])
            mo = ManagedOrder.from_state(item, saved)
            if mo.done:
                continue
            fresh = known.pop(cid, None)
            if fresh is not None and fresh.filled_qty > saved.filled_qty:
                self._count_booked(mo, saved, fresh)
            mo.order = (fresh or saved).model_copy(update={"req": saved.req})
            mo.refresh = True
            self.managed[cid] = mo
            if not mo.order.is_terminal:
                self.portfolio.open_orders[cid] = mo.order
            if callable(register):
                register(mo.order.req)
        by_decision = {mo.decision_id: mo for mo in self.live()}
        for cid in sorted(known):
            order = known[cid]
            if order.is_terminal or not is_ours(cid) or cid in self.managed:
                continue
            if callable(register):
                register(order.req)
            mo = by_decision.get(order.req.decision_id)
            if mo is not None and mo.order.is_terminal:  # re-placed after the last to_state()
                self._count_booked(mo, mo.order.model_copy(update={"filled_qty": D0, "fee_paid": D0}), order)
                self.managed.pop(mo.client_id, None)
                self.portfolio.open_orders.pop(mo.client_id, None)
                mo.order, mo.next_action, mo.next_reason = order, "", ""
                mo.client_ids.append(cid)
                self.managed[cid] = mo
                self.portfolio.open_orders[cid] = order
            else:
                self.adopt(order)
        if self.managed:
            self.balances_dirty = True

    @staticmethod
    def _count_booked(mo: ManagedOrder, old: Order, new: Order) -> None:
        """Fills between ``old`` and ``new`` were already booked before the restart: count them."""
        qty = new.filled_qty - old.filled_qty
        if qty <= 0:
            return
        avg_new = new.avg_price or old.avg_price or D0
        mo.logical_filled += qty
        mo.logical_value += avg_new * new.filled_qty - (old.avg_price or D0) * old.filled_qty
        mo.logical_fee += max(new.fee_paid - old.fee_paid, D0)

    # ------------------------------------------------------------ placement helpers

    async def _send(
        self, req: OrderRequest, now: float, why: str, intent: Intent
    ) -> tuple[Order | None, str]:
        """``place_order`` once. Returns (order, note); (None, "REJECTED: ...") when refused.

        The request is written ahead (``_write_ahead``) before it leaves. An ambiguous failure
        returns an UNKNOWN placeholder order: it may exist on the exchange, so it is tracked,
        counted as a placement and resolved by ``get_order`` (never re-sent).
        """
        self._write_ahead(req, intent, now)
        self._olog(
            "place", req.client_id, req.symbol, {"source": "order_manager", "why": why, **_req_json(req)}
        )
        try:
            placed = await self.exchange.place_order(req)
        except OrderRejected as err:
            self._olog(
                "error",
                req.client_id,
                req.symbol,
                {"code": err.code, "status": err.status, "message": err.message},
            )
            self._store_order(
                Order(
                    req=req,
                    status="REJECTED",
                    created_ts=now,
                    updated_ts=now,
                    raw={"error_code": err.code, "error": err.message},
                )
            )
            return None, f"REJECTED: {err.code} {err.message}"
        except WallexAPIError as err:
            self._olog(
                "error",
                req.client_id,
                req.symbol,
                {"code": err.code, "status": err.status, "message": err.message, "ambiguous": True},
            )
            self.portfolio.record_placement(now)
            unknown = Order(
                req=req,
                status="UNKNOWN",
                created_ts=now,
                updated_ts=now,
                raw={"error_code": err.code, "error": err.message},
            )
            return unknown, (
                f"AMBIGUOUS: {err.code} {err.message}; tracked as UNKNOWN and resolved by "
                f"get_order({req.client_id}), never re-sent"
            )
        self.portfolio.record_placement(now)
        placed = placed.model_copy(update={"req": req})
        self._olog(
            "response",
            req.client_id,
            req.symbol,
            {"source": "order_manager", "status": placed.status, "exchange_id": placed.exchange_id},
        )
        return placed, ""

    def _write_ahead(self, req: OrderRequest, intent: Intent, now: float) -> None:
        """Store ``req`` as UNKNOWN with its intent and commit, before it is sent (crash safety)."""
        if self.store is None:
            return
        placeholder = Order(
            req=req,
            status="UNKNOWN",
            created_ts=now,
            updated_ts=now,
            raw={"write_ahead": True, "intent": intent.model_dump(mode="json")},
        )
        self._store_order(placeholder)
        if self.checkpoint is not None:
            try:
                self.checkpoint()
            except Exception:  # noqa: BLE001 - the placement itself must not fail on persistence
                self.store_errors += 1
                log.exception("order manager: checkpoint before placing %s failed", req.client_id)
        else:
            self._store_call(self.store.checkpoint)

    def _store_order(self, order: Order) -> None:
        if self.store is not None:
            self._store_call(self.store.upsert_order, order)

    def _store_call(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> None:
        """A store write that never raises: the row stays in the store's buffer and is retried on
        its next flush, and the in-memory state (fills, positions) is already up to date."""
        try:
            fn(*args, **kwargs)
        except sqlite3.Error as exc:
            self.store_errors += 1
            log.error("order manager: store write failed (retried on the next flush): %s", exc)

    def _new_request(
        self,
        intent: Intent,
        qty: Decimal,
        price: Decimal | None,
        otype: OrderType,
        stop_price: Decimal | None,
        attempt: int,
        now: float,
    ) -> tuple[OrderRequest, int]:
        # the client id must carry one of our prefixes, or a restart would treat the order as foreign
        id_strategy = intent.strategy if strategy_prefix(intent.strategy) in OUR_PREFIXES else ID_FALLBACK_STRATEGY
        cid = make_client_id(id_strategy, intent.symbol, now, intent.id, attempt)
        while cid in self.managed:  # hash collision with a live order: next attempt number
            attempt += 1
            cid = make_client_id(id_strategy, intent.symbol, now, intent.id, attempt)
        req = OrderRequest(
            client_id=cid,
            symbol=intent.symbol,
            side=intent.side,
            type=otype,
            qty=qty,
            price=price,
            stop_price=stop_price if otype in STOP_TYPES else None,
            strategy=intent.strategy,
            purpose=intent.purpose,
            decision_id=intent.id,
        )
        return req, attempt

    def _max_reprices(self, intent: Intent) -> int:
        return self.ex.flatten_max_reprices if intent.purpose == "flatten" else self.ex.limit_max_reprices

    def _initial_price(self, intent: Intent, market: MarketInfo, book: BookSnapshot | None) -> Decimal | None:
        if intent.order_type == "MARKET":
            return None
        if intent.price is not None:
            return round_price(market, intent.price, intent.side)
        if intent.taker:
            return self._taker_price(market, book, intent.side)
        return self._maker_price(market, book, intent.side)

    def _maker_price(self, market: MarketInfo, book: BookSnapshot | None, side: Side) -> Decimal | None:
        """BUY: best bid + offset ticks while that stays below the best ask, else the best bid."""
        if book is None:
            return None
        bid, ask = book.best_bid, book.best_ask
        offset = market.tick * self.ex.maker_offset_ticks
        if side == "BUY":
            if bid is None:
                return None
            price = bid + offset
            if ask is not None and price >= ask:
                price = bid
        else:
            if ask is None:
                return None
            price = ask - offset
            if bid is not None and price <= bid:
                price = ask
        return round_price(market, price, side)

    @staticmethod
    def _taker_price(market: MarketInfo, book: BookSnapshot | None, side: Side) -> Decimal | None:
        """Cross the spread: BUY at the best ask, SELL at the best bid."""
        if book is None:
            return None
        price = book.best_ask if side == "BUY" else book.best_bid
        return None if price is None else round_price(market, price, side)

    def _ref_price(self, symbol: str, side: Side) -> Decimal | None:
        book = self._books.get(symbol)
        if book is None:
            return None
        return book.best_ask if side == "BUY" else book.best_bid

    # ------------------------------------------------------------ guards

    def _opposite_open(self, symbol: str, side: Side, exclude: str | None = None) -> Order | None:
        """Our open non-stop order on ``symbol`` with the other side, if any."""
        for cid, o in sorted(self.portfolio.open_orders.items()):
            if cid == exclude or o.is_terminal or o.req.type in STOP_TYPES:
                continue
            if o.req.symbol == symbol and o.req.side != side:
                return o
        return None

    def _closing_orders(self, position_id: str, exclude: ManagedOrder | None = None) -> list[ManagedOrder]:
        """Live closing SELLs (not stops) of a position."""
        return [
            m
            for m in self.live()
            if m is not exclude and not m.is_stop and m.closes_position and m.position_id == position_id
        ]

    def _closable_qty(self, pos: Position, exclude: ManagedOrder | None = None) -> Decimal:
        """Part of the position not yet covered by live closing SELLs."""
        covered = sum((m.remaining_qty for m in self._closing_orders(pos.id, exclude)), D0)
        return max(pos.qty - covered, D0)

    def _remaining(self, mo: ManagedOrder) -> Decimal:
        """Logical remainder, capped by what is left of the position for closing orders."""
        rem = mo.remaining_qty
        if mo.closes_position:
            pos = self.portfolio.positions.get(mo.position_id or "")
            if pos is None:
                return D0
            rem = min(rem, self._closable_qty(pos, exclude=mo))
        return round_qty(self.markets[mo.symbol], rem)

    async def _cancel_entries_for(
        self, pos: Position, symbol: str, books: dict[str, BookSnapshot], now: float, result: PollResult
    ) -> None:
        """Before a SELL of ``pos`` in ``symbol``: cancel that position's open BUY entries there."""
        for mo in self.live():
            if (
                mo.purpose == "entry"
                and mo.position_id == pos.id
                and mo.symbol == symbol
                and mo.side == "BUY"
            ):
                mo.detail = "canceled: a SELL of its position was submitted"
                await self._request_cancel(mo, "giveup", CANCELED_FOR_EXIT, books, now, result)

    # ------------------------------------------------------------ positions / logs

    def _set_exit_pending(self, pos: Position, flag: bool, now: float) -> None:
        if bool(pos.meta.get("exit_pending")) != flag:
            pos.meta = {**pos.meta, "exit_pending": flag}
            self._save_position(pos, now)

    def _save_position(self, pos: Position, now: float) -> None:
        if self.store is not None:
            self._store_call(self.store.upsert_position, pos, open=pos.id in self.portfolio.positions, ts=now)

    def _record(
        self,
        kind: DecisionKind,
        ctx: _Ctx,
        notes: str,
        req: OrderRequest | None = None,
        order: Order | None = None,
    ) -> None:
        """One decision record; ``order`` holds the request and, when accepted, the exchange answer."""
        payload: dict[str, Any] | None = None
        if req is not None:
            payload = {**_req_json(req), "accepted": order is not None}
            if order is not None:
                payload["response"] = order.model_dump(mode="json", exclude={"req", "raw"})
        self.decision_log.record(
            kind=kind,
            decision_id=ctx.intent.id,
            strategy=ctx.intent.strategy,
            symbol=ctx.intent.symbol,
            features=ctx.features,
            plan_snapshot=ctx.plan_snapshot,
            intent=ctx.intent,
            risk=ctx.decision,
            order=payload,
            notes=notes,
        )

    def _olog(self, action: str, client_id: str, symbol: str, data: dict[str, Any]) -> None:
        if self.orders_log is not None:
            self.orders_log.log(action, client_id, symbol, data)


def decision_qty(decision: RiskDecision, intent: Intent, ref_price: Decimal) -> Decimal | None:
    """Order quantity: the RiskManager's adjusted qty, else the intent's qty, else notional / price."""
    if decision.adjusted_qty is not None:
        return decision.adjusted_qty
    if intent.qty is not None:
        return intent.qty
    if intent.notional is not None and ref_price > 0:
        return intent.notional / ref_price
    return None


def _req_json(req: OrderRequest) -> dict[str, Any]:
    return {"request": req.model_dump(mode="json")}


def intent_from_raw(order: Order) -> Intent | None:
    """The intent a write-ahead record carries (``raw["intent"]``), if any and valid."""
    data = order.raw.get("intent") if isinstance(order.raw, dict) else None
    if not isinstance(data, dict):
        return None
    try:
        return Intent.model_validate(data)
    except ValueError:
        return None

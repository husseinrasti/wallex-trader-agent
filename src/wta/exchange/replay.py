"""ReplayExchange: the ``Exchange`` of ``python -m wta replay`` (PRD-05 §6).

The engine is driven again over the recorded inputs; this exchange answers every call with what the
recorded run's exchange answered for the same ``client_id``, so the portfolio evolves exactly as it
did. It never touches the network and never simulates a fill.

Inputs
  * ``orders``: the recorded ``orders.jsonl`` family (``iter_family(log_dir, "orders")``), written by
    the OrderManager (``place``, ``response``, ``error``, ``fill``, ``cancel``, ``status``).
  * ``balances`` (optional): the state DB's ``balances`` rows as snapshots ``(ts, {asset: Balance})``
    (``load_balance_snapshots``). The engine writes one when a refresh differs from its own balances,
    the Reconciler one on every run.
  * ``my_trades`` (optional): the state DB's ``fills`` table (``load_recorded_fills``). Fills booked
    by the Reconciler never reach ``orders.jsonl``; they are merged in from here (fills that are in
    ``orders.jsonl`` are recognised by their ``trade_id`` and not counted twice).
  * ``initial_balances`` / ``balances_fallback`` (optional): see ``get_balances``.
  * ``clock``: the clock the engine reads (the runner passes the engine's ``TickClock``). Without a
    clock the answers advance one recorded observation per call (tests, simple tools).

What the orders log says, and how it is turned into answers. Every line of one engine tick carries
the same ``ts`` (the clock is frozen during a tick), so a line's millisecond is its tick. For each
client id the lines become a list of *observations*: states of the order as the recorded engine saw
them, each tagged with its tick and with the call that saw it.

  * ``place`` + ``response`` -> the placement answer (status, exchange id; fills logged right after it
    in the same tick were already in that answer). ``error`` right after the ``place`` ->
    ``place_order`` raises the same ``OrderRejected`` (or, when ``ambiguous``, the same
    ``WallexAPIError``). Only the OrderManager's own ``place``/``response``/``cancel`` lines
    (``data.source == "order_manager"``) are read; exchange-client traffic lines are skipped.
  * ``fill`` -> the cumulative state of the order grows (filled from the fill's ``trade_id``
    ``<client_id>@<filled>``, fee, fee asset). The average price is chosen so that the
    OrderManager's delta formula ``(avg_new x filled_new - avg_old x filled_old) / qty`` gives back
    the recorded fill price (same Decimal string; see ``reproducing_avg``). A fill in the tick of a
    ``cancel`` was in the cancel's answer; any other fill was seen by a ``get_order`` poll in its tick.
  * ``cancel`` -> the cancel answer: terminal (CANCELED, or FILLED when complete) when the logical
    order ended or was re-placed in that tick, or later with nothing observed in between (the
    re-placement waited for a book); else still live (the exchange did not confirm: the
    OrderManager cancels again on a later poll, which is another ``cancel`` line).
  * ``status``: ``done`` with ``EXCHANGE_<S>`` -> a poll in that tick showed ``S``; any other end in a
    tick without a cancel -> a poll showed the order ended (CANCELED); ``source: reconcile`` -> the
    Reconciler learned the final status in that tick (EXPIRED / not found included);
    ``found: false`` (``misses``) -> ``get_order`` answers "unknown" from the previous observation
    on; ``baseline_kept`` -> that one answer lagged (it showed the reported filled quantity and fee;
    the next answers do not lag).
  * A ``place`` with ``why`` ``reprice``/``cross`` ends the previous placement of the same decision in
    that tick (by the cancel answer, or a poll that showed the exchange had ended it).

Answers (clock mode). ``now`` = the clock's tick:
  * ``place_order(req)``: the recorded placement answer for ``req.client_id``; ``created_ts`` and
    ``updated_ts`` are ``now`` (the recorded exchange stamped them in that tick).
  * ``get_order(cid)``: consumes every observation due by ``now`` (polls, exchange-ended, Reconciler
    deltas; a Reconciler observation of this very tick only once ``get_open_orders`` started the
    Reconciler's pass; a fill the Reconciler booked from my-trades only after ``get_my_trades``
    showed it) and stops at a recorded cancel. Asked more often than recorded, it repeats the last
    state.
  * ``cancel_order(cid)``: consumes up to the next recorded cancel due by ``now`` and returns its
    answer. A cancel the recorded run never logged (e.g. an untracked server stop) gets the current
    state as CANCELED (or as it is when already finished) and is listed in ``notes``.
  * ``get_open_orders`` / ``get_my_trades``: the orders this replay placed, as the recorded exchange
    knew them at ``now``; trades are the recorded fills visible by then (Reconciler delta fills
    ``<cid>:reconcile:<filled>`` come only from ``get_order``, as they did).
  * ``get_balances``: a snapshot recorded in this tick, else ``balances_fallback()`` (the runner
    passes the engine's own balances: the recorded engine wrote no row because the exchange's answer
    equalled them), else derived (the latest snapshot so far, or ``initial_balances``, plus the
    recorded fills since, booked like ``PortfolioState.apply_fill_optimistic``: free amounts only,
    no locks). Which row of the tick: the Reconciler's read (the one right after
    ``get_my_trades``) always wrote one: the tick's last row once refreshes used theirs, its first
    while none did (the deferred startup reconcile runs before them). A refresh wrote one only when
    the answer differed from the engine's balances, so it takes the next unused row unless that row
    equals ``balances_fallback()``. A read while the engine has no balances yet is the startup seed,
    which never writes a row: it gets the next row (the startup reconcile's) without using it up.
    Without a clock the snapshots are served in order.

Divergence. A client id the recorded run never placed (a decision it never made), a second
placement of one client id, or an order asked about before this replay placed it, raises
``ReplayDivergence`` (an ``OrderRejected`` with code ``replay_divergence``): the OrderManager logs
the decision as ``REJECTED: replay_divergence ...`` and the replay keeps going; the comparator
reports it. Every divergence is also kept in ``divergences``. A request whose fields differ from
the recorded one under the same client id is answered from the record (the portfolio stays on the
recorded path) and listed in ``divergences`` (``request_mismatch``). ``all_divergences()`` adds the
recorded placements this replay never made (``not_replayed``). A cancel sent before the recorded
cancel of the same order is answered CANCELED and listed as ``early_cancel``.

Known limits (documented, not guessed): a non-ambiguous order that the exchange later "forgot"
answers "unknown" from the poll right after its last observation (the record does not say which
earlier polls still found it); a placement that ended with an unconfirmed cancel and was then seen
ended by the very next poll is replayed as if the cancel had been confirmed; a fill price whose
delta cannot be hit exactly within a few units of the 28th digit keeps the nearest average.

Every call is appended to ``calls`` (method, tick, client id / symbol, how it was answered), so tests
can assert the whole run was served from the record. Determinism: no wall clock, no randomness,
dicts walked in sorted or log order, Decimal arithmetic only.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation, getcontext
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from wta.data.store import Store
from wta.exchange.base import Exchange, OrderRejected, WallexAPIError
from wta.logging_.jsonl import iter_family
from wta.models import D0, TERMINAL_STATUSES, Balance, Fill, MarketInfo, Order, OrderRequest
from wta.utils.timeutil import Clock, iso, parse_iso

DIVERGENCE_CODE = "replay_divergence"
DIVERGENCE_STATUS = 409  # conflict: the replay asked for something the recorded run never did
QUOTE_ASSETS = ("USDT", "TMN")  # symbol suffixes, when no MarketInfo is given
MANAGER_SOURCE = "order_manager"  # data.source of the OrderManager's place/response/cancel lines

_FILL_FIELDS = frozenset(Fill.model_fields)
_REQUEST_FIELDS = (
    "symbol",
    "side",
    "type",
    "qty",
    "price",
    "stop_price",
    "strategy",
    "purpose",
    "decision_id",
)
_REPLACE_WHY = frozenset({"reprice", "cross"})
_NUDGE_ULPS = 6  # how far (units in the last place) the average may move to reproduce a fill price

# observation kinds ("via"): which call saw the state
PLACE, POLL, CANCEL, RECONCILE, ENDED, BOOKED = "place", "poll", "cancel", "reconcile", "ended", "booked"
_GET_VIAS = frozenset({POLL, RECONCILE, ENDED, BOOKED})  # what get_order may consume
_MERGE_VIAS = frozenset({PLACE, CANCEL, POLL, RECONCILE})  # a fill of the same tick joins this answer


class ReplayDivergence(OrderRejected):
    """The replay asked the exchange for something the recorded run never did.

    It is an ``OrderRejected`` (``status=409``, ``code="replay_divergence"``), so the OrderManager
    records the decision as rejected and the Reconciler / poll paths log an error: the replay never
    crashes on it. ``kind`` is ``unknown_client_id``, ``duplicate_placement`` or ``not_placed``;
    ``details`` holds the request and what the recorded run did for the same decision.
    """

    def __init__(self, kind: str, client_id: str, message: str, details: dict[str, Any]) -> None:
        super().__init__(DIVERGENCE_STATUS, DIVERGENCE_CODE, message, payload=details)
        self.kind = kind
        self.client_id = client_id
        self.details = details


@dataclass(frozen=True)
class ReplayCall:
    """One call the engine made; ``answer`` says how it was served."""

    method: str
    ts: float | None
    client_id: str | None = None
    symbol: str | None = None
    # recorded | repeat | recorded_error | unrecorded | divergence | derived | fallback
    answer: str = "recorded"


# ---------------------------------------------------------------- recorded observations


@dataclass
class _Obs:
    """The order as the recorded engine saw it at tick ``ms`` (through the call ``via``)."""

    ms: int
    via: str
    filled: Decimal
    avg: Decimal | None
    fee: Decimal
    fee_asset: str | None
    updated_ts: float
    exists: bool = True  # False: the exchange did not know the order (get_order -> None)
    status: str | None = None  # status the exchange stated (response / EXCHANGE_x / reconcile)
    ended: bool = False  # the recorded engine saw the order end with this answer (no status given)
    lag: tuple[Decimal, Decimal] | None = None  # (filled, fee) this one answer showed instead
    fills: list[Fill] = field(default_factory=list)  # trades that became visible with this state
    listed: bool = True  # its fills appear in get_my_trades
    final: str = "NEW"  # status of the answer (computed once the whole log is read)


@dataclass
class _Script:
    """Everything recorded for one client id, plus the replay's cursor over it."""

    client_id: str
    req: OrderRequest
    placed_ms: int
    why: str
    obs: list[_Obs]
    exchange_id: str | None = None
    created_ts: float = 0.0
    responded: bool = False
    place_error: dict[str, Any] | None = None
    not_found: bool = False
    ended_ms: set[int] = field(default_factory=set)  # ticks in which the recorded engine saw it end
    # cumulative state while reading the log
    filled: Decimal = D0
    avg: Decimal | None = None
    fee: Decimal = D0
    fee_asset: str | None = None
    # replay side
    placed: bool = False
    cursor: int = 0  # observations consumed
    override: Order | None = None  # state after a cancel the recorded run never logged

    def snapshot(self, ms: int, via: str, updated_ts: float, **kw: Any) -> _Obs:
        return _Obs(
            ms=ms,
            via=via,
            filled=self.filled,
            avg=self.avg,
            fee=self.fee,
            fee_asset=self.fee_asset,
            updated_ts=updated_ts,
            **kw,
        )


# ---------------------------------------------------------------- helpers


def _ms_of_iso(text: str) -> int:
    value = parse_iso(text)
    if value is None:
        raise ValueError("empty timestamp")
    return int(round(value * 1000))


def tick_ms(ts: float) -> int:
    """The millisecond the logs print for a clock reading (``iso`` truncates), i.e. its tick key."""
    return _ms_of_iso(iso(ts))


def _dec(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except InvalidOperation:
        return None


def _delta_price(avg: Decimal, filled: Decimal, base: Decimal, qty: Decimal) -> Decimal:
    """The OrderManager's (and the Reconciler's) fill price from two snapshots of one order."""
    return (avg * filled - base) / qty


def reproducing_avg(
    prev_avg: Decimal | None, prev_filled: Decimal, new_filled: Decimal, price: Decimal
) -> Decimal:
    """Average price for ``new_filled`` whose delta against ``(prev_avg, prev_filled)`` is ``price``.

    The OrderManager derives a fill as ``(avg_new x filled_new - avg_old x filled_old) / qty``. On a
    first fill the recorded price itself is the answer (``price x q / q`` gives back the same
    digits). Otherwise the natural candidate ``(avg_old x filled_old + price x qty) / filled_new``
    can be one unit in the last place off after Decimal rounding; the nearest value (within a few
    units) that gives back the recorded price string exactly is used instead.
    """
    qty = new_filled - prev_filled
    if qty <= 0 or new_filled <= 0:
        return prev_avg if prev_avg is not None else price
    base = (prev_avg or D0) * prev_filled
    want = str(price)
    if base == 0 and str(_delta_price(price, new_filled, base, qty)) == want:
        return price
    candidate = (base + price * qty) / new_filled
    if str(_delta_price(candidate, new_filled, base, qty)) == want:
        return candidate
    ulp = Decimal(1).scaleb(candidate.adjusted() - getcontext().prec + 1)
    for k in range(1, _NUDGE_ULPS + 1):
        for c in (candidate + k * ulp, candidate - k * ulp):
            if str(_delta_price(c, new_filled, base, qty)) == want:
                return c
    return candidate


def _split_symbol(symbol: str, markets: Mapping[str, MarketInfo]) -> tuple[str, str]:
    m = markets.get(symbol)
    if m is not None:
        return m.base, m.quote
    for quote in QUOTE_ASSETS:
        if symbol.endswith(quote) and len(symbol) > len(quote):
            return symbol[: -len(quote)], quote
    return symbol, ""


def _as_balance(asset: str, value: Any) -> Balance:
    if isinstance(value, Balance):
        return value
    amount = _dec(value)
    return Balance(asset=asset, free=amount if amount is not None else D0)


def _jsonable(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    return value


def load_balance_snapshots(state_db: str | Path) -> list[tuple[float, dict[str, Balance]]]:
    """The ``balances`` table of a state DB as snapshots ``(ts, {asset: Balance})``, in write order.

    ``Store.add_balances`` writes one call's assets in sorted order, so a new snapshot starts with a
    new ``ts`` or when the asset order restarts (two calls in one tick: a refresh, then the
    Reconciler).
    """
    uri = Path(state_db).resolve().as_uri() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    try:
        rows = conn.execute("SELECT ts, asset, free, locked FROM balances ORDER BY ts, rowid").fetchall()
    finally:
        conn.close()
    out: list[tuple[float, dict[str, Balance]]] = []
    prev_asset = ""
    for ts, asset, free, locked in rows:
        if not out or out[-1][0] != ts or asset <= prev_asset:
            out.append((float(ts), {}))
        out[-1][1][asset] = Balance(asset=asset, free=Decimal(free), locked=Decimal(locked))
        prev_asset = asset
    return out


def load_recorded_fills(state_db: str | Path) -> list[Fill]:
    """The ``fills`` table of a state DB (every fill the recorded engine booked), oldest first."""
    store = Store.open_readonly(state_db)
    try:
        return store.load_fills()
    finally:
        store.close()


# ---------------------------------------------------------------- the exchange


class ReplayExchange(Exchange):
    """Answers with the recorded exchange's answers (see the module docstring)."""

    name = "replay"

    def __init__(
        self,
        orders: Iterable[Mapping[str, Any]],
        *,
        clock: Clock | None = None,
        markets: Mapping[str, MarketInfo] | None = None,
        balances: Iterable[tuple[float, Mapping[str, Balance]]] | None = None,
        initial_balances: Mapping[str, Any] | None = None,
        my_trades: Iterable[Fill] | None = None,
        balances_fallback: Callable[[], Mapping[str, Balance]] | None = None,
    ) -> None:
        self._clock = clock
        self._markets = dict(sorted((markets or {}).items()))
        self._fallback = balances_fallback
        self._initial = {a: _as_balance(a, v) for a, v in sorted((initial_balances or {}).items())}
        self._snapshots: list[tuple[int, dict[str, Balance]]] = [
            (tick_ms(ts), {a: snap[a] for a in sorted(snap)}) for ts, snap in (balances or [])
        ]
        self._snap_by_ms: dict[int, list[dict[str, Balance]]] = {}
        for ms, snap in self._snapshots:
            self._snap_by_ms.setdefault(ms, []).append(snap)
        self._snap_used: dict[int, set[int]] = {}  # tick -> indices of its snapshots already served
        self._snap_cursor = 0  # without a clock: next snapshot in order
        self._reconcile_ms: int | None = None  # tick in which the Reconciler's pass started
        self._trades_ms: int | None = None  # latest tick in which get_my_trades was asked
        self.scripts: dict[str, _Script] = {}
        self.placement_order: list[str] = []  # recorded client ids in placement order
        self.calls: list[ReplayCall] = []
        self.divergences: list[dict[str, Any]] = []
        self.notes: list[dict[str, Any]] = []
        self._build(list(orders), list(my_trades or []))

    @classmethod
    def from_log_dir(
        cls, log_dir: str | Path, *, state_db: str | Path | None = None, **kwargs: Any
    ) -> ReplayExchange:
        """From a recorded ``logs/`` directory (``orders`` family) and, optionally, its state DB
        (balances snapshots and booked fills)."""
        orders = list(iter_family(Path(log_dir), "orders"))
        if state_db is not None:
            kwargs.setdefault("balances", load_balance_snapshots(state_db))
            kwargs.setdefault("my_trades", load_recorded_fills(state_db))
        return cls(orders, **kwargs)

    def set_balances_fallback(self, fallback: Callable[[], Mapping[str, Balance]] | None) -> None:
        """The engine's own balances (``lambda: engine.portfolio.balances``); set once it exists."""
        self._fallback = fallback

    # ------------------------------------------------------------ reading the record

    def _build(self, records: list[Mapping[str, Any]], my_trades: list[Fill]) -> None:
        logged_ids = {
            str((r.get("data") or {}).get("trade_id"))
            for r in records
            if r.get("action") == "fill" and (r.get("data") or {}).get("trade_id")
        }
        booked = sorted(
            (f for f in my_trades if not (f.trade_id and f.trade_id in logged_ids)),
            key=lambda f: (tick_ms(f.ts), f.ts, f.trade_id or ""),
        )
        current: dict[str, str] = {}  # decision id -> client id of its current placement
        b = 0
        for line, rec in enumerate(records, start=1):
            try:
                ms = _ms_of_iso(str(rec.get("ts")))
            except ValueError:
                self._note("bad_line", None, f"orders line {line}: no valid ts; ignored")
                continue
            while b < len(booked) and tick_ms(booked[b].ts) <= ms:
                self._book(booked[b])
                b += 1
            self._read(rec, ms, line, current)
        for fill in booked[b:]:
            self._book(fill)
        for cid in self.placement_order:
            self._resolve(self.scripts[cid])

    def _read(self, rec: Mapping[str, Any], ms: int, line: int, current: dict[str, str]) -> None:
        action = rec.get("action")
        cid = str(rec.get("client_id") or "")
        data: Mapping[str, Any] = rec.get("data") or {}
        if action in ("place", "response", "cancel") and data.get("source") != MANAGER_SOURCE:
            return  # exchange-client traffic (e.g. a REST retry), not an OrderManager step
        if action == "place":
            self._read_place(cid, ms, line, data, current)
            return
        s = self.scripts.get(cid)
        if s is None:
            self._note("no_placement", cid, f"orders line {line}: {action} without a recorded place; ignored")
            return
        if action == "response":
            s.responded = True
            s.exchange_id = data.get("exchange_id") or s.exchange_id
            s.obs[0].status = str(data.get("status") or "NEW")
        elif action == "error":
            if s.responded or s.place_error is not None or len(s.obs) != 1 or "attempt" in data:
                return  # not the OrderManager's placement error (e.g. a cancel refused by the client)
            s.place_error = {
                "code": data.get("code"),
                "status": data.get("status"),
                "message": str(data.get("message") or ""),
                "ambiguous": bool(data.get("ambiguous")),
            }
        elif action == "fill":
            self._read_fill(s, ms, data)
        elif action == "cancel":
            s.obs.append(s.snapshot(ms, CANCEL, ms / 1000))
        elif action == "cancel_response":
            last = s.obs[-1]
            status = str(data.get("status") or "")
            if last.via == CANCEL and last.ms == ms and status in TERMINAL_STATUSES:
                last.status = status
        elif action == "status":
            self._read_status(s, ms, data)

    def _read_place(
        self, cid: str, ms: int, line: int, data: Mapping[str, Any], current: dict[str, str]
    ) -> None:
        try:
            req = OrderRequest.model_validate(data.get("request") or {})
        except ValidationError:
            self._note("bad_line", cid, f"orders line {line}: place without a valid request; ignored")
            return
        if cid in self.scripts:
            self._note("duplicate_place", cid, f"orders line {line}: second place of {cid}; ignored")
            return
        s = _Script(client_id=cid, req=req, placed_ms=ms, why=str(data.get("why") or ""), obs=[])
        s.created_ts = ms / 1000
        s.obs.append(s.snapshot(ms, PLACE, ms / 1000, status="NEW"))
        self.scripts[cid] = s
        self.placement_order.append(cid)
        prev = current.get(req.decision_id)
        if prev is not None and s.why in _REPLACE_WHY:
            self.scripts[prev].ended_ms.add(ms)  # re-placed: the previous placement ended in this tick
        current[req.decision_id] = cid

    def _read_fill(self, s: _Script, ms: int, data: Mapping[str, Any]) -> None:
        try:
            fill = Fill.model_validate({k: v for k, v in data.items() if k in _FILL_FIELDS})
        except ValidationError:
            self._note("bad_line", s.client_id, "fill line without a valid fill; ignored")
            return
        new_filled = None
        prefix = f"{s.client_id}@"
        if fill.trade_id and fill.trade_id.startswith(prefix):
            new_filled = _dec(fill.trade_id[len(prefix) :])
        if new_filled is None or new_filled <= s.filled:
            new_filled = s.filled + fill.qty
        s.avg = reproducing_avg(s.avg, s.filled, new_filled, fill.price)
        s.filled = new_filled
        s.fee = s.fee + fill.fee
        s.fee_asset = fill.fee_asset or s.fee_asset
        last = s.obs[-1]
        if last.ms == ms and last.via in _MERGE_VIAS and not last.fills:
            # in the answer of the call made in this tick (placement, cancel or poll)
            last.filled, last.avg, last.fee, last.fee_asset = s.filled, s.avg, s.fee, s.fee_asset
            last.updated_ts = fill.ts
            last.fills.append(fill)
        else:
            s.obs.append(s.snapshot(ms, POLL, fill.ts, fills=[fill]))

    def _book(self, fill: Fill) -> None:
        """A fill booked outside ``orders.jsonl`` (the Reconciler): visible from its own time on."""
        s = self.scripts.get(fill.order_client_id)
        if s is None:
            self._note(
                "unplaced_fill", fill.order_client_id, f"booked fill {fill.trade_id} without a recorded place"
            )
            return
        delta = bool(fill.trade_id and ":reconcile:" in fill.trade_id)  # from get_order, not my-trades
        new_filled = s.filled + fill.qty
        if delta and s.filled > 0 and s.avg is not None:
            s.avg = reproducing_avg(s.avg, s.filled, new_filled, fill.price)
        elif delta:
            s.avg = fill.price  # Reconciler.delta_fill takes the average itself on a first fill
        else:  # the Reconciler's own update of the order (``Reconciler._apply_fill``)
            s.avg = ((s.avg or D0) * s.filled + fill.price * fill.qty) / new_filled
        s.filled = new_filled
        s.fee = s.fee + fill.fee
        s.fee_asset = fill.fee_asset or s.fee_asset
        s.obs.append(s.snapshot(tick_ms(fill.ts), BOOKED, fill.ts, fills=[fill], listed=not delta))

    def _read_status(self, s: _Script, ms: int, data: Mapping[str, Any]) -> None:
        last = s.obs[-1]
        if data.get("done"):
            s.ended_ms.add(ms)
            reason = str(data.get("done_reason") or "")
            status = reason[len("EXCHANGE_") :] if reason.startswith("EXCHANGE_") else ""
            if status in TERMINAL_STATUSES:
                if last.ms == ms and last.via in (POLL, RECONCILE) and last.status not in TERMINAL_STATUSES:
                    last.status = status
                elif not (last.ms == ms and last.status in TERMINAL_STATUSES):
                    s.obs.append(s.snapshot(ms, POLL, ms / 1000, status=status))
        elif data.get("found") is False and "misses" in data:
            s.not_found = True
            # every poll after the last observation answered "unknown" (the misses before this line)
            s.obs.append(s.snapshot(last.ms + 1, POLL, ms / 1000, exists=False))
        elif data.get("source") == "reconcile":
            status = str(data.get("status") or "CANCELED")
            gone = "not found" in str(data.get("why") or "")
            # after a Reconciler delta fill the answer carried that fill's time (``delta_fill``)
            upd = last.updated_ts if (last.via == BOOKED and not last.listed) else ms / 1000
            s.obs.append(s.snapshot(ms, RECONCILE, upd, status=status, exists=not gone))
            s.ended_ms.add(ms)
        elif data.get("baseline_kept"):
            lag = (_dec(data.get("reported_filled")) or D0, _dec(data.get("reported_fee")) or D0)
            if last.ms == ms and last.via == CANCEL and last.lag is None and not last.fills:
                last.lag = lag  # the cancel's reply lagged
            else:
                s.obs.append(s.snapshot(ms, POLL, ms / 1000, lag=lag))
        elif data.get("adopted"):
            self._note("adopted", s.client_id, "recorded adoption (reconcile/restart) is not replayed")

    def _resolve(self, s: _Script) -> None:
        """Insert the 'seen ended' observations and give every observation its answer status."""
        qty = s.req.qty
        if s.place_error is not None:
            # rejected: never existed; ambiguous: existed unless the polls never found it
            s.obs[0].exists = bool(s.place_error["ambiguous"]) and not s.not_found
        for ms in sorted(s.ended_ms):
            at = [o for o in s.obs if o.ms == ms]
            if any(o.via == CANCEL or o.status in TERMINAL_STATUSES for o in at):
                continue  # the cancel's answer (or a stated status) already ends it in this tick
            k = max((i for i, o in enumerate(s.obs) if o.ms <= ms), default=0)
            prev = s.obs[k]
            if (qty > 0 and prev.filled >= qty) or not prev.exists or prev.ended:
                continue
            if prev.via == CANCEL or (prev.ms == ms and prev.via in (PLACE, POLL)):
                # a cancel confirmed earlier (re-placed once a book came back), or the answer of
                # this tick's call showed the order ended
                prev.ended = True
                continue
            ended = s.snapshot(ms, ENDED, ms / 1000, ended=True)
            ended.filled, ended.avg, ended.fee, ended.fee_asset = (
                prev.filled,
                prev.avg,
                prev.fee,
                prev.fee_asset,
            )
            ended.updated_ts = prev.updated_ts
            s.obs.insert(k + 1, ended)
        terminal: str | None = None  # an order that ended stays ended
        for o in s.obs:
            full = qty > 0 and o.filled >= qty
            if terminal is not None:
                o.final = "FILLED" if full else terminal
                continue
            ends: str | None = None
            if full:
                ends = "FILLED"
            elif o.status in TERMINAL_STATUSES:
                ends = o.status
            elif o.ended or (o.via == CANCEL and o.ms in s.ended_ms):
                ends = "CANCELED"
            if ends is not None:
                o.final = terminal = ends
            elif o.filled > 0:
                o.final = "PARTIALLY_FILLED"
            elif o.via == PLACE and o.status:
                o.final = o.status
            else:
                o.final = "NEW"

    # ------------------------------------------------------------ bookkeeping

    def _now(self) -> float | None:
        return None if self._clock is None else self._clock.now()

    def _now_ms(self) -> int | None:
        now = self._now()
        return None if now is None else tick_ms(now)

    def _call(
        self, method: str, client_id: str | None = None, symbol: str | None = None, answer: str = "recorded"
    ) -> None:
        self.calls.append(ReplayCall(method, self._now(), client_id, symbol, answer))

    def _note(self, kind: str, client_id: str | None, detail: str) -> None:
        self.notes.append({"kind": kind, "client_id": client_id, "ts": self._now(), "detail": detail})

    def _diverge(
        self, method: str, kind: str, client_id: str, message: str, details: dict[str, Any]
    ) -> ReplayDivergence:
        entry = {
            "kind": kind,
            "method": method,
            "client_id": client_id,
            "ts": self._now(),
            "message": message,
        }
        entry.update(details)
        self.divergences.append(entry)
        self._call(method, client_id, details.get("symbol"), answer="divergence")
        return ReplayDivergence(kind, client_id, message, details)

    def _recorded_for(self, decision_id: str) -> list[dict[str, Any]]:
        out = []
        for cid in self.placement_order:
            req = self.scripts[cid].req
            if req.decision_id == decision_id:
                out.append(
                    {
                        "client_id": cid,
                        **{k: _jsonable(getattr(req, k)) for k in ("side", "type", "qty", "price")},
                    }
                )
        return out

    def _script(self, method: str, client_id: str) -> _Script:
        s = self.scripts.get(client_id)
        if s is None:
            raise self._diverge(
                method,
                "unknown_client_id",
                client_id,
                f"{client_id} was never placed by the recorded run (no recorded answer)",
                {"symbol": None},
            )
        if not s.placed:
            raise self._diverge(
                method,
                "not_placed",
                client_id,
                f"{client_id} is recorded but this replay has not placed it",
                {"symbol": s.req.symbol, "decision_id": s.req.decision_id},
            )
        return s

    # ------------------------------------------------------------ answers

    def _answer(self, s: _Script, o: _Obs, fresh: bool = True) -> Order | None:
        """The order as observation ``o`` shows it. A lagging answer lags only once (``fresh``)."""
        if not o.exists:
            return None
        filled, fee, status = o.filled, o.fee, o.final
        if fresh and o.lag is not None:
            filled, fee = o.lag
            if status == "FILLED" and filled < s.req.qty:
                status = "PARTIALLY_FILLED" if filled > 0 else "NEW"
        return Order(
            req=s.req,
            exchange_id=s.exchange_id,
            status=status,  # type: ignore[arg-type]  # one of OrderStatus by construction
            filled_qty=filled,
            avg_price=o.avg,
            fee_paid=fee,
            fee_asset=o.fee_asset,
            created_ts=s.created_ts,
            updated_ts=max(o.updated_ts, s.created_ts),
            raw={"exchange": "replay", "via": o.via},
        )

    def _current(self, s: _Script, fresh: bool) -> Order | None:
        if s.override is not None:
            return s.override.model_copy(deep=True)
        return self._answer(s, s.obs[max(s.cursor - 1, 0)], fresh=fresh)

    def _consumable(self, o: _Obs, now_ms: int | None) -> bool:
        if o.via not in _GET_VIAS:
            return False
        if now_ms is None:
            return True
        if o.ms > now_ms:
            return False
        if o.via == BOOKED and o.listed:
            # booked from my-trades by the recorded Reconciler: get_order shows it only after the
            # replay's Reconciler has seen it there too
            return self._trades_ms is not None and o.ms <= self._trades_ms
        # a Reconciler observation of this very tick belongs to the Reconciler's pass
        return not (o.via == RECONCILE and o.ms == now_ms and self._reconcile_ms != now_ms)

    def _advance_polls(self, s: _Script) -> bool:
        """Consume what a ``get_order`` now sees. Returns True when something new was consumed."""
        now_ms = self._now_ms()
        moved = False
        while s.cursor < len(s.obs) and self._consumable(s.obs[s.cursor], now_ms):
            o = s.obs[s.cursor]
            s.cursor += 1
            moved = True
            if now_ms is None and o.via != BOOKED:
                break  # without a clock: one observation per call
        return moved

    def _peek_index(self, s: _Script, now_ms: int | None) -> int:
        """Index of the state the recorded exchange held at ``now`` (consumed or not)."""
        idx = max(s.cursor - 1, 0)
        if now_ms is None:
            return idx
        k = s.cursor
        while k < len(s.obs):
            o = s.obs[k]
            if o.ms < now_ms or (o.ms == now_ms and o.via not in (CANCEL, PLACE)):
                idx, k = k, k + 1
            else:
                break
        return idx

    async def get_markets(self) -> dict[str, MarketInfo]:
        self._call("get_markets")
        return dict(self._markets)

    async def place_order(self, req: OrderRequest) -> Order:
        cid = req.client_id
        s = self.scripts.get(cid)
        req_info = {k: _jsonable(getattr(req, k)) for k in _REQUEST_FIELDS}
        if s is None:
            recorded = self._recorded_for(req.decision_id)
            what = f"; the recorded run placed {len(recorded)} order(s) for this decision" if recorded else ""
            raise self._diverge(
                "place_order",
                "unknown_client_id",
                cid,
                f"{cid} ({req.side} {req.qty} {req.symbol} {req.type}"
                f"{' @ ' + str(req.price) if req.price is not None else ''}, decision {req.decision_id}) "
                f"was never placed by the recorded run{what}",
                {**req_info, "recorded_for_decision": recorded},
            )
        if s.placed:
            raise self._diverge(
                "place_order",
                "duplicate_placement",
                cid,
                f"{cid} was already placed in this replay",
                req_info,
            )
        diff = {
            k: {"recorded": _jsonable(getattr(s.req, k)), "replayed": _jsonable(getattr(req, k))}
            for k in _REQUEST_FIELDS
            if getattr(s.req, k) != getattr(req, k)
        }
        if diff:  # same client id, other request: answer from the record, report the difference
            self.divergences.append(
                {
                    "kind": "request_mismatch",
                    "method": "place_order",
                    "client_id": cid,
                    "ts": self._now(),
                    "message": f"{cid}: request differs from the recorded one in {', '.join(sorted(diff))}",
                    "symbol": req.symbol,
                    "decision_id": req.decision_id,
                    "fields": diff,
                }
            )
        s.placed = True
        s.cursor = 1
        now = self._now()
        if now is not None:  # the recorded exchange stamped the order in the tick it was placed
            s.created_ts = now
            s.obs[0].updated_ts = now
        if s.place_error is not None:
            err = s.place_error
            self._call("place_order", cid, req.symbol, answer="recorded_error")
            status = err.get("status")
            status = int(status) if isinstance(status, int | float) else None
            code = None if err.get("code") is None else str(err["code"])
            if err["ambiguous"]:
                raise WallexAPIError(status, code, err["message"])
            raise OrderRejected(status, code, err["message"])
        self._call("place_order", cid, req.symbol)
        answer = self._answer(s, s.obs[0])
        if answer is None:  # pragma: no cover - a placement without an error always exists
            raise self._diverge(
                "place_order", "unknown_client_id", cid, f"{cid}: no recorded answer", req_info
            )
        return answer.model_copy(update={"req": req})

    async def get_order(self, client_id: str) -> Order | None:
        s = self._script("get_order", client_id)
        if s.override is not None:
            self._call("get_order", client_id, s.req.symbol, answer="repeat")
            return self._current(s, fresh=False)
        moved = self._advance_polls(s)
        self._call("get_order", client_id, s.req.symbol, answer="recorded" if moved else "repeat")
        return self._current(s, fresh=moved)

    async def cancel_order(self, client_id: str) -> Order | None:
        s = self._script("cancel_order", client_id)
        if s.override is not None:
            self._call("cancel_order", client_id, s.req.symbol, answer="repeat")
            return self._current(s, fresh=False)
        now_ms = self._now_ms()
        target = None
        for k in range(s.cursor, len(s.obs)):
            o = s.obs[k]
            if now_ms is not None and o.ms > now_ms:
                break
            if o.via == CANCEL:
                target = k
                break
        if target is not None:
            s.cursor = target + 1
            self._call("cancel_order", client_id, s.req.symbol)
            return self._current(s, fresh=True)
        # a cancel the recorded run did not log (e.g. an untracked server stop): cancel what is there
        later = next((o for o in s.obs[s.cursor :] if o.via == CANCEL), None)
        self._advance_polls(s)
        state = self._current(s, fresh=False)
        self._call("cancel_order", client_id, s.req.symbol, answer="unrecorded")
        if state is None or state.is_terminal:
            return state
        if later is not None:  # the recorded run canceled it, but in a later tick
            self.divergences.append(
                {
                    "kind": "early_cancel",
                    "method": "cancel_order",
                    "client_id": client_id,
                    "ts": self._now(),
                    "message": f"{client_id} canceled before its recorded cancel at {iso(later.ms / 1000)}",
                    "symbol": s.req.symbol,
                    "decision_id": s.req.decision_id,
                }
            )
        s.override = state.model_copy(
            update={"status": "CANCELED", "updated_ts": self._now() or state.updated_ts}
        )
        self._note(
            "unrecorded_cancel", client_id, f"cancel of {client_id} not in the record: answered CANCELED"
        )
        return self._current(s, fresh=False)

    async def get_open_orders(self, symbol: str | None = None) -> list[Order]:
        now_ms = self._now_ms()
        self._reconcile_ms = now_ms  # the Reconciler's pass starts with this call
        self._call("get_open_orders", symbol=symbol)
        out: list[Order] = []
        for cid in self.placement_order:
            s = self.scripts[cid]
            if not s.placed or (symbol is not None and s.req.symbol != symbol):
                continue
            if s.override is not None:
                state: Order | None = s.override
            else:
                state = self._answer(s, s.obs[self._peek_index(s, now_ms)], fresh=False)
            if state is not None and not state.is_terminal:
                out.append(state.model_copy(deep=True))
        return out

    async def get_my_trades(self, symbol: str | None = None, since_ts: float | None = None) -> list[Fill]:
        now_ms = self._now_ms()
        self._reconcile_ms = now_ms
        if now_ms is not None:
            self._trades_ms = now_ms if self._trades_ms is None else max(self._trades_ms, now_ms)
        self._call("get_my_trades", symbol=symbol)
        out: list[Fill] = []
        for cid in self.placement_order:
            s = self.scripts[cid]
            if not s.placed or (symbol is not None and s.req.symbol != symbol):
                continue
            for o in s.obs[: self._peek_index(s, now_ms) + 1]:
                if o.listed:
                    out.extend(f for f in o.fills if since_ts is None or f.ts >= since_ts)
        return sorted(out, key=lambda f: (f.ts, f.trade_id or ""))

    async def get_balances(self) -> dict[str, Balance]:
        now_ms = self._now_ms()
        if now_ms is None:
            if self._snapshots:
                i = min(self._snap_cursor, len(self._snapshots) - 1)
                self._snap_cursor += 1
                self._call("get_balances", answer="recorded" if i == self._snap_cursor - 1 else "repeat")
                return dict(self._snapshots[i][1])
            return self._unrecorded_balances(now_ms)
        last = self.calls[-1] if self.calls else None
        reconciler = (
            last is not None
            and last.method == "get_my_trades"
            and last.ts is not None
            and tick_ms(last.ts) == now_ms
        )
        rows = self._snap_by_ms.get(now_ms, [])
        used = self._snap_used.setdefault(now_ms, set())
        unused = [i for i in range(len(rows)) if i not in used]
        current = self._fallback() if self._fallback is not None else None
        if unused and reconciler:
            # the Reconciler always wrote its row: the tick's last one once refreshes used theirs,
            # its first while none did (the deferred startup reconcile runs before them)
            return self._serve_row(rows, used, unused[-1] if used else unused[0])
        if unused and current is not None and not current:
            # the engine has no balances yet: the startup seed, which never writes a row; the
            # exchange held what the startup reconcile wrote right after it (left for that read)
            self._call("get_balances", answer="recorded")
            return dict(rows[unused[0]])
        if unused and (current is None or dict(rows[unused[0]]) != dict(current)):
            return self._serve_row(rows, used, unused[0])  # a refresh whose answer was new
        return self._unrecorded_balances(now_ms)

    def _serve_row(self, rows: list[dict[str, Balance]], used: set[int], pick: int) -> dict[str, Balance]:
        used.add(pick)
        self._call("get_balances", answer="recorded")
        return dict(rows[pick])

    def _unrecorded_balances(self, now_ms: int | None) -> dict[str, Balance]:
        current = self._fallback() if self._fallback is not None else None
        if current:
            self._call("get_balances", answer="fallback")
            return {a: current[a] for a in sorted(current)}
        self._call("get_balances", answer="derived")
        return self._derived_balances(now_ms)

    async def close(self) -> None:
        return None

    # ------------------------------------------------------------ derived balances

    def _derived_balances(self, now_ms: int | None) -> dict[str, Balance]:
        """Latest earlier snapshot (or the initial balances) plus the recorded fills since then."""
        base_ms: int | None = None
        free: dict[str, Decimal] = {a: b.free for a, b in self._initial.items()}
        locked: dict[str, Decimal] = {a: b.locked for a, b in self._initial.items()}
        if now_ms is not None:
            for ms, snap in self._snapshots:
                if ms > now_ms:
                    break
                base_ms = ms
                free = {a: b.free for a, b in snap.items()}
                locked = {a: b.locked for a, b in snap.items()}
        for cid in self.placement_order:
            s = self.scripts[cid]
            if not s.placed:
                continue
            upto = self._peek_index(s, now_ms) + 1
            for o in s.obs[:upto]:
                for f in o.fills:
                    if base_ms is not None and tick_ms(f.ts) <= base_ms:
                        continue  # already in the snapshot
                    self._apply_fill(free, f)
        assets = sorted(set(free) | set(locked))
        return {a: Balance(asset=a, free=free.get(a, D0), locked=locked.get(a, D0)) for a in assets}

    def _apply_fill(self, free: dict[str, Decimal], f: Fill) -> None:
        base, quote = _split_symbol(f.symbol, self._markets)
        sign = 1 if f.side == "BUY" else -1
        free[base] = free.get(base, D0) + sign * f.qty
        if quote:
            free[quote] = free.get(quote, D0) - sign * f.qty * f.price
        if f.fee and f.fee_asset:
            free[f.fee_asset] = free.get(f.fee_asset, D0) - f.fee

    # ------------------------------------------------------------ introspection

    def unplaced(self) -> list[str]:
        """Recorded client ids this replay never placed (placement order)."""
        return [cid for cid in self.placement_order if not self.scripts[cid].placed]

    def pending(self) -> dict[str, int]:
        """Placed client ids with recorded observations not consumed yet (client id -> count)."""
        out: dict[str, int] = {}
        for cid in self.placement_order:
            s = self.scripts[cid]
            if s.placed and s.override is None and s.cursor < len(s.obs):
                out[cid] = len(s.obs) - s.cursor
        return out

    def all_divergences(self) -> list[dict[str, Any]]:
        """``divergences`` plus every recorded placement this replay never made (``not_replayed``)."""
        out = list(self.divergences)
        for cid in self.unplaced():
            req = self.scripts[cid].req
            out.append(
                {
                    "kind": "not_replayed",
                    "method": "place_order",
                    "client_id": cid,
                    "ts": None,
                    "message": f"recorded {cid} ({req.side} {req.qty} {req.symbol}) was not replayed",
                    "symbol": req.symbol,
                    "decision_id": req.decision_id,
                }
            )
        return out

    def summary(self) -> dict[str, Any]:
        """Counts for the replay report."""
        answers: dict[str, int] = {}
        for c in self.calls:
            answers[c.answer] = answers.get(c.answer, 0) + 1
        return {
            "recorded_orders": len(self.placement_order),
            "placed": sum(1 for cid in self.placement_order if self.scripts[cid].placed),
            "unplaced": len(self.unplaced()),
            "calls": len(self.calls),
            "answers": dict(sorted(answers.items())),
            "divergences": len(self.divergences),
            "notes": len(self.notes),
        }

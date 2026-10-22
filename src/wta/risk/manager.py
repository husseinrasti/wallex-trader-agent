"""RiskManager — the deterministic guardian between strategy intents and the exchange.

Implements PRD-02 §3 (pre-trade checks) and §6 (kill / pause decisions). Nothing here can be
overridden by the AI layer: strategies propose ``Intent``s, ``RiskManager.evaluate`` decides.

How ``evaluate(intent, ctx)`` works
-----------------------------------
* The checks in the table below run **in this order**, and every one appends a
  ``RiskCheck(code, passed, detail)`` — also when it passed or does not apply
  (``"n/a (<why>)"``) — so every decision record carries the full checklist.
* ``approved`` = no failed check. ``veto_reason`` = ``"<first failed code>: <detail>"``.
* *Hard* checks veto. *Adjust* checks shrink the quantity instead of vetoing. The final
  ``adjusted_qty`` is rounded down to the market step; if it ends below the minimum order, a failed
  ``MIN_NOTIONAL`` check (``"after adjustments: ..."``) is appended at the end.
* Quantity = ``intent.qty``, else ``intent.notional / ref_price``. ``ref_price`` is the limit price,
  else the price we would cross at (best ask for a BUY, best bid for a SELL). MARKET / STOP_MARKET
  orders always use the opposite best: the exchange ignores a price on them, so a stale ``price``
  must never make a MARKET order look small. TMN values use the USDTTMN bid (``quote_to_tmn``),
  the same conservative side the equity calculation uses.
* Purposes: *entry-like* = ``entry``, ``rebalance`` (and any unknown purpose such as ``probe`` —
  the safe side); *risk-reducing* = ``exit``, ``stop``, ``leg2``, ``flatten``. Every risk-reducing
  order in this codebase is a SELL (positions are long only; flatten sells to TMN), so a **BUY is
  always treated as entry-like**, whatever its purpose label says. Exposure limits apply to
  entry-like intents only: the guardian must never stop the bot from getting smaller.
* *Taker* = flagged ``taker``, a MARKET order, or a LIMIT whose price already crosses the book
  (BUY at/above the best ask, SELL at/below the best bid): such an order takes liquidity and pays
  the taker fee whatever the flag says.
* Missing data fails *closed* for entries (no equity, no book, no USDTTMN price -> veto) and
  *open* for risk-reducing orders — except ``STALE_DATA``, which also guards exits and leg 2.
* Exposure checks count our **live orders** too: open coin BUYs count as coins already held
  (``COIN_EXPOSURE``), open USDT-moving orders as USDT already moved (``USDT_BOUNDS``), and the
  rate limits reserve the re-placements our open entries may still make (``RATE_*``).
* Pure: reads only ``intent``, ``ctx`` and the config; ``ctx.now`` is the only clock. No I/O.

=====================  ===========  ================================================================
code                   kind         rule
=====================  ===========  ================================================================
KILLED                 hard         kill-switch on -> only flatten / exit / stop SELLs pass
PAUSED                 hard         engine paused -> no entry / rebalance / BUY (exits, stops, leg 2 ok)
DEADLINE               hard         phase 1 (T-2h): no entry-like; 2 (T-30m): only flatten SELLs; 3: nothing
BANNED_MARKET          hard         symbol on the organizer ban list or not in /v1/markets
MARKET_ORDER_DISABLED  hard         MARKET / STOP_MARKET where isMarketTypeEnable=false
MIN_NOTIONAL           hard         rounded qty >= minQty and qty x price >= minNotional
MAX_ORDER_PCT          adjust       entries: notional <= 25 % of equity
BEST_LEVEL_QTY         adjust       taker orders: qty <= 80 % of the opposite best level (a closing
                                    order is never cut below the smallest sellable quantity)
COIN_EXPOSURE          adjust/hard  coin buys: coins held + our open coin BUYs + this <= 40 % of equity
USDT_BOUNDS            adjust/hard  entries moving USDT (lock-in: the whole two-leg cycle): post-trade
                                    USDT share, incl. our open USDT-moving orders, inside S2 bounds
MAX_POSITIONS          hard         entries: < 3 positions (incl. pending entries); S1 <= 2, S3 <= 1
MAX_OPEN_ORDERS        hard         entries: < 8 open orders
RATE_HOUR / RATE_DAY   hard         entries: placements + re-placements reserved by open entries +
                                    (1 + limit_max_reprices) for this one <= 10 per rolling hour / 60
                                    per UTC day
DAILY_LOSS             hard         entries: daily PnL > -2.5 %
DRAWDOWN_KILL          hard         entries: drawdown < 5 % (the kill itself: update_rolling_state)
FEE_BUDGET             hard         entries: fees paid + expected fee <= 2 % of initial equity
EDGE_VS_COST           hard         purpose entry: expected edge >= cost + margin_min
STALE_DATA             hard         every book the decision used is <= stale_data_s old
PRICE_SANITY           hard         limit within +-3 % of mid (closing SELLs: >= bid x (1 - 3 %));
                                    cross-book deviation < 5 % (entries)
SELF_CROSS             hard         never cross our own resting orders (wash-trade prevention); a
                                    closing SELL ignores its own position's entry BUYs, which the
                                    OrderManager cancels right before placing it
BALANCE                adjust/hard  BUY: quote free (less buffer) covers qty x price x (1+fee);
                                    SELL: qty <= free base
DUPLICATE              hard         same strategy/symbol/side/purpose already open or just approved
                                    (a position's own protective stop never counts for its exits)
=====================  ===========  ================================================================

Rolling state (``update_rolling_state``)
----------------------------------------
Called by the engine every fast tick; returns ``RiskEvent``s that the engine applies to
``EngineFlags`` (copying ``reason`` into ``pause_reason``) and writes to ``events.jsonl``:

* **kill** when drawdown from peak >= ``max_drawdown_kill_pct`` — emitted once (not while
  ``flags.killed``). A kill is never resumed here: manual ``reset-kill`` only.
* **pause** ``daily_loss`` when daily PnL <= ``-daily_loss_limit_pct``, until the next UTC
  midnight; **resume** at the new UTC day.
* **pause** ``anomaly`` when the API error burst reaches ``consecutive_api_errors_pause``, the
  USDTTMN book is stale, or the engine reports anomalies (e.g. cross-book disagreement > 5 %);
  **resume** only after the condition has been clear for ``anomaly_clear_s`` (an instance timer).
* The manager only resumes pauses it started (``pause_reason`` prefixed ``daily_loss`` /
  ``anomaly``); a pause with any other reason (manual, exception storm, ...) is left alone.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any, Literal

from wta.config import Config
from wta.execution.positions import FIAT, USDT, USDTTMN, Valuation, asset_price_tmn, quote_to_tmn
from wta.models import D0, BookSnapshot, Intent, MarketInfo, Order, RiskCheck, RiskDecision
from wta.risk.context import RiskContext
from wta.utils.decimalutil import D, fmt, pct, quantize_up, round_qty
from wta.utils.timeutil import day_start, iso

ENTRY_LIKE = frozenset({"entry", "rebalance"})
RISK_REDUCING = frozenset({"exit", "stop", "leg2", "flatten"})
KILL_ALLOWED = frozenset({"flatten", "exit", "stop"})
MARKET_TYPES = frozenset({"MARKET", "STOP_MARKET"})
STOP_TYPES = frozenset({"STOP_LIMIT", "STOP_MARKET"})
QUOTE_ASSETS = frozenset({FIAT, USDT})

CHECK_CODES: tuple[str, ...] = (
    "KILLED",
    "PAUSED",
    "DEADLINE",
    "BANNED_MARKET",
    "MARKET_ORDER_DISABLED",
    "MIN_NOTIONAL",
    "MAX_ORDER_PCT",
    "BEST_LEVEL_QTY",
    "COIN_EXPOSURE",
    "USDT_BOUNDS",
    "MAX_POSITIONS",
    "MAX_OPEN_ORDERS",
    "RATE_HOUR",
    "RATE_DAY",
    "DAILY_LOSS",
    "DRAWDOWN_KILL",
    "FEE_BUDGET",
    "EDGE_VS_COST",
    "STALE_DATA",
    "PRICE_SANITY",
    "SELF_CROSS",
    "BALANCE",
    "DUPLICATE",
)

# pause reasons this module owns (the engine stores RiskEvent.reason in flags.pause_reason)
DAILY_LOSS_REASON = "daily_loss"
ANOMALY_REASON = "anomaly"
DAY_S = 86400.0


# ---------------------------------------------------------------- public helpers


@dataclass(frozen=True)
class RiskEvent:
    """A kill / pause / resume decision for the engine to apply and log."""

    kind: Literal["kill", "pause", "resume"]
    reason: str
    until_ts: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "reason": self.reason, "until_ts": self.until_ts}


def ref_price(intent: Intent, book: BookSnapshot | None) -> Decimal | None:
    """The price the intent trades at: its limit price, else the opposite best (what we would cross).

    MARKET / STOP_MARKET orders always use the opposite best: the exchange ignores their ``price``.
    """
    if intent.price is not None and intent.order_type not in MARKET_TYPES:
        return intent.price
    if book is None:
        return None
    return book.best_ask if intent.side == "BUY" else book.best_bid


def round_trip_cost(market: MarketInfo, entry_taker: bool) -> Decimal:
    """Fees of a same-book round trip: entry fee (taker or maker) + a taker exit. Spread excluded."""
    entry_fee = market.taker_fee if entry_taker else market.maker_fee
    return entry_fee + market.taker_fee


def min_notional(market: MarketInfo, cfg: Config) -> Decimal:
    """Exchange minNotional; the config fallback (TMN 50,000 / USDT 1) when the exchange sent 0."""
    if market.min_notional > 0:
        return market.min_notional
    return cfg.risk.min_notional_usdt if market.quote == USDT else cfg.risk.min_notional_tmn


def is_entry_like(purpose: str) -> bool:
    """True for exposure-increasing purposes (anything not explicitly risk-reducing)."""
    return purpose not in RISK_REDUCING


def increases_exposure(intent: Intent) -> bool:
    """Entry-like purpose, or any BUY: every risk-reducing order of ours is a SELL."""
    return is_entry_like(intent.purpose) or intent.side == "BUY"


def is_taker(intent: Intent, book: BookSnapshot | None = None) -> bool:
    """A taker order crosses the spread: flagged by the strategy, a MARKET order, or a LIMIT whose
    price already crosses ``book`` (BUY at/above the best ask, SELL at/below the best bid)."""
    if intent.taker or intent.order_type == "MARKET":
        return True
    if intent.order_type != "LIMIT" or intent.price is None or book is None:
        return False
    if intent.side == "BUY":
        return book.best_ask is not None and intent.price >= book.best_ask
    return book.best_bid is not None and intent.price <= book.best_bid


# ---------------------------------------------------------------- internals


def _ok(code: str, detail: str) -> RiskCheck:
    return RiskCheck(code=code, passed=True, detail=detail)


def _fail(code: str, detail: str) -> RiskCheck:
    return RiskCheck(code=code, passed=False, detail=detail)


def _na(code: str, why: str) -> RiskCheck:
    return RiskCheck(code=code, passed=True, detail=f"n/a ({why})")


def _dec(value: Any) -> Decimal | None:
    """Feature values arrive as strings/Decimals/numbers; None when absent or unparsable."""
    if value is None:
        return None
    try:
        return D(value)
    except (InvalidOperation, ValueError, TypeError):
        return None


def _amt(value: Decimal, asset: str) -> str:
    return f"{fmt(value)} {asset}"


def _symbols(value: Any) -> list[str]:
    """``features.books_used`` as a list of symbols (accepts a list/tuple/set or one string)."""
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, Iterable):
        return [str(v) for v in value]
    return []


def _order_price(order: Order) -> Decimal | None:
    """Price at which a resting order of ours can trade: its limit, else its stop price."""
    return order.req.price if order.req.price is not None else order.req.stop_price


@dataclass
class _Eval:
    """Working state of one ``evaluate`` call. Adjust checks shrink ``qty``."""

    intent: Intent
    ctx: RiskContext
    market: MarketInfo | None
    book: BookSnapshot | None
    ref: Decimal | None  # price in the symbol's quote currency
    qty: Decimal | None  # None when the intent cannot be sized
    sizing_error: str
    val: Valuation
    checks: list[RiskCheck] = field(default_factory=list)

    @property
    def entry_like(self) -> bool:
        """Exposure-increasing: an entry-like purpose or any BUY (see ``increases_exposure``)."""
        return increases_exposure(self.intent)

    @property
    def taker(self) -> bool:
        return is_taker(self.intent, self.book)

    @property
    def sized(self) -> bool:
        return self.market is not None and self.qty is not None and self.ref is not None

    @property
    def equity(self) -> Decimal | None:
        eq = self.val.equity_tmn
        return eq if eq > 0 else None

    def price_tmn(self) -> Decimal | None:
        """``ref`` converted to TMN (USDT quote at the USDTTMN bid)."""
        if self.market is None or self.ref is None:
            return None
        return quote_to_tmn(self.market.quote, self.ref, self.ctx.books)

    def shown(self, qty: Decimal | None) -> str:
        if qty is None:
            return "n/a"
        q = round_qty(self.market, qty) if self.market is not None else qty
        return format(q, "f")

    def adjust(self, code: str, new_qty: Decimal, why: str) -> RiskCheck:
        old, self.qty = self.qty, new_qty
        return _ok(code, f"adjusted qty {self.shown(old)} -> {self.shown(new_qty)}: {why}")


# ---------------------------------------------------------------- the manager


class RiskManager:
    """Pre-trade checks (``evaluate``) and kill/pause decisions (``update_rolling_state``)."""

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        # anomaly-pause resume timer: when the anomaly conditions were last seen clear
        self._anomaly_clear_since: float | None = None
        self._checks: tuple[Callable[[_Eval], RiskCheck], ...] = (
            self._check_killed,
            self._check_paused,
            self._check_deadline,
            self._check_banned,
            self._check_market_order,
            self._check_min_notional,
            self._check_max_order_pct,
            self._check_best_level,
            self._check_coin_exposure,
            self._check_usdt_bounds,
            self._check_max_positions,
            self._check_max_open_orders,
            self._check_rate_hour,
            self._check_rate_day,
            self._check_daily_loss,
            self._check_drawdown,
            self._check_fee_budget,
            self._check_edge_vs_cost,
            self._check_stale_data,
            self._check_price_sanity,
            self._check_self_cross,
            self._check_balance,
            self._check_duplicate,
        )

    # ------------------------------------------------------------ evaluate

    def evaluate(self, intent: Intent, ctx: RiskContext) -> RiskDecision:
        """Run every check in order; approve only when none failed. Pure (no side effects)."""
        ev = self._start(intent, ctx)
        for check in self._checks:
            ev.checks.append(check(ev))

        adjusted: Decimal | None = None
        if ev.sized:
            assert ev.market is not None and ev.qty is not None and ev.ref is not None
            adjusted = round_qty(ev.market, ev.qty)
            already_failed = any(c.code == "MIN_NOTIONAL" and not c.passed for c in ev.checks)
            problem = self._size_problem(ev.market, ev.qty, ev.ref)
            if problem and not already_failed:
                ev.checks.append(_fail("MIN_NOTIONAL", f"after adjustments: {problem}"))

        failed = [c for c in ev.checks if not c.passed]
        return RiskDecision(
            approved=not failed,
            checks=ev.checks,
            adjusted_qty=adjusted,
            veto_reason=f"{failed[0].code}: {failed[0].detail}" if failed else None,
        )

    def _start(self, intent: Intent, ctx: RiskContext) -> _Eval:
        market = ctx.markets.get(intent.symbol)
        book = ctx.books.get(intent.symbol)
        ref = ref_price(intent, book)
        qty: Decimal | None = None
        why = ""
        if ref is None or ref <= 0:
            why = "no reference price (no limit price and no opposite best level in the book)"
        elif intent.qty is not None:
            qty = intent.qty
        elif intent.notional is not None:
            qty = intent.notional / ref
        else:
            why = "the intent has neither qty nor notional"
        return _Eval(
            intent=intent,
            ctx=ctx,
            market=market,
            book=book,
            ref=ref if ref is not None and ref > 0 else None,
            qty=qty,
            sizing_error=why,
            val=ctx.portfolio.valuation(ctx.books),
        )

    def _size_problem(self, market: MarketInfo, qty: Decimal, ref: Decimal) -> str | None:
        """Why ``qty`` (rounded down to step) is not a valid order size; None when it is."""
        q = round_qty(market, qty)
        if q <= 0:
            return f"qty rounds down to 0 at step {market.step}"
        if q < market.min_qty:
            return f"qty {q} < min qty {market.min_qty}"
        mn = min_notional(market, self.cfg)
        if q * ref < mn:
            return f"notional {q} x {ref} = {_amt(q * ref, market.quote)} < min {_amt(mn, market.quote)}"
        return None

    # ------------------------------------------------------------ engine state checks

    def _check_killed(self, ev: _Eval) -> RiskCheck:
        code, flags, purpose = "KILLED", ev.ctx.flags, ev.intent.purpose
        if not flags.killed:
            return _ok(code, "kill-switch off")
        why = flags.kill_reason or "no reason recorded"
        if purpose in KILL_ALLOWED and ev.intent.side == "SELL":
            return _ok(code, f"kill-switch active ({why}); {purpose} SELL allowed (reduces exposure)")
        return _fail(
            code,
            f"kill-switch active ({why}): only flatten/exit/stop SELLs allowed, got {ev.intent.side} {purpose}",
        )

    def _check_paused(self, ev: _Eval) -> RiskCheck:
        code, flags, purpose = "PAUSED", ev.ctx.flags, ev.intent.purpose
        if not flags.paused:
            return _ok(code, "engine not paused")
        until = f" until {iso(flags.paused_until_ts)}" if flags.paused_until_ts is not None else ""
        why = f"{flags.pause_reason or 'no reason recorded'}{until}"
        if not ev.entry_like:
            return _ok(code, f"engine paused ({why}); {purpose} allowed (reduces exposure)")
        return _fail(code, f"engine paused ({why}): no new {ev.intent.side} {purpose} orders")

    def _check_deadline(self, ev: _Eval) -> RiskCheck:
        code, ctx, purpose = "DEADLINE", ev.ctx, ev.intent.purpose
        # the flatten scheduler may also force a phase through the engine flags; the stricter wins
        phase = max(ctx.deadline.phase(ctx.now), ctx.flags.flatten_phase)
        if phase >= 3:
            return _fail(code, "challenge is over (phase 3): no orders at all")
        if phase == 2:
            if purpose == "flatten" and ev.intent.side == "SELL":
                return _ok(code, "flatten_final reached (phase 2); flatten SELL allowed")
            return _fail(
                code,
                f"flatten_final reached (phase 2): only flatten SELLs allowed, got {ev.intent.side} {purpose}",
            )
        if phase == 1:
            if ev.entry_like:
                return _fail(
                    code,
                    f"flatten_start reached (phase 1): no exposure-increasing orders, got {ev.intent.side} {purpose}",
                )
            return _ok(code, f"flatten_start reached (phase 1); {purpose} allowed (reduces exposure)")
        left = ctx.deadline.seconds_to_end(ctx.now)
        return _ok(code, "before flatten_start" + (f" ({left:.0f}s to the end)" if left is not None else ""))

    # ------------------------------------------------------------ market checks

    def _check_banned(self, ev: _Eval) -> RiskCheck:
        code, sym = "BANNED_MARKET", ev.intent.symbol
        if self.cfg.markets.is_banned(sym):
            return _fail(code, f"{sym} is on the organizer's ban list")
        if ev.market is None:
            return _fail(code, f"{sym} is not a known market (/v1/markets)")
        return _ok(code, f"{sym} allowed")

    def _check_market_order(self, ev: _Eval) -> RiskCheck:
        code, otype = "MARKET_ORDER_DISABLED", ev.intent.order_type
        if otype not in MARKET_TYPES:
            return _na(code, f"{otype} order")
        if ev.market is None:
            return _na(code, "unknown market")
        if ev.market.market_orders_enabled:
            return _ok(code, f"MARKET orders enabled on {ev.market.symbol}")
        return _fail(
            code,
            f"MARKET orders disabled on {ev.market.symbol} (isMarketTypeEnable=false); "
            "retry as a LIMIT crossing the spread",
        )

    def _check_min_notional(self, ev: _Eval) -> RiskCheck:
        code = "MIN_NOTIONAL"
        if ev.market is None:
            return _na(code, "unknown market")
        if not ev.sized:
            return _fail(code, f"cannot size the order: {ev.sizing_error}")
        assert ev.qty is not None and ev.ref is not None
        problem = self._size_problem(ev.market, ev.qty, ev.ref)
        if problem:
            return _fail(code, problem)
        q, m = round_qty(ev.market, ev.qty), ev.market
        return _ok(
            code,
            f"qty {q} x {ev.ref} = {_amt(q * ev.ref, m.quote)} >= min {_amt(min_notional(m, self.cfg), m.quote)}",
        )

    # ------------------------------------------------------------ size limits (adjust)

    def _check_max_order_pct(self, ev: _Eval) -> RiskCheck:
        code, cap_pct = "MAX_ORDER_PCT", self.cfg.risk.max_order_pct_equity
        if not ev.entry_like:
            return _na(code, ev.intent.purpose)
        if not ev.sized:
            return _na(code, "order cannot be sized")
        assert ev.qty is not None
        equity, px_tmn = ev.equity, ev.price_tmn()
        if equity is None:
            return _fail(code, "equity unknown: cannot size an entry")
        if not px_tmn:
            return _fail(code, f"cannot value {ev.intent.symbol} in TMN (no USDTTMN bid)")
        cap = cap_pct * equity
        notional = ev.qty * px_tmn
        if notional <= cap:
            return _ok(
                code, f"notional {_amt(notional, FIAT)} <= {pct(cap_pct, 0)} of equity = {_amt(cap, FIAT)}"
            )
        return ev.adjust(
            code,
            cap / px_tmn,
            f"notional {_amt(notional, FIAT)} > {pct(cap_pct, 0)} of equity = {_amt(cap, FIAT)}",
        )

    def _check_best_level(self, ev: _Eval) -> RiskCheck:
        code, frac = "BEST_LEVEL_QTY", self.cfg.risk.max_order_pct_of_best_level_qty
        if not ev.taker:
            return _na(code, "maker order")
        if not ev.sized:
            return _na(code, "order cannot be sized")
        assert ev.qty is not None
        buy = ev.intent.side == "BUY"
        level = None if ev.book is None else (ev.book.best_ask_qty if buy else ev.book.best_bid_qty)
        side_name = "best ask" if buy else "best bid"
        if level is None or level <= 0:
            if ev.entry_like:
                return _fail(code, f"no {side_name} level in the book to size a taker order against")
            return _na(code, f"no {side_name} level; {ev.intent.purpose} not blocked")
        cap = frac * level
        why = f"taker qty capped at {pct(frac, 0)} of {side_name} qty {level}"
        floor = self._min_order_qty(ev.market, ev.ref) if not ev.entry_like else None
        if floor is not None and cap < floor:
            # a closing order is never cut below the smallest sellable quantity: a thin top level
            # must not block the bot from getting smaller (the OrderManager re-crosses the rest)
            cap = floor
            why = f"{why}, raised to the minimum order {floor} so the {ev.intent.purpose} is not blocked"
        if ev.qty <= cap:
            return _ok(code, f"qty {ev.shown(ev.qty)} <= {pct(frac, 0)} of {side_name} qty {level}")
        return ev.adjust(code, cap, why)

    def _min_order_qty(self, market: MarketInfo | None, ref: Decimal | None) -> Decimal | None:
        """Smallest step multiple that meets minQty and minNotional at ``ref``; None if unknown."""
        if market is None or ref is None or ref <= 0:
            return None
        need = max(market.min_qty, min_notional(market, self.cfg) / ref, market.step)
        return quantize_up(need, market.step_decimals)

    def _check_coin_exposure(self, ev: _Eval) -> RiskCheck:
        code, cap_pct = "COIN_EXPOSURE", self.cfg.risk.max_coin_exposure_pct
        m = ev.market
        if m is None or ev.intent.side != "BUY" or m.base in QUOTE_ASSETS:
            return _na(code, "not a coin purchase")
        if not ev.entry_like:
            return _na(code, ev.intent.purpose)
        if not ev.sized:
            return _na(code, "order cannot be sized")
        assert ev.qty is not None and ev.ref is not None
        equity, px_tmn = ev.equity, ev.price_tmn()
        if equity is None:
            return _fail(code, "equity unknown: cannot bound coin exposure")
        if not px_tmn:
            return _fail(code, f"cannot value {m.symbol} in TMN (no USDTTMN bid)")
        pending, unvalued = _open_coin_buys_tmn(ev.ctx)
        if unvalued:
            return _fail(code, f"cannot value our open coin BUY {unvalued[0]} in TMN")
        # our open coin BUYs count as coins already held: their TMN still sits in equity, their
        # coins arrive when they fill
        coins_now = ev.val.coins_tmn_value + pending
        unpriced = f" (unpriced assets: {', '.join(ev.val.missing_prices)})" if ev.val.missing_prices else ""
        held = f"held {_amt(ev.val.coins_tmn_value, FIAT)} + open BUYs {_amt(pending, FIAT)}"
        cap = cap_pct * equity
        post = coins_now + ev.qty * px_tmn
        if post <= cap:
            return _ok(
                code,
                f"post-trade coins {_amt(post, FIAT)} ({held} + this {_amt(ev.qty * px_tmn, FIAT)}) = "
                f"{pct(post / equity)} <= {pct(cap_pct, 0)} of equity" + unpriced,
            )
        room = max(cap - coins_now, D0)
        room_qty = room / px_tmn
        problem = self._size_problem(m, room_qty, ev.ref)
        if problem:
            return _fail(
                code,
                f"coins already {_amt(coins_now, FIAT)} ({held}) = {pct(coins_now / equity)} of equity; room "
                f"{_amt(room, FIAT)} under the {pct(cap_pct, 0)} cap is below the minimum order ({problem})"
                + unpriced,
            )
        return ev.adjust(
            code,
            room_qty,
            f"post-trade coins ({held} + this) would be {pct(post / equity)} > {pct(cap_pct, 0)} of equity"
            + unpriced,
        )

    def _check_usdt_bounds(self, ev: _Eval) -> RiskCheck:
        """Keep the USDT share inside the S2 band; outside it, only trades toward the band pass.

        The share counts our open USDT-moving orders as filled. A lock-in entry is bounded on its
        whole cycle (leg 1 + the leg-2 SELL it commits to), because leg 2 is risk-reducing and so
        exempt: an entry whose leg 2 would leave the band must be sized here, never blocked later.
        """
        code = "USDT_BOUNDS"
        m = ev.market
        if not ev.entry_like:
            return _na(code, ev.intent.purpose)
        if m is None:
            return _na(code, "unknown market")
        lockin = ev.intent.features.get("cost_model") == "lockin"
        leg2 = ev.ctx.markets.get(str(ev.intent.features.get("leg2_symbol"))) if lockin else None
        if USDT not in (m.base, m.quote) and (leg2 is None or USDT not in (leg2.base, leg2.quote)):
            return _na(code, "does not change the USDT holding")
        if not ev.sized:
            return _na(code, "order cannot be sized")
        assert ev.qty is not None and ev.ref is not None
        u_bid, equity = asset_price_tmn(USDT, ev.ctx.books), ev.equity
        if not u_bid:
            return _fail(code, "no USDTTMN bid to value USDT")
        if equity is None:
            return _fail(code, "equity unknown: cannot bound the USDT share")
        # change of the USDT holding's TMN value per unit of qty (equity treated as constant:
        # fees and the spread move the share by far less than the band width)
        per_unit = _usdt_per_unit(m, ev.intent.side, ev.ref, u_bid)
        cycle = ""
        if leg2 is not None and USDT in (leg2.base, leg2.quote):
            b2 = ev.ctx.books.get(leg2.symbol)
            leg2_bid = b2.best_bid if b2 is not None else None
            if not leg2_bid:
                return _fail(code, f"no {leg2.symbol} bid to value the USDT the lock-in leg 2 brings")
            per_unit += _usdt_per_unit(leg2, "SELL", leg2_bid, u_bid)
            cycle = f" (lock-in cycle incl. leg 2 SELL on {leg2.symbol})"
        lo, hi = self.cfg.strategies.s2_usdtalloc.bounds
        band = f"[{pct(lo, 0)}, {pct(hi, 0)}]"
        pending = _open_usdt_effect_tmn(ev.ctx, u_bid)
        usdt_now = ev.val.usdt_tmn_value + pending
        share_now = usdt_now / equity
        share_post = (usdt_now + ev.qty * per_unit) / equity
        incl = f" incl. our open orders ({_amt(pending, FIAT)})" if pending else ""
        moves = f"USDT share{incl} {pct(share_now)} -> {pct(share_post)}{cycle}"
        if (per_unit > 0 and share_now > hi) or (per_unit < 0 and share_now < lo):
            return _fail(code, f"{moves}: already outside {band} and this trade moves it further away")
        if (
            lo <= share_post <= hi
            or (share_now < lo and share_post <= hi)
            or (share_now > hi and share_post >= lo)
        ):
            toward = "" if lo <= share_now <= hi else " (moves toward the band)"
            return _ok(code, f"{moves} within {band}{toward}")
        room = (hi * equity - usdt_now) if per_unit > 0 else (usdt_now - lo * equity)
        room_qty = max(room, D0) / abs(per_unit)
        problem = self._size_problem(m, room_qty, ev.ref)
        if problem:
            return _fail(
                code, f"{moves} leaves {band}; room to the bound is below the minimum order ({problem})"
            )
        bound = pct(hi, 0) if per_unit > 0 else pct(lo, 0)
        return ev.adjust(code, room_qty, f"{moves} would cross {bound}; sized to stop at the bound")

    # ------------------------------------------------------------ portfolio limits (hard)

    def _check_max_positions(self, ev: _Eval) -> RiskCheck:
        code, it, port, r = "MAX_POSITIONS", ev.intent, ev.ctx.portfolio, self.cfg.risk
        if it.purpose != "entry":
            return _na(code, it.purpose)
        if it.linked_position_id and it.linked_position_id in port.positions:
            return _ok(code, f"adds to existing position {it.linked_position_id}")
        # entry orders still working count as positions: they become one when they fill
        entry_ids = {str(p.meta.get("entry_decision_id")) for p in port.positions.values()}
        pending = [
            o
            for _, o in sorted(port.open_orders.items())
            if not o.is_terminal and o.req.purpose == "entry" and o.req.decision_id not in entry_ids
        ]
        total = len(port.positions) + len(pending)
        if total >= r.max_concurrent_positions:
            return _fail(
                code,
                f"{total} positions (incl. {len(pending)} pending entries) >= max {r.max_concurrent_positions}",
            )
        cap = r.max_positions_per_strategy.get(it.strategy)
        mine = len(port.positions_by_strategy(it.strategy)) + sum(
            1 for o in pending if o.req.strategy == it.strategy
        )
        if cap is not None and mine >= cap:
            return _fail(code, f"{it.strategy} already has {mine} positions (incl. pending) >= its cap {cap}")
        per = f"; {it.strategy} {mine} < {cap}" if cap is not None else ""
        return _ok(code, f"{total} positions (incl. pending) < max {r.max_concurrent_positions}{per}")

    def _check_max_open_orders(self, ev: _Eval) -> RiskCheck:
        code, limit = "MAX_OPEN_ORDERS", self.cfg.risk.max_open_orders
        if not ev.entry_like:
            return _na(code, ev.intent.purpose)
        n = sum(1 for o in ev.ctx.portfolio.open_orders.values() if not o.is_terminal)
        if n >= limit:
            return _fail(code, f"{n} open orders >= max {limit}")
        return _ok(code, f"{n} open orders < max {limit}")

    def _check_rate_hour(self, ev: _Eval) -> RiskCheck:
        n = ev.ctx.portfolio.orders_last_hour(ev.ctx.now)
        return self._rate_check(ev, "RATE_HOUR", n, self.cfg.risk.max_orders_per_hour, "in the last 60 min")

    def _check_rate_day(self, ev: _Eval) -> RiskCheck:
        n = ev.ctx.portfolio.orders_today(ev.ctx.now)
        return self._rate_check(ev, "RATE_DAY", n, self.cfg.risk.max_orders_per_day, "today (UTC)")

    def _rate_check(self, ev: _Eval, code: str, n: int, limit: int, window: str) -> RiskCheck:
        """Worst case, not just ``n < limit``: the OrderManager re-places an unfilled entry up to
        ``limit_max_reprices`` times without asking again, so an approval reserves all of them, and
        our open entry-like orders keep the re-placements they may still make."""
        if not ev.entry_like:
            return _na(code, f"{ev.intent.purpose} is exempt from rate limits")
        max_reprices = self.cfg.execution.limit_max_reprices
        reserved = sum(
            max(max_reprices - o.reprices, 0)
            for o in ev.ctx.portfolio.open_orders.values()
            if not o.is_terminal and o.req.type not in STOP_TYPES and is_entry_like(o.req.purpose)
        )
        this = 1 + max_reprices
        total = n + reserved + this
        summary = (
            f"{n} placements {window} + {reserved} re-placements reserved by our open entries + "
            f"{this} for this one (1 + {max_reprices} re-prices) = {total}"
        )
        if total > limit:
            return _fail(code, f"{summary} > max {limit}")
        return _ok(code, f"{summary} <= max {limit}")

    def _check_daily_loss(self, ev: _Eval) -> RiskCheck:
        code, limit = "DAILY_LOSS", self.cfg.risk.daily_loss_limit_pct
        if not ev.entry_like:
            return _na(code, ev.intent.purpose)
        d = ev.ctx.portfolio.daily_pnl_pct
        if d <= -limit:
            return _fail(code, f"daily PnL {pct(d)} <= -{pct(limit)}: no new entries until the next UTC day")
        return _ok(code, f"daily PnL {pct(d)} > -{pct(limit)}")

    def _check_drawdown(self, ev: _Eval) -> RiskCheck:
        code, limit = "DRAWDOWN_KILL", self.cfg.risk.max_drawdown_kill_pct
        if not ev.entry_like:
            return _na(code, ev.intent.purpose)
        dd = ev.ctx.portfolio.drawdown_pct
        if dd >= limit:
            return _fail(code, f"drawdown {pct(dd)} >= {pct(limit)} from peak: kill-switch territory")
        return _ok(code, f"drawdown {pct(dd)} < {pct(limit)}")

    def _check_fee_budget(self, ev: _Eval) -> RiskCheck:
        code, r = "FEE_BUDGET", self.cfg.risk
        if not ev.entry_like:
            return _na(code, ev.intent.purpose)
        if not ev.sized:
            return _na(code, "order cannot be sized")
        assert ev.market is not None and ev.qty is not None
        px_tmn = ev.price_tmn()
        if not px_tmn:
            return _fail(code, f"cannot value {ev.intent.symbol} in TMN (no USDTTMN bid)")
        notional_tmn = ev.qty * px_tmn
        rate = ev.market.taker_fee if ev.taker else ev.market.maker_fee
        how = f"{pct(rate, 3)} {'taker' if ev.taker else 'maker'}"
        if ev.intent.features.get("cost_model") == "lockin":
            # leg 2 sells the same coin quantity in the other book: about the same TMN value
            leg2 = ev.ctx.markets.get(str(ev.intent.features.get("leg2_symbol")))
            if leg2 is None:
                return _fail(code, "lock-in entry without a known leg2_symbol: cannot price its fees")
            rate += leg2.taker_fee
            how += f" + {pct(leg2.taker_fee, 3)} taker leg 2 on {leg2.symbol}"
        expected = notional_tmn * rate
        port = ev.ctx.portfolio
        budget = r.fee_budget_pct * port.initial_equity_tmn
        total = port.fees_paid_tmn_total + expected
        summary = (
            f"fees paid {_amt(port.fees_paid_tmn_total, FIAT)} + expected {_amt(expected, FIAT)} ({how}) "
            f"= {_amt(total, FIAT)}"
        )
        if total > budget:
            return _fail(
                code, f"{summary} > budget {_amt(budget, FIAT)} ({pct(r.fee_budget_pct, 1)} of initial)"
            )
        return _ok(code, f"{summary} <= budget {_amt(budget, FIAT)} ({pct(r.fee_budget_pct, 1)} of initial)")

    def _check_edge_vs_cost(self, ev: _Eval) -> RiskCheck:
        code, it = "EDGE_VS_COST", ev.intent
        if it.purpose != "entry":
            return _na(code, f"{it.purpose}: no edge required")
        if ev.market is None:
            return _na(code, "unknown market")
        edge = it.expected_edge_pct
        if edge is None:
            return _fail(code, "entry without expected_edge_pct")
        cost, how = self._entry_cost(ev)
        if cost is None:
            return _fail(code, how)
        margin = self._margin_min(it)
        need = cost + margin
        summary = (
            f"edge {pct(edge, 3)} vs cost {pct(cost, 3)} ({how}) + margin {pct(margin, 3)} = {pct(need, 3)}"
        )
        if edge >= need:
            return _ok(code, summary)
        return _fail(code, f"{summary}: edge too small")

    def _entry_cost(self, ev: _Eval) -> tuple[Decimal | None, str]:
        """Cost of the trade the entry starts: lock-in (two legs) or a same-book round trip."""
        assert ev.market is not None
        m, taker = ev.market, ev.taker
        entry_fee = m.taker_fee if taker else m.maker_fee
        entry_txt = f"{pct(entry_fee, 3)} {'taker' if taker else 'maker'} {m.symbol}"
        if ev.intent.features.get("cost_model") == "lockin":
            leg2 = ev.ctx.markets.get(str(ev.intent.features.get("leg2_symbol")))
            if leg2 is None:
                return None, "lock-in entry without a known leg2_symbol"
            return (
                entry_fee + leg2.taker_fee,
                f"lock-in: {entry_txt} + {pct(leg2.taker_fee, 3)} taker {leg2.symbol}",
            )
        spread = ev.book.spread_pct if ev.book is not None else None
        if spread is None:
            return None, f"no two-sided {m.symbol} book to measure the spread"
        rt = round_trip_cost(m, taker)
        return (
            rt + spread,
            f"round trip: {entry_txt} + {pct(m.taker_fee, 3)} taker exit + spread {pct(spread, 3)}",
        )

    def _margin_min(self, intent: Intent) -> Decimal:
        """max(features.margin_min_pct, S1's configured margin for S1 entries, 0)."""
        margin = max(_dec(intent.features.get("margin_min_pct")) or D0, D0)
        if intent.strategy == "S1":
            margin = max(margin, self.cfg.strategies.s1_crossbook.margin_min_pct)
        return margin

    # ------------------------------------------------------------ data quality

    def _check_stale_data(self, ev: _Eval) -> RiskCheck:
        code, it, ctx = "STALE_DATA", ev.intent, ev.ctx
        if it.purpose in ("stop", "flatten"):
            return _na(code, f"{it.purpose} is never blocked by stale data")
        quote = ev.market.quote if ev.market is not None else ""
        symbols = {it.symbol, *_symbols(it.features.get("books_used"))}
        if quote == USDT or it.strategy == "S1":
            symbols.add(USDTTMN)
        limit = self.cfg.engine.stale_data_s
        problems: list[str] = []
        ages: list[str] = []
        for sym in sorted(symbols):
            book = ctx.books.get(sym)
            if book is None:
                problems.append(f"{sym}: no book")
                continue
            age = book.age(ctx.now)
            ages.append(f"{sym} {age:.1f}s")
            if age > limit:
                problems.append(f"{sym}: {age:.1f}s old")
        if problems:
            return _fail(code, f"stale market data (limit {limit:g}s): " + "; ".join(problems))
        return _ok(code, f"books fresh (<= {limit:g}s): " + ", ".join(ages))

    def _check_price_sanity(self, ev: _Eval) -> RiskCheck:
        code, it, r = "PRICE_SANITY", ev.intent, self.cfg.risk
        fails: list[str] = []
        notes: list[str] = []
        # 1) fat-finger guard: a LIMIT price must sit near the market
        if it.order_type in MARKET_TYPES:
            notes.append("price band n/a (MARKET order has no limit price)")
        elif it.order_type == "STOP_LIMIT":
            notes.append("price band n/a (a STOP_LIMIT's limit sits at the stop distance by design)")
        elif it.price is None:
            notes.append("no limit price (priced from the book at placement)")
        elif not ev.entry_like and it.side == "SELL":
            # a closing SELL is measured from the best bid, downward only: a sell at or near the
            # bid is never rejected, however wide the spread (a crash must not block flatten_final)
            bid = ev.book.best_bid if ev.book is not None else None
            band = r.price_sanity_pct
            if not bid:
                notes.append(f"no best bid; {it.purpose} not blocked")
            else:
                dev = it.price / bid - 1
                txt = f"sell limit {it.price} is {pct(dev)} from the best bid {format(bid, 'f')} (floor -{pct(band, 1)})"
                (fails if dev < -band else notes).append(txt)
        else:
            mid = ev.book.mid if ev.book is not None else None
            if not mid:
                if ev.entry_like:
                    fails.append(f"no mid price to sanity-check limit {it.price}")
                else:
                    notes.append(f"no mid price; {it.purpose} not blocked")
            else:
                dev = it.price / mid - 1
                band = r.price_sanity_pct
                txt = f"limit {it.price} is {pct(dev)} from mid {format(mid, 'f')} (band +-{pct(band, 1)})"
                (fails if abs(dev) > band else notes).append(txt)
        # 2) anomaly guard: the two books of a cross-book trade must roughly agree
        dev_mid = _dec(it.features.get("dev_mid_pct"))
        if dev_mid is not None and ev.entry_like:
            limit = r.price_disagreement_pause_pct
            txt = f"cross-book deviation {pct(dev_mid)} (anomaly at {pct(limit, 0)})"
            (fails if abs(dev_mid) >= limit else notes).append(txt)
        if fails:
            return _fail(code, "; ".join(fails))
        return _ok(code, "; ".join(notes))

    # ------------------------------------------------------------ orders & balances

    def _check_self_cross(self, ev: _Eval) -> RiskCheck:
        """Wash-trade prevention: never let a new order trade against one of our resting orders."""
        code, it = "SELF_CROSS", ev.intent
        buy = it.side == "BUY"
        opp_side = "SELL" if buy else "BUY"
        own_entry = self._own_entry_decision(ev)
        candidates = [
            o for o in ev.ctx.portfolio.open_orders_for(it.symbol) if not o.is_terminal and o.req.side == opp_side
        ]
        own = [o for o in candidates if own_entry is not None and o.req.purpose == "entry" and o.req.decision_id == own_entry]
        opposite = [o for o in candidates if o not in own]
        skipped = f" (the position's own entry BUY {own[0].client_id} is canceled before this SELL)" if own else ""
        if not opposite:
            return _ok(code, f"no open {opp_side} orders of ours in {it.symbol}{skipped}")
        ours = it.price if it.price is not None else ev.ref
        if ours is None:
            return _fail(code, f"cannot price this {it.side} while our {opp_side} orders rest in {it.symbol}")
        unpriced = [o for o in opposite if _order_price(o) is None]
        if unpriced:
            return _fail(
                code, f"our unpriced {opp_side} order {unpriced[0].client_id} is open in {it.symbol}"
            )
        priced = sorted((p, o.client_id) for o in opposite if (p := _order_price(o)) is not None)
        worst_price, worst_id = priced[0] if buy else priced[-1]
        crosses = ours >= worst_price if buy else ours <= worst_price
        rel = ">=" if buy else "<="
        if crosses:
            return _fail(
                code,
                f"{it.side} at {ours} {rel} our open {opp_side} {worst_id} at {worst_price}: would trade with ourselves",
            )
        other = "<" if buy else ">"
        best = "lowest" if buy else "highest"
        return _ok(code, f"{it.side} at {ours} {other} our {best} open {opp_side} at {worst_price}{skipped}")

    @staticmethod
    def _own_entry_decision(ev: _Eval) -> str | None:
        """Entry decision id of the position a closing SELL closes (its entry BUYs do not count as
        self-cross: the OrderManager cancels them right before placing the SELL)."""
        it = ev.intent
        if it.side != "SELL" or it.purpose not in RISK_REDUCING or not it.linked_position_id:
            return None
        pos = ev.ctx.portfolio.positions.get(it.linked_position_id)
        if pos is None or pos.symbol != it.symbol:
            return None
        entry_id = pos.meta.get("entry_decision_id")
        return str(entry_id) if entry_id else None

    def _check_balance(self, ev: _Eval) -> RiskCheck:
        code, it, port = "BALANCE", ev.intent, ev.ctx.portfolio
        if not ev.sized:
            return _na(code, "order cannot be sized")
        assert ev.market is not None and ev.qty is not None and ev.ref is not None
        m = ev.market
        if it.side == "BUY":
            fee = m.taker_fee if ev.taker else m.maker_fee
            free = port.free(m.quote)
            buffer = self.cfg.risk.balance_buffer_pct
            usable = free * (1 - buffer)
            need = ev.qty * ev.ref * (1 + fee)
            have = f"free {_amt(free, m.quote)} less {pct(buffer, 1)} buffer = {_amt(usable, m.quote)}"
            if need <= usable:
                return _ok(code, f"needs {_amt(need, m.quote)} incl. {pct(fee, 2)} fee <= {have}")
            max_qty = max(usable, D0) / (ev.ref * (1 + fee))
            short = f"needs {_amt(need, m.quote)} incl. fee > {have}"
        else:
            free = port.free(m.base)
            if ev.qty <= free:
                return _ok(code, f"sells {ev.shown(ev.qty)} {m.base} <= free {free} {m.base}")
            max_qty = max(free, D0)  # selling what we hold cannot overdraw: no buffer
            short = f"sells {ev.shown(ev.qty)} {m.base} > free {free} {m.base}"
        problem = self._size_problem(m, max_qty, ev.ref)
        if problem:
            return _fail(code, f"{short}; nothing tradable left ({problem})")
        return ev.adjust(code, max_qty, short)

    def _check_duplicate(self, ev: _Eval) -> RiskCheck:
        """Reject an intent that repeats work already in flight.

        * Any purpose except ``leg2``: an open order with the same (strategy, symbol, side,
          purpose). Leg 2 is exempt because S1 sends one leg 2 per leg-1 fill increment; their sum
          can never oversell because BALANCE caps every SELL at the free coin balance.
        * Entry-like only: the same key approved within ``duplicate_cooldown_s``
          (``ctx.recent_intents``). Exits never wait out a cooldown — a stop must go out now.
        * ``exit``/``stop`` for a position: another exit/stop SELL of the same strategy is already
          working in that symbol.

        The position's own protective stop order never counts (also not for the first rule: a
        stop exit has the same key as the resting server stop): the OrderManager cancels it
        before selling.
        """
        code, it, ctx = "DUPLICATE", ev.intent, ev.ctx
        key = (it.strategy, it.symbol, it.side, it.purpose)
        pid = it.linked_position_id
        pos = ctx.portfolio.positions.get(pid) if pid and it.purpose in ("exit", "stop") else None
        protective = pos.stop_order_client_id if pos is not None else None
        open_orders = [
            (cid, o)
            for cid, o in sorted(ctx.portfolio.open_orders.items())
            if not o.is_terminal and cid != protective
        ]
        label = "/".join(key)
        if it.purpose != "leg2":
            for cid, o in open_orders:
                if (o.req.strategy, o.req.symbol, o.req.side, o.req.purpose) == key:
                    return _fail(code, f"open order {cid} already works {label}")
        if ev.entry_like:
            cooldown = self.cfg.risk.duplicate_cooldown_s
            for ri in ctx.recent_intents:
                age = ctx.now - ri.ts
                if (ri.strategy, ri.symbol, ri.side, ri.purpose) == key and 0 <= age < cooldown:
                    return _fail(code, f"{label} already approved {age:.0f}s ago (cooldown {cooldown:g}s)")
        if it.purpose in ("exit", "stop") and pid:
            for cid, o in open_orders:
                if (
                    o.req.purpose in ("exit", "stop")
                    and (o.req.strategy, o.req.symbol, o.req.side) == (it.strategy, it.symbol, it.side)
                ):
                    return _fail(code, f"open {o.req.purpose} order {cid} already closes position {pid}")
        scope = " or approved within the cooldown" if ev.entry_like else ""
        return _ok(code, f"no open order{scope} for {label}")

    # ------------------------------------------------------------ rolling state (§6)

    def anomaly_reasons(self, ctx: RiskContext, anomalies: list[str] | None = None) -> list[str]:
        """Active anomaly conditions: API error burst, stale USDTTMN book, engine-reported anomalies."""
        r = self.cfg.risk
        reasons: list[str] = []
        if ctx.api_consecutive_errors >= r.consecutive_api_errors_pause:
            reasons.append(
                f"{ctx.api_consecutive_errors} consecutive API errors (>= {r.consecutive_api_errors_pause})"
            )
        book, limit = ctx.books.get(USDTTMN), self.cfg.engine.stale_data_s
        if book is None:
            reasons.append("no USDTTMN book")
        elif book.age(ctx.now) > limit:
            reasons.append(f"USDTTMN book {book.age(ctx.now):.1f}s old (> {limit:g}s)")
        reasons.extend(sorted(set(anomalies or [])))
        return reasons

    def update_rolling_state(self, ctx: RiskContext, anomalies: list[str] | None = None) -> list[RiskEvent]:
        """Kill / pause / resume decisions for this tick; the engine applies and logs them."""
        r, flags, port, now = self.cfg.risk, ctx.flags, ctx.portfolio, ctx.now
        reasons = self.anomaly_reasons(ctx, anomalies)
        if reasons:
            self._anomaly_clear_since = None
        elif self._anomaly_clear_since is None:
            self._anomaly_clear_since = now

        dd = port.drawdown_pct
        if not flags.killed and dd >= r.max_drawdown_kill_pct:
            return [
                RiskEvent(
                    "kill",
                    f"drawdown_kill: drawdown {pct(dd)} >= {pct(r.max_drawdown_kill_pct)} from peak "
                    f"{_amt(port.peak_equity_tmn or D0, FIAT)} (equity {_amt(port.equity_tmn or D0, FIAT)})",
                )
            ]
        if flags.killed:
            return []  # a kill outranks every pause and is only ever reset by hand

        kind = _pause_kind(flags.paused, flags.pause_reason)
        daily = port.daily_pnl_pct
        if daily <= -r.daily_loss_limit_pct:
            if kind in (None, ANOMALY_REASON):
                until = day_start(now) + DAY_S
                return [
                    RiskEvent(
                        "pause",
                        f"{DAILY_LOSS_REASON}: daily PnL {pct(daily)} <= -{pct(r.daily_loss_limit_pct)}; "
                        f"no new entries until {iso(until)}",
                        until_ts=until,
                    )
                ]
            return []
        if kind == DAILY_LOSS_REASON:
            if flags.paused_until_ts is not None and now < flags.paused_until_ts:
                return []  # same UTC day
            if reasons:  # the day is over but an anomaly is active: switch the pause over
                return [RiskEvent("pause", f"{ANOMALY_REASON}: " + "; ".join(reasons))]
            return [RiskEvent("resume", f"{DAILY_LOSS_REASON} pause over: new UTC day")]
        if reasons:
            if kind is None:
                return [RiskEvent("pause", f"{ANOMALY_REASON}: " + "; ".join(reasons))]
            return []
        if kind == ANOMALY_REASON and self._anomaly_clear_since is not None:
            clear_for = now - self._anomaly_clear_since
            if clear_for >= r.anomaly_clear_s:
                return [
                    RiskEvent(
                        "resume", f"{ANOMALY_REASON} cleared for {clear_for:.0f}s (>= {r.anomaly_clear_s:g}s)"
                    )
                ]
        return []


def _usdt_per_unit(market: MarketInfo, side: str, price: Decimal, u_bid: Decimal) -> Decimal:
    """Change of the USDT holding, in TMN, per unit of base traded on ``market`` at ``price``."""
    buy = side == "BUY"
    if market.base == USDT:
        return u_bid if buy else -u_bid
    if market.quote == USDT:
        return -price * u_bid if buy else price * u_bid
    return D0


def _open_coin_buys_tmn(ctx: RiskContext) -> tuple[Decimal, list[str]]:
    """TMN value of the unfilled part of our live BUY orders for coins (any purpose).

    Returns (value, client ids that could not be valued). The price is the order's limit (or stop)
    price, else the best ask.
    """
    total, unvalued = D0, []
    for cid, o in sorted(ctx.portfolio.open_orders.items()):
        if o.is_terminal or o.req.side != "BUY":
            continue
        m = ctx.markets.get(o.req.symbol)
        if m is None or m.base in QUOTE_ASSETS:
            continue
        book = ctx.books.get(o.req.symbol)
        price = _order_price(o) or (book.best_ask if book is not None else None)
        value = quote_to_tmn(m.quote, o.remaining_qty * price, ctx.books) if price else None
        if value is None:
            unvalued.append(cid)
            continue
        total += value
    return total, unvalued


def _open_usdt_effect_tmn(ctx: RiskContext, u_bid: Decimal) -> Decimal:
    """TMN change of the USDT holding once our live USDT-moving orders fill (stops excluded:
    they protect positions and are not expected to fill)."""
    total = D0
    for _, o in sorted(ctx.portfolio.open_orders.items()):
        if o.is_terminal or o.req.type in STOP_TYPES:
            continue
        m = ctx.markets.get(o.req.symbol)
        if m is None or USDT not in (m.base, m.quote):
            continue
        book = ctx.books.get(o.req.symbol)
        best = None if book is None else (book.best_ask if o.req.side == "BUY" else book.best_bid)
        price = o.req.price if o.req.price is not None else best
        if price is None:
            continue
        total += o.remaining_qty * _usdt_per_unit(m, o.req.side, price, u_bid)
    return total


def _pause_kind(paused: bool, reason: str) -> str | None:
    """Which owner a pause has: ``daily_loss``, ``anomaly``, ``other`` — None when not paused."""
    if not paused:
        return None
    for kind in (DAILY_LOSS_REASON, ANOMALY_REASON):
        if reason.startswith(kind):
            return kind
    return "other"

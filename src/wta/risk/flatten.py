"""Flatten to TMN (PRD-02 §7): turn every non-TMN balance into TMN before the challenge ends.

Only TMN counts at the official end (unrealized coin value is not accepted), so the last two hours
run a deterministic, SELL-only routine. ``FlattenManager.plan`` is called every fast tick while
``flatten_phase >= 1`` (and by the kill-switch). It looks at balances, positions and our open
orders and returns the next SELL intents. It never talks to the exchange itself: its intents go
through the RiskManager and the OrderManager like every other intent, so each one is risk-checked
and has a decision record.

Rules
* **Route.** Coin X is sold on ``XTMN`` when that market exists and is not banned, else on
  ``XUSDT``; USDT is sold on ``USDTTMN``. Coins sold for USDT come back as USDT, which a later
  round sells on USDTTMN.
* **Phase 1** (``flatten_start`` = T-2h; also used by the kill-switch): maker-first LIMIT
  (``taker=False``, price chosen by the OrderManager from the live book). The OrderManager
  re-prices it up to ``execution.flatten_max_reprices`` times, then crosses the spread.
* **Phase 2** (``flatten_final`` = T-30min): MARKET where the market allows MARKET orders,
  else a crossing LIMIT at ``best_bid x (1 - execution.flatten_final_cross_pct)``.
* **One SELL per asset at a time.** An asset with one of our non-stop SELL orders still open
  (a flatten order, or a position exit in progress) is skipped. Re-running ``plan`` therefore just
  continues where it was: idempotent and restart-safe.
* **Positions.** Coins held by a strategy position are sold with ``linked_position_id``, so the
  OrderManager cancels that position's stop order right before the sell and the position closes
  with ``exit_reason`` ``flatten`` (or ``kill``). Coins held by no position are sold first —
  nothing protects them.
* **Dust.** A balance worth less than the target market's minimum order is reported, never traded:
  topping it up would need a BUY, and flatten never buys (checked before ``plan`` returns).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from decimal import Decimal
from typing import Any, Literal

from wta.config import Config
from wta.execution.positions import FIAT, USDTTMN, quote_to_tmn, valuate, value_tmn
from wta.models import D0, Balance, BookSnapshot, Intent, MarketInfo, Order, OrderType, Position
from wta.risk.context import Deadline, RiskContext
from wta.utils.decimalutil import fmt, pct, round_price, round_qty
from wta.utils.ids import make_decision_id

STRATEGY = "FLATTEN"  # client ids get the "FL" prefix (wta.utils.ids.strategy_prefix)
STOP_ORDER_TYPES = frozenset({"STOP_LIMIT", "STOP_MARKET"})
# Open orders of these purposes are canceled when flatten starts (exits and stops keep running;
# a position's stop is canceled by the OrderManager right before its flatten sell).
CANCEL_AT_FLATTEN_START = frozenset({"entry", "rebalance"})
PHASE_EVENTS: dict[int, str] = {1: "flatten_start", 2: "flatten_final", 3: "challenge_end"}

Trigger = Literal["deadline", "kill"]
AssetState = Literal["sell", "working", "dust", "no_market", "no_book"]


@dataclass(frozen=True)
class AssetStatus:
    """What ``plan`` decided for one non-TMN asset (kept in ``FlattenManager.last_scan``)."""

    asset: str
    total: Decimal
    market: str | None
    state: AssetState
    detail: str

    def as_dict(self) -> dict[str, Any]:
        return {**asdict(self), "total": str(self.total)}


@dataclass(frozen=True)
class _Unit:
    """One sell: a quantity of an asset, optionally the coins of one position."""

    qty: Decimal
    position: Position | None
    label: Literal["free", "position", "all"]


class FlattenManager:
    """Plans the SELL intents that bring the account to 100% TMN (see the module docstring)."""

    def __init__(self, cfg: Config, markets: dict[str, MarketInfo]) -> None:
        self.cfg = cfg
        self.markets = markets
        self.last_scan: list[AssetStatus] = []
        self._seq = 0  # decision-id sequence; deterministic for a given call history (replay)

    # ------------------------------------------------------------ routing

    def target_market(self, asset: str) -> MarketInfo | None:
        """The market an asset is sold on: ``<asset>TMN`` if listed and allowed, else ``<asset>USDT``."""
        if asset == FIAT:
            return None
        for symbol in (f"{asset}TMN", f"{asset}USDT"):
            market = self.markets.get(symbol)
            if market is not None and market.base == asset and not self.cfg.markets.is_banned(symbol):
                return market
        return None

    def min_notional(self, market: MarketInfo) -> Decimal:
        """The market's minimum order value (config fallback when the market lists none)."""
        if market.min_notional > 0:
            return market.min_notional
        risk = self.cfg.risk
        return risk.min_notional_tmn if market.quote == FIAT else risk.min_notional_usdt

    def markets_needed(self, balances: dict[str, Balance]) -> list[str]:
        """Books the flatten routine needs (target markets of held assets, plus USDTTMN)."""
        needed = {USDTTMN}
        for asset, bal in balances.items():
            market = self.target_market(asset)
            if market is not None and bal.total > 0:
                needed.add(market.symbol)
        return sorted(needed)

    # ------------------------------------------------------------ planning

    def plan(
        self,
        ctx: RiskContext,
        open_orders: list[Order],
        phase: int,
        books: dict[str, BookSnapshot],
        *,
        trigger: Trigger = "deadline",
    ) -> list[Intent]:
        """SELL intents for this round (at most one per asset). Empty outside phases 1 and 2.

        ``open_orders`` are *our* open orders; ``phase`` is 1 (maker-first; also the kill-switch,
        with ``trigger="kill"``) or 2 (final: MARKET / crossing LIMIT).
        """
        self.last_scan = []
        if phase not in (1, 2):
            return []  # nothing before flatten_start; no orders at all after the challenge end
        selling = self._assets_being_sold(open_orders)
        intents: list[Intent] = []
        for asset in sorted(ctx.portfolio.balances):
            bal = ctx.portfolio.balances[asset]
            if asset == FIAT or bal.total <= 0:
                continue
            status, intent = self._plan_asset(ctx, asset, bal, selling.get(asset, []), phase, books, trigger)
            self.last_scan.append(status)
            if intent is not None:
                intents.append(intent)
        assert_sell_only(intents)
        return intents

    def _assets_being_sold(self, open_orders: list[Order]) -> dict[str, list[str]]:
        """asset -> client ids of our open non-stop SELL orders on any market of that asset."""
        out: dict[str, list[str]] = {}
        for order in sorted(open_orders, key=lambda o: o.client_id):
            req = order.req
            market = self.markets.get(req.symbol)
            if order.is_terminal or market is None or req.side != "SELL" or req.type in STOP_ORDER_TYPES:
                continue
            out.setdefault(market.base, []).append(order.client_id)
        return out

    def _plan_asset(
        self,
        ctx: RiskContext,
        asset: str,
        bal: Balance,
        working: list[str],
        phase: int,
        books: dict[str, BookSnapshot],
        trigger: Trigger,
    ) -> tuple[AssetStatus, Intent | None]:
        market = self.target_market(asset)
        if market is None:
            detail = f"no allowed {asset}TMN or {asset}USDT market: cannot be sold"
            return AssetStatus(asset, bal.total, None, "no_market", detail), None
        if working:
            detail = f"our SELL order(s) {', '.join(working)} still working; waiting for them"
            return AssetStatus(asset, bal.total, market.symbol, "working", detail), None
        book = books.get(market.symbol)
        bid = book.best_bid if book is not None else None
        if bid is None or bid <= 0:
            detail = f"no bid on {market.symbol} yet; retried next tick"
            return AssetStatus(asset, bal.total, market.symbol, "no_book", detail), None

        positions = sorted(
            (p for p in ctx.portfolio.positions.values() if p.base == asset and p.qty > 0),
            key=lambda p: (p.entry_ts, p.id),
        )
        unit = self._next_unit(bal, positions, market, bid)
        if unit is None:
            detail = (
                f"dust: {bal.total} {asset} is worth {fmt(round_qty(market, bal.total) * bid)} {market.quote} "
                f"< minimum order {fmt(self.min_notional(market))} {market.quote} on {market.symbol}; left as is"
            )
            return AssetStatus(asset, bal.total, market.symbol, "dust", detail), None
        intent = self._intent(ctx.now, asset, bal, unit, market, bid, phase, books, trigger)
        detail = f"sell {unit.qty} {asset} on {market.symbol} ({unit.label})"
        return AssetStatus(asset, bal.total, market.symbol, "sell", detail), intent

    def _sellable(self, market: MarketInfo, qty: Decimal, bid: Decimal) -> Decimal | None:
        """``qty`` rounded down to the step if it meets minQty and minNotional at ``bid``, else None."""
        q = round_qty(market, qty)
        if q <= 0 or q < market.min_qty or q * bid < self.min_notional(market):
            return None
        return q

    def _next_unit(
        self, bal: Balance, positions: list[Position], market: MarketInfo, bid: Decimal
    ) -> _Unit | None:
        """The next sell for this asset, or None when everything left is dust.

        1. coins held by no position (only what is free right now) — nothing protects them;
        2. one position's coins (the last position also takes any leftover free coins, so no dust
           is stranded next to it);
        3. the whole balance, when no smaller part reaches the minimum order.
        """
        in_positions = sum((p.qty for p in positions), D0)
        loose = max(bal.total - in_positions, D0)
        q = self._sellable(market, min(loose, bal.free), bid)
        if q is not None:
            return _Unit(q, None, "free")
        for i, pos in enumerate(positions):
            extra = loose if i == len(positions) - 1 else D0
            q = self._sellable(market, min(pos.qty + extra, bal.total), bid)
            if q is not None:
                return _Unit(q, pos, "position")
        q = self._sellable(market, bal.total, bid)
        if q is not None:
            return _Unit(q, positions[0] if positions else None, "all")
        return None

    def _intent(
        self,
        now: float,
        asset: str,
        bal: Balance,
        unit: _Unit,
        market: MarketInfo,
        bid: Decimal,
        phase: int,
        books: dict[str, BookSnapshot],
        trigger: Trigger,
    ) -> Intent:
        order_type: OrderType = "LIMIT"
        price: Decimal | None = None
        if phase == 2 and market.market_orders_enabled:
            order_type = "MARKET"
        elif phase == 2:
            price = round_price(market, bid * (1 - self.cfg.execution.flatten_final_cross_pct), "SELL")
        self._seq += 1
        est_tmn = quote_to_tmn(market.quote, unit.qty * bid, books)
        features: dict[str, Any] = {
            "phase": phase,
            "trigger": trigger,
            "asset": asset,
            "balance_total": str(bal.total),
            "unit": unit.label,
            "target_market": market.symbol,
            "route": "TMN" if market.quote == FIAT else "USDT",
            "bid": str(bid),
            "min_notional": str(self.min_notional(market)),
            "est_value_tmn": str(est_tmn) if est_tmn is not None else None,
            "exit_reason": "kill" if trigger == "kill" else "flatten",
        }
        if unit.position is not None:
            features["position_id"] = unit.position.id
        return Intent(
            id=make_decision_id(now, STRATEGY, market.symbol, self._seq),
            ts=now,
            strategy=STRATEGY,
            symbol=market.symbol,
            side="SELL",
            order_type=order_type,
            qty=unit.qty,
            notional=unit.qty * (price or bid),
            price=price,
            purpose="flatten",
            reason=self._reason(asset, unit, market, bid, price, order_type, phase, trigger, est_tmn),
            features=features,
            linked_position_id=unit.position.id if unit.position is not None else None,
            taker=phase == 2,
        )

    def _reason(
        self,
        asset: str,
        unit: _Unit,
        market: MarketInfo,
        bid: Decimal,
        price: Decimal | None,
        order_type: OrderType,
        phase: int,
        trigger: Trigger,
        est_tmn: Decimal | None,
    ) -> str:
        """One readable sentence per intent for the decision log."""
        if trigger == "kill":
            why = "Kill-switch flatten: convert everything to TMN and halt"
        elif phase == 1:
            why = "Flatten phase 1 (T-2h): only TMN counts at the challenge end"
        else:
            why = "Flatten phase 2 (T-30min, final): anything not yet TMN is sold now"
        if market.quote == FIAT:
            route = f"{market.symbol} is the direct TMN market"
        else:
            route = f"no allowed {asset}TMN market, so via {market.symbol}; the USDT is sold on USDTTMN next"
        if unit.label == "position" and unit.position is not None:
            what = (
                f"the coins of position {unit.position.id} ({unit.position.strategy}); "
                "its stop order is canceled right before this sell"
            )
        elif unit.label == "free":
            what = "coins held by no position"
        else:
            what = "the whole balance (no smaller part reaches the minimum order)"
        if phase == 1:
            how = (
                "maker-first LIMIT, re-priced up to "
                f"{self.cfg.execution.flatten_max_reprices} times, then crossing the spread"
            )
        elif order_type == "MARKET":
            how = "MARKET sell"
        else:
            how = (
                f"crossing LIMIT at bid {bid} x (1 - {pct(self.cfg.execution.flatten_final_cross_pct)}) = {price}"
                f" because MARKET orders are disabled on {market.symbol}"
            )
        value = f"~{fmt(est_tmn)} TMN" if est_tmn is not None else "value unknown"
        return (
            f"{why}. Sell {unit.qty} {asset} on {market.symbol} ({value} at bid {bid}): {what}. "
            f"Route: {route}. Order: {how}."
        )

    # ------------------------------------------------------------ verification

    def final_state(self, ctx: RiskContext) -> dict[str, Any]:
        """The T-5min ``final_state`` event: balances, equity and what is still not TMN.

        ``non_tmn_over_min`` lists assets that could still be sold (worth at least the target
        market's minimum order, or of unknown value — alert-worthy); ``dust`` lists the rest.
        """
        balances = ctx.portfolio.balances
        val = valuate(balances, ctx.books)
        over: list[dict[str, Any]] = []
        dust: list[dict[str, Any]] = []
        for asset in sorted(balances):
            total = balances[asset].total
            if asset == FIAT or total <= 0:
                continue
            entry, sellable = self._final_entry(asset, total, ctx.books)
            (over if sellable else dust).append(entry)
        equity = val.equity_tmn
        return {
            "ts": ctx.now,
            "balances": {
                a: {"free": str(b.free), "locked": str(b.locked), "total": str(b.total)}
                for a, b in sorted(balances.items())
            },
            "equity_tmn": str(equity),
            "tmn_share": str((val.tmn / equity).quantize(Decimal("0.000001"))) if equity > 0 else None,
            "missing_prices": list(val.missing_prices),
            "non_tmn_over_min": over,
            "dust": dust,
            "complete": not over,
        }

    def _final_entry(
        self, asset: str, total: Decimal, books: dict[str, BookSnapshot]
    ) -> tuple[dict[str, Any], bool]:
        market = self.target_market(asset)
        book = books.get(market.symbol) if market is not None else None
        bid = book.best_bid if book is not None else None
        tmn_value = value_tmn(asset, total, books)
        if market is not None and bid is not None and bid > 0:
            sellable = self._sellable(market, total, bid) is not None
        elif tmn_value is None:
            sellable = True  # value unknown: cannot prove it is dust, so it is reported as left over
        else:
            sellable = tmn_value >= self.cfg.risk.min_notional_tmn
        entry = {
            "asset": asset,
            "total": str(total),
            "market": market.symbol if market is not None else None,
            "value_tmn": str(tmn_value) if tmn_value is not None else None,
            "min_notional": str(self.min_notional(market)) if market is not None else None,
        }
        return entry, sellable

    def is_complete(self, ctx: RiskContext) -> bool:
        """True when nothing but TMN and dust is left."""
        return not self.final_state(ctx)["non_tmn_over_min"]

    @staticmethod
    def flatten_phase_transitions(
        deadline: Deadline, now: float, previous_phase: int
    ) -> list[tuple[int, str]]:
        """See the module-level ``flatten_phase_transitions``."""
        return flatten_phase_transitions(deadline, now, previous_phase)


# ---------------------------------------------------------------- module helpers


def flatten_phase_transitions(deadline: Deadline, now: float, previous_phase: int) -> list[tuple[int, str]]:
    """Phase boundaries crossed since ``previous_phase``: ``[(1, "flatten_start"), (2, "flatten_final"), ...]``.

    Phases only move forward. After a restart late in the run every missed boundary is returned
    in order, so the engine applies each one (cancel entries, then go final). Empty when no
    challenge end is configured.
    """
    current = deadline.phase(now)
    return [(p, PHASE_EVENTS[p]) for p in range(previous_phase + 1, current + 1)]


def cancel_at_flatten_start(managed: Any) -> bool:
    """Predicate for ``OrderManager.cancel_where`` at ``flatten_start``: entry/rebalance orders.

    Accepts a ManagedOrder (``.order.req.purpose``) or an Order (``.req.purpose``).
    """
    order = getattr(managed, "order", managed)
    return order.req.purpose in CANCEL_AT_FLATTEN_START


def assert_sell_only(intents: list[Intent]) -> None:
    """Flatten never buys. Raised explicitly (not ``assert``) so ``python -O`` cannot drop it."""
    for intent in intents:
        if intent.side != "SELL" or intent.purpose != "flatten":
            raise AssertionError(f"flatten produced a non-SELL/non-flatten intent: {intent.id} {intent.side}")

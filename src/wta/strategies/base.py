"""Strategy interface (PRD-03 §1).

Strategies are pure: given a ``StrategyContext`` (+ their own serialisable state) they return
``Intent`` objects with complete, human-readable reasons. No I/O, no clock reads, no randomness
other than ``ctx.rng_seed``. Per-symbol cooldowns and "one strategy per symbol" live in the
engine's ``StrategyRouter``, not here (``symbol_owners`` is the shared ownership rule, which also
reserves an S1 lock-in's leg-2 book).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Protocol

from wta.config import Config
from wta.features.engine import FeatureSnapshot
from wta.models import BookSnapshot, Candle, Fill, Intent, MarketInfo, MarketPlan, Order, Position
from wta.utils.ids import make_decision_id


@dataclass(frozen=True)
class PortfolioView:
    """Read-only portfolio numbers a strategy may use."""

    equity_tmn: Decimal
    free: dict[str, Decimal]
    total: dict[str, Decimal]
    positions: tuple[Position, ...]
    open_orders: tuple[Order, ...]
    usdt_share: Decimal
    coin_exposure_pct: Decimal
    u_bid: Decimal | None  # USDTTMN bid, for TMN<->USDT conversions

    def positions_of(self, strategy: str) -> list[Position]:
        return [p for p in self.positions if p.strategy == strategy]

    def open_orders_of(self, strategy: str, symbol: str | None = None) -> list[Order]:
        return [
            o for o in self.open_orders if o.req.strategy == strategy and (symbol is None or o.req.symbol == symbol)
        ]


@dataclass
class StrategyContext:
    now: float
    books: dict[str, BookSnapshot]
    features: FeatureSnapshot
    candles: dict[tuple[str, str], list[Candle]]
    markets: dict[str, MarketInfo]
    portfolio: PortfolioView
    plan: MarketPlan | None
    cfg: Config
    rng_seed: int = 42
    flatten_phase: int = 0
    seq: list[int] = field(default_factory=lambda: [0])  # shared counter for deterministic ids

    def next_id(self, strategy: str, symbol: str) -> str:
        self.seq[0] += 1
        return make_decision_id(self.now, strategy, symbol, self.seq[0])

    def plan_fresh(self) -> MarketPlan | None:
        """The plan if it is not stale (ai.plan_stale_s), else None (rules-only defaults)."""
        if self.plan is None:
            return None
        if self.now - self.plan.ts > self.cfg.ai.plan_stale_s:
            return None
        return self.plan


class Strategy(Protocol):
    id: str

    def on_tick(self, ctx: StrategyContext) -> list[Intent]: ...

    def on_fill(self, fill: Fill, position: Position | None, ctx: StrategyContext) -> list[Intent]: ...

    def on_order_done(self, order: Order, ctx: StrategyContext) -> list[Intent]: ...

    def state(self) -> dict[str, Any]: ...

    def load_state(self, state: dict[str, Any] | None) -> None: ...

    def diagnostics(self) -> dict[str, Any]: ...


class BaseStrategy:
    """Default no-op hooks; concrete strategies override what they need."""

    id = "S0"

    def on_tick(self, ctx: StrategyContext) -> list[Intent]:
        return []

    def on_fill(self, fill: Fill, position: Position | None, ctx: StrategyContext) -> list[Intent]:
        return []

    def on_order_done(self, order: Order, ctx: StrategyContext) -> list[Intent]:
        return []

    def state(self) -> dict[str, Any]:
        return {}

    def load_state(self, state: dict[str, Any] | None) -> None:
        return None

    def diagnostics(self) -> dict[str, Any]:
        """Latest evaluation numbers (logged in health every minute, shown in the TUI)."""
        return {}


LOCKIN_STRATEGY = "S1"  # the strategy whose entries are two-book lock-ins (leg 2 in the coin's other book)


def other_book(symbol: str, markets: dict[str, MarketInfo]) -> str | None:
    """The coin's other quote book: ``XTMN`` <-> ``XUSDT`` (None for USDTTMN or an unknown market)."""
    m = markets.get(symbol)
    if m is None or m.base in ("TMN", "USDT"):
        return None
    if m.quote == "TMN":
        return f"{m.base}USDT"
    if m.quote == "USDT":
        return f"{m.base}TMN"
    return None


def symbol_owners(ctx: StrategyContext) -> dict[str, str]:
    """symbol -> the strategy that holds or works it ("one strategy per symbol", PRD-03 §6).

    A symbol is owned by the strategy of a position or an open order in it (positions first, by id;
    then orders, by client id). An S1 lock-in also owns its **leg-2 book** for the whole cycle: the
    leg-2 SELL must be able to cross there at any moment, and another strategy's BUY resting there
    would make the RiskManager (``SELF_CROSS``) and the OrderManager ("never a BUY and a SELL of ours
    open at once") refuse it, leaving the lock-in coins unhedged. So a lock-in position reserves
    ``meta.leg2_symbol`` (else the coin's other book), and a working S1 entry BUY reserves the coin's
    other book (its leg 2 starts the moment it fills). Direct holdings win over reservations.
    """
    owner: dict[str, str] = {}
    positions = sorted(ctx.portfolio.positions, key=lambda p: p.id)
    orders = sorted(ctx.portfolio.open_orders, key=lambda o: o.client_id)
    for p in positions:
        owner.setdefault(p.symbol, p.strategy)
    for o in orders:
        owner.setdefault(o.req.symbol, o.req.strategy)
    for p in positions:
        if p.meta.get("mode") == "lockin":
            leg2 = p.meta.get("leg2_symbol") or other_book(p.symbol, ctx.markets)
            if leg2:
                owner.setdefault(str(leg2), p.strategy)
    for o in orders:
        if o.req.strategy == LOCKIN_STRATEGY and o.req.purpose == "entry" and o.req.side == "BUY":
            other = other_book(o.req.symbol, ctx.markets)
            if other:
                owner.setdefault(other, o.req.strategy)
    return owner


def plan_enabled(ctx: StrategyContext, strategy_id: str) -> bool:
    """Strategy allowed by the (fresh) plan; with no plan, rules-only mode allows it."""
    plan = ctx.plan_fresh()
    if plan is None:
        return True
    return strategy_id in plan.enabled_strategies


def plan_size_multiplier(ctx: StrategyContext) -> Decimal:
    plan = ctx.plan_fresh()
    if plan is None:
        return Decimal(1)
    return max(Decimal(0), min(Decimal(1), plan.size_multiplier))

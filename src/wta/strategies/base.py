"""Strategy interface (PRD-03 §1).

Strategies are pure: given a ``StrategyContext`` (+ their own serialisable state) they return
``Intent`` objects with complete, human-readable reasons. No I/O, no clock reads, no randomness
other than ``ctx.rng_seed``. Per-symbol cooldowns and "one strategy per symbol" live in the
engine's ``StrategyRouter``, not here.
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

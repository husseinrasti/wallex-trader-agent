"""All pydantic data models — the single source of truth for every module.

Money fields are ``Decimal``. Models are frozen unless they represent mutable
runtime state (Order, Position). JSON serialisation (``model_dump(mode="json")``)
renders Decimals as strings, so logs never lose precision.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

D0 = Decimal("0")

OrderType = Literal["LIMIT", "MARKET", "STOP_LIMIT", "STOP_MARKET"]
Side = Literal["BUY", "SELL"]
OrderStatus = Literal["NEW", "PARTIALLY_FILLED", "FILLED", "CANCELED", "REJECTED", "EXPIRED", "UNKNOWN"]
TERMINAL_STATUSES: frozenset[str] = frozenset({"FILLED", "CANCELED", "REJECTED", "EXPIRED"})
Purpose = Literal["entry", "exit", "stop", "rebalance", "flatten", "leg2", "probe"]
BookSource = Literal["ws", "rest", "replay", "backtest"]
Regime = Literal["range", "trend_up", "trend_down", "chaos"]
DecisionKind = Literal["intent", "veto", "order", "plan", "outcome", "no_trade", "event"]
ExitReason = Literal["target", "stop", "time_stop", "lockin", "flatten", "kill", "manual_reset", "regime_flip"]


class Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Mutable(BaseModel):
    model_config = ConfigDict(frozen=False, extra="forbid", validate_assignment=True)


# ---------------------------------------------------------------- market data


class MarketInfo(Frozen):
    symbol: str
    base: str
    quote: str
    tick_decimals: int  # price decimals (from tickSize)
    step_decimals: int  # quantity decimals (from stepSize)
    min_qty: Decimal = D0
    min_notional: Decimal = D0
    market_orders_enabled: bool = True  # isMarketTypeEnable
    maker_fee: Decimal = Decimal("0.0025")
    taker_fee: Decimal = Decimal("0.003")

    @property
    def tick(self) -> Decimal:
        return Decimal(1).scaleb(-self.tick_decimals)

    @property
    def step(self) -> Decimal:
        return Decimal(1).scaleb(-self.step_decimals)


class Level(Frozen):
    price: Decimal
    qty: Decimal


class BookSnapshot(Frozen):
    symbol: str
    ts: float
    source: BookSource
    bids: list[Level] = Field(default_factory=list)  # descending price
    asks: list[Level] = Field(default_factory=list)  # ascending price

    @property
    def best_bid(self) -> Decimal | None:
        return self.bids[0].price if self.bids else None

    @property
    def best_ask(self) -> Decimal | None:
        return self.asks[0].price if self.asks else None

    @property
    def best_bid_qty(self) -> Decimal | None:
        return self.bids[0].qty if self.bids else None

    @property
    def best_ask_qty(self) -> Decimal | None:
        return self.asks[0].qty if self.asks else None

    @property
    def mid(self) -> Decimal | None:
        if self.best_bid is None or self.best_ask is None:
            return None
        return (self.best_bid + self.best_ask) / 2

    @property
    def spread_pct(self) -> Decimal | None:
        mid = self.mid
        if mid is None or mid == 0:
            return None
        return (self.best_ask - self.best_bid) / mid  # type: ignore[operator]

    def age(self, now: float) -> float:
        return now - self.ts

    def same_levels(self, other: BookSnapshot | None) -> bool:
        """True if bids/asks equal (used by the recorder to dedupe)."""
        return other is not None and self.bids == other.bids and self.asks == other.asks


class Trade(Frozen):
    symbol: str
    ts: float
    price: Decimal
    qty: Decimal
    side: Side | None = None  # taker (aggressor) side; Wallex isBuyOrder=true means maker was BUY -> SELL


class Candle(Frozen):
    symbol: str
    resolution: str  # "1", "15", "60", "1D"...
    ts: int  # bar open time, unix seconds
    o: Decimal
    h: Decimal
    l: Decimal  # noqa: E741
    c: Decimal
    v: Decimal


# ---------------------------------------------------------------- account


class Balance(Frozen):
    asset: str
    free: Decimal
    locked: Decimal = D0

    @property
    def total(self) -> Decimal:
        return self.free + self.locked


class OrderRequest(Frozen):
    client_id: str
    symbol: str
    side: Side
    type: OrderType
    qty: Decimal
    price: Decimal | None = None
    stop_price: Decimal | None = None
    strategy: str
    purpose: Purpose
    decision_id: str


class Order(Mutable):
    req: OrderRequest
    exchange_id: str | None = None
    status: OrderStatus = "NEW"
    filled_qty: Decimal = D0
    avg_price: Decimal | None = None
    fee_paid: Decimal = D0
    fee_asset: str | None = None
    created_ts: float
    updated_ts: float
    reprices: int = 0  # how many times this logical order was re-placed (OrderManager bookkeeping)
    raw: dict[str, Any] = Field(default_factory=dict)

    @property
    def client_id(self) -> str:
        return self.req.client_id

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES

    @property
    def remaining_qty(self) -> Decimal:
        return max(self.req.qty - self.filled_qty, D0)


class Fill(Frozen):
    order_client_id: str
    symbol: str
    side: Side
    price: Decimal
    qty: Decimal
    fee: Decimal = D0
    fee_asset: str = ""
    ts: float
    is_maker: bool | None = None
    trade_id: str | None = None  # exchange trade id/timestamp key, for de-duplication


class Position(Mutable):
    id: str
    strategy: str
    symbol: str
    base: str
    quote: str
    qty: Decimal
    entry_price: Decimal
    entry_ts: float
    stop_price: Decimal | None = None
    target_price: Decimal | None = None
    time_stop_ts: float | None = None
    stop_order_client_id: str | None = None
    meta: dict[str, Any] = Field(default_factory=dict)


# ---------------------------------------------------------------- decisions


class Intent(Frozen):
    id: str
    ts: float
    strategy: str
    symbol: str
    side: Side
    order_type: OrderType
    qty: Decimal | None = None
    notional: Decimal | None = None  # in the symbol's quote currency
    price: Decimal | None = None
    stop_price: Decimal | None = None
    purpose: Purpose
    reason: str  # human readable, goes to the decision log verbatim
    features: dict[str, Any] = Field(default_factory=dict)
    expected_edge_pct: Decimal | None = None
    linked_position_id: str | None = None
    taker: bool = False  # True if the order is meant to cross the spread


class RiskCheck(Frozen):
    code: str
    passed: bool
    detail: str


class RiskDecision(Frozen):
    approved: bool
    checks: list[RiskCheck] = Field(default_factory=list)
    adjusted_qty: Decimal | None = None
    veto_reason: str | None = None

    @property
    def failed_codes(self) -> list[str]:
        return [c.code for c in self.checks if not c.passed]


class RegimeCall(Frozen):
    regime: Regime
    confidence: float = 0.0
    evidence: str = ""


class MarketPlan(Frozen):
    ts: float
    regimes: dict[str, RegimeCall] = Field(default_factory=dict)
    enabled_strategies: list[str] = Field(default_factory=list)
    s1_margin_min_pct: Decimal = Decimal("0.0015")
    size_multiplier: Decimal = Decimal("1")
    usdt_target_share: Decimal = Decimal("0.5")
    risk_flags: list[str] = Field(default_factory=list)
    notes: str = ""
    model: str = ""
    prompt_hash: str = ""
    cached: bool = False
    tools_used: list[str] = Field(default_factory=list)
    tools_source: Literal["mcp", "local", "none"] = "none"
    latency_ms: int | None = None
    clamped: dict[str, Any] = Field(default_factory=dict)  # field -> original value, when clamped


class DecisionRecord(Frozen):
    """One line of logs/decisions.jsonl."""

    kind: DecisionKind
    decision_id: str
    ts: str  # ISO-8601 UTC
    mode: str
    git_commit: str
    config_hash: str
    strategy: str
    symbol: str
    features: dict[str, Any] = Field(default_factory=dict)
    plan_snapshot: dict[str, Any] | None = None
    intent: dict[str, Any] | None = None
    risk: dict[str, Any] = Field(default_factory=dict)
    order: dict[str, Any] | None = None
    outcome: dict[str, Any] | None = None  # filled later by an OutcomeUpdate record
    notes: str = ""


class OutcomeUpdate(Frozen):
    """Append-only update for an earlier decision (never rewrite earlier lines)."""

    kind: Literal["outcome"] = "outcome"
    decision_id: str
    ts: str
    outcome: dict[str, Any]  # pnl_tmn, pnl_pct, hold_s, closed_ts, exit_reason


class HealthRecord(Frozen):
    ts: str
    mode: str
    git_commit: str
    config_hash: str
    equity_tmn: Decimal | None = None
    drawdown_pct: Decimal | None = None
    open_orders: int = 0
    positions: int = 0
    ws_ok: bool = False
    rest_ok: bool = False
    api_errors_1m: int = 0
    llm_ok: bool | None = None
    paused: bool = False
    killed: bool = False
    reasons: list[str] = Field(default_factory=list)
    extra: dict[str, Any] = Field(default_factory=dict)


class EventRecord(Frozen):
    ts: str
    kind: str  # start, stop, restart, pause, resume, kill, reset_kill, flatten, config_change, final_state, ...
    reason: str = ""
    data: dict[str, Any] = Field(default_factory=dict)


class OrderLogRecord(Frozen):
    """One line of logs/orders.jsonl: raw request/response/cancel/fill traffic."""

    ts: str
    action: Literal["place", "response", "cancel", "cancel_response", "status", "fill", "error", "timeout"]
    client_id: str
    symbol: str = ""
    data: dict[str, Any] = Field(default_factory=dict)

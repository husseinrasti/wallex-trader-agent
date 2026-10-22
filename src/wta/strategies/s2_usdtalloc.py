"""S2 — USDTAlloc (PRD-03 §4): keep an explicit, bounded, regime-driven USDT share of equity.

The challenge is valued in TMN and USDTTMN moves 1–3 %/day, while the cheap USDT pairs need a
USDT balance. Holding USDT is therefore an unavoidable bet on the dollar; S2 makes it explicit:

    regime  = plan.regimes["USDTTMN"].regime   (LLM, if a fresh plan exists)
            | rule_regime(dollar features)      (rules-only fallback)
    target  = clamp(plan.usdt_target_share | target_share_by_regime[regime], bounds=[25 %, 70 %])
    trade   when |usdt_share - target| >= 15 pp, at most once per 30 min, maker-first on USDTTMN.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from wta.config import S2Cfg
from wta.models import Intent, Order
from wta.strategies.base import BaseStrategy, StrategyContext, plan_enabled
from wta.utils.decimalutil import fmt, pct, round_price, round_qty

SYMBOL = "USDTTMN"
RETRY_AFTER_UNFILLED_S = 120.0


def rule_regime(dollar: dict[str, Any], cfg: S2Cfg) -> tuple[str, str]:
    """Rules-only dollar regime + a one-line evidence string (PRD-03 §4)."""
    trend = dollar.get("trend_flag") or 0
    r24 = dollar.get("ret_24h")
    gap = dollar.get("ema_gap_pct")
    atr_pct = dollar.get("atr_15m_pct")
    atr_med = dollar.get("atr_15m_pct_median_24h")
    ev = f"ema20 vs ema60 {pct(gap)}, 24h {pct(r24)}, ATR15m {pct(atr_pct, 3)} (24h median {pct(atr_med, 3)})"
    if trend > 0 and r24 is not None and r24 > cfg.trend_ret_24h_pct:
        return "trend_up", ev
    if trend < 0 and r24 is not None and r24 < -cfg.trend_ret_24h_pct:
        return "trend_down", ev
    if atr_pct is not None and atr_med and atr_pct > cfg.chaos_atr_mult * atr_med:
        return "chaos", ev
    return "range", ev


class S2USDTAlloc(BaseStrategy):
    id = "S2"

    def __init__(self, cfg: S2Cfg) -> None:
        self.cfg = cfg
        self.last_rebalance_ts: float | None = None
        self.last_attempt_ts: float | None = None
        self._diag: dict[str, Any] = {}

    def on_tick(self, ctx: StrategyContext) -> list[Intent]:
        cfg = self.cfg
        if not cfg.enabled or ctx.flatten_phase > 0 or not plan_enabled(ctx, self.id):
            self._diag = {"status": "disabled" if not cfg.enabled else "flatten/plan"}
            return []
        book = ctx.books.get(SYMBOL)
        market = ctx.markets.get(SYMBOL)
        if book is None or market is None or book.best_bid is None or book.best_ask is None:
            self._diag = {"status": "no_book"}
            return []
        plan = ctx.plan_fresh()
        dollar = ctx.features.dollar
        if plan is not None and SYMBOL in plan.regimes:
            call = plan.regimes[SYMBOL]
            regime, source = call.regime, f"plan (conf {call.confidence:.2f})"
            evidence = call.evidence
        else:
            regime, evidence = rule_regime(dollar, cfg)
            source = "rules"
        lo, hi = cfg.bounds
        base_target = cfg.target_share_by_regime.get(regime, cfg.target_share_by_regime["range"])
        raw_target = plan.usdt_target_share if plan is not None else base_target
        target = min(max(raw_target, lo), hi)
        current = ctx.portfolio.usdt_share
        gap = target - current
        equity = ctx.portfolio.equity_tmn
        self._diag = {
            "regime": regime,
            "regime_source": source,
            "target": str(target),
            "current": str(current.quantize(Decimal("0.0001"))),
            "gap_pp": str(gap.quantize(Decimal("0.0001"))),
            "threshold_pp": str(cfg.rebalance_threshold_pp),
        }
        if abs(gap) < cfg.rebalance_threshold_pp:
            self._diag["status"] = "within_threshold"
            return []
        if self.last_rebalance_ts is not None and ctx.now - self.last_rebalance_ts < cfg.min_rebalance_interval_s:
            self._diag["status"] = "min_interval"
            return []
        if self.last_attempt_ts is not None and ctx.now - self.last_attempt_ts < RETRY_AFTER_UNFILLED_S:
            self._diag["status"] = "retry_wait"
            return []
        if ctx.portfolio.open_orders_of(self.id, SYMBOL):
            self._diag["status"] = "order_open"
            return []
        side = "BUY" if gap > 0 else "SELL"
        tick = market.tick
        if side == "BUY":
            price = book.best_bid + tick if book.best_bid + tick < book.best_ask else book.best_bid
        else:
            price = book.best_ask - tick if book.best_ask - tick > book.best_bid else book.best_ask
        price = round_price(market, price, side)
        notional_tmn = abs(gap) * equity
        qty = round_qty(market, notional_tmn / price)
        if qty <= 0:
            self._diag["status"] = "qty_zero"
            return []
        self.last_attempt_ts = ctx.now
        verb = "buy" if side == "BUY" else "sell"
        reason = (
            f"USDTTMN regime={regime} [{source}] ({evidence}); target USDT share {pct(target, 0)}"
            f"{' (plan)' if plan is not None else ''} vs current {pct(current, 1)} -> gap {pct(gap, 1)} "
            f">= threshold {pct(cfg.rebalance_threshold_pp, 0)} -> {verb} {fmt(notional_tmn)} TMN of USDT "
            f"({qty} USDT) as maker at {fmt(price)}."
        )
        self._diag["status"] = "intent"
        return [
            Intent(
                id=ctx.next_id(self.id, SYMBOL),
                ts=ctx.now,
                strategy=self.id,
                symbol=SYMBOL,
                side=side,
                order_type="LIMIT",
                qty=qty,
                notional=qty * price,
                price=price,
                purpose="rebalance",
                reason=reason,
                features={
                    "regime": regime,
                    "regime_source": source,
                    "target_share": str(target),
                    "usdt_share": str(current),
                    "gap_pp": str(gap),
                    "equity_tmn": str(equity),
                    "books_used": [SYMBOL],
                    "bid": str(book.best_bid),
                    "ask": str(book.best_ask),
                },
                expected_edge_pct=None,
                taker=False,
            )
        ]

    def on_order_done(self, order: Order, ctx: StrategyContext) -> list[Intent]:
        if order.req.strategy == self.id and order.filled_qty > 0:
            self.last_rebalance_ts = ctx.now
        return []

    def state(self) -> dict[str, Any]:
        return {"last_rebalance_ts": self.last_rebalance_ts, "last_attempt_ts": self.last_attempt_ts}

    def load_state(self, state: dict[str, Any] | None) -> None:
        state = state or {}
        self.last_rebalance_ts = state.get("last_rebalance_ts")
        self.last_attempt_ts = state.get("last_attempt_ts")

    def diagnostics(self) -> dict[str, Any]:
        return dict(self._diag)

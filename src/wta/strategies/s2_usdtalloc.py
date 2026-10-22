"""S2 — USDTAlloc (PRD-03 §4): keep an explicit, bounded, regime-driven USDT share of equity.

The challenge is valued in TMN and USDTTMN moves 1–3 %/day, while the cheap USDT pairs need a
USDT balance. Holding USDT is therefore an unavoidable bet on the dollar; S2 makes it explicit:

    regime = plan.regimes["USDTTMN"].regime          (fresh plan only, ai.plan_stale_s)
           | rule_regime(dollar features)             (rules-only fallback, in this order:)
               trend_up   : trend_flag > 0 and ret_24h > +0.8 %
               trend_down : trend_flag < 0 and ret_24h < -0.8 %
               chaos      : atr_15m_pct > 2 x its 24h median
               range      : otherwise
    target = clamp(plan.usdt_target_share (fresh plan) | target_share_by_regime[regime], [25 %, 70 %])

Rebalance when ALL hold: |usdt_share - target| >= rebalance_threshold_pp (15 pp, hysteresis),
>= min_rebalance_interval_s (30 min) since the last rebalance that filled, no open S2 order, and
not within RETRY_AFTER_UNFILLED_S of the last attempt (emitted, or ended unfilled). Never while
flattening. The intent is a maker-first LIMIT on USDTTMN: BUY at best_bid + 1 tick while that
stays below the best ask (else at the best bid), SELL mirrored; notional = |gap| x equity — the
RiskManager caps one order at ``risk.max_order_pct_equity`` (25 %), so a larger gap closes over
several rebalances. The OrderManager re-prices at most twice and then gives up; the next tick
re-evaluates. The very first tick with a USDTTMN book performs the initial allocation from
100 % TMN (the "first trade").

Time only comes from ``ctx.now`` / ``Fill.ts``; the state is a small JSON-able dict (replay).
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any

from wta.config import S2Cfg
from wta.models import Fill, Intent, MarketInfo, MarketPlan, Order, Position
from wta.strategies.base import BaseStrategy, StrategyContext, plan_enabled
from wta.utils.decimalutil import D, fmt, pct, round_price, round_qty
from wta.utils.ids import strategy_prefix

SYMBOL = "USDTTMN"
REGIMES = ("range", "trend_up", "trend_down", "chaos")
RETRY_AFTER_UNFILLED_S = 120.0  # wait after an attempt that did not fill (or never reached the book)
PLAN_EVIDENCE_MAX_CHARS = 160
INITIAL_SHARE_MAX = Decimal("0.01")  # below 1 % USDT the first rebalance is the initial allocation
ZERO = Decimal(0)
HUNDRED = Decimal(100)


# ---------------------------------------------------------------- formatting helpers


def _dec(v: Any) -> Decimal | None:
    if v is None or isinstance(v, bool):
        return None
    try:
        return D(v)
    except (InvalidOperation, ValueError, TypeError):
        return None


def _signed_pct(v: Decimal | None, decimals: int = 2) -> str:
    return "n/a" if v is None else f"{v * HUNDRED:+.{decimals}f}%"


def _pp(v: Decimal, decimals: int = 1, signed: bool = True) -> str:
    """Percentage points: 0.172 -> '+17.2 pp'."""
    return f"{v * HUNDRED:{'+' if signed else ''}.{decimals}f} pp"


def _share(v: Decimal) -> str:
    """A share as a percentage, without noise decimals: 0.65 -> '65%', 0.655 -> '65.5%'."""
    p = v * HUNDRED
    return f"{p:.0f}%" if p == p.to_integral_value() else f"{p:.1f}%"


def _opt_float(v: Any) -> float | None:
    return None if v is None else float(v)


# ---------------------------------------------------------------- regime + target


def rule_regime(dollar: dict[str, Any], cfg: S2Cfg) -> tuple[str, str]:
    """Rules-only dollar regime (PRD-03 §4) and a one-line evidence string with the numbers.

    Order matters: a strong trend wins over high volatility; chaos only when no trend rule fires.
    """
    trend = _dec(dollar.get("trend_flag")) or ZERO
    r24 = _dec(dollar.get("ret_24h"))
    gap = _dec(dollar.get("ema_gap_pct"))
    atr_pct = _dec(dollar.get("atr_15m_pct"))
    atr_med = _dec(dollar.get("atr_15m_pct_median_24h"))
    fast, slow = f"ema{cfg.ema_fast}", f"ema{cfg.ema_slow}"
    if gap is None:
        ema_txt = f"{fast}/{slow} n/a"
    elif gap == 0:
        ema_txt = f"{fast}={slow}"
    else:
        ema_txt = f"{fast}{'>' if gap > 0 else '<'}{slow} by {pct(abs(gap))}"
    numbers = f"{ema_txt}, 24h {_signed_pct(r24)}, ATR15m {pct(atr_pct, 3)} vs 24h median {pct(atr_med, 3)}"
    thr = cfg.trend_ret_24h_pct
    if trend > 0 and r24 is not None and r24 > thr:
        return "trend_up", f"{numbers}; trend_flag +1 and 24h > {_signed_pct(thr)}"
    if trend < 0 and r24 is not None and r24 < -thr:
        return "trend_down", f"{numbers}; trend_flag -1 and 24h < {_signed_pct(-thr)}"
    if atr_pct is not None and atr_med is not None and atr_med > 0 and atr_pct > cfg.chaos_atr_mult * atr_med:
        return "chaos", f"{numbers}; ATR15m > {cfg.chaos_atr_mult}x its 24h median"
    return "range", f"{numbers}; no trend rule (|24h| > {pct(thr)} with trend_flag) and no chaos rule fired"


def _clamp(v: Decimal, lo: Decimal, hi: Decimal) -> Decimal:
    return min(max(v, lo), hi)


# ---------------------------------------------------------------- strategy


class S2USDTAlloc(BaseStrategy):
    id = "S2"

    def __init__(self, cfg: S2Cfg) -> None:
        self.cfg = cfg
        self.last_rebalance_ts: float | None = None  # last rebalance that filled (done time / fill time)
        self.last_attempt_ts: float | None = None  # last intent emitted
        self.last_unfilled_ts: float | None = None  # last S2 order that ended without any fill
        self.last_fill_ts: float | None = None
        self.attempt_filled: Decimal = ZERO  # qty filled on the in-flight attempt (all its placements)
        self.attempts = 0
        self.rebalances = 0
        self._diag: dict[str, Any] = {}

    # ------------------------------------------------------------ decision

    def _regime(self, ctx: StrategyContext, plan: MarketPlan | None) -> tuple[str, str, str, str]:
        """(regime, source, evidence, rules-only regime). The plan is used only when fresh."""
        rules, rules_ev = rule_regime(ctx.features.dollar, self.cfg)
        if plan is not None and SYMBOL in plan.regimes:
            call = plan.regimes[SYMBOL]
            source = f"plan, confidence {call.confidence:.2f}"
            plan_ev = " ".join(call.evidence.split())
            if len(plan_ev) > PLAN_EVIDENCE_MAX_CHARS:
                plan_ev = plan_ev[: PLAN_EVIDENCE_MAX_CHARS - 3] + "..."
            evidence = (f"analyst: {plan_ev}; " if plan_ev else "") + f"rules would say {rules}: {rules_ev}"
            return call.regime, source, evidence, rules
        source = "rules" if plan is None else "rules, plan has no USDTTMN call"
        return rules, source, rules_ev, rules

    def _target(self, regime: str, plan: MarketPlan | None) -> tuple[Decimal, str, Decimal, bool]:
        """(target, where it came from, raw value, clamped?) — always inside cfg.bounds."""
        lo, hi = self.cfg.bounds
        table = self.cfg.target_share_by_regime
        if plan is not None:
            raw, origin = plan.usdt_target_share, "plan"
        else:
            raw = table.get(regime, table["range"])
            origin = f"rules table for {regime}"
        target = _clamp(raw, lo, hi)
        clamped = target != raw
        if clamped:
            origin = f"{origin} asked {_share(raw)}, clamped to bounds [{_share(lo)}, {_share(hi)}]"
        return target, origin, raw, clamped

    @staticmethod
    def _maker_price(market: MarketInfo, side: str, bid: Decimal, ask: Decimal) -> Decimal:
        """BUY at bid + 1 tick while that stays below the ask (else at the bid); SELL mirrored."""
        tick = market.tick
        if side == "BUY":
            return round_price(market, bid + tick if bid + tick < ask else bid, "BUY")
        return round_price(market, ask - tick if ask - tick > bid else ask, "SELL")

    def on_tick(self, ctx: StrategyContext) -> list[Intent]:
        cfg = self.cfg
        if not cfg.enabled:
            self._diag = {"status": "disabled"}
            return []
        if ctx.flatten_phase > 0:
            self._diag = {"status": "flatten", "flatten_phase": ctx.flatten_phase}
            return []
        if not plan_enabled(ctx, self.id):
            self._diag = {"status": "plan_disabled"}
            return []
        book, market = ctx.books.get(SYMBOL), ctx.markets.get(SYMBOL)
        if book is None or market is None or book.best_bid is None or book.best_ask is None:
            self._diag = {"status": "no_book"}
            return []
        bid, ask = book.best_bid, book.best_ask
        if bid >= ask:
            self._diag = {"status": "crossed_book", "bid": str(bid), "ask": str(ask)}
            return []

        plan = ctx.plan_fresh()
        regime, source, evidence, rules = self._regime(ctx, plan)
        target, target_origin, raw_target, clamped = self._target(regime, plan)
        current = ctx.portfolio.usdt_share
        equity = ctx.portfolio.equity_tmn
        gap = target - current
        threshold = cfg.rebalance_threshold_pp
        diag: dict[str, Any] = {
            "regime": regime,
            "regime_source": source,
            "rule_regime": rules,
            "plan_fresh": plan is not None,
            "target": str(target),
            "target_origin": target_origin,
            "current": str(current.quantize(Decimal("0.0001"))),
            "gap_pp": str(gap.quantize(Decimal("0.0001"))),
            "threshold_pp": str(threshold),
            "last_rebalance_ts": self.last_rebalance_ts,
            "rebalances": self.rebalances,
            "attempts": self.attempts,
        }
        self._diag = diag
        if equity <= 0:
            diag["status"] = "no_equity"
            return []
        if abs(gap) < threshold:
            diag["status"] = "within_threshold"
            return []
        if ctx.portfolio.open_orders_of(self.id):
            diag["status"] = "order_open"
            return []
        if (
            self.last_rebalance_ts is not None
            and ctx.now - self.last_rebalance_ts < cfg.min_rebalance_interval_s
        ):
            diag["status"] = "min_interval"
            diag["wait_s"] = round(cfg.min_rebalance_interval_s - (ctx.now - self.last_rebalance_ts), 3)
            return []
        retry_from = max(
            (t for t in (self.last_attempt_ts, self.last_unfilled_ts) if t is not None), default=None
        )
        if retry_from is not None and ctx.now - retry_from < RETRY_AFTER_UNFILLED_S:
            diag["status"] = "retry_wait"
            diag["wait_s"] = round(RETRY_AFTER_UNFILLED_S - (ctx.now - retry_from), 3)
            return []

        side = "BUY" if gap > 0 else "SELL"
        price = self._maker_price(market, side, bid, ask)
        notional_tmn = abs(gap) * equity
        qty = round_qty(market, notional_tmn / price)
        if qty <= 0 or qty < market.min_qty or qty * price < market.min_notional:
            diag["status"] = "below_min_notional"
            diag["qty"] = str(qty)
            return []

        cap_pct = ctx.cfg.risk.max_order_pct_equity
        if self.last_rebalance_ts is None and current < INITIAL_SHARE_MAX:
            timing = "initial allocation from 100% TMN (no S2 rebalance has filled yet)"
        elif self.last_rebalance_ts is None:
            timing = "first S2 rebalance (none has filled yet)"
        else:
            ago = (ctx.now - self.last_rebalance_ts) / 60
            timing = f"last rebalance {ago:.0f} min ago (min {cfg.min_rebalance_interval_s / 60:.0f} min)"
        cap_txt = ""
        if abs(gap) > cap_pct:
            cap_txt = (
                f"; the RiskManager caps one order at {_share(cap_pct)} of equity ({fmt(cap_pct * equity)} TMN), "
                f"the rest of the gap waits for the next rebalance"
            )
        verb = "buy" if side == "BUY" else "sell"
        reason = (
            f"USDTTMN regime={regime} [{source}] ({evidence}); target USDT share {_share(target)} "
            f"({target_origin}) vs current {pct(current, 1)} -> gap {_pp(gap)} >= threshold "
            f"{_pp(threshold, 0, signed=False)}; {timing}; {verb} {fmt(notional_tmn)} TMN of USDT ({qty} USDT, "
            f"{_share(abs(gap))} of equity {fmt(equity)} TMN) as maker at {fmt(price, market.tick_decimals)} "
            f"(bid {fmt(bid, market.tick_decimals)} / ask {fmt(ask, market.tick_decimals)}){cap_txt}."
        )
        intent = Intent(
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
                "rule_regime": rules,
                "plan_fresh": plan is not None,
                "plan_ts": plan.ts if plan is not None else None,
                "target_share": str(target),
                "target_raw": str(raw_target),
                "target_clamped": clamped,
                "target_origin": target_origin,
                "usdt_share": str(current),
                "gap_pp": str(gap),
                "threshold_pp": str(threshold),
                "equity_tmn": str(equity),
                "notional_tmn": str(notional_tmn),
                "max_order_pct_equity": str(cap_pct),
                "bid": str(bid),
                "ask": str(ask),
                "tick": str(market.tick),
                "maker_price": str(price),
                "initial_allocation": self.last_rebalance_ts is None,
                "last_rebalance_ts": self.last_rebalance_ts,
                "dollar": {
                    k: (str(v) if isinstance(v, Decimal) else v)
                    for k, v in sorted(ctx.features.dollar.items())
                    if k in ("trend_flag", "ret_24h", "ema_gap_pct", "atr_15m_pct", "atr_15m_pct_median_24h")
                },
                "books_used": [SYMBOL],
            },
            expected_edge_pct=None,
            taker=False,
        )
        self.last_attempt_ts = ctx.now
        self.attempt_filled = ZERO
        self.attempts += 1
        diag.update({"status": "intent", "side": side, "price": str(price), "qty": str(qty)})
        return [intent]

    # ------------------------------------------------------------ order feedback

    def on_fill(self, fill: Fill, position: Position | None, ctx: StrategyContext) -> list[Intent]:
        """A fill of our rebalance: start the min-interval clock at the fill already."""
        if (
            fill.symbol != SYMBOL
            or fill.qty <= 0
            or not fill.order_client_id.startswith(f"{strategy_prefix(self.id)}_")
        ):
            return []
        self.attempt_filled += fill.qty
        self.last_fill_ts = fill.ts if self.last_fill_ts is None else max(self.last_fill_ts, fill.ts)
        self.last_rebalance_ts = (
            fill.ts if self.last_rebalance_ts is None else max(self.last_rebalance_ts, fill.ts)
        )
        return []

    def on_order_done(self, order: Order, ctx: StrategyContext) -> list[Intent]:
        """Only a rebalance that filled (any placement of it) restarts the min-interval clock."""
        if order.req.strategy != self.id or order.req.symbol != SYMBOL:
            return []
        if order.filled_qty > 0 or self.attempt_filled > 0:
            self.last_rebalance_ts = ctx.now
            self.rebalances += 1
        else:
            self.last_unfilled_ts = ctx.now
        self.attempt_filled = ZERO
        return []

    # ------------------------------------------------------------ persistence

    def state(self) -> dict[str, Any]:
        return {
            "attempt_filled": str(self.attempt_filled),
            "attempts": self.attempts,
            "last_attempt_ts": self.last_attempt_ts,
            "last_fill_ts": self.last_fill_ts,
            "last_rebalance_ts": self.last_rebalance_ts,
            "last_unfilled_ts": self.last_unfilled_ts,
            "rebalances": self.rebalances,
        }

    def load_state(self, state: dict[str, Any] | None) -> None:
        state = state or {}
        self.last_rebalance_ts = _opt_float(state.get("last_rebalance_ts"))
        self.last_attempt_ts = _opt_float(state.get("last_attempt_ts"))
        self.last_unfilled_ts = _opt_float(state.get("last_unfilled_ts"))
        self.last_fill_ts = _opt_float(state.get("last_fill_ts"))
        self.attempt_filled = _dec(state.get("attempt_filled")) or ZERO
        self.attempts = int(state.get("attempts") or 0)
        self.rebalances = int(state.get("rebalances") or 0)

    def diagnostics(self) -> dict[str, Any]:
        return dict(self._diag)

"""S3 — RegimeMomentum (PRD-03 §5), disabled by default; enable after MVP live.

Long-only 15-minute breakout on BTCUSDT/ETHUSDT/SOLUSDT, only when the Regime Analyst says
``trend_up`` with confidence >= 0.6 (or, with ``allow_rules_only``, the rules regime agrees):

    long_breakout = close(15m) > max(high of previous 20 bars)
                    and volume ratio >= 1.5 and 0.3 % <= ATR% <= 3 %
    size  = (0.5 % of equity) / (1.5 x ATR), capped at 20 % of equity
    stop  = entry - 1.5 ATR  (server-side STOP_LIMIT placed by the OrderManager)
    target= entry + 2.5 ATR  (software target in the PositionManager — a resting target SELL next to
            the stop SELL would need the same coins twice), time stop 4 h,
    exit early on regime_flip (plan turns trend_down/chaos).
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from wta.config import S3Cfg
from wta.models import Intent
from wta.strategies.base import BaseStrategy, StrategyContext, plan_enabled, plan_size_multiplier
from wta.utils.decimalutil import fmt, pct, round_price, round_qty

ZERO = Decimal(0)


class S3Momentum(BaseStrategy):
    id = "S3"

    def __init__(self, cfg: S3Cfg, ai_enabled: bool = True) -> None:
        self.cfg = cfg
        self.ai_enabled = ai_enabled
        self.last_bar_seen: dict[str, int] = {}
        self._diag: dict[str, Any] = {}

    def _regime(self, ctx: StrategyContext, sym: str) -> tuple[bool, str]:
        plan = ctx.plan_fresh()
        if plan is not None and sym in plan.regimes:
            call = plan.regimes[sym]
            ok = call.regime == "trend_up" and Decimal(str(call.confidence)) >= self.cfg.min_regime_confidence
            return ok, f"plan regime {call.regime} (conf {call.confidence:.2f})"
        if self.cfg.allow_rules_only:
            f = ctx.features.symbols.get(sym, {})
            r24 = f.get("ret_24h")
            ok = (f.get("trend_flag") or 0) > 0 and r24 is not None and r24 > Decimal("0.008")
            return ok, f"rules regime ({'trend_up' if ok else 'not trend_up'}, 24h {pct(r24)})"
        return False, "no plan regime"

    def on_tick(self, ctx: StrategyContext) -> list[Intent]:
        cfg = self.cfg
        self._diag = {}
        if not cfg.enabled or ctx.flatten_phase > 0 or not plan_enabled(ctx, self.id):
            self._diag["status"] = "disabled" if not cfg.enabled else "flatten/plan"
            return []
        intents = self._regime_flip_exits(ctx)
        if ctx.portfolio.positions_of(self.id) or ctx.portfolio.open_orders_of(self.id):
            self._diag["status"] = "position/order open"
            return intents
        mult = plan_size_multiplier(ctx)
        for sym in sorted(cfg.symbols):
            f = ctx.features.symbols.get(sym)
            m, b = ctx.markets.get(sym), ctx.books.get(sym)
            if not f or m is None or b is None or b.best_bid is None or b.best_ask is None:
                continue
            bar_ts = f.get("last_bar_ts_15m")
            if bar_ts is None or self.last_bar_seen.get(sym) == bar_ts:
                continue  # evaluate once per completed 15-minute bar
            self.last_bar_seen[sym] = bar_ts
            close, hh, vr, atr_pct, atr_abs = (f.get(k) for k in
                                               ("last_close_15m", "hh20_15m", "vol_ratio_15m", "atr_15m_pct", "atr_15m"))
            d = {"close": str(close), "hh20": str(hh), "vol_ratio": str(vr), "atr_pct": str(atr_pct)}
            self._diag[sym] = d
            if None in (close, hh, vr, atr_pct, atr_abs):
                d["skip"] = "warming up"
                continue
            breakout = close > hh and vr >= cfg.volume_mult and cfg.atr_min_pct <= atr_pct <= cfg.atr_max_pct
            regime_ok, regime_txt = self._regime(ctx, sym)
            d.update({"breakout": breakout, "regime": regime_txt})
            if not (breakout and regime_ok) or mult <= 0:
                continue
            u = ctx.portfolio.u_bid
            if not u:
                continue
            equity_usdt = ctx.portfolio.equity_tmn / u
            stop_dist = cfg.stop_atr_mult * atr_abs
            price = b.best_bid + m.tick if b.best_bid + m.tick < b.best_ask else b.best_bid
            price = round_price(m, price, "BUY")
            qty_risk = cfg.risk_per_trade_pct * equity_usdt * mult / stop_dist
            qty_cap = cfg.max_size_pct_equity * equity_usdt / price
            qty = round_qty(m, min(qty_risk, qty_cap))
            if qty <= 0 or qty * price < m.min_notional:
                d["skip"] = "size below min notional"
                continue
            stop = round_price(m, price - stop_dist, "SELL")
            target = round_price(m, price + cfg.target_atr_mult * atr_abs, "SELL")
            reason = (
                f"{sym}: 15m close {fmt(close, m.tick_decimals)} > 20-bar high {fmt(hh, m.tick_decimals)} with volume "
                f"{float(vr):.2f}x average (>= {cfg.volume_mult}) and ATR {pct(atr_pct)} in [{pct(cfg.atr_min_pct, 1)}, "
                f"{pct(cfg.atr_max_pct, 1)}]; {regime_txt}; buy {qty} at {fmt(price, m.tick_decimals)} as maker, "
                f"stop {fmt(stop, m.tick_decimals)} (-{cfg.stop_atr_mult} ATR, server STOP_LIMIT), target "
                f"{fmt(target, m.tick_decimals)} (+{cfg.target_atr_mult} ATR), time stop {int(cfg.max_hold_s)} s; "
                f"risk {pct(cfg.risk_per_trade_pct, 1)} of equity."
            )
            intents.append(Intent(
                id=ctx.next_id(self.id, sym), ts=ctx.now, strategy=self.id, symbol=sym, side="BUY",
                order_type="LIMIT", qty=qty, notional=qty * price, price=price, stop_price=stop, purpose="entry",
                reason=reason,
                features={"close_15m": str(close), "hh20_15m": str(hh), "vol_ratio_15m": str(vr),
                          "atr_15m_pct": str(atr_pct), "atr_pct": str(atr_pct), "target_price": str(target),
                          "time_stop_s": cfg.max_hold_s, "regime": regime_txt, "cost_model": "roundtrip",
                          "margin_min_pct": "0", "books_used": [sym, "USDTTMN"]},
                expected_edge_pct=(target - price) / price, taker=False,
            ))
            break  # one S3 entry at a time
        return intents

    def _regime_flip_exits(self, ctx: StrategyContext) -> list[Intent]:
        plan = ctx.plan_fresh()
        out: list[Intent] = []
        if plan is None:
            return out
        for pos in ctx.portfolio.positions_of(self.id):
            call = plan.regimes.get(pos.symbol)
            if call is None or call.regime not in ("trend_down", "chaos") or pos.meta.get("exit_pending"):
                continue
            out.append(Intent(
                id=ctx.next_id(self.id, pos.symbol), ts=ctx.now, strategy=self.id, symbol=pos.symbol, side="SELL",
                order_type="LIMIT", qty=pos.qty, purpose="exit",
                reason=f"{pos.symbol}: plan regime flipped to {call.regime} (conf {call.confidence:.2f}) -> exit {pos.qty}.",
                features={"exit_reason": "regime_flip", "position_id": pos.id, "books_used": [pos.symbol]},
                linked_position_id=pos.id, taker=True,
            ))
        return out

    def state(self) -> dict[str, Any]:
        return {"last_bar_seen": dict(sorted(self.last_bar_seen.items()))}

    def load_state(self, state: dict[str, Any] | None) -> None:
        self.last_bar_seen = {k: int(v) for k, v in ((state or {}).get("last_bar_seen") or {}).items()}

    def diagnostics(self) -> dict[str, Any]:
        return self._diag

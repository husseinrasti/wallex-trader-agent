"""S3 — RegimeMomentum (PRD-03 §5). Disabled by default (``enabled: false``); enable after MVP live.

Thesis: USDT-pair fees are cheap (0.10 % maker / 0.13 % taker), so a conservative 15-minute breakout
on BTCUSDT/ETHUSDT/SOLUSDT is worth taking when the Regime Analyst confirms an up-trend. Spot only,
so long only; at most one S3 position in total; a small fixed risk per trade; a server-side stop.

Evaluated **once per completed, settled 15-minute bar** per symbol (bars resampled from 1-minute
candles by the FeatureEngine)::

    breakout  = close > hh20 (max high of the previous 20 completed bars)
                and vol_ratio = bar volume / mean volume of those 20 bars >= 1.5
                and 0.3 % <= ATR(14) / close <= 3 %
    regime_ok = fresh plan: regimes[sym].regime == trend_up and confidence >= 0.6
                | rules-only (only with ``allow_rules_only``, when the fresh plan has no call for the
                  symbol or there is no fresh plan): trend_flag > 0 and 24h return > S2's trend threshold
    qty       = min(risk_per_trade_pct x equity_usdt / (1.5 ATR),
                    max_size_pct_equity x equity_usdt / price) x plan size multiplier,
                capped by the free USDT (less the risk balance buffer, incl. the maker fee)
    entry     = maker-first LIMIT BUY at best bid + maker_offset_ticks (the OrderManager re-prices)
    stop      = entry - 1.5 ATR -> ``Intent.stop_price``: the OrderManager places a server STOP_LIMIT
    target    = entry + 2.5 ATR -> ``features.target_price``: a *software* target watched by the
                PositionManager (a resting target SELL next to the resting stop SELL would need the
                same coins twice — see DECISIONS.md)
    time stop = 4 h             -> ``features.time_stop_s`` (PositionManager)
    regime_flip exit: the fresh plan turns trend_down/chaos for a held symbol -> crossing LIMIT SELL.

``expected_edge_pct`` is the target distance, so the RiskManager's EDGE_VS_COST passes only when the
target covers the round trip (maker entry + taker exit + spread).

Bar bookkeeping:

* A bar is *settled* once a 1-minute candle of the next bar is in ``ctx.candles[(sym, "1")]`` (the
  candle poller re-reads the last stored minute every round, so the bar's last minute is then final)
  or two candle-poll intervals after the bar closed. Before that its last minute may be partial.
* A bar that closed a full bar length ago or more is stale (candle feed behind) and is skipped.
* A settled bar is consumed once evaluated — also when the entry is blocked (an S3 position or
  order is open, another strategy owns the symbol, the regime gate is closed). A signal is never
  acted on late, and a vetoed or unfilled entry is not retried on the same bar.
* With several breakouts on the same bar the strongest one (breakout distance in ATR units, then
  symbol name) is taken; the others are named in the reason.

Regime-flip exits are emitted for S3's own positions even when S3 is disabled by config or by the
plan (they only reduce risk), but not while flattening (the flatten routine owns every position),
not while a closing SELL is live (``exit_pending``), and not when the PositionManager's own stop,
target or time stop already fires for the position this tick.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from wta.config import S3Cfg
from wta.models import Intent, MarketInfo, MarketPlan, Position
from wta.strategies.base import BaseStrategy, StrategyContext, plan_enabled, plan_size_multiplier
from wta.utils.decimalutil import D, check_min_notional, fmt, pct, quantize_down, round_price, round_qty

ZERO = Decimal(0)
ONE = Decimal(1)
BAR_S = 900  # the FeatureEngine resamples 1-minute candles into 15-minute bars
SETTLE_POLLS = 2  # a closed bar is final after this many candle-poll intervals at the latest
EXIT_REGIMES = frozenset({"trend_down", "chaos"})
USDT = "USDT"
USDTTMN = "USDTTMN"


@dataclass(frozen=True)
class RegimeGate:
    ok: bool
    source: str  # "plan" | "rules" | "none"
    regime: str  # plan regime, "trend_up"/"not_trend_up" (rules) or "none"
    confidence: Decimal | None
    text: str  # sentence fragment for the reason


@dataclass(frozen=True)
class EntryPlan:
    """Every number behind one S3 entry (reason, features and the intent are built from it)."""

    symbol: str
    market: MarketInfo
    bar_ts: int
    close: Decimal
    hh20: Decimal
    vol_ratio: Decimal
    atr: Decimal
    atr_pct: Decimal
    strength_atr: Decimal  # (close - hh20) / ATR
    regime: RegimeGate
    price: Decimal
    qty: Decimal
    stop: Decimal
    target: Decimal
    equity_usdt: Decimal
    stop_dist: Decimal
    qty_risk: Decimal
    qty_cap: Decimal
    qty_free: Decimal
    size_mult: Decimal
    binding: str  # which limit set the size: risk | max_size | free_usdt

    @property
    def edge(self) -> Decimal:
        return (self.target - self.price) / self.price


class S3Momentum(BaseStrategy):
    id = "S3"

    def __init__(self, cfg: S3Cfg, ai_enabled: bool = True) -> None:
        self.cfg = cfg
        self.ai_enabled = ai_enabled  # False (rules-only ablation): the plan's regimes are never read
        self.last_bar_seen: dict[str, int] = {}  # symbol -> open time of the last evaluated 15m bar
        self._evals: dict[str, dict[str, Any]] = {}  # latest evaluation per symbol (diagnostics only)
        self._status = "idle"

    # ------------------------------------------------------------ tick

    def on_tick(self, ctx: StrategyContext) -> list[Intent]:
        cfg = self.cfg
        if ctx.flatten_phase > 0:
            self._status = "flatten phase: no S3 decisions"
            return []
        intents = self._regime_flip_exits(ctx)
        if not cfg.enabled:
            self._status = "disabled by config"
            return intents
        if not plan_enabled(ctx, self.id):
            self._status = "disabled by the plan (S3 not in enabled_strategies)"
            return intents
        blocked = self._blocked(ctx)
        mult = plan_size_multiplier(ctx)
        found: list[EntryPlan] = []
        for sym in sorted(set(cfg.symbols)):
            entry = self._evaluate(ctx, sym, blocked, mult)
            if entry is not None:
                found.append(entry)
        if not found:
            self._status = blocked or "watching"
            return intents
        found.sort(key=lambda e: (-e.strength_atr, e.symbol))
        best, others = found[0], [e.symbol for e in found[1:]]
        for e in found[1:]:
            self._evals[e.symbol]["status"] = f"breakout, but {best.symbol} is stronger (one S3 position at a time)"
        intents.append(self._entry_intent(ctx, best, others))
        self._status = f"entry {best.symbol}"
        return intents

    def _blocked(self, ctx: StrategyContext) -> str | None:
        """Why no S3 entry may start now (global cap: one S3 position or entry at a time)."""
        positions = sorted(ctx.portfolio.positions_of(self.id), key=lambda p: p.id)
        if positions:
            return f"S3 position {positions[0].id} is open (max 1 S3 position)"
        orders = sorted(ctx.portfolio.open_orders_of(self.id), key=lambda o: o.client_id)
        if orders:
            return f"S3 order {orders[0].client_id} is open"
        return None

    @staticmethod
    def _owner(ctx: StrategyContext, sym: str) -> str | None:
        """Another strategy holding or working ``sym`` (the router would drop our intent)."""
        for p in sorted(ctx.portfolio.positions, key=lambda p: p.id):
            if p.symbol == sym and p.strategy != S3Momentum.id:
                return p.strategy
        for o in sorted(ctx.portfolio.open_orders, key=lambda o: o.client_id):
            if o.req.symbol == sym and o.req.strategy != S3Momentum.id:
                return o.req.strategy
        return None

    def _plan(self, ctx: StrategyContext) -> MarketPlan | None:
        return ctx.plan_fresh() if self.ai_enabled else None

    @staticmethod
    def _settled(ctx: StrategyContext, sym: str, bar_end: int, age: float) -> bool:
        if age >= SETTLE_POLLS * ctx.cfg.data.candle_poll_interval_s:
            return True
        one = ctx.candles.get((sym, "1")) or []
        return bool(one) and max(c.ts for c in one) >= bar_end

    # ------------------------------------------------------------ signal

    def _evaluate(self, ctx: StrategyContext, sym: str, blocked: str | None, mult: Decimal) -> EntryPlan | None:
        cfg = self.cfg
        f = ctx.features.symbols.get(sym) or {}
        raw_ts = f.get("last_bar_ts_15m")
        if raw_ts is None:
            self._evals[sym] = {"status": "no completed 15m bar yet"}
            return None
        bar_ts = int(raw_ts)
        if bar_ts <= self.last_bar_seen.get(sym, -1):
            return None  # this bar was already evaluated: once per completed bar
        bar_end = bar_ts + BAR_S
        age = ctx.now - bar_end
        ev: dict[str, Any] = {"bar_ts": bar_ts, "bar_age_s": round(age, 3)}
        self._evals[sym] = ev
        if age >= BAR_S:
            self.last_bar_seen[sym] = bar_ts
            ev["status"] = f"stale bar (closed {age:.0f}s ago, no newer completed bar)"
            return None
        if not self._settled(ctx, sym, bar_end, age):
            ev["status"] = "waiting for the bar's last minute to settle"
            return None
        m, b, u = ctx.markets.get(sym), ctx.books.get(sym), ctx.portfolio.u_bid
        if m is None or b is None or b.best_bid is None or b.best_ask is None or not u:
            ev["status"] = "no market, two-sided book or USDTTMN bid yet"
            return None
        self.last_bar_seen[sym] = bar_ts
        if m.quote != USDT:
            ev["status"] = f"{sym} is not a USDT pair (S3 sizes in USDT)"
            return None
        close, hh, vr, atr_pct, atr = (
            f.get(k) for k in ("last_close_15m", "hh20_15m", "vol_ratio_15m", "atr_15m_pct", "atr_15m")
        )
        ev.update({"close": _s(close), "hh20": _s(hh), "vol_ratio": _s(vr), "atr_pct": _s(atr_pct)})
        if close is None or hh is None or vr is None or atr_pct is None or atr is None:
            ev["status"] = (
                f"warming up (needs {cfg.breakout_lookback + 1} completed 15m bars and ATR({cfg.atr_period}))"
            )
            return None
        fails: list[str] = []
        if close <= hh:
            fails.append(f"close {close} <= hh20 {hh}")
        if vr < cfg.volume_mult:
            fails.append(f"volume ratio {vr:.2f} < {cfg.volume_mult}")
        if not cfg.atr_min_pct <= atr_pct <= cfg.atr_max_pct:
            fails.append(f"ATR {pct(atr_pct, 3)} outside [{pct(cfg.atr_min_pct, 1)}, {pct(cfg.atr_max_pct, 1)}]")
        gate = self._regime(ctx, sym, f)
        ev.update({"breakout": not fails, "regime": gate.text})
        if fails:
            ev["status"] = "no breakout: " + "; ".join(fails)
            return None
        if not gate.ok:
            ev["status"] = f"breakout, but regime gate closed: {gate.text}"
            return None
        if blocked:
            ev["status"] = f"breakout, but {blocked}"
            return None
        owner = self._owner(ctx, sym)
        if owner is not None:
            ev["status"] = f"breakout, but {sym} is held or worked by {owner}"
            return None
        if mult <= 0:
            ev["status"] = "breakout, but the plan size multiplier is 0"
            return None
        return self._size(ctx, sym, m, b.best_bid, b.best_ask, u, bar_ts, close, hh, vr, atr, atr_pct, gate, mult)

    def _regime(self, ctx: StrategyContext, sym: str, f: dict[str, Any]) -> RegimeGate:
        min_conf = self.cfg.min_regime_confidence
        plan = self._plan(ctx)
        if plan is not None and sym in plan.regimes:
            call = plan.regimes[sym]
            conf = D(call.confidence)
            ok = call.regime == "trend_up" and conf >= min_conf
            cmp = ">=" if conf >= min_conf else "<"
            text = f"plan regime {call.regime} with confidence {conf:.2f} ({cmp} min {min_conf:.2f})"
            return RegimeGate(ok, "plan", call.regime, conf, text)
        if self.cfg.allow_rules_only:
            threshold = ctx.cfg.strategies.s2_usdtalloc.trend_ret_24h_pct
            trend = f.get("trend_flag") or 0
            r24 = f.get("ret_24h")
            ok = trend > 0 and r24 is not None and r24 > threshold
            regime = "trend_up" if ok else "not_trend_up"
            why = "no fresh plan" if plan is None else "no plan call for the symbol"
            text = (
                f"rules-only regime {regime} ({why}; ema20 vs ema60 {pct(f.get('ema_gap_pct'))}, trend flag "
                f"{trend}, 24h return {pct(r24)} vs > {pct(threshold, 1)})"
            )
            return RegimeGate(ok, "rules", regime, None, text)
        why = "no fresh plan" if plan is None else f"the fresh plan has no regime call for {sym}"
        return RegimeGate(False, "none", "none", None, f"{why} and rules-only mode is off")

    # ------------------------------------------------------------ sizing + intent

    def _size(
        self,
        ctx: StrategyContext,
        sym: str,
        m: MarketInfo,
        bid: Decimal,
        ask: Decimal,
        u_bid: Decimal,
        bar_ts: int,
        close: Decimal,
        hh: Decimal,
        vr: Decimal,
        atr: Decimal,
        atr_pct: Decimal,
        gate: RegimeGate,
        mult: Decimal,
    ) -> EntryPlan | None:
        cfg, ev = self.cfg, self._evals[sym]
        offset = max(ctx.cfg.execution.maker_offset_ticks, 0) * m.tick
        price = bid + offset if bid + offset < ask else bid
        price = round_price(m, price, "BUY")
        equity_usdt = ctx.portfolio.equity_tmn / u_bid
        stop_dist = cfg.stop_atr_mult * atr
        qty_risk = cfg.risk_per_trade_pct * equity_usdt / stop_dist
        qty_cap = cfg.max_size_pct_equity * equity_usdt / price
        free = ctx.portfolio.free.get(USDT, ZERO)
        usable = max(free * (ONE - ctx.cfg.risk.balance_buffer_pct), ZERO)
        qty_free = usable / (price * (ONE + m.maker_fee))
        qty_plan = min(qty_risk, qty_cap) * mult
        binding = "risk" if qty_risk <= qty_cap else "max_size"
        if qty_free < qty_plan:
            binding = "free_usdt"
        qty = round_qty(m, min(qty_plan, qty_free))
        floor = m.min_notional if m.min_notional > 0 else ctx.cfg.risk.min_notional_usdt
        if qty <= 0 or not check_min_notional(m, qty, price) or qty * price < floor:
            ev["status"] = (
                f"breakout, but size {qty} x {fmt(price, m.tick_decimals)} is below the minimum "
                f"({floor} USDT notional, min qty {m.min_qty}); free USDT {free}"
            )
            return None
        stop = round_price(m, price - stop_dist, "SELL")
        target = round_price(m, price + cfg.target_atr_mult * atr, "SELL")
        ev["status"] = "breakout: entry"
        return EntryPlan(
            symbol=sym, market=m, bar_ts=bar_ts, close=close, hh20=hh, vol_ratio=vr, atr=atr, atr_pct=atr_pct,
            strength_atr=(close - hh) / atr, regime=gate, price=price, qty=qty, stop=stop, target=target,
            equity_usdt=equity_usdt, stop_dist=stop_dist, qty_risk=qty_risk, qty_cap=qty_cap, qty_free=qty_free,
            size_mult=mult, binding=binding,
        )

    def _entry_intent(self, ctx: StrategyContext, e: EntryPlan, others: list[str]) -> Intent:
        cfg = self.cfg
        features: dict[str, Any] = {
            "bar_ts_15m": e.bar_ts,
            "close_15m": str(e.close),
            "hh20_15m": str(e.hh20),
            "vol_ratio_15m": str(e.vol_ratio),
            "atr_15m": str(e.atr),
            "atr_15m_pct": str(e.atr_pct),
            "atr_pct": str(e.atr_pct),
            "breakout_atr": str(e.strength_atr),
            "regime": e.regime.regime,
            "regime_source": e.regime.source,
            "regime_confidence": _s(e.regime.confidence),
            "entry_price": str(e.price),
            "stop_price": str(e.stop),
            "stop_dist": str(e.stop_dist),
            "target_price": str(e.target),
            "time_stop_s": cfg.max_hold_s,
            "equity_usdt": str(e.equity_usdt),
            "qty_risk": str(e.qty_risk),
            "qty_cap": str(e.qty_cap),
            "qty_free": str(e.qty_free),
            "size_binding": e.binding,
            "plan_size_multiplier": str(e.size_mult),
            "other_breakouts": others,
            "cost_model": "roundtrip",
            "margin_min_pct": "0",
            "books_used": [e.symbol, USDTTMN],
        }
        return Intent(
            id=ctx.next_id(self.id, e.symbol), ts=ctx.now, strategy=self.id, symbol=e.symbol, side="BUY",
            order_type="LIMIT", qty=e.qty, notional=e.qty * e.price, price=e.price, stop_price=e.stop,
            purpose="entry", reason=_entry_reason(cfg, e, others), features=features,
            expected_edge_pct=e.edge, taker=False,
        )

    # ------------------------------------------------------------ exits

    def _regime_flip_exits(self, ctx: StrategyContext) -> list[Intent]:
        plan = self._plan(ctx)
        positions = sorted(ctx.portfolio.positions_of(self.id), key=lambda p: p.id)
        out: list[Intent] = []
        if plan is None or not positions:
            return out
        for pos in positions:
            call = plan.regimes.get(pos.symbol)
            if call is None or call.regime not in EXIT_REGIMES or pos.qty <= 0 or pos.meta.get("exit_pending"):
                continue
            book = ctx.books.get(pos.symbol)
            bid = book.best_bid if book is not None else None
            if bid is None or _position_manager_exit(pos, bid, ctx.now) is not None:
                continue  # no price to cross at, or the PositionManager's own exit fires this tick
            m = ctx.markets.get(pos.symbol)
            d = m.tick_decimals if m is not None else None
            upnl = bid / pos.entry_price - ONE if pos.entry_price else None
            reason = (
                f"{pos.symbol}: the analyst plan of {ctx.now - plan.ts:.0f}s ago now calls {call.regime} "
                f"(confidence {call.confidence:.2f}) and S3 only holds in trend_up, so position {pos.id} "
                f"exits on regime flip: sell {pos.qty} {pos.base} (bought at {fmt(pos.entry_price, d)}, bid now "
                f"{fmt(bid, d)}, {pct(upnl)} before fees; stop {fmt(pos.stop_price, d)}, target "
                f"{fmt(pos.target_price, d)}) as a LIMIT crossing at the bid; the OrderManager cancels the "
                f"resting server stop first."
            )
            out.append(Intent(
                id=ctx.next_id(self.id, pos.symbol), ts=ctx.now, strategy=self.id, symbol=pos.symbol, side="SELL",
                order_type="LIMIT", qty=pos.qty, purpose="exit", reason=reason,
                features={
                    "exit_reason": "regime_flip",
                    "position_id": pos.id,
                    "regime": call.regime,
                    "regime_confidence": str(D(call.confidence)),
                    "plan_ts": plan.ts,
                    "bid": str(bid),
                    "entry_price": str(pos.entry_price),
                    "books_used": [pos.symbol],
                },
                linked_position_id=pos.id, taker=True,
            ))
        return out

    # ------------------------------------------------------------ state

    def state(self) -> dict[str, Any]:
        return {"last_bar_seen": dict(sorted(self.last_bar_seen.items()))}

    def load_state(self, state: dict[str, Any] | None) -> None:
        self.last_bar_seen = {str(k): int(v) for k, v in ((state or {}).get("last_bar_seen") or {}).items()}

    def diagnostics(self) -> dict[str, Any]:
        return {"status": self._status, "symbols": {s: dict(self._evals[s]) for s in sorted(self._evals)}}


# ---------------------------------------------------------------- helpers


def _s(v: Any) -> str | None:
    return None if v is None else str(v)


def _position_manager_exit(pos: Position, bid: Decimal, now: float) -> str | None:
    """The PositionManager exit (stop / target / time stop) that this tick's bid already triggers.

    A stop hit with a resting server stop is left to that stop; either way no second SELL is needed.
    """
    if pos.stop_price is not None and bid <= pos.stop_price:
        return "stop"
    if pos.target_price is not None and bid >= pos.target_price:
        return "target"
    if pos.time_stop_ts is not None and now >= pos.time_stop_ts:
        return "time_stop"
    return None


def _entry_reason(cfg: S3Cfg, e: EntryPlan, others: list[str]) -> str:
    m = e.market
    d = m.tick_decimals

    def q(x: Decimal) -> str:
        return format(quantize_down(x, m.step_decimals), "f")

    when = datetime.fromtimestamp(e.bar_ts, tz=UTC).strftime("%Y-%m-%d %H:%M")
    caps = {
        "risk": "the risk budget",
        "max_size": f"the {pct(cfg.max_size_pct_equity, 0)}-of-equity cap",
        "free_usdt": "the free USDT",
    }
    text = (
        f"{e.symbol}: the 15m bar of {when} UTC closed at {fmt(e.close, d)}, above the previous "
        f"{cfg.breakout_lookback} bars' high {fmt(e.hh20, d)} (+{pct(e.close / e.hh20 - ONE, 3)}, "
        f"{e.strength_atr:.2f} ATR), on {e.vol_ratio:.2f}x their average volume (>= {cfg.volume_mult}x), with "
        f"ATR({cfg.atr_period}) {fmt(e.atr, d)} = {pct(e.atr_pct, 3)} of the close inside "
        f"[{pct(cfg.atr_min_pct, 1)}, {pct(cfg.atr_max_pct, 1)}]; {e.regime.text}. "
        f"Buy {format(e.qty, 'f')} {m.base} at {fmt(e.price, d)} as maker ({fmt(e.qty * e.price, 2)} USDT): risk "
        f"{pct(cfg.risk_per_trade_pct)} of {fmt(e.equity_usdt, 2)} USDT equity / stop distance "
        f"{fmt(e.stop_dist, d)} ({cfg.stop_atr_mult} ATR) = {q(e.qty_risk)}, cap "
        f"{pct(cfg.max_size_pct_equity, 0)} of equity = {q(e.qty_cap)}, free USDT allows {q(e.qty_free)}, "
        f"plan size mult {e.size_mult:.2f} -> sized by {caps[e.binding]}. "
        f"Exits: server STOP_LIMIT stop {fmt(e.stop, d)} (entry - {cfg.stop_atr_mult} ATR), software target "
        f"{fmt(e.target, d)} (entry + {cfg.target_atr_mult} ATR = +{pct(e.edge)} expected edge), time stop "
        f"{cfg.max_hold_s / 3600:g} h, or a plan regime flip to trend_down/chaos."
    )
    if others:
        text += f" Also breaking out on this bar (weaker in ATR units, one S3 position at a time): {', '.join(others)}."
    return text

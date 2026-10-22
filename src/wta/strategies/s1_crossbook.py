"""S1 — CrossBook (PRD-03 §3): trade the gap between a coin's TMN book and its USDT book.

Wallex prices X/TMN and X/USDT with different participants; during fast moves one book lags.
Implied USDT price of the TMN book = XTMN / USDTTMN. When one book is cheaper than the other by
more than fees + margin we:

* **Lock-in** (preferred, direction-neutral): buy in the cheap book (leg 1, taker by default),
  then sell the same coin quantity in the expensive book (leg 2, crossing LIMIT).
      TMN cheap : edge = bid(XUSDT) * bid(USDTTMN) / ask(XTMN) - 1,  cost = fee1(XTMN) + taker(XUSDT)
      USDT cheap: edge = bid(XTMN) / (ask(XUSDT) * ask(USDTTMN)) - 1, cost = fee1(XUSDT) + taker(XTMN)
      enter when edge >= cost + margin_min (+ the unhedged-remainder charge, see Sizing),
      margin_min = max(config, fresh plan).
* **Revert** (bounded directional): when no lock-in edge exists but the mid deviation is large vs
  its 1-h noise (|z| >= z_min, z and the deviation on the same side), buy the cheap book only and
  sell it back in the same book once ``revert_target_fraction`` (f) of its gap to the 1-h mean has
  closed: target = mean + (dev - mean) x (1 - f). The trade is only worth the part the exit
  captures, so the gate and ``expected_edge_pct`` are ``f x |dev - mean|`` (not the whole |dev|),
  which must be >= the same-book round trip (maker + taker + spread) + margin. Stop = entry x (1 -
  revert_stop_pct) (server-side STOP_LIMIT placed by the OrderManager), time stop
  ``revert_max_hold_s``. Never in a ``chaos`` regime (fresh plan per symbol, else the rules-only
  ATR test of PRD-03 §4).

Entry gates (per coin, every tick, the "why no trade" lands in ``diagnostics()``): strategy enabled
by config and the fresh plan, plan size multiplier > 0, all three books fresh (<= stale_data_s),
coin cooldown, both symbols free (no position or open order of any strategy), ``max_concurrent``
S1 cycles (positions + working entries) and ``revert_max_concurrent`` revert/degraded cycles.
Candidates are ranked lock-in first, then by edge above the requirement, then by coin.

Boundary: ``edge == need`` enters (``>=``, the same comparison as the RiskManager's EDGE_VS_COST,
whose need = cost + margin is never above S1's), so an entry S1 emits is never vetoed for its edge.

Sizing: min(size_pct_equity x equity x plan size multiplier, 80 % of the best level of both legs
(leg-1 ask, leg-2 bid; also for a maker leg 1), free quote of leg 1 less the balance buffer incl.
the fee). A leg-1 BUY pays its fee in the coin, so leg 1 buys ``q1`` and leg 2 can sell only
``q2 = q1 x (1 - fee1)`` rounded down to the leg-2 step: ``lockin_leg_qtys`` (shared with the
RiskManager) picks the smallest leg-1 step multiple whose coins after the fee cover ``q2``. The
minimum quantity/notional must hold for q1 on leg 1 and for q2 on leg 2 (at its bid). The coins
leg 2 cannot sell, ``u = (q1 x (1 - fee1) - q2) / q1`` of the cycle (below one exchange step: at
~1M TMN about 0.4 % for ETH, 2 % for BTC, 4.5 % for DOGE), stay unhedged; they are priced in as
``u x UNHEDGED_STRESS_PCT`` (a 3 % adverse move) on top of cost + margin: ``need = cost + margin +
u x 3 %`` (``features.need_pct``). ``features.sizing`` names the binding limit; a lock-in edge that
cannot be sized does not fall back to revert.

Hedging (leg 2) is derived, never remembered
--------------------------------------------
Emitting a leg-2 intent does not hedge anything: the RiskManager may veto it (stale data,
balance, ...). So S1 never books quantity as "hedged" when it emits; it derives the state from the
portfolio each time it is asked:

    unhedged(cycle) = position.qty (lock-in coins still held)
                      - remaining qty of our open S1 SELL orders in the leg-2 symbol
                      - leg-2 intents emitted in this same tick that are not orders yet

* ``on_fill`` (a leg-1 BUY fill) emits leg 2 at once for every unhedged coin: the new fill plus any
  earlier remainder (fills too small for the leg-2 minimum accumulate until they are not).
* ``on_tick`` re-emits leg 2 for the whole unhedged remainder of every lock-in position that has no
  open leg-2 order: a vetoed, rejected or expired leg 2 is retried on the next tick. The
  OrderManager never re-crosses leg 2 itself (it gives an unfilled one up after
  ``limit_reprice_after_s``), so every leg-2 price passes the loss-limit check below first.
* While the cycle lasts its leg-2 book is reserved for S1 (``base.symbol_owners``): no other
  strategy may rest an order where leg 2 must cross.
* Never two leg-2 SELLs for the same coins (the RiskManager exempts leg 2 from its duplicate
  rule, so S1 guarantees it): an emitted intent covers its coins only during the tick it was
  emitted in (half a fast-loop interval); after that only real open orders count.
* If leg 2 would realise a loss >= ``leg2_max_loss_pct`` (cycle edge after fees; equality degrades,
  the PRD completes leg 2 only below the limit), the cycle degrades to revert mode
  (``lockin_degraded_to_revert``): a protective STOP_LIMIT intent, stop = entry x (1 -
  revert_stop_pct), ``features.time_stop_s`` = revert_max_hold_s (the OrderManager sets the
  position's stop price and time stop from it). It covers the coins no leg-2 order is selling
  already; it is re-emitted while the position has no stop (a veto) and no exit is going out.
  Then revert-mode exits apply: the deviation target comes from the position meta, else from the
  cycle (recorded at the lock-in entry: mean + (dev - mean) x (1 - f), with the 1-h mean of that
  moment, or dev x (1 - f) before the z-score has warmed up). When the degrade came from the leg-2 book moving, the gap
  has usually closed from that side, so the coins are sold back in the leg-1 book (maker-first)
  right away; when both books moved, S1 waits for the gap to close under the stop and time stop.
  Exits are evaluated before stop re-emission: a reached target beats re-sending a vetoed stop.
* A remainder below the leg-2 minimum, once leg 1 is no longer working, is sold back in the leg-1
  book (it would otherwise block the coin and a ``max_concurrent`` slot forever).

Pure: reads only ``ctx`` and its own JSON-able state (``state()``/``load_state()``); no clock, no
I/O, no randomness; dicts walked in sorted order. During a flatten phase S1 emits nothing at all:
the flatten routine owns every coin sale then.
"""

from __future__ import annotations

import copy
from decimal import Decimal
from typing import Any

from wta.config import S1Cfg
from wta.models import Fill, Intent, MarketInfo, Order, Position
from wta.risk.manager import lockin_leg_qtys
from wta.strategies.base import BaseStrategy, StrategyContext, plan_enabled, plan_size_multiplier
from wta.utils.decimalutil import D, fmt, pct, quantize_down, round_price

ZERO = Decimal(0)
ONE = Decimal(1)
USDTTMN = "USDTTMN"
LEG_LIQ_FRACTION = Decimal("0.8")  # at most 80 % of each leg's best level (PRD-03 §3 sizing)
STOP_TYPES = frozenset({"STOP_LIMIT", "STOP_MARKET"})
PENDING_TICK_FRACTION = 0.5  # an emitted intent covers its coins for half a fast-loop interval
Z_MIN_SAMPLES = 30  # FeatureEngine needs this many prior deviations for a z-score
# The lock-in coins leg 2 cannot sell (below one leg-2 step after the leg-1 fee, which a BUY pays in
# the coin) stay unhedged: their share of the cycle x this adverse move is added to the need.
UNHEDGED_STRESS_PCT = Decimal("0.03")


def _cid(pos: Position) -> str:
    """Cycle id of an S1 position: its entry decision id (= the entry intent id), else its own id."""
    return str(pos.meta.get("entry_decision_id") or pos.id)


def _jsonable(cf: dict[str, Any]) -> dict[str, Any]:
    return {k: (str(v) if isinstance(v, Decimal) else v) for k, v in sorted(cf.items())}


def _plain(x: Decimal) -> str:
    """A Decimal without exponent or trailing zeros (0.000000250 -> 0.00000025)."""
    return format(x.normalize(), "f") if x != 0 else "0"


def _s(x: Decimal | None, places: str = "0.0000001") -> str | None:
    """Compact, stable string for diagnostics."""
    return None if x is None else str(x.quantize(Decimal(places)))


def _min_notional(m: MarketInfo, ctx: StrategyContext) -> Decimal:
    """Exchange minNotional, else the config fallback (mirrors the RiskManager's MIN_NOTIONAL)."""
    if m.min_notional > 0:
        return m.min_notional
    return ctx.cfg.risk.min_notional_usdt if m.quote == "USDT" else ctx.cfg.risk.min_notional_tmn


def revert_target(dev: Decimal, mean: Decimal | None, fraction: Decimal) -> Decimal:
    """Deviation at which ``fraction`` of the gap between ``dev`` and its 1-h ``mean`` has closed
    (``mean`` None, e.g. before the z-score warmed up: the gap to 0)."""
    m = mean if mean is not None else ZERO
    return m + (dev - m) * (ONE - fraction)


def _size_problem(m: MarketInfo, qty: Decimal, price: Decimal, ctx: StrategyContext) -> str | None:
    """Why ``qty`` at ``price`` is not a valid order in ``m`` (None when it is)."""
    q = quantize_down(max(qty, ZERO), m.step_decimals)
    if q <= 0:
        return f"{m.symbol} qty rounds down to 0 at step {m.step}"
    if q < m.min_qty:
        return f"{m.symbol} qty {q} < min qty {m.min_qty}"
    mn = _min_notional(m, ctx)
    if q * price < mn:
        return (
            f"{m.symbol} notional {q} x {fmt(price, m.tick_decimals)} = {fmt(q * price, 2)} {m.quote} "
            f"< min {fmt(mn, 2)} {m.quote}"
        )
    return None


class S1CrossBook(BaseStrategy):
    id = "S1"

    def __init__(self, cfg: S1Cfg) -> None:
        self.cfg = cfg
        self.last_entry_ts: dict[str, float] = {}
        # cycle id (= entry intent id = position.meta.entry_decision_id) -> cycle record (JSON-able)
        self.cycles: dict[str, dict[str, Any]] = {}
        self._diag: dict[str, Any] = {}

    # ------------------------------------------------------------ small helpers

    def _margin(self, ctx: StrategyContext) -> Decimal:
        plan = ctx.plan_fresh()
        m = self.cfg.margin_min_pct
        if plan is not None:
            m = max(m, plan.s1_margin_min_pct)
        return m

    def _margin_text(self, ctx: StrategyContext, margin: Decimal) -> str:
        plan = ctx.plan_fresh()
        if plan is None:
            return f"min {pct(margin, 3)} (config; no fresh plan)"
        return (
            f"min {pct(margin, 3)} = max(config {pct(self.cfg.margin_min_pct, 3)}, "
            f"plan {pct(plan.s1_margin_min_pct, 3)})"
        )

    @staticmethod
    def _plan_text(ctx: StrategyContext, coin: str, mult: Decimal) -> str:
        """Plan inputs of an entry: size multiplier and the plan's regime calls for the coin."""
        plan = ctx.plan_fresh()
        if plan is None:
            return "no fresh plan (rules-only, size mult 1)"
        calls = [
            f"{sym} {call.regime} ({call.confidence:.2f})"
            for sym in sorted({f"{coin}TMN", f"{coin}USDT", USDTTMN})
            if (call := plan.regimes.get(sym)) is not None
        ]
        regimes = f", regime {', '.join(calls)}" if calls else ", no plan regime for this coin"
        return f"plan size mult {mult}{regimes}"

    @staticmethod
    def _stale(ctx: StrategyContext, symbols: list[str]) -> str | None:
        """Problems with the books a decision uses (missing or older than engine.stale_data_s)."""
        limit = ctx.cfg.engine.stale_data_s
        problems: list[str] = []
        for sym in sorted(set(symbols)):
            book = ctx.books.get(sym)
            if book is None:
                problems.append(f"{sym} no book")
            elif book.age(ctx.now) > limit:
                problems.append(f"{sym} {book.age(ctx.now):.1f}s old")
        return f"stale books (limit {limit:g}s): {', '.join(problems)}" if problems else None

    def _chaos(self, ctx: StrategyContext, coin: str) -> str | None:
        """Why the regime is ``chaos`` for this coin (None when it is not)."""
        plan = ctx.plan_fresh()
        s2 = ctx.cfg.strategies.s2_usdtalloc
        for sym in sorted({f"{coin}TMN", f"{coin}USDT", USDTTMN}):
            call = plan.regimes.get(sym) if plan is not None else None
            if call is not None:
                if call.regime == "chaos":
                    return f"plan regime chaos on {sym} (conf {call.confidence:.2f})"
                continue
            f = ctx.features.symbols.get(sym) or {}
            a, med = f.get("atr_15m_pct"), f.get("atr_15m_pct_median_24h")
            if a is not None and med and a > s2.chaos_atr_mult * med:
                return (
                    f"rules regime chaos on {sym}: ATR15m {pct(a, 3)} > {s2.chaos_atr_mult}x its 24h median "
                    f"{pct(med, 3)}"
                )
        return None

    def _open_sell_qty(self, ctx: StrategyContext, symbol: str) -> Decimal:
        """Remaining qty of our open (non-stop) S1 SELL orders in ``symbol``."""
        return sum(
            (
                o.remaining_qty
                for o in ctx.portfolio.open_orders_of(self.id, symbol)
                if o.req.side == "SELL" and o.req.type not in STOP_TYPES and not o.is_terminal
            ),
            ZERO,
        )

    def _pending(self, ctx: StrategyContext, cyc: dict[str, Any], kind: str) -> list[dict[str, Any]]:
        """Intents of ``kind`` this cycle emitted during the current tick (older records expire)."""
        window = PENDING_TICK_FRACTION * ctx.cfg.engine.fast_loop_interval_s
        live = [p for p in cyc.get("pending", []) if 0 <= ctx.now - float(p["ts"]) < window]
        cyc["pending"] = live
        return [p for p in live if p["kind"] == kind]

    def _pending_unplaced_qty(self, ctx: StrategyContext, cyc: dict[str, Any], kind: str) -> Decimal:
        """Qty of this tick's ``kind`` intents that are not (yet) open orders."""
        placed = {o.req.decision_id for o in ctx.portfolio.open_orders}
        return sum((D(p["qty"]) for p in self._pending(ctx, cyc, kind) if p["id"] not in placed), ZERO)

    @staticmethod
    def _note_pending(ctx: StrategyContext, cyc: dict[str, Any], kind: str, intent: Intent) -> None:
        cyc.setdefault("pending", []).append(
            {"kind": kind, "id": intent.id, "qty": str(intent.qty or ZERO), "ts": ctx.now}
        )

    def _cycle_for(self, pos: Position) -> tuple[str, dict[str, Any]]:
        """The cycle of an S1 position; rebuilt from the position's meta when the state lacks it."""
        cid = _cid(pos)
        cyc = self.cycles.get(cid)
        if cyc is not None:
            return cid, cyc
        mode = str(pos.meta.get("mode") or "revert")
        dev_raw = pos.meta.get("entry_dev_pct", pos.meta.get("dev_mid_pct"))
        dev = D(dev_raw) if dev_raw is not None else None
        target_raw = pos.meta.get("target_dev_pct")
        target = D(target_raw) if target_raw is not None else None
        if target is None and dev is not None:
            target = revert_target(dev, None, self.cfg.revert_target_fraction)
        other = f"{pos.base}USDT" if pos.quote == "TMN" else f"{pos.base}TMN"
        cyc = {
            "coin": pos.base,
            "mode": mode,
            "side": "TMN_CHEAP" if pos.quote == "TMN" else "USDT_CHEAP",
            "leg1_symbol": pos.symbol,
            "leg2_symbol": str(pos.meta.get("leg2_symbol") or other) if mode == "lockin" else None,
            "entry_dev_pct": str(dev) if dev is not None else None,
            "target_dev_pct": str(target) if target is not None else None,
            "status": "degraded" if mode == "lockin" and pos.stop_price is not None else "open",
            "ts": pos.entry_ts,
            "leg2_emitted_qty": "0",
            "leg2_intents": 0,
            "pending": [],
            "rebuilt_from_position": True,
        }
        self.cycles[cid] = cyc
        return cid, cyc

    @staticmethod
    def _is_degraded(cyc: dict[str, Any], pos: Position) -> bool:
        # a lock-in position never has a stop unless S1 degraded it (the OrderManager sets the
        # stop price from the degrade STOP_LIMIT intent), so the stop also survives a lost state
        return cyc.get("status") == "degraded" or pos.stop_price is not None

    def _targets(self, pos: Position, cyc: dict[str, Any]) -> tuple[Decimal | None, Decimal | None]:
        """(target_dev, entry_dev) of a revert or degraded position: position meta, else the cycle."""

        def pick(key: str) -> Decimal | None:
            v = pos.meta.get(key)
            if v is None:
                v = cyc.get(key)
            return D(v) if v is not None else None

        entry_dev = pick("entry_dev_pct")
        if entry_dev is None and pos.meta.get("dev_mid_pct") is not None:
            entry_dev = D(pos.meta["dev_mid_pct"])
        target = pick("target_dev_pct")
        if target is None and entry_dev is not None:
            target = revert_target(entry_dev, None, self.cfg.revert_target_fraction)
        return target, entry_dev

    # ------------------------------------------------------------ main loop

    def on_tick(self, ctx: StrategyContext) -> list[Intent]:
        self._diag = {"ts": ctx.now, "coins": {}, "cycles": {}}
        if ctx.flatten_phase > 0:
            self._idle(ctx, f"flatten phase {ctx.flatten_phase}: the flatten routine owns every coin sale")
            return []
        self._prune(ctx)
        # open cycles are always tended (exits, leg 2, degrade stops): they only reduce exposure.
        # Exits first: a reached revert target beats re-sending a protective stop.
        intents = self._revert_exits(ctx)
        intents += self._tend_cycles(ctx)
        if not self.cfg.enabled:
            self._idle(ctx, "disabled by config: no new entries (open cycles are still tended)")
            return intents
        if not plan_enabled(ctx, self.id):
            self._idle(ctx, "not in the plan's enabled_strategies: no new entries (open cycles are still tended)")
            return intents
        self._diag["status"] = "scanning"
        return intents + self._entries(ctx)

    def _idle(self, ctx: StrategyContext, status: str) -> None:
        """No entries this tick: still log every coin's edges and costs (PRD-03 §9 acceptance)."""
        margin = self._margin(ctx)
        self._diag.update({"status": status, "margin_min_pct": str(margin)})
        for coin in sorted(self.cfg.coins):
            cf = ctx.features.cross.get(coin)
            d: dict[str, Any] = self._numbers(cf, margin) if cf is not None else {}
            d["why_no_trade"] = status if cf is not None else (
                f"no cross features (a book or market of {coin}TMN/{coin}USDT/USDTTMN missing)"
            )
            self._diag["coins"][coin] = d

    def _prune(self, ctx: StrategyContext) -> None:
        """Forget cycles that are over: no position, no working entry order, past the entry's lifetime."""
        held = {_cid(p) for p in ctx.portfolio.positions_of(self.id)}
        working = {o.req.decision_id for o in ctx.portfolio.open_orders_of(self.id)}
        grace = ctx.cfg.execution.limit_cancel_after_s + ctx.cfg.engine.fast_loop_interval_s
        for cid in sorted(self.cycles):
            if cid in held or cid in working:
                continue
            cyc = self.cycles[cid]
            if cyc.get("status") == "entry" and ctx.now - float(cyc.get("ts", ctx.now)) < grace:
                continue  # the entry may still be placed / filled
            del self.cycles[cid]

    def _active_cycles(self, ctx: StrategyContext) -> dict[str, str]:
        """cycle id -> lockin | revert | degraded, for positions and working entry orders."""
        active: dict[str, str] = {}
        for pos in sorted(ctx.portfolio.positions_of(self.id), key=lambda p: p.id):
            cid = _cid(pos)
            mode = str(pos.meta.get("mode") or "revert")
            if mode == "lockin" and self._is_degraded(self.cycles.get(cid, {}), pos):
                mode = "degraded"
            active[cid] = mode
        for o in sorted(ctx.portfolio.open_orders_of(self.id), key=lambda o: o.client_id):
            if o.req.purpose == "entry" and o.req.side == "BUY" and o.req.decision_id not in active:
                active[o.req.decision_id] = str(self.cycles.get(o.req.decision_id, {}).get("mode", "lockin"))
        return active

    @staticmethod
    def _busy(ctx: StrategyContext) -> dict[str, str]:
        """symbol -> what uses it (a position or open order of any strategy)."""
        busy: dict[str, str] = {}
        for p in sorted(ctx.portfolio.positions, key=lambda p: p.id):
            busy.setdefault(p.symbol, f"{p.strategy} position {p.id}")
        for o in sorted(ctx.portfolio.open_orders, key=lambda o: o.client_id):
            busy.setdefault(o.req.symbol, f"{o.req.strategy} {o.req.side} order {o.client_id}")
        return busy

    def _entries(self, ctx: StrategyContext) -> list[Intent]:
        cfg = self.cfg
        mult = plan_size_multiplier(ctx)
        margin = self._margin(ctx)
        active = self._active_cycles(ctx)
        n_open = len(active)
        n_revert = sum(1 for m in active.values() if m in ("revert", "degraded"))
        busy = self._busy(ctx)
        self._diag.update(
            {
                "margin_min_pct": str(margin),
                "size_multiplier": str(mult),
                "open_cycles": n_open,
                "revert_cycles": n_revert,
            }
        )
        cands: list[dict[str, Any]] = []
        for coin in sorted(cfg.coins):
            d: dict[str, Any] = {}
            self._diag["coins"][coin] = d
            cf = ctx.features.cross.get(coin)
            if cf is None:
                d["why_no_trade"] = (
                    f"no cross features (a book or market of {coin}TMN/{coin}USDT/USDTTMN missing)"
                )
                continue
            d.update(self._numbers(cf, margin))
            why = self._gate(ctx, coin, cf, mult, busy)
            if why is not None:
                d["why_no_trade"] = why
                continue
            cand: dict[str, Any] | None = None
            lockin_edge = False
            if cfg.lockin_enabled:
                cand, lockin_edge = self._lockin(ctx, cf, margin, mult, d)
            else:
                d["lockin"] = "lock-in disabled by config"
            if cand is None and not lockin_edge:
                if cfg.revert_enabled:
                    cand = self._revert(ctx, cf, margin, mult, d)
                else:
                    d["revert"] = "revert disabled by config"
            if cand is None:
                d["why_no_trade"] = "; ".join(str(d[k]) for k in ("lockin", "revert") if k in d)
                continue
            cands.append(cand)
        # lock-in before revert, then the larger edge above the requirement, then the coin
        cands.sort(key=lambda c: (c["mode"] != "lockin", -c["excess"], c["coin"]))
        out: list[Intent] = []
        for c in cands:
            d = self._diag["coins"][c["coin"]]
            if n_open >= cfg.max_concurrent:
                d["why_no_trade"] = (
                    f"{c['mode']} candidate not taken: max_concurrent {n_open}/{cfg.max_concurrent} S1 cycles open"
                )
                continue
            if c["mode"] == "revert" and n_revert >= cfg.revert_max_concurrent:
                d["why_no_trade"] = (
                    f"revert candidate not taken: revert_max_concurrent {n_revert}/{cfg.revert_max_concurrent} "
                    "revert/degraded cycles open"
                )
                continue
            intent = self._commit(ctx, c)
            out.append(intent)
            d["why_no_trade"] = None
            d["decision"] = f"ENTER {c['mode']} {c['cycle']['side']} {intent.id}"
            n_open += 1
            n_revert += 1 if c["mode"] == "revert" else 0
        return out

    @staticmethod
    def _numbers(cf: dict[str, Any], margin: Decimal) -> dict[str, Any]:
        z = cf.get("dev_zscore")
        return {
            "edge_tmn_cheap_pct": _s(cf["edge_tmn_cheap_pct"]),
            "cost_tmn_cheap_pct": _s(cf["cost_lockin_tmn_cheap"]),
            "need_tmn_cheap_pct": _s(cf["cost_lockin_tmn_cheap"] + margin),
            "edge_usdt_cheap_pct": _s(cf["edge_usdt_cheap_pct"]),
            "cost_usdt_cheap_pct": _s(cf["cost_lockin_usdt_cheap"]),
            "need_usdt_cheap_pct": _s(cf["cost_lockin_usdt_cheap"] + margin),
            "dev_mid_pct": _s(cf["dev_mid_pct"]),
            "dev_zscore": _s(z, "0.01") if z is not None else None,
            "dev_samples": cf.get("dev_samples"),
        }

    def _gate(
        self, ctx: StrategyContext, coin: str, cf: dict[str, Any], mult: Decimal, busy: dict[str, str]
    ) -> str | None:
        stale = self._stale(ctx, [cf["tmn_symbol"], cf["usdt_symbol"], USDTTMN])
        if stale is not None:
            return stale
        if mult <= 0:
            return "plan size_multiplier 0: no new entries"
        last = self.last_entry_ts.get(coin)
        if last is not None and ctx.now - last < self.cfg.cooldown_s:
            return f"cooldown: last {coin} entry {ctx.now - last:.0f}s ago < {self.cfg.cooldown_s:g}s"
        for sym in (cf["tmn_symbol"], cf["usdt_symbol"]):
            if sym in busy:
                return f"{sym} in use ({busy[sym]})"
        return None

    def _size(
        self,
        ctx: StrategyContext,
        buy_m: MarketInfo,
        price: Decimal,
        fee: Decimal,
        mult: Decimal,
        buy_level_qty: Decimal | None = None,
        sell_m: MarketInfo | None = None,
        sell_level_qty: Decimal | None = None,
    ) -> tuple[Decimal, dict[str, Any]]:
        """Leg-1 quantity: min of the budget, the given best levels (80 %) and the free quote balance.

        With ``sell_m`` (a lock-in) the quantity is ``lockin_leg_qtys`` of that limit: the coins left
        after the leg-1 fee (``fee``, paid in the coin) land on a leg-2 step; ``sizing`` then also
        names the leg-2 quantity and the unhedged remainder.
        """
        port = ctx.portfolio
        dec = buy_m.step_decimals
        px_tmn = price if buy_m.quote == "TMN" else (price * port.u_bid if port.u_bid else None)
        limits: dict[str, Decimal] = {}
        budget_tmn = self.cfg.size_pct_equity * port.equity_tmn * mult
        limits["budget"] = budget_tmn / px_tmn if px_tmn and budget_tmn > 0 else ZERO
        if buy_level_qty is not None:
            limits["leg1_liquidity"] = LEG_LIQ_FRACTION * buy_level_qty
        if sell_level_qty is not None:
            limits["leg2_liquidity"] = LEG_LIQ_FRACTION * sell_level_qty
        free = max(port.free.get(buy_m.quote, ZERO), ZERO)
        limits["free_quote"] = free * (ONE - ctx.cfg.risk.balance_buffer_pct) / (price * (ONE + fee))
        binding = min(sorted(limits), key=lambda k: limits[k])
        limit = max(limits[binding], ZERO)
        sizing: dict[str, Any] = {
            "binding_limit": binding,
            "qty_decimals": dec,
            "budget_tmn": str(quantize_down(budget_tmn, 0)),
            "free_quote": f"{free} {buy_m.quote}",
            **{f"{k}_qty": str(quantize_down(max(v, ZERO), dec)) for k, v in sorted(limits.items())},
        }
        if sell_m is None:
            qty = quantize_down(limit, dec)
        else:
            qty, q2 = lockin_leg_qtys(limit, fee, buy_m, sell_m)
            held = qty * (ONE - fee)
            sizing.update(
                {
                    "leg1_fee_pct": str(fee),
                    "held_after_fee": _plain(held),
                    "leg2_sell": format(q2, "f"),
                    "unhedged": _plain(held - q2),
                    "unhedged_pct": _s((held - q2) / qty) if qty > 0 else None,
                }
            )
        sizing["qty"] = str(qty)
        return qty, sizing

    @staticmethod
    def _sizing_text(sizing: dict[str, Any], coin: str) -> str:
        parts = ", ".join(
            f"{k.removesuffix('_qty')} {v}"
            for k, v in sorted(sizing.items())
            if k.endswith("_qty") and k != "qty"
        )
        return f"size {sizing['qty']} {coin} limited by {sizing['binding_limit']} ({parts})"

    # ------------------------------------------------------------ entries

    def _lockin(
        self, ctx: StrategyContext, cf: dict[str, Any], margin: Decimal, mult: Decimal, d: dict[str, Any]
    ) -> tuple[dict[str, Any] | None, bool]:
        """(candidate, edge_ok). edge_ok without a candidate = the size failed (no revert then)."""
        coin = cf["coin"]
        t_sym, u_sym = cf["tmn_symbol"], cf["usdt_symbol"]
        tm, um = ctx.markets[t_sym], ctx.markets[u_sym]
        tb, ub = ctx.books[t_sym], ctx.books[u_sym]
        sides = {
            "TMN_CHEAP": (cf["edge_tmn_cheap_pct"], cf["cost_lockin_tmn_cheap"], tm, um, tb, ub),
            "USDT_CHEAP": (cf["edge_usdt_cheap_pct"], cf["cost_lockin_usdt_cheap"], um, tm, ub, tb),
        }
        ok = sorted(
            (s for s, v in sides.items() if v[0] >= v[1] + margin),
            key=lambda s: (-(sides[s][0] - sides[s][1]), s),
        )
        if not ok:
            e_t, c_t = sides["TMN_CHEAP"][:2]
            e_u, c_u = sides["USDT_CHEAP"][:2]
            d["lockin"] = (
                f"no lock-in: TMN-cheap edge {pct(e_t, 3)} < cost {pct(c_t, 3)} + margin {pct(margin, 3)} and "
                f"USDT-cheap edge {pct(e_u, 3)} < cost {pct(c_u, 3)} + margin {pct(margin, 3)}"
            )
            return None, False
        side = ok[0]
        edge, cost, buy_m, sell_m, buy_b, sell_b = sides[side]
        taker = self.cfg.leg1_taker
        if taker:
            price = buy_b.best_ask
        else:
            price = (
                buy_b.best_bid + buy_m.tick
                if buy_b.best_bid + buy_m.tick < buy_b.best_ask
                else buy_b.best_bid
            )
        price = round_price(buy_m, price, "BUY")
        fee1 = buy_m.taker_fee if taker else buy_m.maker_fee
        # both legs' best levels bound the size (leg 1's ask also when leg 1 rests as a maker: the
        # lag that makes the edge rarely leaves more size than the level shows)
        qty, sizing = self._size(ctx, buy_m, price, fee1, mult, buy_b.best_ask_qty, sell_m, sell_b.best_bid_qty)
        q2 = D(sizing["leg2_sell"])
        problem = _size_problem(buy_m, qty, price, ctx) or _size_problem(sell_m, q2, sell_b.best_bid, ctx)
        if problem is not None:
            d["lockin"] = (
                f"lock-in {side} edge {pct(edge, 3)} >= need {pct(cost + margin, 3)}, but "
                f"{self._sizing_text(sizing, coin)} fails the minimum order: {problem}"
            )
            return None, True
        unhedged = D(sizing["unhedged"])
        unhedged_pct = unhedged / qty
        stress = unhedged_pct * UNHEDGED_STRESS_PCT
        need = cost + margin + stress
        remainder_txt = (
            f"buying {qty} {coin} leaves {sizing['held_after_fee']} after the {pct(fee1, 3)} leg-1 fee (paid in "
            f"{coin}) and leg 2 on {sell_m.symbol} (step {sell_m.step}) can sell {q2}: the unhedged {_plain(unhedged)} "
            f"{coin} ({pct(unhedged_pct, 3)} of the cycle) x a {pct(UNHEDGED_STRESS_PCT, 0)} adverse move = "
            f"{pct(stress, 3)}"
        )
        if edge < need:
            d["lockin"] = (
                f"lock-in {side} edge {pct(edge, 3)} >= cost {pct(cost, 3)} + margin {pct(margin, 3)}, but "
                f"{remainder_txt} -> need {pct(need, 3)}"
            )
            return None, True
        u_bid, u_ask = cf["u_bid"], cf["u_ask"]
        if side == "TMN_CHEAP":
            how = (
                f"{t_sym} ask {fmt(tb.best_ask, tm.tick_decimals)} TMN / USDTTMN bid {fmt(u_bid)} = "
                f"{fmt(tb.best_ask / u_bid, um.tick_decimals)} USDT vs {u_sym} bid {fmt(ub.best_bid, um.tick_decimals)}"
                f" -> TMN book {pct(edge, 3)} cheap"
            )
        else:
            how = (
                f"{u_sym} ask {fmt(ub.best_ask, um.tick_decimals)} USDT x USDTTMN ask {fmt(u_ask)} = "
                f"{fmt(ub.best_ask * u_ask, tm.tick_decimals)} TMN vs {t_sym} bid {fmt(tb.best_bid, tm.tick_decimals)}"
                f" -> USDT book {pct(edge, 3)} cheap"
            )
        plan_txt = self._plan_text(ctx, coin, mult)
        reason = (
            f"{coin}: {how}; cost {pct(cost, 3)} ({pct(fee1, 3)} {'taker' if taker else 'maker'} {buy_m.symbol} "
            f"+ {pct(sell_m.taker_fee, 3)} taker {sell_m.symbol}); edge - cost = {pct(edge - cost, 3)} >= margin "
            f"{self._margin_text(ctx, margin)}; unhedged remainder: {remainder_txt}, so edge {pct(edge, 3)} >= "
            f"need {pct(need, 3)}; lock-in: buy {qty} {coin} on {buy_m.symbol} at "
            f"{fmt(price, buy_m.tick_decimals)} ({fmt(qty * price, 2)} {buy_m.quote}) as "
            f"{'taker' if taker else 'maker'}, then sell the {q2} {coin} left after the leg-1 fee on "
            f"{sell_m.symbol} at its bid {fmt(sell_b.best_bid, sell_m.tick_decimals)} (leg 2); "
            f"{self._sizing_text(sizing, coin)}; {plan_txt}."
        )
        dev = cf["dev_mid_pct"]
        cycle = {
            "coin": coin,
            "mode": "lockin",
            "side": side,
            "leg1_symbol": buy_m.symbol,
            "leg2_symbol": sell_m.symbol,
            "entry_edge_pct": str(edge),
            "cost_pct": str(cost),
            "margin_min_pct": str(margin),
            "entry_dev_pct": str(dev),
            "entry_dev_mean_pct": str(cf["dev_mean_1h"]) if cf.get("dev_mean_1h") is not None else None,
            "target_dev_pct": str(revert_target(dev, cf.get("dev_mean_1h"), self.cfg.revert_target_fraction)),
            "status": "entry",
            "leg2_emitted_qty": "0",
            "leg2_intents": 0,
            "pending": [],
        }
        d["lockin"] = f"lock-in {side}: edge {pct(edge, 3)} >= need {pct(need, 3)}"
        return {
            "coin": coin,
            "mode": "lockin",
            "excess": edge - need,
            "symbol": buy_m.symbol,
            "cycle": cycle,
            "intent": {
                "symbol": buy_m.symbol,
                "side": "BUY",
                "order_type": "LIMIT",
                "qty": qty,
                "notional": qty * price,
                "price": price,
                "purpose": "entry",
                "reason": reason,
                "features": {
                    **_jsonable(cf),
                    "mode": "lockin",
                    "side": side,
                    "leg2_symbol": sell_m.symbol,
                    "cost_model": "lockin",
                    "cost_pct": str(cost),
                    "margin_min_pct": str(margin),
                    "unhedged_stress_pct": str(stress),
                    "need_pct": str(need),
                    "size_multiplier": str(mult),
                    "sizing": sizing,
                    "books_used": sorted({t_sym, u_sym, USDTTMN}),
                },
                "expected_edge_pct": edge,
                "taker": taker,
            },
        }, True

    def _revert(
        self, ctx: StrategyContext, cf: dict[str, Any], margin: Decimal, mult: Decimal, d: dict[str, Any]
    ) -> dict[str, Any] | None:
        cfg = self.cfg
        coin = cf["coin"]
        chaos = self._chaos(ctx, coin)
        if chaos is not None:
            d["revert"] = f"revert blocked: {chaos}"
            return None
        z, dev = cf.get("dev_zscore"), cf["dev_mid_pct"]
        if z is None:
            d["revert"] = (
                f"revert: z-score n/a (warming up, {cf.get('dev_samples', 0)} of {Z_MIN_SAMPLES} deviation "
                "samples in the window)"
            )
            return None
        if abs(z) < cfg.z_min:
            d["revert"] = f"revert: |z| {abs(z):.2f} < z_min {cfg.z_min}"
            return None
        if (z > 0) != (dev > 0):
            d["revert"] = f"revert: z {z:.2f} and deviation {pct(dev, 3)} disagree in sign"
            return None
        # dev > 0: TMN book expensive -> USDT book cheap; dev < 0: TMN book cheap
        sym = cf["usdt_symbol"] if dev > 0 else cf["tmn_symbol"]
        m, b = ctx.markets[sym], ctx.books[sym]
        spread = b.spread_pct or ZERO
        cost = m.maker_fee + m.taker_fee + spread
        need = cost + margin
        # the exit fires once ``revert_target_fraction`` of the gap to the 1-h mean has closed: that
        # part, not the whole deviation, is what the trade can earn
        mean = cf["dev_mean_1h"]
        frac = cfg.revert_target_fraction
        capture = frac * abs(dev - mean)
        target = revert_target(dev, mean, frac)
        if capture < need:
            d["revert"] = (
                f"revert: the exit captures {pct(frac, 0)} of |dev {pct(dev, 3)} - 1-h mean {pct(mean, 3)}| = "
                f"{pct(capture, 3)} < {sym} round trip {pct(cost, 3)} + margin {pct(margin, 3)}"
            )
            return None
        price = b.best_bid + m.tick if b.best_bid + m.tick < b.best_ask else b.best_bid
        price = round_price(m, price, "BUY")
        qty, sizing = self._size(ctx, m, price, m.maker_fee, mult)
        problem = _size_problem(m, qty, price, ctx)
        if problem is not None:
            d["revert"] = f"revert: {self._sizing_text(sizing, coin)} fails the minimum order: {problem}"
            return None
        stop = round_price(m, price * (ONE - cfg.revert_stop_pct), "SELL")
        plan_txt = self._plan_text(ctx, coin, mult)
        reason = (
            f"{coin}: no lock-in edge (TMN-cheap {pct(cf['edge_tmn_cheap_pct'], 3)}, USDT-cheap "
            f"{pct(cf['edge_usdt_cheap_pct'], 3)}), but the mid deviation {pct(dev, 3)} is {z:.2f} sigma from its "
            f"1-h mean {pct(mean, 3)} (|z| >= {cfg.z_min}); the exit at the target {pct(target, 3)} (= mean + "
            f"{pct(ONE - frac, 0)} x (dev - mean), i.e. {pct(frac, 0)} of the gap closed) captures {pct(capture, 3)}, "
            f">= the {sym} round trip {pct(cost, 3)} ({pct(m.maker_fee, 3)} maker + {pct(m.taker_fee, 3)} taker + "
            f"spread {pct(spread, 3)}) + margin {self._margin_text(ctx, margin)} = {pct(need, 3)}; revert: buy "
            f"{qty} {coin} on {sym} (the cheap book) as maker at {fmt(price, m.tick_decimals)} "
            f"({fmt(qty * price, 2)} {m.quote}), sell it back in the same book at the target; stop "
            f"{fmt(stop, m.tick_decimals)} (entry -{pct(cfg.revert_stop_pct, 1)}), time stop "
            f"{int(cfg.revert_max_hold_s)} s; {self._sizing_text(sizing, coin)}; {plan_txt}."
        )
        d["revert"] = (
            f"revert: |z| {abs(z):.2f} >= {cfg.z_min} and the captured {pct(capture, 3)} >= {pct(need, 3)}"
        )
        return {
            "coin": coin,
            "mode": "revert",
            "excess": capture - need,
            "symbol": sym,
            "cycle": {
                "coin": coin,
                "mode": "revert",
                "side": "TMN_CHEAP" if dev < 0 else "USDT_CHEAP",
                "leg1_symbol": sym,
                "leg2_symbol": None,
                "entry_dev_pct": str(dev),
                "entry_dev_mean_pct": str(mean),
                "target_dev_pct": str(target),
                "cost_pct": str(cost),
                "margin_min_pct": str(margin),
                "status": "entry",
                "pending": [],
            },
            "intent": {
                "symbol": sym,
                "side": "BUY",
                "order_type": "LIMIT",
                "qty": qty,
                "notional": qty * price,
                "price": price,
                "stop_price": stop,
                "purpose": "entry",
                "reason": reason,
                "features": {
                    **_jsonable(cf),
                    "mode": "revert",
                    "cost_model": "roundtrip",
                    "cost_pct": str(cost),
                    "margin_min_pct": str(margin),
                    "size_multiplier": str(mult),
                    "target_dev_pct": str(target),
                    "entry_dev_pct": str(dev),
                    "entry_dev_mean_pct": str(mean),
                    "revert_capture_pct": str(capture),
                    "time_stop_s": cfg.revert_max_hold_s,
                    "sizing": sizing,
                    "books_used": sorted({cf["tmn_symbol"], cf["usdt_symbol"], USDTTMN}),
                },
                "expected_edge_pct": capture,
                "taker": False,
            },
        }

    def _target_text(self, target: Decimal, entry_dev: Decimal) -> str:
        """How a target relates to the entry deviation: target = mean + (1 - f) x (entry - mean),
        so the 1-h mean of the entry moment is (target - (1 - f) x entry) / f."""
        f = self.cfg.revert_target_fraction
        if f <= 0:
            return f"entry deviation {pct(entry_dev, 3)}"
        mean = (target - (ONE - f) * entry_dev) / f
        return (
            f"{pct(f, 0)} of the gap between the entry deviation {pct(entry_dev, 3)} and its 1-h mean "
            f"{pct(mean, 3)} closed (target = mean + {pct(ONE - f, 0)} x (entry - mean))"
        )

    def _commit(self, ctx: StrategyContext, cand: dict[str, Any]) -> Intent:
        iid = ctx.next_id(self.id, cand["symbol"])
        self.cycles[iid] = {**cand["cycle"], "ts": ctx.now}
        self.last_entry_ts[cand["coin"]] = ctx.now
        return Intent(id=iid, ts=ctx.now, strategy=self.id, **cand["intent"])

    # ------------------------------------------------------------ open cycles

    def _tend_cycles(self, ctx: StrategyContext) -> list[Intent]:
        out: list[Intent] = []
        for pos in sorted(ctx.portfolio.positions_of(self.id), key=lambda p: p.id):
            cid, cyc = self._cycle_for(pos)
            if cyc.get("status") == "entry":
                cyc["status"] = "open"
            if cyc.get("mode") == "lockin":
                out += self._tend_lockin(ctx, cid, cyc, pos)
        return out

    def _tend_lockin(
        self, ctx: StrategyContext, cid: str, cyc: dict[str, Any], pos: Position
    ) -> list[Intent]:
        d: dict[str, Any] = self._diag["cycles"].setdefault(cid, {})
        d.update({"mode": "lockin", "coin": cyc["coin"], "position_id": pos.id, "position_qty": str(pos.qty)})
        if self._is_degraded(cyc, pos):
            cyc["status"] = "degraded"
            d["status"] = "degraded (revert mode)"
            selling = (
                pos.meta.get("exit_pending")
                or self._pending(ctx, cyc, "exit")
                or self._open_sell_qty(ctx, pos.symbol) > 0
            )
            if pos.stop_price is None and not self._pending(ctx, cyc, "stop") and not selling:
                # the stop intent was vetoed / refused (the OrderManager sets the position's stop
                # price when it places one): send it again
                return self._degrade_stop(ctx, cid, cyc, pos, None, True, d)
            return []
        leg2 = str(cyc["leg2_symbol"])
        open_qty = self._open_sell_qty(ctx, leg2)
        pend = self._pending(ctx, cyc, "leg2")
        d.update(
            {"status": cyc.get("status"), "open_leg2_qty": str(open_qty), "unhedged": str(pos.qty - open_qty)}
        )
        if open_qty > 0 or pend:
            d["action"] = "leg 2 working: wait for it"
            return []
        if (
            pos.meta.get("exit_pending")
            or self._pending(ctx, cyc, "exit")
            or self._open_sell_qty(ctx, pos.symbol)
        ):
            d["action"] = f"a SELL on {pos.symbol} is working: wait for it"
            return []
        out = self._hedge(ctx, cid, cyc, pos, pos.qty, None, d)
        if out or not d.get("leg2_too_small"):
            return out
        return self._remainder_exit(ctx, cid, cyc, pos, d)

    def _hedge(
        self,
        ctx: StrategyContext,
        cid: str,
        cyc: dict[str, Any],
        pos: Position,
        want: Decimal,
        fill: Fill | None,
        d: dict[str, Any],
    ) -> list[Intent]:
        """Leg 2 for ``want`` coins (or the degrade stop when leg 2 would lose too much)."""
        leg1, leg2 = str(cyc["leg1_symbol"]), str(cyc["leg2_symbol"])
        m1, m2, b2 = ctx.markets.get(leg1), ctx.markets.get(leg2), ctx.books.get(leg2)
        if m1 is None or m2 is None:
            d["action"] = f"wait: market {leg1 if m1 is None else leg2} unknown"
            return []
        stale = self._stale(ctx, [leg2, USDTTMN])
        if stale is not None or b2 is None or b2.best_bid is None:
            d["action"] = f"wait: {stale or f'no {leg2} bid'}; leg 2 is retried next tick"
            return []
        price = round_price(m2, b2.best_bid, "SELL")
        qty = quantize_down(max(want, ZERO), m2.step_decimals)
        problem = _size_problem(m2, qty, price, ctx)
        if problem is not None:
            d["action"] = f"wait: unhedged {want} {cyc['coin']} too small for leg 2 ({problem})"
            d["leg2_too_small"] = True
            return []
        got = self._edge_now(ctx, m1, m2, pos.entry_price, price)
        if got is None:
            d["action"] = "wait: no USDTTMN bid/ask to value the cycle"
            return []
        edge_now, fees = got
        if -edge_now >= self.cfg.leg2_max_loss_pct:
            # "complete leg 2 only if the loss would be < leg2_max_loss_pct": equality degrades
            return self._degrade_stop(ctx, cid, cyc, pos, edge_now, False, d)
        open_qty = self._open_sell_qty(ctx, leg2)
        trigger = (
            f"on the leg-1 fill of {fill.qty} at {fmt(fill.price, m1.tick_decimals)}"
            if fill is not None
            else "retry of the unhedged remainder (no leg-2 order open)"
        )
        coin = cyc["coin"]
        entry_edge = cyc.get("entry_edge_pct")
        reason = (
            f"{coin}: lock-in leg 2 {trigger}: {pos.qty} {coin} held from leg 1 on {leg1} at avg "
            f"{fmt(pos.entry_price, m1.tick_decimals)}, {open_qty} already in open leg-2 orders; sell {qty} {coin} "
            f"on {leg2} at bid {fmt(price, m2.tick_decimals)} ({fmt(qty * price, 2)} {m2.quote}, crossing, taker "
            f"{pct(m2.taker_fee, 3)}); cycle edge now {pct(edge_now, 3)} after {pct(fees, 3)} fees (entry edge "
            f"{pct(D(entry_edge), 3) if entry_edge is not None else 'n/a'} before fees) is above the "
            f"-{pct(self.cfg.leg2_max_loss_pct, 2)} leg-2 loss limit."
        )
        intent = Intent(
            id=ctx.next_id(self.id, leg2),
            ts=ctx.now,
            strategy=self.id,
            symbol=leg2,
            side="SELL",
            order_type="LIMIT",
            qty=qty,
            notional=qty * price,
            price=price,
            purpose="leg2",
            reason=reason,
            features={
                "exit_reason": "lockin",
                "cycle": cid,
                "position_id": pos.id,
                "trigger": "fill" if fill is not None else "retry",
                "edge_now_pct": str(edge_now),
                "fees_pct": str(fees),
                "entry_edge_pct": entry_edge,
                "position_qty": str(pos.qty),
                "open_leg2_qty": str(open_qty),
                "leg2_max_loss_pct": str(self.cfg.leg2_max_loss_pct),
                "books_used": sorted({leg2, USDTTMN}),
            },
            linked_position_id=pos.id,
            taker=True,
        )
        self._note_pending(ctx, cyc, "leg2", intent)
        cyc["status"] = "hedging"
        cyc["leg2_emitted_qty"] = str(D(cyc.get("leg2_emitted_qty", "0")) + qty)
        cyc["leg2_intents"] = int(cyc.get("leg2_intents", 0)) + 1
        d["action"] = f"leg 2 {intent.id}: sell {qty} on {leg2}"
        return [intent]

    def _edge_now(
        self, ctx: StrategyContext, m1: MarketInfo, m2: MarketInfo, entry_price: Decimal, sell_price: Decimal
    ) -> tuple[Decimal, Decimal] | None:
        """(cycle edge after fees, fees) if leg 2 sold at ``sell_price`` now; the entry-edge formula
        with the actual leg-1 price (USDT bought at the USDTTMN ask, sold at its bid)."""
        dollar = ctx.books.get(USDTTMN)
        if dollar is None or dollar.best_bid is None or dollar.best_ask is None or entry_price <= 0:
            return None
        buy_tmn = entry_price * (dollar.best_ask if m1.quote == "USDT" else ONE)
        sell_tmn = sell_price * (dollar.best_bid if m2.quote == "USDT" else ONE)
        fees = (m1.taker_fee if self.cfg.leg1_taker else m1.maker_fee) + m2.taker_fee
        return sell_tmn / buy_tmn - ONE - fees, fees

    def _degrade_stop(
        self,
        ctx: StrategyContext,
        cid: str,
        cyc: dict[str, Any],
        pos: Position,
        edge_now: Decimal | None,
        retry: bool,
        d: dict[str, Any],
    ) -> list[Intent]:
        """``lockin_degraded_to_revert``: protective STOP_LIMIT (+ time stop) for the leg-1 coins.

        The cycle is degraded either way (no more leg 2). The stop covers the coins no leg-2 order
        is already selling (open, or emitted this tick); when leg 2 covers them all, it waits.
        """
        cfg = self.cfg
        m1 = ctx.markets[pos.symbol]
        leg2 = str(cyc["leg2_symbol"])
        if edge_now is not None:
            cyc["degrade_edge_pct"] = str(edge_now)
        cyc.setdefault("degraded_ts", ctx.now)
        cyc["status"] = "degraded"
        in_leg2 = self._open_sell_qty(ctx, leg2) + self._pending_unplaced_qty(ctx, cyc, "leg2")
        qty = quantize_down(max(pos.qty - in_leg2, ZERO), m1.step_decimals)
        if qty <= 0:
            d["action"] = (
                f"degraded; the leg-2 orders cover all {pos.qty} {cyc['coin']}: the protective stop waits"
            )
            return []
        stop = round_price(m1, pos.entry_price * (ONE - cfg.revert_stop_pct), "SELL")
        limit = round_price(m1, stop * (ONE - ctx.cfg.execution.stop_limit_slippage_pct), "SELL")
        target, entry_dev = self._targets(pos, cyc)
        known = cyc.get("degrade_edge_pct")
        loss = pct(D(known), 3) if known is not None else "n/a"
        working = f" ({in_leg2} {cyc['coin']} are already in leg-2 orders on {leg2})" if in_leg2 > 0 else ""
        reason = (
            f"{cyc['coin']}: {'retry: the protective stop was not placed yet; ' if retry else ''}"
            f"lockin_degraded_to_revert: leg 2 on {leg2} would realise {loss} after fees, a loss "
            f">= the {pct(cfg.leg2_max_loss_pct, 2)} limit, so the {qty} {cyc['coin']} bought on {pos.symbol} at "
            f"{fmt(pos.entry_price, m1.tick_decimals)} are kept as a revert position{working}: STOP_LIMIT SELL "
            f"stop {fmt(stop, m1.tick_decimals)} (entry -{pct(cfg.revert_stop_pct, 1)}), limit "
            f"{fmt(limit, m1.tick_decimals)}, time stop {int(cfg.revert_max_hold_s)} s, revert exit when the mid "
            f"deviation (entry {pct(entry_dev, 3)}) has closed to {pct(target, 3)}."
        )
        intent = Intent(
            id=ctx.next_id(self.id, pos.symbol),
            ts=ctx.now,
            strategy=self.id,
            symbol=pos.symbol,
            side="SELL",
            order_type="STOP_LIMIT",
            qty=qty,
            price=limit,
            stop_price=stop,
            purpose="stop",
            reason=reason,
            features={
                "exit_reason": "stop",
                "note": "lockin_degraded_to_revert",
                "cycle": cid,
                "position_id": pos.id,
                "edge_now_pct": known,
                "leg2_max_loss_pct": str(cfg.leg2_max_loss_pct),
                "time_stop_s": cfg.revert_max_hold_s,
                "entry_dev_pct": str(entry_dev) if entry_dev is not None else None,
                "target_dev_pct": str(target) if target is not None else None,
                "in_leg2_qty": str(in_leg2),
                "retry": retry,
                "books_used": [pos.symbol],
            },
            linked_position_id=pos.id,
            taker=False,
        )
        self._note_pending(ctx, cyc, "stop", intent)
        d["action"] = f"{'re-emit' if retry else 'degrade:'} protective stop {intent.id} for {qty} on {pos.symbol}"
        return [intent]

    def _remainder_exit(
        self, ctx: StrategyContext, cid: str, cyc: dict[str, Any], pos: Position, d: dict[str, Any]
    ) -> list[Intent]:
        """Lock-in remainder below the leg-2 minimum: sell it back in the leg-1 book once leg 1 is done."""
        if any(o.req.decision_id == cid for o in ctx.portfolio.open_orders):
            return []  # leg 1 still working: more fills may make the remainder hedgeable
        m1, b1 = ctx.markets.get(pos.symbol), ctx.books.get(pos.symbol)
        if m1 is None or b1 is None or b1.best_bid is None or self._stale(ctx, [pos.symbol]) is not None:
            return []
        qty = quantize_down(pos.qty, m1.step_decimals)
        problem = _size_problem(m1, qty, b1.best_bid, ctx)
        if problem is not None:
            d["action"] = f"{d.get('action')}; not sellable on {pos.symbol} either ({problem})"
            return []
        coin = cyc["coin"]
        intent = Intent(
            id=ctx.next_id(self.id, pos.symbol),
            ts=ctx.now,
            strategy=self.id,
            symbol=pos.symbol,
            side="SELL",
            order_type="LIMIT",
            qty=qty,
            notional=qty * b1.best_bid,
            purpose="exit",
            reason=(
                f"{coin}: lock-in remainder {qty} {coin} of position {pos.id} is below the {cyc['leg2_symbol']} "
                f"minimum order and leg 1 is done, so it is sold back on {pos.symbol} near bid "
                f"{fmt(b1.best_bid, m1.tick_decimals)} ({fmt(qty * b1.best_bid, 2)} {m1.quote}) to close the cycle."
            ),
            features={
                "exit_reason": "lockin",
                "note": "lockin_remainder",
                "cycle": cid,
                "position_id": pos.id,
                "books_used": [pos.symbol],
            },
            linked_position_id=pos.id,
            taker=True,
        )
        self._note_pending(ctx, cyc, "exit", intent)
        d["action"] = f"sell the remainder {qty} on {pos.symbol} ({intent.id})"
        return [intent]

    def _revert_exits(self, ctx: StrategyContext) -> list[Intent]:
        """Exit revert and degraded lock-in positions in their own book once the deviation closed."""
        out: list[Intent] = []
        for pos in sorted(ctx.portfolio.positions_of(self.id), key=lambda p: p.id):
            cid, cyc = self._cycle_for(pos)
            mode = pos.meta.get("mode")
            degraded = mode == "lockin" and self._is_degraded(cyc, pos)
            if mode != "revert" and not degraded:
                continue
            d = self._diag["cycles"].setdefault(cid, {})
            d.update({"mode": "degraded" if degraded else "revert", "coin": cyc["coin"], "position_id": pos.id,
                      "position_qty": str(pos.qty)})
            leg2 = cyc.get("leg2_symbol")
            if (
                pos.meta.get("exit_pending")
                or self._pending(ctx, cyc, "exit")
                or self._pending(ctx, cyc, "stop")  # the degrade stop (from on_fill) goes out first this tick
                or self._open_sell_qty(ctx, pos.symbol) > 0
                or (leg2 and self._open_sell_qty(ctx, str(leg2)) > 0)
            ):
                d["exit"] = "a SELL of this position is working"
                continue
            target, entry_dev = self._targets(pos, cyc)
            cf = ctx.features.cross.get(pos.base)
            if target is None or entry_dev is None or cf is None:
                d["exit"] = "no deviation target or cross features"
                continue
            stale = self._stale(ctx, [cf["tmn_symbol"], cf["usdt_symbol"], USDTTMN])
            if stale is not None:
                d["exit"] = f"wait: {stale}"
                continue
            dev = cf["dev_mid_pct"]
            # bought the TMN book -> profit when the deviation rises back; USDT book -> when it falls
            closed = dev >= target if pos.quote == "TMN" else dev <= target
            d.update({"dev_mid_pct": _s(dev), "target_dev_pct": _s(target), "entry_dev_pct": _s(entry_dev)})
            if not closed:
                d["exit"] = f"holding: deviation {pct(dev, 3)} has not reached the target {pct(target, 3)}"
                continue
            m, b = ctx.markets.get(pos.symbol), ctx.books.get(pos.symbol)
            if m is None or b is None or b.best_bid is None:
                d["exit"] = f"target reached but no {pos.symbol} bid"
                continue
            qty = quantize_down(pos.qty, m.step_decimals)
            problem = _size_problem(m, qty, b.best_bid, ctx)
            if problem is not None:
                d["exit"] = f"target reached but {problem}"
                continue
            kind = "degraded lock-in" if degraded else "revert"
            intent = Intent(
                id=ctx.next_id(self.id, pos.symbol),
                ts=ctx.now,
                strategy=self.id,
                symbol=pos.symbol,
                side="SELL",
                order_type="LIMIT",
                qty=qty,
                notional=qty * b.best_bid,
                price=None,
                purpose="exit",
                reason=(
                    f"{pos.base}: {kind} position {pos.id}: the mid deviation is now {pct(dev, 3)} and has reached "
                    f"the target {pct(target, 3)} set at entry: {self._target_text(target, entry_dev)} -> sell {qty} "
                    f"{pos.base} back on {pos.symbol} (the book it was bought in) as maker near bid "
                    f"{fmt(b.best_bid, m.tick_decimals)}; bought at {fmt(pos.entry_price, m.tick_decimals)}."
                ),
                features={
                    "exit_reason": "target",
                    "mode": "degraded" if degraded else "revert",
                    "cycle": cid,
                    "position_id": pos.id,
                    "dev_mid_pct": str(dev),
                    "target_dev_pct": str(target),
                    "entry_dev_pct": str(entry_dev),
                    "books_used": sorted({pos.symbol, cf["tmn_symbol"], cf["usdt_symbol"], USDTTMN}),
                },
                linked_position_id=pos.id,
                taker=False,
            )
            self._note_pending(ctx, cyc, "exit", intent)
            d["exit"] = f"target reached: {intent.id}"
            out.append(intent)
        return out

    # ------------------------------------------------------------ fills / orders

    def on_fill(self, fill: Fill, position: Position | None, ctx: StrategyContext) -> list[Intent]:
        """A leg-1 BUY fill of a lock-in cycle -> leg 2 for the new coins (or the degrade stop).

        ``position`` is the position after this fill was applied (``PositionManager.on_fill``).
        """
        if (
            not isinstance(position, Position)  # None, or a ClosedPosition (nothing left to hedge)
            or ctx.flatten_phase > 0
            or position.strategy != self.id
            or position.meta.get("mode") != "lockin"
            or fill.side != "BUY"
            or fill.symbol != position.symbol
        ):
            return []
        cid, cyc = self._cycle_for(position)
        d: dict[str, Any] = self._diag.setdefault("cycles", {}).setdefault(cid, {})
        d.update(
            {
                "mode": "lockin",
                "coin": cyc["coin"],
                "position_id": position.id,
                "position_qty": str(position.qty),
            }
        )
        if cyc.get("status") == "entry":
            cyc["status"] = "open"
        if self._is_degraded(cyc, position):
            d["action"] = "leg-1 fill on a degraded cycle: no leg 2 (revert mode)"
            return []
        leg2 = str(cyc["leg2_symbol"])
        covered = self._open_sell_qty(ctx, leg2) + self._pending_unplaced_qty(ctx, cyc, "leg2")
        # every coin not covered yet: the new fill, plus an earlier remainder (a vetoed leg 2, fills
        # too small for the leg-2 minimum) that would otherwise wait for this leg 2 to finish
        unhedged = position.qty - covered
        d["unhedged"] = str(unhedged)
        if unhedged <= 0:
            d["action"] = (
                f"fill {fill.qty}: nothing unhedged ({position.qty} held, {covered} covered by leg 2)"
            )
            return []
        return self._hedge(ctx, cid, cyc, position, unhedged, fill, d)

    def on_order_done(self, order: Order, ctx: StrategyContext) -> list[Intent]:
        """An S1 entry that ended without any fill ends its cycle; everything else is tended per tick."""
        if order.req.strategy != self.id:
            return []
        did = order.req.decision_id
        held = any(p.meta.get("entry_decision_id") == did for p in ctx.portfolio.positions_of(self.id))
        # ``filled_qty`` is the last placement's; an earlier placement may have opened the position
        if order.req.purpose == "entry" and order.filled_qty == 0 and not held:
            self.cycles.pop(did, None)
        return []

    # ------------------------------------------------------------ state

    def state(self) -> dict[str, Any]:
        return {
            "last_entry_ts": dict(sorted(self.last_entry_ts.items())),
            "cycles": {k: copy.deepcopy(self.cycles[k]) for k in sorted(self.cycles)},
        }

    def load_state(self, state: dict[str, Any] | None) -> None:
        state = state or {}
        self.last_entry_ts = {k: float(v) for k, v in (state.get("last_entry_ts") or {}).items()}
        self.cycles = {k: copy.deepcopy(v) for k, v in sorted((state.get("cycles") or {}).items())}

    def prune(self, open_position_entry_ids: set[str]) -> None:
        """Drop cycles whose position has closed (entries not filled yet are kept; ``on_tick`` prunes too)."""
        for cid in sorted(self.cycles):
            if cid not in open_position_entry_ids and self.cycles[cid].get("status") != "entry":
                del self.cycles[cid]

    def diagnostics(self) -> dict[str, Any]:
        return self._diag

"""S1 — CrossBook (PRD-03 §3): trade the gap between a coin's TMN book and its USDT book.

Wallex prices X/TMN and X/USDT with different participants; during fast moves one book lags.
Implied USDT price of the TMN book = XTMN / USDTTMN. When one book is cheaper than the other by
more than fees + margin we:

* **Lock-in** (preferred, direction-neutral): buy in the cheap book (leg 1, taker by default),
  then immediately sell the same coin quantity in the expensive book (leg 2, crossing LIMIT).
      TMN cheap : edge = bid(XUSDT) * bid(USDTTMN) / ask(XTMN) - 1,  cost = fee1(XTMN) + taker(XUSDT)
      USDT cheap: edge = bid(XTMN) / (ask(XUSDT) * ask(USDTTMN)) - 1, cost = fee1(XUSDT) + taker(XTMN)
      enter when edge >= cost + margin_min, margin_min = max(config, plan).
* **Revert** (bounded directional): when no lock-in edge exists but the mid deviation is large vs
  its 1-h noise (|z| >= z_min and |dev| >= same-book round trip + margin), buy the cheap book only
  and sell it back in the same book once 60 % of the deviation has closed; 1 % stop (server-side
  STOP_LIMIT placed by the OrderManager), 20-min time stop. Never in a ``chaos`` regime.

If the edge has vanished when leg 1 fills, leg 2 is still sent when the loss is < leg2_max_loss_pct,
otherwise the cycle degrades to revert mode (stop + time stop), logged as ``lockin_degraded_to_revert``.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from wta.config import S1Cfg
from wta.models import Fill, Intent, MarketInfo, Order, Position
from wta.strategies.base import BaseStrategy, StrategyContext, plan_enabled, plan_size_multiplier
from wta.utils.decimalutil import D, fmt, pct, quantize_down, round_price

ZERO = Decimal(0)
BALANCE_BUFFER = Decimal("0.005")
LEG_LIQ_FRACTION = Decimal("0.8")


def _qty_decimals(*markets: MarketInfo) -> int:
    return min(m.step_decimals for m in markets)


class S1CrossBook(BaseStrategy):
    id = "S1"

    def __init__(self, cfg: S1Cfg) -> None:
        self.cfg = cfg
        self.last_entry_ts: dict[str, float] = {}
        # cycles keyed by leg-1 decision id: coin, mode, side, leg symbols, hedged/unhedged qty
        self.cycles: dict[str, dict[str, Any]] = {}
        self._diag: dict[str, Any] = {}

    # ------------------------------------------------------------ helpers

    def _margin(self, ctx: StrategyContext) -> Decimal:
        plan = ctx.plan_fresh()
        m = self.cfg.margin_min_pct
        if plan is not None:
            m = max(m, plan.s1_margin_min_pct)
        return m

    def _regime_chaos(self, ctx: StrategyContext, coin: str) -> bool:
        plan = ctx.plan_fresh()
        if plan is None:
            return False
        for sym in (f"{coin}USDT", "USDTTMN"):
            call = plan.regimes.get(sym)
            if call is not None and call.regime == "chaos":
                return True
        return False

    def _to_tmn(self, quote: str, amount: Decimal, ctx: StrategyContext) -> Decimal | None:
        if quote == "TMN":
            return amount
        u = ctx.portfolio.u_bid
        return amount * u if u else None

    def _size_qty(
        self, ctx: StrategyContext, buy_m: MarketInfo, sell_m: MarketInfo | None, buy_price: Decimal,
        sell_level_qty: Decimal | None, buy_level_qty: Decimal | None, mult: Decimal,
    ) -> tuple[Decimal, dict[str, str]]:
        """Quantity for leg 1 (and leg 2): min of size budget, both legs' best-level liquidity, free quote."""
        eq = ctx.portfolio.equity_tmn
        budget_tmn = self.cfg.size_pct_equity * eq * mult
        px_tmn = self._to_tmn(buy_m.quote, buy_price, ctx)
        if not px_tmn:
            return ZERO, {"why": "no USDTTMN price"}
        limits = {"budget_qty": budget_tmn / px_tmn}
        if buy_level_qty is not None:
            limits["leg1_liquidity_qty"] = LEG_LIQ_FRACTION * buy_level_qty
        if sell_level_qty is not None:
            limits["leg2_liquidity_qty"] = LEG_LIQ_FRACTION * sell_level_qty
        free_quote = ctx.portfolio.free.get(buy_m.quote, ZERO) * (1 - BALANCE_BUFFER)
        limits["free_quote_qty"] = free_quote / buy_price / (1 + buy_m.taker_fee)
        qty = min(limits.values())
        dec = _qty_decimals(buy_m, sell_m) if sell_m else buy_m.step_decimals
        qty = quantize_down(max(qty, ZERO), dec)
        binding = min(limits, key=lambda k: limits[k])
        return qty, {"binding_limit": binding, **{k: str(quantize_down(v, dec)) for k, v in sorted(limits.items())}}

    @staticmethod
    def _min_ok(m: MarketInfo, qty: Decimal, price: Decimal) -> bool:
        return qty > 0 and qty >= m.min_qty and qty * price >= m.min_notional

    # ------------------------------------------------------------ main loop

    def on_tick(self, ctx: StrategyContext) -> list[Intent]:
        cfg = self.cfg
        self._diag = {"coins": {}}
        if not cfg.enabled or ctx.flatten_phase > 0 or not plan_enabled(ctx, self.id):
            self._diag["status"] = "disabled" if not cfg.enabled else "flatten/plan"
            return []
        intents: list[Intent] = self._revert_exits(ctx)
        mult = plan_size_multiplier(ctx)
        margin = self._margin(ctx)
        self._diag.update({"margin_min_pct": str(margin), "size_multiplier": str(mult)})
        open_s1 = ctx.portfolio.positions_of(self.id)
        busy_symbols = {p.symbol for p in open_s1} | {o.req.symbol for o in ctx.portfolio.open_orders_of(self.id)}
        n_open = len(open_s1) + len([i for i in intents if i.purpose == "entry"])
        n_revert = len([p for p in open_s1 if p.meta.get("mode") == "revert"])
        for coin in sorted(cfg.coins):
            cf = ctx.features.cross.get(coin)
            d: dict[str, Any] = {}
            self._diag["coins"][coin] = d
            if cf is None:
                d["skip"] = "no cross features"
                continue
            d.update({
                "edge_tmn_cheap": str(cf["edge_tmn_cheap_pct"].quantize(Decimal("0.00001"))),
                "edge_usdt_cheap": str(cf["edge_usdt_cheap_pct"].quantize(Decimal("0.00001"))),
                "cost_tmn_cheap": str(cf["cost_lockin_tmn_cheap"]),
                "cost_usdt_cheap": str(cf["cost_lockin_usdt_cheap"]),
                "dev_mid": str(cf["dev_mid_pct"].quantize(Decimal("0.00001"))),
                "z": str(cf["dev_zscore"].quantize(Decimal("0.01"))) if cf.get("dev_zscore") is not None else None,
            })
            if mult <= 0:
                d["skip"] = "plan size_multiplier 0"
                continue
            if ctx.now - self.last_entry_ts.get(coin, -1e18) < cfg.cooldown_s:
                d["skip"] = "cooldown"
                continue
            if cf["tmn_symbol"] in busy_symbols or cf["usdt_symbol"] in busy_symbols:
                d["skip"] = "symbol busy"
                continue
            if n_open >= cfg.max_concurrent:
                d["skip"] = "max_concurrent"
                continue
            intent = None
            if cfg.lockin_enabled:
                intent = self._lockin(ctx, cf, margin, mult, d)
            if intent is None and cfg.revert_enabled and n_revert < cfg.revert_max_concurrent:
                if self._regime_chaos(ctx, coin):
                    d["revert_skip"] = "chaos regime"
                else:
                    intent = self._revert(ctx, cf, margin, mult, d)
            if intent is not None:
                intents.append(intent)
                n_open += 1
                busy_symbols |= {cf["tmn_symbol"], cf["usdt_symbol"]}
                if intent.features.get("mode") == "revert":
                    n_revert += 1
        return intents

    def _lockin(self, ctx: StrategyContext, cf: dict[str, Any], margin: Decimal, mult: Decimal,
                d: dict[str, Any]) -> Intent | None:
        coin = cf["coin"]
        t_sym, u_sym = cf["tmn_symbol"], cf["usdt_symbol"]
        tm, um = ctx.markets[t_sym], ctx.markets[u_sym]
        tb, ub = ctx.books[t_sym], ctx.books[u_sym]
        e_t, c_t = cf["edge_tmn_cheap_pct"], cf["cost_lockin_tmn_cheap"]
        e_u, c_u = cf["edge_usdt_cheap_pct"], cf["cost_lockin_usdt_cheap"]
        if e_t >= c_t + margin:
            side, edge, cost = "TMN_CHEAP", e_t, c_t
            buy_m, sell_m, buy_b, sell_b = tm, um, tb, ub
        elif e_u >= c_u + margin:
            side, edge, cost = "USDT_CHEAP", e_u, c_u
            buy_m, sell_m, buy_b, sell_b = um, tm, ub, tb
        else:
            d["lockin"] = "edge below cost+margin"
            return None
        taker = self.cfg.leg1_taker
        if taker:
            price = buy_b.best_ask
        else:
            price = buy_b.best_bid + buy_m.tick if buy_b.best_bid + buy_m.tick < buy_b.best_ask else buy_b.best_bid
        price = round_price(buy_m, price, "BUY")
        qty, sizing = self._size_qty(ctx, buy_m, sell_m, price, sell_b.best_bid_qty, buy_b.best_ask_qty if taker else None, mult)
        if not (self._min_ok(buy_m, qty, price) and self._min_ok(sell_m, qty, sell_b.best_bid)):
            d["lockin"] = f"size {qty} below min notional ({sizing.get('binding_limit')})"
            return None
        u_price = cf["u_bid"] if side == "TMN_CHEAP" else cf["u_ask"]
        if side == "TMN_CHEAP":
            how = (f"TMN book ask {fmt(tb.best_ask)} implies {fmt(tb.best_ask / u_price, 2)} USDT vs {u_sym} bid "
                   f"{fmt(ub.best_bid, 2)} -> TMN book {pct(edge)} cheap")
        else:
            how = (f"{u_sym} ask {fmt(ub.best_ask, 2)} = {fmt(ub.best_ask * u_price)} TMN vs {t_sym} bid "
                   f"{fmt(tb.best_bid)} -> USDT book {pct(edge)} cheap")
        leg1_fee = buy_m.taker_fee if taker else buy_m.maker_fee
        reason = (
            f"{coin}: {how}; cost {pct(cost)} ({pct(leg1_fee)} {'taker' if taker else 'maker'} {buy_m.symbol} + "
            f"{pct(sell_m.taker_fee)} taker {sell_m.symbol}); margin {pct(edge - cost)} >= min {pct(margin)}; "
            f"lock-in: buy {qty} {coin} on {buy_m.symbol} at {fmt(price, buy_m.tick_decimals)}, then sell on "
            f"{sell_m.symbol}; size limited by {sizing.get('binding_limit')}; plan size mult {mult}."
        )
        self.last_entry_ts[coin] = ctx.now
        iid = ctx.next_id(self.id, buy_m.symbol)
        self.cycles[iid] = {
            "coin": coin, "mode": "lockin", "side": side, "leg1_symbol": buy_m.symbol, "leg2_symbol": sell_m.symbol,
            "entry_edge": str(edge), "filled": "0", "hedged": "0", "status": "leg1",
        }
        d["lockin"] = f"ENTER {side}"
        return Intent(
            id=iid, ts=ctx.now, strategy=self.id, symbol=buy_m.symbol, side="BUY", order_type="LIMIT",
            qty=qty, notional=qty * price, price=price, purpose="entry", reason=reason,
            features={**_jsonable(cf), "mode": "lockin", "side": side, "leg2_symbol": sell_m.symbol,
                      "cost_model": "lockin", "cost_pct": str(cost), "margin_min_pct": str(margin),
                      "sizing": sizing, "books_used": sorted({t_sym, u_sym, "USDTTMN"})},
            expected_edge_pct=edge, taker=taker,
        )

    def _revert(self, ctx: StrategyContext, cf: dict[str, Any], margin: Decimal, mult: Decimal,
                d: dict[str, Any]) -> Intent | None:
        cfg = self.cfg
        z, dev = cf.get("dev_zscore"), cf["dev_mid_pct"]
        if z is None:
            d["revert"] = "z n/a (warming up)"
            return None
        if abs(z) < cfg.z_min:
            d["revert"] = f"|z| {abs(z):.2f} < {cfg.z_min}"
            return None
        coin = cf["coin"]
        # dev > 0: TMN book expensive -> USDT book cheap; dev < 0: TMN book cheap
        sym = cf["usdt_symbol"] if dev > 0 else cf["tmn_symbol"]
        m, b = ctx.markets[sym], ctx.books[sym]
        cost = m.maker_fee + m.taker_fee + (b.spread_pct or ZERO)
        if abs(dev) < cost + margin:
            d["revert"] = f"|dev| {pct(abs(dev))} < round trip {pct(cost)} + margin"
            return None
        price = b.best_bid + m.tick if b.best_bid + m.tick < b.best_ask else b.best_bid
        price = round_price(m, price, "BUY")
        qty, sizing = self._size_qty(ctx, m, None, price, None, None, mult)
        if not self._min_ok(m, qty, price):
            d["revert"] = f"size {qty} below min notional"
            return None
        stop = round_price(m, price * (1 - cfg.revert_stop_pct), "SELL")
        target_dev = dev * (1 - cfg.revert_target_fraction)
        reason = (
            f"{coin}: no lock-in edge, but mid deviation {pct(dev)} is {abs(z):.2f} sigma from its 1-h mean "
            f"(|z| >= {cfg.z_min}) and exceeds the {sym} round trip {pct(cost)} + margin {pct(margin)}; revert: "
            f"buy {qty} {coin} on {sym} (cheap book) as maker at {fmt(price, m.tick_decimals)}, exit in the same "
            f"book when the deviation closes to {pct(target_dev)} ({pct(cfg.revert_target_fraction, 0)} closed); "
            f"stop {fmt(stop, m.tick_decimals)} (-{pct(cfg.revert_stop_pct, 1)}), time stop {int(cfg.revert_max_hold_s)} s."
        )
        self.last_entry_ts[coin] = ctx.now
        iid = ctx.next_id(self.id, sym)
        self.cycles[iid] = {"coin": coin, "mode": "revert", "side": "TMN_CHEAP" if dev < 0 else "USDT_CHEAP",
                            "leg1_symbol": sym, "entry_dev": str(dev), "target_dev": str(target_dev), "status": "entry"}
        d["revert"] = "ENTER"
        return Intent(
            id=iid, ts=ctx.now, strategy=self.id, symbol=sym, side="BUY", order_type="LIMIT", qty=qty,
            notional=qty * price, price=price, stop_price=stop, purpose="entry", reason=reason,
            features={**_jsonable(cf), "mode": "revert", "cost_model": "roundtrip", "cost_pct": str(cost),
                      "margin_min_pct": str(margin), "target_dev_pct": str(target_dev), "entry_dev_pct": str(dev),
                      "time_stop_s": cfg.revert_max_hold_s, "sizing": sizing,
                      "books_used": sorted({cf["tmn_symbol"], cf["usdt_symbol"], "USDTTMN"})},
            expected_edge_pct=abs(dev), taker=False,
        )

    def _revert_exits(self, ctx: StrategyContext) -> list[Intent]:
        """Exit revert positions in the same book once the deviation has closed enough."""
        out: list[Intent] = []
        for pos in ctx.portfolio.positions_of(self.id):
            if pos.meta.get("mode") != "revert" or pos.meta.get("exit_pending"):
                continue
            coin = pos.base
            cf = ctx.features.cross.get(coin)
            if cf is None or pos.meta.get("target_dev_pct") is None:
                continue
            dev, target = cf["dev_mid_pct"], D(pos.meta["target_dev_pct"])
            entry_dev = D(pos.meta.get("dev_mid_pct", pos.meta.get("entry_dev_pct", target)))
            closed = dev >= target if entry_dev < 0 else dev <= target
            if not closed:
                continue
            book = ctx.books.get(pos.symbol)
            if book is None or book.best_bid is None:
                continue
            out.append(Intent(
                id=ctx.next_id(self.id, pos.symbol), ts=ctx.now, strategy=self.id, symbol=pos.symbol, side="SELL",
                order_type="LIMIT", qty=pos.qty, price=None, purpose="exit",
                reason=(f"{coin}: deviation now {pct(dev)} reached target {pct(target)} (entry {pct(entry_dev)}) -> "
                        f"sell {pos.qty} {coin} back on {pos.symbol} (revert target)."),
                features={"exit_reason": "target", "dev_mid_pct": str(dev), "target_dev_pct": str(target),
                          "position_id": pos.id, "books_used": [pos.symbol]},
                linked_position_id=pos.id, taker=False,
            ))
        return out

    # ------------------------------------------------------------ fills

    def on_fill(self, fill: Fill, position: Position | None, ctx: StrategyContext) -> list[Intent]:
        """Leg-1 fill of a lock-in cycle -> emit leg 2 (or degrade to revert)."""
        if position is None:
            return []
        cyc = self.cycles.get(str(position.meta.get("entry_decision_id", "")))
        if cyc is None or cyc.get("mode") != "lockin" or fill.symbol != cyc["leg1_symbol"]:
            return []
        cyc["filled"] = str(D(cyc["filled"]) + fill.qty)
        return self._hedge(cyc, position, ctx, final=False)

    def on_order_done(self, order: Order, ctx: StrategyContext) -> list[Intent]:
        cyc = self.cycles.get(order.req.decision_id)
        if cyc is None:
            return []
        if order.req.purpose == "entry":
            if order.filled_qty == 0:
                self.cycles.pop(order.req.decision_id, None)
                return []
            pos = next((p for p in ctx.portfolio.positions_of(self.id)
                        if p.meta.get("entry_decision_id") == order.req.decision_id), None)
            if cyc.get("mode") == "lockin" and pos is not None:
                return self._hedge(cyc, pos, ctx, final=True)
        return []

    def _hedge(self, cyc: dict[str, Any], pos: Position, ctx: StrategyContext, final: bool) -> list[Intent]:
        if cyc.get("status") == "degraded":
            return []
        leg2 = cyc["leg2_symbol"]
        m2, b2 = ctx.markets.get(leg2), ctx.books.get(leg2)
        if m2 is None or b2 is None or b2.best_bid is None:
            return []
        unhedged = quantize_down(D(cyc["filled"]) - D(cyc["hedged"]), m2.step_decimals)
        if unhedged <= 0:
            return []
        price = round_price(m2, b2.best_bid, "SELL")
        if not self._min_ok(m2, unhedged, price):
            if final:
                cyc["status"] = "dust_unhedged"
            return []
        # realised edge now: sell value (TMN) vs leg-1 cost (TMN)
        m1 = ctx.markets[cyc["leg1_symbol"]]
        u = ctx.portfolio.u_bid or ZERO
        buy_tmn = pos.entry_price * (u if m1.quote == "USDT" else 1)
        sell_tmn = price * (u if m2.quote == "USDT" else 1)
        fees = (m1.taker_fee if self.cfg.leg1_taker else m1.maker_fee) + m2.taker_fee
        edge_now = (sell_tmn / buy_tmn - 1 - fees) if buy_tmn else ZERO
        if edge_now < -self.cfg.leg2_max_loss_pct:
            cyc["status"] = "degraded"
            stop = round_price(m1, pos.entry_price * (1 - self.cfg.revert_stop_pct), "SELL")
            return [Intent(
                id=ctx.next_id(self.id, pos.symbol), ts=ctx.now, strategy=self.id, symbol=pos.symbol, side="SELL",
                order_type="STOP_LIMIT", qty=pos.qty, stop_price=stop,
                price=round_price(m1, stop * (1 - D("0.004")), "SELL"), purpose="stop",
                reason=(f"{cyc['coin']}: lock-in leg 2 would lose {pct(-edge_now)} (> {pct(self.cfg.leg2_max_loss_pct)}) "
                        f"-> lockin_degraded_to_revert: keep {pos.qty} on {pos.symbol} with stop {stop} and time stop "
                        f"{int(self.cfg.revert_max_hold_s)} s."),
                features={"exit_reason": "stop", "note": "lockin_degraded_to_revert", "edge_now": str(edge_now),
                          "time_stop_s": self.cfg.revert_max_hold_s, "position_id": pos.id, "books_used": [pos.symbol, leg2]},
                linked_position_id=pos.id, taker=False,
            )]
        cyc["hedged"] = str(D(cyc["hedged"]) + unhedged)
        cyc["status"] = "leg2"
        return [Intent(
            id=ctx.next_id(self.id, leg2), ts=ctx.now, strategy=self.id, symbol=leg2, side="SELL", order_type="LIMIT",
            qty=unhedged, notional=unhedged * price, price=price, purpose="leg2",
            reason=(f"{cyc['coin']}: leg 1 filled {pos.qty} on {cyc['leg1_symbol']} at {pos.entry_price}; leg 2: sell "
                    f"{unhedged} on {leg2} at bid {fmt(price, m2.tick_decimals)} (crossing), realised edge after fees "
                    f"{pct(edge_now)}."),
            features={"exit_reason": "lockin", "edge_now": str(edge_now), "position_id": pos.id,
                      "books_used": sorted({leg2, cyc['leg1_symbol'], 'USDTTMN'})},
            linked_position_id=pos.id, taker=True,
        )]

    # ------------------------------------------------------------ state

    def state(self) -> dict[str, Any]:
        return {"last_entry_ts": dict(sorted(self.last_entry_ts.items())),
                "cycles": {k: self.cycles[k] for k in sorted(self.cycles)}}

    def load_state(self, state: dict[str, Any] | None) -> None:
        state = state or {}
        self.last_entry_ts = {k: float(v) for k, v in (state.get("last_entry_ts") or {}).items()}
        self.cycles = dict(state.get("cycles") or {})

    def prune(self, open_position_entry_ids: set[str]) -> None:
        """Drop finished cycles (called by the engine after positions close)."""
        for k in list(self.cycles):
            st = self.cycles[k].get("status")
            if k not in open_position_entry_ids and st in ("leg2", "dust_unhedged", "degraded", "entry"):
                self.cycles.pop(k, None)

    def diagnostics(self) -> dict[str, Any]:
        return self._diag


def _jsonable(cf: dict[str, Any]) -> dict[str, Any]:
    return {k: (str(v) if isinstance(v, Decimal) else v) for k, v in sorted(cf.items())}

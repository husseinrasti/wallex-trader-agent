"""S1 CrossBook (PRD-03 §3) on crafted books, run through the real FeatureEngine and PositionManager.

World used below (fees from conftest: TMN maker 0.25 % / taker 0.30 %, USDT maker 0.10 % / taker
0.13 %; leg1_taker=true, so both lock-in costs are 0.30 % + 0.13 % = 0.43 %; margin_min 0.15 %
-> a lock-in needs an edge >= 0.58 %). USDTTMN bid 100,000 / ask 100,100.

* TMN cheap : BTCTMN 7,990,000,000 / 8,000,000,000, BTCUSDT 80,480.00 / 80,490.00
              edge = 80,480 x 100,000 / 8,000,000,000 - 1 = 0.600 %
* USDT cheap: BTCTMN 8,056,048,000 / 8,060,000,000, BTCUSDT 79,990.00 / 80,000.00
              edge = 8,056,048,000 / (80,000 x 100,100) - 1 = 0.600 %
* neutral   : BTCTMN 8,003,000,000 / 8,005,000,000, BTCUSDT 79,995.00 / 80,005.00 -> dev 0

Equity 50,000,000 TMN, size 20 % -> budget 10,000,000 TMN = 0.00125 BTC at 8,000,000,000.
"""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any

import pytest

from wta.config import Config, load_config
from wta.execution.positions import PortfolioState, PositionManager
from wta.features.engine import FeatureEngine
from wta.models import (
    BookSnapshot,
    Fill,
    Intent,
    Level,
    MarketInfo,
    MarketPlan,
    Order,
    OrderRequest,
    Position,
    RegimeCall,
)
from wta.strategies.base import PortfolioView, StrategyContext
from wta.strategies.s1_crossbook import S1CrossBook
from wta.utils.ids import make_client_id

from .conftest import T0, TMN_FEES, USDT_FEES

D = Decimal
TMN_CHEAP = {"t": ("7990000000", "8000000000"), "u": ("80480.00", "80490.00")}
USDT_CHEAP = {"t": ("8056048000", "8060000000"), "u": ("79990.00", "80000.00")}
NEUTRAL = {"t": ("8003000000", "8005000000"), "u": ("79995.00", "80005.00")}


def _market(symbol: str, base: str, quote: str, tick: int, step: int) -> MarketInfo:
    extra: dict[str, Any] = (
        {"min_notional": D("50000"), **TMN_FEES} if quote == "TMN" else {"min_notional": D("1"), **USDT_FEES}
    )
    return MarketInfo(symbol=symbol, base=base, quote=quote, tick_decimals=tick, step_decimals=step, **extra)


def _markets(**step_overrides: int) -> dict[str, MarketInfo]:
    items = [
        _market("USDTTMN", "USDT", "TMN", 0, 2),
        _market("BTCTMN", "BTC", "TMN", 0, 6),
        _market("BTCUSDT", "BTC", "USDT", 2, 6),
        _market("ETHTMN", "ETH", "TMN", 0, 4),
        _market("ETHUSDT", "ETH", "USDT", 2, 4),
    ]
    out = {m.symbol: m for m in items}
    for sym, step in step_overrides.items():
        out[sym] = out[sym].model_copy(update={"step_decimals": step})
    return out


MARKETS = _markets()


def _cfg(**overrides: Any) -> Config:
    base: dict[str, Any] = {"strategies.s1_crossbook.coins": ["BTC", "ETH"]}
    base.update({k if "." in k else f"strategies.s1_crossbook.{k}": v for k, v in overrides.items()})
    return load_config(overrides=base, env={})


def book(
    sym: str, bid: str, ask: str, *, bid_qty: str = "1", ask_qty: str = "1", ts: float = T0
) -> BookSnapshot:
    return BookSnapshot(
        symbol=sym,
        ts=ts,
        source="rest",
        bids=[Level(price=D(bid), qty=D(bid_qty))],
        asks=[Level(price=D(ask), qty=D(ask_qty))],
    )


def world(
    now: float = T0,
    *,
    t: tuple[str, str] = TMN_CHEAP["t"],
    u: tuple[str, str] = TMN_CHEAP["u"],
    d: tuple[str, str] = ("100000", "100100"),
    t_qty: tuple[str, str] = ("1", "1"),
    u_qty: tuple[str, str] = ("1", "1"),
    coin: str = "BTC",
    ts: dict[str, float] | None = None,
    extra: dict[str, BookSnapshot] | None = None,
) -> dict[str, BookSnapshot]:
    ts = ts or {}
    books = {
        "USDTTMN": book("USDTTMN", *d, bid_qty="1000", ask_qty="1000", ts=ts.get("USDTTMN", now)),
        f"{coin}TMN": book(f"{coin}TMN", *t, bid_qty=t_qty[0], ask_qty=t_qty[1], ts=ts.get(f"{coin}TMN", now)),
        f"{coin}USDT": book(f"{coin}USDT", *u, bid_qty=u_qty[0], ask_qty=u_qty[1], ts=ts.get(f"{coin}USDT", now)),
    }
    books.update(extra or {})
    return books


def plan(**kw: Any) -> MarketPlan:
    return MarketPlan(**{"ts": T0 - 10, "enabled_strategies": ["S1", "S2"], **kw})


class Rig:
    """One S1 instance + the real FeatureEngine (its z-score history) + a PositionManager."""

    def __init__(self, cfg: Config | None = None, markets: dict[str, MarketInfo] | None = None) -> None:
        self.cfg = cfg or _cfg()
        self.markets = markets or MARKETS
        self.fe = FeatureEngine(self.cfg, self.markets)
        self.s1 = S1CrossBook(self.cfg.strategies.s1_crossbook)
        self.pm = PositionManager(PortfolioState(initial_equity_tmn=D("50000000")), self.markets)

    def ctx(
        self,
        books: dict[str, BookSnapshot],
        now: float = T0,
        *,
        orders: tuple[Order, ...] = (),
        positions: tuple[Position, ...] | None = None,
        plan: MarketPlan | None = None,
        free: dict[str, str] | None = None,
        equity: str = "50000000",
        flatten_phase: int = 0,
    ) -> StrategyContext:
        feats = self.fe.compute(now, books, {}, sorted(books))
        if positions is None:
            positions = tuple(p for _, p in sorted(self.pm.portfolio.positions.items()))
        free_d = {k: D(v) for k, v in (free or {"TMN": "30000000", "USDT": "300", "BTC": "1"}).items()}
        view = PortfolioView(
            equity_tmn=D(equity),
            free=free_d,
            total=dict(free_d),
            positions=positions,
            open_orders=orders,
            usdt_share=D("0.5"),
            coin_exposure_pct=D("0"),
            u_bid=books["USDTTMN"].best_bid if "USDTTMN" in books else None,
        )
        return StrategyContext(
            now=now, books=books, features=feats, candles={}, markets=self.markets, portfolio=view, plan=plan,
            cfg=self.cfg, flatten_phase=flatten_phase,
        )

    def tick(self, books: dict[str, BookSnapshot], now: float = T0, **kw: Any) -> list[Intent]:
        return self.s1.on_tick(self.ctx(books, now, **kw))

    def fill(
        self, intent: Intent, qty: str, price: str | Decimal, books: dict[str, BookSnapshot], ts: float = T0
    ) -> tuple[Fill, Any]:
        """Book a fill of ``intent`` in the PositionManager (fee 0, so the position qty = fill qty)."""
        req = order_of(intent).req
        f = Fill(order_client_id=req.client_id, symbol=req.symbol, side=req.side, price=D(price), qty=D(qty), ts=ts)
        return f, self.pm.on_fill(f, req, books, intent)


def order_of(intent: Intent, *, qty: str | None = None, filled: str = "0") -> Order:
    req = OrderRequest(
        client_id=make_client_id(intent.strategy, intent.symbol, intent.ts, intent.id),
        symbol=intent.symbol,
        side=intent.side,
        type=intent.order_type,
        qty=D(qty) if qty is not None else (intent.qty or D(0)),
        price=intent.price,
        stop_price=intent.stop_price,
        strategy=intent.strategy,
        purpose=intent.purpose,
        decision_id=intent.id,
    )
    return Order(req=req, filled_qty=D(filled), created_ts=intent.ts, updated_ts=intent.ts)


def dumps(intents: list[Intent]) -> str:
    return json.dumps([i.model_dump(mode="json") for i in intents], sort_keys=True)


def only(intents: list[Intent], purpose: str | None = None) -> Intent:
    sel = [i for i in intents if purpose is None or i.purpose == purpose]
    assert len(sel) == 1, [(i.purpose, i.symbol, i.qty) for i in intents]
    return sel[0]


def lockin_position(rig: Rig, books: dict[str, BookSnapshot] | None = None) -> tuple[Intent, Fill, Position]:
    """A TMN-cheap lock-in entry at T0, filled in full (0.00125 BTC at 8,000,000,000)."""
    books = books or world()
    entry = only(rig.tick(books), "entry")
    f, pos = rig.fill(entry, "0.00125", "8000000000", books)
    assert isinstance(pos, Position)
    return entry, f, pos


# ---------------------------------------------------------------- lock-in entry


def test_lockin_tmn_cheap_exact_numbers() -> None:
    rig = Rig()
    intents = rig.tick(world())
    it = only(intents)
    assert (it.symbol, it.side, it.order_type, it.purpose, it.taker) == ("BTCTMN", "BUY", "LIMIT", "entry", True)
    assert it.expected_edge_pct == D("80480.00") * D("100000") / D("8000000000") - 1 == D("0.006")
    assert it.price == D("8000000000")  # taker: the best ask
    assert it.qty == D("0.001250")  # 10,000,000 TMN budget / 8,000,000,000
    assert it.notional == D("10000000")
    f = it.features
    assert f["mode"] == "lockin" and f["side"] == "TMN_CHEAP" and f["leg2_symbol"] == "BTCUSDT"
    assert (f["cost_model"], D(f["cost_pct"]), D(f["margin_min_pct"])) == ("lockin", D("0.0043"), D("0.0015"))
    assert f["sizing"]["binding_limit"] == "budget"
    assert (f["sizing"]["leg2_sell"], f["sizing"]["unhedged"]) == ("0.001246", "0.00000025")
    assert D(f["need_pct"]) == D("0.0058") + D("0.0002") * D("0.03")  # cost + margin + 0.02 % x 3 %
    assert f["books_used"] == ["BTCTMN", "BTCUSDT", "USDTTMN"]
    r = it.reason
    for text in (
        "BTCTMN ask 8,000,000,000 TMN / USDTTMN bid 100,000 = 80,000.00 USDT vs BTCUSDT bid 80,480.00",
        "TMN book 0.600% cheap",
        "cost 0.430% (0.300% taker BTCTMN + 0.130% taker BTCUSDT)",
        "edge - cost = 0.170% >= margin min 0.150% (config; no fresh plan)",
        # the 0.3 % fee is paid in BTC: 0.00124625 left, leg 2 sells 0.001246 (BTCUSDT step)
        "unhedged remainder: buying 0.001250 BTC leaves 0.00124625 after the 0.300% leg-1 fee (paid in BTC) "
        "and leg 2 on BTCUSDT (step 0.000001) can sell 0.001246: the unhedged 0.00000025 BTC (0.020% of the "
        "cycle) x a 3% adverse move = 0.001%, so edge 0.600% >= need 0.581%",
        "buy 0.001250 BTC on BTCTMN at 8,000,000,000 (10,000,000.00 TMN)",
        "then sell the 0.001246 BTC left after the leg-1 fee on BTCUSDT at its bid 80,480.00 (leg 2)",
        "size 0.001250 BTC limited by budget (budget 0.001250, free_quote 0.003720, leg1_liquidity 0.800000, "
        "leg2_liquidity 0.800000)",
        "no fresh plan (rules-only, size mult 1).",
    ):
        assert text in r, (text, r)
    cyc = rig.s1.cycles[it.id]
    assert (cyc["mode"], cyc["side"], cyc["leg1_symbol"], cyc["leg2_symbol"], cyc["status"]) == (
        "lockin", "TMN_CHEAP", "BTCTMN", "BTCUSDT", "entry",
    )


def test_lockin_usdt_cheap_exact_numbers() -> None:
    rig = Rig()
    it = only(rig.tick(world(**USDT_CHEAP)))
    assert (it.symbol, it.side, it.price, it.features["side"]) == ("BTCUSDT", "BUY", D("80000.00"), "USDT_CHEAP")
    assert it.expected_edge_pct == D("8056048000") / (D("80000.00") * D("100100")) - 1 == D("0.006")
    assert it.features["leg2_symbol"] == "BTCTMN"
    assert it.qty == D("0.001250")  # 10,000,000 TMN / (80,000 x 100,000)
    assert "BTCUSDT ask 80,000.00 USDT x USDTTMN ask 100,100 = 8,008,000,000 TMN vs BTCTMN bid 8,056,048,000" in it.reason
    assert "USDT book 0.600% cheap" in it.reason
    assert "cost 0.430% (0.130% taker BTCUSDT + 0.300% taker BTCTMN)" in it.reason


@pytest.mark.parametrize(
    ("usdt_bid", "trades"), [("80464.48", True), ("80464.47", False), ("80463.99", False), ("80400.00", False)]
)
def test_entry_needs_edge_at_least_cost_plus_margin(usdt_bid: str, trades: bool) -> None:
    rig = Rig()
    books = world(u=(usdt_bid, "80490.00"))
    intents = rig.tick(books)
    edge = D(usdt_bid) * D("100000") / D("8000000000") - 1
    # need = cost 0.43 % + margin 0.15 % + the unhedged 0.00000025 of 0.00125 BTC (0.02 %) x 3 %
    need = D("0.0058") + D("0.000006")
    if trades:
        assert edge == need  # exactly the need: enters (>=)
        assert only(intents).expected_edge_pct == edge
    elif edge >= D("0.0058"):  # covers fees + margin, but not the unhedged-remainder charge
        assert intents == []
        why = rig.s1.diagnostics()["coins"]["BTC"]["why_no_trade"]
        assert "the unhedged 0.00000025 BTC (0.020% of the cycle) x a 3% adverse move = 0.001%" in why
        assert "-> need 0.581%" in why
    else:
        assert intents == []
        why = rig.s1.diagnostics()["coins"]["BTC"]["why_no_trade"]
        assert f"TMN-cheap edge {edge * 100:.3f}% < cost 0.430% + margin 0.150%" in why


def test_maker_leg1_prices_inside_the_spread_and_uses_the_maker_cost() -> None:
    rig = Rig(_cfg(leg1_taker=False))
    it = only(rig.tick(world()))
    assert (it.symbol, it.price, it.taker) == ("BTCTMN", D("7990000001"), False)  # best bid + 1 tick
    assert D(it.features["cost_pct"]) == D("0.0038")  # 0.25 % maker TMN + 0.13 % taker USDT
    assert it.qty == D("0.001251")  # 10,000,000 / 7,990,000,001 rounded down
    assert "cost 0.380% (0.250% maker BTCTMN + 0.130% taker BTCUSDT)" in it.reason and "as maker" in it.reason
    # the leg-1 best level still bounds the size of a resting maker leg 1
    thin = only(Rig(_cfg(leg1_taker=False)).tick(world(t_qty=("1", "0.001"))))
    assert (thin.qty, thin.features["sizing"]["binding_limit"]) == (D("0.000800"), "leg1_liquidity")


def test_reason_names_the_plan_inputs() -> None:
    rig = Rig()
    p = plan(regimes={"BTCUSDT": RegimeCall(regime="range", confidence=0.7)}, size_multiplier=D("0.8"))
    it = only(rig.tick(world(), plan=p))
    assert "plan size mult 0.8, regime BTCUSDT range (0.70)." in it.reason
    assert it.qty == D("0.001000")  # 20 % x 50,000,000 x 0.8 / 8,000,000,000


def test_plan_margin_overrides_config_only_when_larger() -> None:
    rig = Rig()
    assert rig.tick(world(), plan=plan(s1_margin_min_pct=D("0.0020"))) == []  # need 0.63 % > 0.60 %
    diag = rig.s1.diagnostics()
    assert diag["margin_min_pct"] == "0.0020"
    assert diag["coins"]["BTC"]["need_tmn_cheap_pct"] == "0.0063000"
    rig2 = Rig()
    it = only(rig2.tick(world(), plan=plan(s1_margin_min_pct=D("0.0010"))))
    assert it.features["margin_min_pct"] == "0.0015"  # config wins
    assert "margin min 0.150% = max(config 0.150%, plan 0.100%)" in it.reason
    # a stale plan is ignored altogether
    rig3 = Rig()
    stale = plan(ts=T0 - 5000, s1_margin_min_pct=D("0.0020"))
    assert only(rig3.tick(world(), plan=stale)).features["margin_min_pct"] == "0.0015"


# ---------------------------------------------------------------- sizing


@pytest.mark.parametrize(
    ("kw", "ctx_kw", "qty", "binding"),
    [
        ({}, {}, "0.001250", "budget"),
        ({"u_qty": ("0.001", "1")}, {}, "0.000800", "leg2_liquidity"),  # leg 2 thinner: 0.8 x 0.001
        ({"t_qty": ("1", "0.0012")}, {}, "0.000960", "leg1_liquidity"),  # 0.8 x 0.0012 at the ask
        ({}, {"free": {"TMN": "5000000", "USDT": "300"}}, "0.000620", "free_quote"),  # 4,975,000 / (8e9 x 1.003)
        ({}, {"plan": plan(size_multiplier=D("0.5"))}, "0.000625", "budget"),
    ],
)
def test_size_is_the_binding_limit(kw: dict[str, Any], ctx_kw: dict[str, Any], qty: str, binding: str) -> None:
    rig = Rig()
    it = only(rig.tick(world(**kw), **ctx_kw))
    assert it.qty == D(qty)
    assert it.features["sizing"]["binding_limit"] == binding
    assert f"size {qty} BTC limited by {binding}" in it.reason


def test_leg1_qty_lands_on_a_leg2_step_after_the_coin_fee() -> None:
    rig = Rig(markets=_markets(BTCUSDT=4))  # the leg-2 market has the coarser step
    it = only(rig.tick(world()))
    # budget 0.00125 x (1 - 0.3 %) = 0.00124625 -> leg 2 can sell 0.0012; the smallest leg-1 qty
    # whose coins after the fee still cover it: 0.0012 / 0.997 = 0.00120361 -> 0.001204
    assert it.qty == D("0.001204")
    sizing = it.features["sizing"]
    assert (sizing["leg2_sell"], sizing["held_after_fee"]) == ("0.0012", "0.001200388")
    assert sizing["qty_decimals"] == 6 and sizing["unhedged"] == "0.000000388"
    leg2 = only(rig.s1.on_fill(*rig_fill_with_fee(rig, it)))
    assert leg2.qty == D("0.0012")  # leg 2 sells the planned quantity


def rig_fill_with_fee(rig: Rig, it: Intent) -> tuple[Fill, Position, StrategyContext]:
    """Fill ``it`` in full at its price with the 0.3 % taker fee taken in BTC."""
    books = world()
    req = order_of(it).req
    qty = it.qty or D(0)
    f = Fill(order_client_id=req.client_id, symbol=req.symbol, side="BUY", price=it.price or D(0), qty=qty,
             fee=qty * D("0.003"), fee_asset="BTC", ts=T0)
    pos = rig.pm.on_fill(f, req, books, it)
    assert isinstance(pos, Position)
    return f, pos, rig.ctx(books)


def test_min_notional_failure_on_leg2() -> None:
    rig = Rig()
    # leg 2 bid holds 0.0000125 -> qty 0.00001; after the 0.3 % fee (paid in BTC) 0.00000997 are left
    # and leg 2 can sell 0.000009: 80,000 TMN on leg 1 is fine, 0.72 USDT on leg 2 is not
    assert rig.tick(world(u_qty=("0.0000125", "1"))) == []
    why = rig.s1.diagnostics()["coins"]["BTC"]["why_no_trade"]
    assert "fails the minimum order: BTCUSDT notional 0.000009 x 80,480.00 = 0.72 USDT < min 1.00 USDT" in why
    assert "revert" not in why  # a lock-in edge that cannot be sized does not fall back to revert


def test_leg2_minimum_is_checked_after_the_coin_fee() -> None:
    """Review fix: 0.000013 BTC x 80,480 = 1.05 USDT passes the leg-2 minimum gross, but only
    0.000012961 BTC are held after the fee and leg 2 could sell 0.000012 (0.97 USDT < 1): no entry
    (before, leg 1 went out and the coins were later sold back unhedged at a certain loss)."""
    rig = Rig()
    assert rig.tick(world(u_qty=("0.00001625", "1"))) == []  # 80 % of the leg-2 bid = 0.000013
    why = rig.s1.diagnostics()["coins"]["BTC"]["why_no_trade"]
    assert "BTCUSDT notional 0.000012 x 80,480.00 = 0.97 USDT < min 1.00 USDT" in why


def test_min_notional_failure_on_leg1() -> None:
    rig = Rig()
    # USDT cheap, leg 2 (TMN bid) holds 0.0000125 -> qty 0.00001: leg 1 = 0.80 USDT < 1
    assert rig.tick(world(**USDT_CHEAP, t_qty=("0.0000125", "1"))) == []
    why = rig.s1.diagnostics()["coins"]["BTC"]["why_no_trade"]
    assert "BTCUSDT notional 0.000010 x 80,000.00 = 0.80 USDT < min 1.00 USDT" in why


def test_plan_size_multiplier_zero_blocks_entries_but_not_leg2() -> None:
    rig = Rig()
    entry, _, pos = lockin_position(rig)
    zero = plan(ts=T0 + 90, size_multiplier=D("0"))
    leg2 = only(rig.tick(world(T0 + 100), T0 + 100, plan=zero))  # the leg-2 retry still goes out
    assert (leg2.purpose, leg2.symbol, leg2.qty, leg2.linked_position_id) == ("leg2", "BTCUSDT", D("0.00125"), pos.id)
    rig2 = Rig()
    assert rig2.tick(world(), plan=plan(size_multiplier=D("0"))) == []
    assert rig2.s1.diagnostics()["coins"]["BTC"]["why_no_trade"] == "plan size_multiplier 0: no new entries"


# ---------------------------------------------------------------- leg 2


def test_leg2_on_fill_sells_the_fill_qty_in_the_other_book() -> None:
    rig = Rig()
    books = world()
    entry = only(rig.tick(books))
    f, pos = rig.fill(entry, "0.00125", "8000000000", books)
    assert pos.meta["mode"] == "lockin" and pos.meta["entry_decision_id"] == entry.id
    leg2 = only(rig.s1.on_fill(f, pos, rig.ctx(books)))
    assert (leg2.symbol, leg2.side, leg2.purpose, leg2.order_type, leg2.taker) == (
        "BTCUSDT", "SELL", "leg2", "LIMIT", True,
    )
    assert leg2.qty == D("0.00125") and leg2.price == D("80480.00")
    assert leg2.linked_position_id == pos.id
    assert D(leg2.features["edge_now_pct"]) == D("0.006") - D("0.0043")  # nothing moved: margin 0.17 %
    assert "sell 0.001250 BTC on BTCUSDT at bid 80,480.00 (100.60 USDT, crossing, taker 0.130%)" in leg2.reason
    assert "cycle edge now 0.170% after 0.430% fees" in leg2.reason
    assert rig.s1.cycles[entry.id]["status"] == "hedging"


def test_partial_fills_each_get_their_own_leg2() -> None:
    rig = Rig()
    books = world()
    entry = only(rig.tick(books))
    f1, pos = rig.fill(entry, "0.0005", "8000000000", books)
    leg_a = only(rig.s1.on_fill(f1, pos, rig.ctx(books, orders=(order_of(entry, filled="0.0005"),))))
    assert leg_a.qty == D("0.0005")
    # next tick: leg A is working, leg 1 fills 0.0004 more -> leg B for exactly the new coins
    books2 = world(T0 + 2)
    f2, pos = rig.fill(entry, "0.0004", "8000000000", books2, ts=T0 + 2)
    open_orders = (order_of(entry, filled="0.0009"), order_of(leg_a))
    ctx2 = rig.ctx(books2, T0 + 2, orders=open_orders)
    leg_b = only(rig.s1.on_fill(f2, pos, ctx2))
    assert leg_b.qty == D("0.0004") and pos.qty == D("0.0009")
    # the tick itself sees leg 2 working and does not add a third SELL
    assert rig.s1.on_tick(ctx2) == []
    assert rig.s1.diagnostics()["cycles"][entry.id]["action"] == "leg 2 working: wait for it"


def test_next_fill_also_hedges_an_earlier_vetoed_remainder() -> None:
    rig = Rig()
    books = world()
    entry = only(rig.tick(books))
    f1, pos = rig.fill(entry, "0.0005", "8000000000", books)
    first = only(rig.s1.on_fill(f1, pos, rig.ctx(books, orders=(order_of(entry, filled="0.0005"),))))
    assert first.qty == D("0.0005")  # ... vetoed: no order appears
    # next tick leg 1 fills 0.0004 more: leg 2 covers the new coins and the 0.0005 left unhedged
    books2 = world(T0 + 2)
    f2, pos = rig.fill(entry, "0.0004", "8000000000", books2, ts=T0 + 2)
    ctx2 = rig.ctx(books2, T0 + 2, orders=(order_of(entry, filled="0.0009"),))
    leg = only(rig.s1.on_fill(f2, pos, ctx2))
    assert (leg.purpose, leg.qty, leg.features["trigger"]) == ("leg2", D("0.0009"), "fill")
    assert "on the leg-1 fill of 0.0004" in leg.reason and "sell 0.000900 BTC on BTCUSDT" in leg.reason
    assert rig.s1.on_tick(ctx2) == []  # the same tick does not add another SELL


def test_leg2_sells_what_the_position_holds_after_a_base_coin_fee() -> None:
    rig = Rig()
    books = world()
    entry = only(rig.tick(books))
    req = order_of(entry).req
    # the 0.3 % taker fee is taken from the received BTC: the position holds 0.00124625, not 0.00125
    f = Fill(
        order_client_id=req.client_id, symbol="BTCTMN", side="BUY", price=D("8000000000"), qty=D("0.00125"),
        fee=D("0.00000375"), fee_asset="BTC", ts=T0,
    )
    pos = rig.pm.on_fill(f, req, books, entry)
    assert isinstance(pos, Position) and pos.qty == D("0.00124625")
    leg = only(rig.s1.on_fill(f, pos, rig.ctx(books)))
    assert leg.qty == D("0.001246")  # never more than held, rounded down to the BTCUSDT step


def test_on_fill_ignores_closed_positions_and_other_fills() -> None:
    rig = Rig()
    books = world()
    entry, f, pos = lockin_position(rig, books)
    leg = only(rig.s1.on_fill(f, pos, rig.ctx(books)))
    f2, closed = rig.fill(leg, "0.00125", "80480.00", books)  # leg 2 filled: the position closed
    assert not isinstance(closed, Position)
    assert rig.s1.on_fill(f2, closed, rig.ctx(books)) == []
    assert rig.s1.on_fill(f2, None, rig.ctx(books)) == []


def test_tiny_partial_fills_accumulate_until_leg2_is_sellable() -> None:
    rig = Rig()
    books = world()
    entry = only(rig.tick(books))
    working = (order_of(entry),)
    f1, pos = rig.fill(entry, "0.000005", "8000000000", books)  # 0.40 USDT: below the leg-2 minimum
    assert rig.s1.on_fill(f1, pos, rig.ctx(books, orders=working)) == []
    assert "too small for leg 2" in rig.s1.diagnostics()["cycles"][entry.id]["action"]
    f2, pos = rig.fill(entry, "0.00001", "8000000000", books)  # alone 0.80 USDT; with the first 1.21 USDT
    leg2 = only(rig.s1.on_fill(f2, pos, rig.ctx(books, orders=working)))
    assert leg2.qty == D("0.000015")


def test_vetoed_leg2_is_retried_on_the_next_tick() -> None:
    rig = Rig()
    books = world()
    entry, f, pos = lockin_position(rig, books)
    first = only(rig.s1.on_fill(f, pos, rig.ctx(books)))
    # the RiskManager vetoes it (no order appears); the same tick must not send a second one
    assert rig.s1.on_tick(rig.ctx(books)) == []
    # next tick: no leg-2 order open -> the whole unhedged remainder again, as a retry
    retry = only(rig.tick(world(T0 + 2), T0 + 2))
    assert (retry.purpose, retry.symbol, retry.qty) == ("leg2", "BTCUSDT", D("0.00125"))
    assert retry.id != first.id and retry.features["trigger"] == "retry"
    assert "retry of the unhedged remainder" in retry.reason
    # once that retry is an open order, later ticks wait for it
    assert rig.tick(world(T0 + 4), T0 + 4, orders=(order_of(retry),)) == []
    assert rig.s1.cycles[entry.id]["leg2_intents"] == 2


def test_never_two_leg2_sells_for_the_same_coins() -> None:
    rig = Rig()
    books = world()
    entry, f, pos = lockin_position(rig, books)
    leg = only(rig.s1.on_fill(f, pos, rig.ctx(books)))
    # same tick, the order already placed: still exactly one leg 2 for these coins
    assert rig.s1.on_tick(rig.ctx(books, orders=(order_of(leg),))) == []
    # a repeated fill notification for coins already covered by the open leg 2: nothing
    assert rig.s1.on_fill(f, pos, rig.ctx(books, orders=(order_of(leg),))) == []
    # leg 2 partly filled: the position shrinks and so does the open remainder -> still covered
    books2 = world(T0 + 2)
    rig.fill(leg, "0.0005", "80480.00", books2, ts=T0 + 2)
    assert pos.qty == D("0.00075")
    assert rig.tick(books2, T0 + 2, orders=(order_of(leg, filled="0.0005"),)) == []
    # the rest of leg 2 expired unfilled -> exactly the remaining 0.00075 is sent again
    again = only(rig.tick(world(T0 + 4), T0 + 4))
    assert again.qty == D("0.00075")


def test_leg2_waits_for_fresh_books_and_never_degrades_on_stale_prices() -> None:
    rig = Rig()
    entry, f, pos = lockin_position(rig)
    # BTCUSDT (leg 2) last updated 40 s ago and quoting a crash: no leg 2, no degrade
    stale = world(T0 + 40, u=("70000.00", "70010.00"), ts={"BTCUSDT": T0})
    assert rig.tick(stale, T0 + 40) == []
    assert "stale books" in rig.s1.diagnostics()["cycles"][entry.id]["action"]
    assert rig.s1.cycles[entry.id]["status"] != "degraded"
    assert only(rig.tick(world(T0 + 42), T0 + 42)).purpose == "leg2"


# ---------------------------------------------------------------- degradation to revert


@pytest.mark.parametrize(("usdt_bid", "degrades"), [("80104.00", True), ("80104.01", False), ("79500.00", True)])
def test_degrade_when_leg2_loss_reaches_the_limit(usdt_bid: str, degrades: bool) -> None:
    rig = Rig()
    entry, f, _ = lockin_position(rig)
    books = world(T0 + 2, u=(usdt_bid, "80600.00"))
    pos = rig.pm.portfolio.positions[next(iter(rig.pm.portfolio.positions))]
    out = only(rig.s1.on_fill(f, pos, rig.ctx(books, T0 + 2)))
    edge_now = D(usdt_bid) * D("100000") / D("8000000000") - 1 - D("0.0043")
    if not degrades:
        assert out.purpose == "leg2" and -edge_now < D("0.003")
        return
    assert -edge_now >= D("0.003")  # 80,104.00 -> exactly -0.30 %
    assert (out.purpose, out.order_type, out.side, out.symbol) == ("stop", "STOP_LIMIT", "SELL", "BTCTMN")
    assert out.stop_price == D("7920000000")  # entry x (1 - 1 %)
    assert out.price == D("7888320000")  # stop x (1 - 0.4 % stop-limit slippage)
    assert out.qty == D("0.00125") and out.linked_position_id == pos.id
    assert out.features["note"] == "lockin_degraded_to_revert"
    assert out.features["time_stop_s"] == 1200
    assert "lockin_degraded_to_revert" in out.reason and "time stop 1200 s" in out.reason
    assert rig.s1.cycles[entry.id]["status"] == "degraded"
    # a degraded cycle sends no leg 2 any more, also not for later leg-1 fills
    assert rig.s1.on_fill(f, pos, rig.ctx(books, T0 + 2)) == []


# both books fell: BTCUSDT 79,500 (leg 2 would realise -1.06 %) and BTCTMN 7,925,000,000, so the TMN
# book is still the cheap one (dev -0.36 %, not yet closed to the -0.29 % target): hold under the stop
DROP = {"t": ("7925000000", "7927000000"), "u": ("79500.00", "79510.00")}


def test_degrade_stop_is_re_emitted_until_the_position_has_a_stop() -> None:
    rig = Rig()
    entry, f, pos = lockin_position(rig)
    stop = only(rig.s1.on_fill(f, pos, rig.ctx(world(T0 + 2, **DROP), T0 + 2)))
    assert stop.purpose == "stop"
    assert rig.s1.on_tick(rig.ctx(world(T0 + 2, **DROP), T0 + 2)) == []  # same tick: no duplicate
    again = only(rig.tick(world(T0 + 4, **DROP), T0 + 4))  # vetoed -> retried next tick
    assert (again.purpose, again.order_type, again.qty) == ("stop", "STOP_LIMIT", D("0.00125"))
    assert again.id != stop.id and again.features["retry"] is True
    assert again.stop_price == stop.stop_price and "retry: the protective stop was not placed yet" in again.reason
    assert "holding" in rig.s1.diagnostics()["cycles"][entry.id]["exit"]
    # approved: the OrderManager sets the position's stop (and time stop) -> no more stop intents
    pos.stop_price, pos.time_stop_ts = stop.stop_price, T0 + 6 + 1200
    assert rig.tick(world(T0 + 6, **DROP), T0 + 6) == []


def test_reached_revert_target_beats_re_sending_the_stop() -> None:
    rig = Rig()
    entry, f, pos = lockin_position(rig)
    # only the USDT book fell: leg 2 loses >= 0.3 %, and the gap has closed from the USDT side
    crash = world(T0 + 2, u=("79500.00", "79510.00"))
    assert only(rig.s1.on_fill(f, pos, rig.ctx(crash, T0 + 2))).purpose == "stop"
    # the stop was vetoed; next tick the deviation (+0.51 %) is past the -0.29 % target: sell back on
    # BTCTMN instead of re-sending the stop (the exit is the protection now)
    ex = only(rig.tick(world(T0 + 4, u=("79500.00", "79510.00")), T0 + 4))
    assert (ex.purpose, ex.symbol, ex.side, ex.qty) == ("exit", "BTCTMN", "SELL", D("0.00125"))
    assert ex.features["mode"] == "degraded" and ex.features["exit_reason"] == "target"


def test_degrade_with_an_open_leg2_protects_only_the_uncovered_coins() -> None:
    rig = Rig()
    books = world()
    entry = only(rig.tick(books))
    f1, pos = rig.fill(entry, "0.0005", "8000000000", books)
    leg_a = only(rig.s1.on_fill(f1, pos, rig.ctx(books, orders=(order_of(entry, filled="0.0005"),))))
    # leg A works on BTCUSDT; both books drop, then leg 1 fills 0.0004 more
    f2, pos = rig.fill(entry, "0.0004", "8000000000", world(T0 + 2, **DROP), ts=T0 + 2)
    working = (order_of(entry, filled="0.0009"), order_of(leg_a))
    stop = only(rig.s1.on_fill(f2, pos, rig.ctx(world(T0 + 2, **DROP), T0 + 2, orders=working)))
    assert (stop.purpose, stop.order_type, stop.symbol) == ("stop", "STOP_LIMIT", "BTCTMN")
    assert stop.qty == D("0.0004")  # 0.0009 held - 0.0005 already being sold by leg A
    assert "(0.000500 BTC are already in leg-2 orders on BTCUSDT)" in stop.reason
    assert rig.s1.cycles[entry.id]["status"] == "degraded"
    # leg A keeps working (it must finish); no exit and no second stop meanwhile
    assert rig.s1.on_tick(rig.ctx(world(T0 + 2, **DROP), T0 + 2, orders=working)) == []
    # a stop retry while leg-2 orders cover every coin waits instead of overselling
    covered = (order_of(leg_a, qty="0.0009"),)
    assert rig.tick(world(T0 + 4, **DROP), T0 + 4, orders=covered) == []
    assert "the protective stop waits" in rig.s1.diagnostics()["cycles"][entry.id]["action"]


def test_degraded_position_gets_revert_exits_from_the_cycle_deviation() -> None:
    rig = Rig()
    entry, f, pos = lockin_position(rig)
    stop = only(rig.s1.on_fill(f, pos, rig.ctx(world(T0 + 2, u=("79500.00", "79510.00")), T0 + 2)))
    pos.stop_price = stop.stop_price  # the OrderManager placed it
    cyc = rig.s1.cycles[entry.id]
    entry_dev = D(cyc["entry_dev_pct"])
    target = D(cyc["target_dev_pct"])
    assert entry_dev < 0 and target == entry_dev * D("0.4")
    assert "target_dev_pct" not in pos.meta  # lock-in meta has none: the cycle provides it
    # the deviation is still wide open (TMN book still cheap): hold
    still = world(T0 + 10, t=("7990000000", "8000000000"), u=("80480.00", "80490.00"))
    assert rig.tick(still, T0 + 10) == []
    assert "holding" in rig.s1.diagnostics()["cycles"][entry.id]["exit"]
    # the TMN book caught up (neutral books, dev 0 >= target): sell back on BTCTMN
    stop_order = order_of(stop)
    ex = only(rig.tick(world(T0 + 20, **NEUTRAL), T0 + 20, orders=(stop_order,)))
    assert (ex.purpose, ex.symbol, ex.side, ex.qty) == ("exit", "BTCTMN", "SELL", D("0.00125"))
    assert ex.features["exit_reason"] == "target" and ex.features["mode"] == "degraded"
    assert ex.linked_position_id == pos.id
    assert "degraded lock-in position" in ex.reason and f"target {target * 100:.3f}%" in ex.reason


def test_degraded_state_survives_a_lost_cycle() -> None:
    rig = Rig()
    entry, f, pos = lockin_position(rig)
    pos.stop_price = D("7920000000")  # degraded earlier; the strategy state was lost since
    rig.s1.load_state({})
    ex = only(rig.tick(world(T0 + 20, **NEUTRAL), T0 + 20))
    assert ex.purpose == "exit" and ex.features["mode"] == "degraded"
    assert rig.s1.cycles[entry.id]["rebuilt_from_position"] is True


# ---------------------------------------------------------------- revert mode


def _warm(rig: Rig, n: int = 40, step: float = 2.0, end: float = T0) -> None:
    """Feed the FeatureEngine n neutral ticks whose deviation wobbles by +-0.005 %."""
    for i in range(n):
        now = end - step * (n - i)
        mid_tmn = D("8004000000") + (D("400000") if i % 2 else D("-400000"))
        books = world(now, t=(str(mid_tmn - 1000000), str(mid_tmn + 1000000)), u=NEUTRAL["u"])
        rig.fe.compute(now, books, {}, sorted(books))


# BTCTMN 8,054,000,000 / 8,066,056,000 (mid 8,060,028,000): implied 80,560.00 USDT vs BTCUSDT mid
# 80,000 -> dev +0.700 % against a 1-h mean of 0 (the warm-up wobbles +-0.005 %). The executable
# USDT-cheap edge is only 8,054,000,000 / (80,005 x 100,100) - 1 = 0.568 % < 0.58 % -> no lock-in.
# Revert in the USDT book: the exit at 40 % of the gap captures 60 % x 0.700 % = 0.420 % >= round
# trip 0.2425 % (0.10 % maker + 0.13 % taker + 0.0125 % spread) + margin 0.15 % = 0.3925 %.
SPIKE = {"t": ("8054000000", "8066056000"), "u": NEUTRAL["u"]}


def test_revert_entry_after_warmup() -> None:
    rig = Rig()
    _warm(rig)
    it = only(rig.tick(world(**SPIKE)))
    dev = D(it.features["dev_mid_pct"])
    assert dev == D("0.007") and abs(D(it.features["dev_mean_1h"])) < D("1e-20")  # the warm-up mean: 0
    assert (it.symbol, it.side, it.purpose, it.taker) == ("BTCUSDT", "BUY", "entry", False)
    assert it.price == D("79995.01")  # maker: best bid + 1 tick
    assert it.qty == D("0.001250")
    assert it.stop_price == D("79195.06")  # 79,995.01 x 0.99 = 79,195.0599, rounded up as a SELL
    assert abs(D(it.features["target_dev_pct"]) - D("0.0028")) < D("1e-20")  # mean 0 + 40 % x (0.7 % - 0)
    assert it.features["time_stop_s"] == 1200 and it.features["mode"] == "revert"
    assert it.features["cost_model"] == "roundtrip"
    cost = D("0.001") + D("0.0013") + D("10") / D("80000")  # maker + taker + spread
    assert D(it.features["cost_pct"]) == cost
    # the trade is worth only what the exit captures: 60 % of the gap to the mean, not the whole |dev|
    assert it.expected_edge_pct is not None and abs(it.expected_edge_pct - D("0.0042")) < D("1e-20")
    assert it.expected_edge_pct >= cost + D("0.0015")
    assert D(it.features["revert_capture_pct"]) == it.expected_edge_pct
    assert D(it.features["dev_zscore"]) >= 2
    assert "sigma from its 1-h mean 0.000%" in it.reason and "stop 79,195.06 (entry -1.0%)" in it.reason
    assert "the exit at the target 0.280% (= mean + 40% x (dev - mean), i.e. 60% of the gap closed) captures 0.420%" in it.reason
    assert rig.s1.cycles[it.id]["mode"] == "revert"


def test_revert_needs_the_captured_part_to_cover_cost_and_margin() -> None:
    """Review fix: dev +0.47 % at the old |dev| >= cost + margin threshold (0.3925 %) is no entry any
    more: the exit at 60 % of the gap captures only 0.282 %, less than the 0.2425 % + 0.15 % needed."""
    rig = Rig(_cfg(lockin_enabled=False))
    _warm(rig)
    # mid 8,041,602,000 / 100,050 = 80,376.0 USDT -> dev +0.470 %
    assert rig.tick(world(t=("8040602000", "8042602000"), u=NEUTRAL["u"])) == []
    why = rig.s1.diagnostics()["coins"]["BTC"]["why_no_trade"]
    assert "revert: the exit captures 60% of |dev 0.470% - 1-h mean 0.000%| = 0.282% < BTCUSDT round trip" in why


def test_revert_target_is_measured_from_the_1h_mean() -> None:
    """Review fix: with a structural mean of +0.30 %, a deviation of +0.70 % reverts toward +0.30 %,
    not toward 0: the gap is 0.40 %, the exit captures 0.24 % < 0.3925 % -> no entry (the old rule
    entered on |dev| and set a target of +0.28 %, beyond the mean, which a full reversion never hits)."""
    rig = Rig(_cfg(lockin_enabled=False))
    for i in range(40):  # 40 samples around +0.30 % (+-0.005 %)
        now = T0 - 2.0 * (40 - i)
        mid = D("8028012000") + (D("400000") if i % 2 else D("-400000"))  # 80,240 USDT -> +0.30 %
        books = world(now, t=(str(mid - 1000000), str(mid + 1000000)), u=NEUTRAL["u"])
        rig.fe.compute(now, books, {}, sorted(books))
    assert rig.tick(world(**SPIKE)) == []
    why = rig.s1.diagnostics()["coins"]["BTC"]["why_no_trade"]
    assert "the exit captures 60% of |dev 0.700% - 1-h mean 0.300%| = 0.240%" in why


def test_revert_in_the_tmn_book_when_it_is_the_cheap_one() -> None:
    # lock-in off: BTCTMN mid 7,899,948,000 -> implied 78,960.00 USDT vs 80,000 -> dev -1.300 %; the TMN
    # round trip is 0.25 % + 0.30 % + spread 2,000,000 / 7,899,948,000 = 0.5753 %, + margin 0.15 %:
    # the exit captures 60 % x 1.3 % = 0.78 % >= 0.7253 %
    rig = Rig(_cfg(lockin_enabled=False))
    _warm(rig)
    books = world(t=("7898948000", "7900948000"), u=NEUTRAL["u"])
    it = only(rig.tick(books))
    dev = D(it.features["dev_mid_pct"])
    assert dev == D("-0.013") and D(it.features["dev_zscore"]) <= -2
    assert (it.symbol, it.side, it.price, it.taker) == ("BTCTMN", "BUY", D("7898948001"), False)
    assert it.qty == D("0.001265")  # 10,000,000 TMN / 7,898,948,001
    assert it.stop_price == D("7819958521")  # x 0.99 = 7,819,958,520.99, rounded up as a SELL
    cost = D("0.0025") + D("0.003") + D("2000000") / D("7899948000")
    assert D(it.features["cost_pct"]) == cost and it.expected_edge_pct is not None
    assert abs(it.expected_edge_pct - D("0.0078")) < D("1e-20") and it.expected_edge_pct >= cost + D("0.0015")
    assert abs(D(it.features["target_dev_pct"]) - D("-0.0052")) < D("1e-20")  # 40 % of -1.3 %
    assert "lock-in disabled by config" in rig.s1.diagnostics()["coins"]["BTC"]["lockin"]
    _, pos = rig.fill(it, "0.001265", "7898948001", books)
    # the TMN book caught up (dev ~0 >= target -0.52 %): sell back on BTCTMN
    ex = only(rig.tick(world(T0 + 60, **NEUTRAL), T0 + 60))
    assert (ex.purpose, ex.symbol, ex.qty, ex.linked_position_id) == ("exit", "BTCTMN", D("0.001265"), pos.id)


def test_revert_needs_the_zscore_warmup() -> None:
    rig = Rig()
    _warm(rig, n=10)
    assert rig.tick(world(**SPIKE)) == []
    assert "warming up" in rig.s1.diagnostics()["coins"]["BTC"]["why_no_trade"]


def test_chaos_regime_blocks_revert() -> None:
    rig = Rig()
    _warm(rig)
    chaos = plan(regimes={"BTCUSDT": RegimeCall(regime="chaos", confidence=0.9)})
    assert rig.tick(world(**SPIKE), plan=chaos) == []
    why = rig.s1.diagnostics()["coins"]["BTC"]["why_no_trade"]
    assert "revert blocked: plan regime chaos on BTCUSDT (conf 0.90)" in why


def test_rules_only_chaos_blocks_revert() -> None:
    rig = Rig()
    _warm(rig)
    books = world(**SPIKE)
    ctx = rig.ctx(books)
    ctx.features.symbols["USDTTMN"].update({"atr_15m_pct": D("0.009"), "atr_15m_pct_median_24h": D("0.003")})
    assert rig.s1.on_tick(ctx) == []
    assert "rules regime chaos on USDTTMN" in rig.s1.diagnostics()["coins"]["BTC"]["why_no_trade"]


def test_chaos_does_not_block_lockin() -> None:
    rig = Rig()
    chaos = plan(regimes={"BTCUSDT": RegimeCall(regime="chaos", confidence=0.9)})
    assert only(rig.tick(world(), plan=chaos)).features["mode"] == "lockin"


def test_revert_exit_when_the_deviation_closes() -> None:
    rig = Rig()
    _warm(rig)
    books = world(**SPIKE)
    entry = only(rig.tick(books))
    _, pos = rig.fill(entry, "0.00125", "79995.01", books)
    assert pos.meta["mode"] == "revert" and pos.meta["target_dev_pct"] == entry.features["target_dev_pct"]
    assert pos.stop_price == D("79195.06") and pos.time_stop_ts == T0 + 1200
    target = D(pos.meta["target_dev_pct"])
    # dev still above the target (+0.70 % -> about +0.31 % > +0.28 %): hold
    half = world(T0 + 60, t=("8028000000", "8030000000"), u=NEUTRAL["u"])
    assert rig.tick(half, T0 + 60) == []
    assert "holding" in rig.s1.diagnostics()["cycles"][entry.id]["exit"]
    # dev back to 0 <= target: sell back in the same (USDT) book
    ex = only(rig.tick(world(T0 + 120, **NEUTRAL), T0 + 120))
    assert (ex.purpose, ex.symbol, ex.side, ex.qty, ex.taker) == ("exit", "BTCUSDT", "SELL", D("0.00125"), False)
    assert ex.features["exit_reason"] == "target" and ex.linked_position_id == pos.id
    assert f"has reached the target {target * 100:.3f}% set at entry" in ex.reason
    # review fix: the parenthetical states the real relation (target = mean + 40 % x (entry - mean))
    assert "60% of the gap between the entry deviation 0.700% and its 1-h mean 0.000% closed" in ex.reason
    assert "(target = mean + 40% x (entry - mean))" in ex.reason
    # the exit is working (exit_pending): no second exit
    pos.meta = {**pos.meta, "exit_pending": True}
    assert rig.tick(world(T0 + 122, **NEUTRAL), T0 + 122) == []


# ---------------------------------------------------------------- gates


def test_cooldown_per_coin() -> None:
    rig = Rig(_cfg(cooldown_s=60))
    entry = only(rig.tick(world()))
    rig.s1.on_order_done(order_of(entry), rig.ctx(world()))  # the entry ended unfilled
    assert rig.tick(world(T0 + 30), T0 + 30) == []
    assert rig.s1.diagnostics()["coins"]["BTC"]["why_no_trade"] == "cooldown: last BTC entry 30s ago < 60s"
    assert only(rig.tick(world(T0 + 60), T0 + 60)).purpose == "entry"


def _two_coins(now: float = T0) -> dict[str, BookSnapshot]:
    """BTC TMN-cheap by 0.60 %, ETH TMN-cheap by 0.70 %."""
    eth = world(now, t=("199000000", "200000000"), u=("2014.00", "2015.00"), coin="ETH")
    return {**world(now), **eth}


def test_max_concurrent_takes_the_best_edge_first() -> None:
    rig = Rig(_cfg(max_concurrent=1))
    it = only(rig.tick(_two_coins()))
    assert it.symbol == "ETHTMN" and it.expected_edge_pct == D("0.007")
    why = rig.s1.diagnostics()["coins"]["BTC"]["why_no_trade"]
    assert why == "lockin candidate not taken: max_concurrent 1/1 S1 cycles open"
    rig2 = Rig(_cfg(max_concurrent=2))
    assert [i.symbol for i in rig2.tick(_two_coins())] == ["ETHTMN", "BTCTMN"]


def test_working_entries_count_toward_max_concurrent() -> None:
    rig = Rig(_cfg(max_concurrent=1))
    eth_entry = only(rig.tick(world(coin="ETH", t=("199000000", "200000000"), u=("2014.00", "2015.00"))))
    # the ETH entry is working (no fill, no position yet): BTC must wait
    assert rig.tick(world(T0 + 2), T0 + 2, orders=(order_of(eth_entry),)) == []
    assert "max_concurrent 1/1" in rig.s1.diagnostics()["coins"]["BTC"]["why_no_trade"]


def test_revert_max_concurrent() -> None:
    rig = Rig()
    _warm(rig)
    other = Position(
        id="p_S1_ETHUSDT_1", strategy="S1", symbol="ETHUSDT", base="ETH", quote="USDT", qty=D("0.01"),
        entry_price=D("2000"), entry_ts=T0 - 100, meta={"entry_decision_id": "d_other", "mode": "revert"},
    )
    assert rig.tick(world(**SPIKE), positions=(other,)) == []
    why = rig.s1.diagnostics()["coins"]["BTC"]["why_no_trade"]
    assert why == "revert candidate not taken: revert_max_concurrent 1/1 revert/degraded cycles open"


def test_symbols_used_by_any_strategy_block_the_coin() -> None:
    rig = Rig()
    s3 = Position(
        id="p_S3_BTCUSDT_1", strategy="S3", symbol="BTCUSDT", base="BTC", quote="USDT", qty=D("0.001"),
        entry_price=D("80000"), entry_ts=T0 - 100,
    )
    assert rig.tick(world(), positions=(s3,)) == []
    assert rig.s1.diagnostics()["coins"]["BTC"]["why_no_trade"] == "BTCUSDT in use (S3 position p_S3_BTCUSDT_1)"


def test_stale_books_block_entries() -> None:
    rig = Rig()
    assert rig.tick(world(T0 + 31, ts={"USDTTMN": T0}), T0 + 31) == []
    why = rig.s1.diagnostics()["coins"]["BTC"]["why_no_trade"]
    assert why == "stale books (limit 30s): USDTTMN 31.0s old"


def test_flatten_phase_emits_nothing() -> None:
    rig = Rig()
    entry, f, pos = lockin_position(rig)
    assert rig.s1.on_fill(f, pos, rig.ctx(world(), flatten_phase=1)) == []
    assert rig.tick(world(T0 + 2), T0 + 2, flatten_phase=1) == []
    assert "flatten" in rig.s1.diagnostics()["status"]


def test_plan_disabling_s1_stops_entries_but_still_hedges() -> None:
    rig = Rig()
    entry, f, pos = lockin_position(rig)
    off = plan(enabled_strategies=["S2"])
    leg2 = only(rig.tick(world(T0 + 2), T0 + 2, plan=off))
    assert leg2.purpose == "leg2"
    assert "enabled_strategies" in rig.s1.diagnostics()["status"]
    assert Rig().tick(world(), plan=off) == []


def test_unhedgeable_remainder_is_sold_back_in_the_leg1_book_once_leg1_is_done() -> None:
    rig = Rig()
    books = world()
    entry = only(rig.tick(books))
    f, pos = rig.fill(entry, "0.00001", "8000000000", books)  # 0.80 USDT: never sellable on BTCUSDT
    assert rig.s1.on_fill(f, pos, rig.ctx(books, orders=(order_of(entry, filled="0.00001"),))) == []
    # leg 1 still working: wait (more fills may make it hedgeable)
    assert rig.tick(world(T0 + 2), T0 + 2, orders=(order_of(entry, filled="0.00001"),)) == []
    ex = only(rig.tick(world(T0 + 4), T0 + 4))
    assert (ex.purpose, ex.symbol, ex.side, ex.qty) == ("exit", "BTCTMN", "SELL", D("0.00001"))
    assert ex.features["note"] == "lockin_remainder" and ex.linked_position_id == pos.id


def test_cycles_are_pruned_when_over() -> None:
    rig = Rig()
    entry, f, pos = lockin_position(rig)
    leg = only(rig.s1.on_fill(f, pos, rig.ctx(world())))
    rig.fill(leg, "0.00125", "80480.00", world())  # leg 2 filled: the position closes
    assert rig.pm.portfolio.positions == {}
    rig.tick(world(T0 + 2), T0 + 2)
    assert entry.id not in rig.s1.cycles
    # an entry that never became an order is forgotten after the entry lifetime
    rig2 = Rig()
    vetoed = only(rig2.tick(world()))
    rig2.tick(world(T0 + 10, **NEUTRAL), T0 + 10)
    assert vetoed.id in rig2.s1.cycles
    rig2.tick(world(T0 + 60, **NEUTRAL), T0 + 60)
    assert vetoed.id not in rig2.s1.cycles


# ---------------------------------------------------------------- diagnostics, state, determinism


def test_diagnostics_cover_every_coin_every_tick() -> None:
    rig = Rig()
    rig.tick(world(**NEUTRAL))
    diag = rig.s1.diagnostics()
    assert sorted(diag["coins"]) == ["BTC", "ETH"]
    btc = diag["coins"]["BTC"]
    for key in ("edge_tmn_cheap_pct", "cost_tmn_cheap_pct", "need_tmn_cheap_pct", "edge_usdt_cheap_pct",
                "cost_usdt_cheap_pct", "need_usdt_cheap_pct", "dev_mid_pct", "why_no_trade"):
        assert key in btc
    assert btc["cost_tmn_cheap_pct"] == "0.0043000" and btc["need_tmn_cheap_pct"] == "0.0058000"
    assert diag["coins"]["ETH"]["why_no_trade"].startswith("no cross features")
    json.dumps(diag)


@pytest.mark.parametrize(
    ("cfg_kw", "tick_kw", "status"),
    [
        ({"enabled": False}, {}, "disabled by config"),
        ({}, {"plan": plan(enabled_strategies=["S2"])}, "not in the plan's enabled_strategies"),
        ({}, {"flatten_phase": 1}, "flatten phase 1"),
    ],
)
def test_idle_ticks_still_report_every_coin(cfg_kw: dict[str, Any], tick_kw: dict[str, Any], status: str) -> None:
    rig = Rig(_cfg(**cfg_kw))
    assert rig.tick(world(), **tick_kw) == []  # a 0.60 % lock-in edge, but no entries now
    diag = rig.s1.diagnostics()
    assert diag["status"].startswith(status)
    btc = diag["coins"]["BTC"]
    assert btc["edge_tmn_cheap_pct"] == "0.0060000" and btc["need_tmn_cheap_pct"] == "0.0058000"
    assert btc["why_no_trade"].startswith(status)
    assert diag["coins"]["ETH"]["why_no_trade"].startswith("no cross features")


def test_lost_state_still_hedges_an_open_lockin_position() -> None:
    rig = Rig()
    entry, _, pos = lockin_position(rig)
    rig.s1.load_state(None)  # restart without the strategy state: the position meta is enough
    leg = only(rig.tick(world(T0 + 2), T0 + 2))
    assert (leg.purpose, leg.symbol, leg.qty, leg.linked_position_id) == ("leg2", "BTCUSDT", D("0.00125"), pos.id)
    cyc = rig.s1.cycles[entry.id]
    assert cyc["rebuilt_from_position"] is True and cyc["leg2_symbol"] == "BTCUSDT"


def test_state_round_trip_reproduces_behaviour() -> None:
    rig = Rig()
    entry, f, pos = lockin_position(rig)
    rig.s1.on_fill(f, pos, rig.ctx(world()))  # a vetoed leg 2 is in flight in this tick
    st = rig.s1.state()
    restored = S1CrossBook(rig.cfg.strategies.s1_crossbook)
    restored.load_state(json.loads(json.dumps(st)))
    assert restored.state() == st
    # both instances retry leg 2 identically on the next tick
    a = rig.s1.on_tick(rig.ctx(world(T0 + 2), T0 + 2))
    b = restored.on_tick(rig.ctx(world(T0 + 2), T0 + 2))
    assert dumps(a) == dumps(b) and len(a) == 1


def test_same_context_gives_identical_intents() -> None:
    def run(order: list[str]) -> tuple[str, str]:
        rig = Rig(_cfg(max_concurrent=2))
        _warm(rig)
        books = _two_coins()
        books = {k: books[k] for k in order}  # insertion order must not matter
        intents = rig.tick(books)
        return dumps(intents), json.dumps(rig.s1.state(), sort_keys=True)

    keys = sorted(_two_coins())
    first = run(keys)
    assert json.loads(first[0])
    for _ in range(3):
        assert run(list(reversed(keys))) == first
        assert run(keys) == first

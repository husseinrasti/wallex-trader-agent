"""Determinism (PRD-03 §8, PRD-05 replay): same inputs -> byte-identical decisions.

1. Strategy level: the same ``StrategyContext`` contents and the same strategy state give
   byte-identical intents (``model_dump_json``, keys *not* re-sorted) over 1000 iterations in which
   every input dict (books, markets, features, balances, candles, router order of strategies) is
   rebuilt in a different random insertion order. S1 (lock-in entry + leg 2 on the fill), S2 (a
   rebalance) and S3 (a breakout) all emit, through the real ``StrategyRouter``.
2. Engine level: two engines fed identical deterministic books on ``SimClock`` ticks (S2 initial
   allocation, an S1 lock-in cycle, fills on the paper exchange) write byte-identical
   ``decisions`` and ``orders`` logs.
"""

from __future__ import annotations

import copy
import json
import random
from decimal import Decimal
from pathlib import Path
from typing import Any

from wta.config import Config, load_config
from wta.features.engine import FeatureEngine, FeatureSnapshot
from wta.logging_.jsonl import family_files
from wta.models import BookSnapshot, Fill, Level, MarketInfo, MarketPlan, Position, RegimeCall
from wta.strategies.base import PortfolioView, StrategyContext
from wta.strategies.router import StrategyRouter
from wta.strategies.s1_crossbook import S1CrossBook
from wta.strategies.s2_usdtalloc import S2USDTAlloc
from wta.strategies.s3_momentum import S3Momentum
from wta.utils.decimalutil import D
from wta.utils.ids import make_client_id

from .conftest import T0
from .test_s3 import NOW, btc_bars, candles_for
from .test_strategies_engine import LAG_TMN_CHEAP, make_cfg, make_rig

ITERATIONS = 1000
SYMBOLS = ("USDTTMN", "BTCTMN", "BTCUSDT", "ETHTMN", "ETHUSDT")
BOOKS = {  # (bid, ask, qty): ETH TMN book 0.81 % cheap (S1), BTCUSDT at the S3 breakout close
    "USDTTMN": ("250000", "250050", "1000"),
    "ETHTMN": ("743800000", "744000000", "1"),
    "ETHUSDT": ("3000.00", "3001.00", "1"),
    "BTCUSDT": ("84400.00", "84420.00", "1"),
    "BTCTMN": ("21100000000", "21110000000", "1"),
}
PLAN = MarketPlan(
    ts=NOW - 10,
    enabled_strategies=["S1", "S2", "S3"],
    regimes={
        "BTCUSDT": RegimeCall(regime="trend_up", confidence=0.8, evidence="higher highs"),
        "USDTTMN": RegimeCall(regime="range", confidence=0.7, evidence="flat"),
    },
    usdt_target_share=Decimal("0.5"),
)
STATE: dict[str, Any] = {
    "last_fill_ts": {"SOLTMN": NOW - 5},
    "strategies": {
        "S1": {"last_entry_ts": {"BTC": NOW - 500}, "cycles": {}},
        "S2": {"last_rebalance_ts": NOW - 7200, "attempts": 3, "rebalances": 1, "attempt_filled": "0"},
        "S3": {"last_bar_seen": {"BTCUSDT": int(T0) - 1800}},
    },
}


def _cfg() -> Config:
    return load_config(
        overrides={
            "strategies.s1_crossbook.coins": ["ETH", "BTC"],
            "strategies.s3_momentum.enabled": True,
            "strategies.s3_momentum.symbols": ["BTCUSDT"],
        },
        env={},
    )


def _book(symbol: str, now: float) -> BookSnapshot:
    bid, ask, qty = BOOKS[symbol]
    return BookSnapshot(
        symbol=symbol,
        ts=now,
        source="rest",
        bids=[Level(price=D(bid), qty=D(qty))],
        asks=[Level(price=D(ask), qty=D(qty))],
    )


class World:
    """Fixed inputs; ``ctx(rng)`` rebuilds them with every dict in a random insertion order."""

    def __init__(self, cfg: Config, markets: dict[str, MarketInfo]) -> None:
        self.cfg = cfg
        self.markets = {s: markets[s] for s in SYMBOLS}
        self.candles = {"BTCUSDT": candles_for("BTCUSDT", btc_bars())}
        books = {s: _book(s, NOW) for s in SYMBOLS}
        fe = FeatureEngine(cfg, self.markets)
        self.snap = fe.compute(NOW, books, self.candles, list(SYMBOLS))  # computed once (stateful)

    @staticmethod
    def _shuffled(d: dict[Any, Any], rng: random.Random) -> dict[Any, Any]:
        items = list(d.items())
        rng.shuffle(items)
        return dict(items)

    def ctx(
        self,
        rng: random.Random,
        now: float = NOW,
        positions: tuple[Position, ...] = (),
        extra_free: dict[str, Decimal] | None = None,
    ) -> StrategyContext:
        sh = self._shuffled
        books = sh({s: _book(s, now) for s in SYMBOLS}, rng)
        snap = FeatureSnapshot(
            ts=self.snap.ts,
            symbols=sh({s: sh(dict(f), rng) for s, f in self.snap.symbols.items()}, rng),
            cross=sh({c: sh(dict(f), rng) for c, f in self.snap.cross.items()}, rng),
            dollar=sh(dict(self.snap.dollar), rng),
        )
        free = {"TMN": D("30000000"), "USDT": D("40"), **(extra_free or {})}
        portfolio = PortfolioView(
            equity_tmn=D("50000000"),
            free=sh(dict(free), rng),
            total=sh(dict(free), rng),
            positions=positions,
            open_orders=(),
            usdt_share=D("0.2"),
            coin_exposure_pct=D("0"),
            u_bid=D("250000"),
        )
        candles = sh({(s, "1"): list(self.candles.get(s, [])) for s in SYMBOLS}, rng)
        return StrategyContext(
            now=now,
            books=books,
            features=snap,
            candles=candles,
            markets=sh(dict(self.markets), rng),
            portfolio=portfolio,
            plan=PLAN,
            cfg=self.cfg,
            rng_seed=self.cfg.engine.rng_seed,
        )


def _router(cfg: Config, rng: random.Random) -> StrategyRouter:
    strategies = [
        S1CrossBook(cfg.strategies.s1_crossbook),
        S2USDTAlloc(cfg.strategies.s2_usdtalloc),
        S3Momentum(cfg.strategies.s3_momentum),
    ]
    rng.shuffle(strategies)
    router = StrategyRouter(strategies, cfg)  # type: ignore[arg-type]
    router.load_state(copy.deepcopy(STATE))
    return router


def _leg1(entry_id: str, qty: Decimal) -> tuple[Fill, Position]:
    fee = qty * D("0.003")
    fill = Fill(
        order_client_id=make_client_id("S1", "ETHTMN", NOW, entry_id),
        symbol="ETHTMN",
        side="BUY",
        price=D("744000000"),
        qty=qty,
        fee=fee,
        fee_asset="ETH",
        ts=NOW + 2,
    )
    position = Position(
        id="p_S1_ETHTMN_1",
        strategy="S1",
        symbol="ETHTMN",
        base="ETH",
        quote="TMN",
        qty=qty - fee,
        entry_price=D("744000000"),
        entry_ts=NOW + 2,
        meta={"entry_decision_id": entry_id, "mode": "lockin", "leg2_symbol": "ETHUSDT", "side": "BUY"},
    )
    return fill, position


def _one_run(world: World, rng: random.Random) -> bytes:
    router = _router(world.cfg, rng)
    intents = router.tick(world.ctx(rng))
    out = [i.model_dump_json().encode() for i in intents]
    entry = next(i for i in intents if i.strategy == "S1")
    fill, position = _leg1(entry.id, entry.qty or D(0))
    s1 = router.by_id("S1")
    assert s1 is not None
    ctx2 = world.ctx(rng, now=NOW + 2, positions=(position,), extra_free={"ETH": position.qty})
    out += [i.model_dump_json().encode() for i in s1.on_fill(fill, position, ctx2)]
    out.append(json.dumps(router.state()).encode())
    return b"\n".join(out)


def test_same_ctx_and_state_give_identical_intent_bytes_over_shuffled_dicts(markets: dict[str, MarketInfo]) -> None:
    world = World(_cfg(), markets)
    rng = random.Random(20261022)
    first = _one_run(world, rng)
    intents = [json.loads(line) for line in first.split(b"\n")[:-1]]
    kinds = [(i["strategy"], i["symbol"], i["purpose"]) for i in intents]
    assert kinds == [
        ("S1", "ETHTMN", "entry"),
        ("S2", "USDTTMN", "rebalance"),
        ("S3", "BTCUSDT", "entry"),
        ("S1", "ETHUSDT", "leg2"),
    ]
    for _ in range(ITERATIONS - 1):
        assert _one_run(world, rng) == first


# ---------------------------------------------------------------- engine level


async def _engine_run(tmp: Path, cfg: Config) -> tuple[bytes, bytes]:
    tmp.mkdir()
    rig = await make_rig(
        tmp, cfg=cfg, balances={"TMN": D("10000000")}, log_dir=tmp / "logs", db_path=tmp / "state.sqlite"
    )
    for i in range(45):
        rig.feed.lag = LAG_TMN_CHEAP if 5 <= i < 8 else D(0)
        rig.feed.usdt = D("266600") if i >= 3 else D("266739")  # the dollar dips: S2's maker BUY fills
        rig.feed.btc = D("82877") + (D(i % 4) if not 4 <= i < 10 else D(0))  # quiet while S1 trades
        await rig.tick()
    rig.close()

    def family(base: str) -> bytes:
        return b"".join(p.read_bytes() for p in family_files(tmp / "logs", base))

    return family("decisions"), family("orders")


async def test_two_engines_on_identical_books_write_identical_decision_logs(tmp_path: Path) -> None:
    cfg = make_cfg(tmp_path)  # one config (and config hash) for both runs; separate stores and logs
    decisions_a, orders_a = await _engine_run(tmp_path / "a", cfg)
    decisions_b, orders_b = await _engine_run(tmp_path / "b", cfg)
    assert decisions_a == decisions_b and orders_a == orders_b
    records = [json.loads(line) for line in decisions_a.splitlines()]
    purposes = {(r.get("intent") or {}).get("purpose") for r in records if r["kind"] != "outcome"}
    assert {"rebalance", "entry", "leg2"} <= purposes  # S2 and a whole S1 cycle are in the compared bytes
    pnl = [r["outcome"] for r in records if r["kind"] == "outcome" and "pnl_tmn" in r["outcome"]]
    assert pnl and pnl[0]["exit_reason"] == "lockin"


def test_order_snapshot_is_independent_of_dict_order() -> None:
    """``FeatureSnapshot.compact`` (decision-log features) sorts every level of its output."""
    snap = FeatureSnapshot(ts=T0, symbols={"B": {"y": D(1), "x": D(2)}, "A": {"z": D("0.5")}}, cross={}, dollar={})
    other = FeatureSnapshot(ts=T0, symbols={"A": {"z": D("0.5")}, "B": {"x": D(2), "y": D(1)}}, cross={}, dollar={})
    assert json.dumps(snap.compact()) == json.dumps(other.compact())

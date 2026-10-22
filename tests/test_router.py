"""StrategyRouter (PRD-03 §6): which strategy intents reach the RiskManager each tick.

Rules pinned down here: exits always pass and come first; one open order per (strategy, symbol);
a symbol held or worked by another strategy is skipped; while flattening only exits pass (and the
strategies are not even asked); a per-symbol cooldown after any fill; deterministic output order;
and a JSON-able state that survives a restart.
"""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any

import pytest

from wta.config import Config, load_config
from wta.features.engine import FeatureSnapshot
from wta.models import Intent, Order, OrderRequest, Position, Purpose
from wta.strategies.base import BaseStrategy, PortfolioView, StrategyContext
from wta.strategies.router import EXIT_PURPOSES, StrategyRouter
from wta.utils.ids import make_client_id

from .conftest import T0


class FakeStrategy(BaseStrategy):
    """Returns a fixed list of intents every tick and records how often it was asked."""

    def __init__(self, sid: str, intents: list[Intent] | None = None) -> None:
        self.id = sid
        self.intents = intents or []
        self.calls = 0
        self.loaded: dict[str, Any] | None = None
        self.counter = 0

    def on_tick(self, ctx: StrategyContext) -> list[Intent]:
        self.calls += 1
        return list(self.intents)

    def state(self) -> dict[str, Any]:
        return {"counter": self.counter, "id": self.id}

    def load_state(self, state: dict[str, Any] | None) -> None:
        self.loaded = state
        self.counter = int((state or {}).get("counter", 0))


def _intent(strategy: str, symbol: str, n: int, purpose: Purpose = "entry", linked: str | None = None) -> Intent:
    return Intent(
        id=f"d_{int(T0 * 1000)}_{strategy}_{symbol}_{n}",
        ts=T0,
        strategy=strategy,
        symbol=symbol,
        side="SELL" if purpose in EXIT_PURPOSES else "BUY",
        order_type="LIMIT",
        qty=Decimal("1"),
        purpose=purpose,
        reason=f"{strategy} {purpose} on {symbol}",
        linked_position_id=linked,
    )


def _order(strategy: str, symbol: str, purpose: Purpose = "entry") -> Order:
    decision = f"d_{strategy}_{symbol}_open"
    req = OrderRequest(
        client_id=make_client_id(strategy, symbol, T0, decision), symbol=symbol, side="BUY", type="LIMIT",
        qty=Decimal("1"), price=Decimal("1"), strategy=strategy, purpose=purpose, decision_id=decision,
    )
    return Order(req=req, created_ts=T0 - 5, updated_ts=T0 - 5)


def _position(strategy: str, symbol: str) -> Position:
    base = symbol.removesuffix("USDT").removesuffix("TMN")
    return Position(
        id=f"p_{strategy}_{symbol}", strategy=strategy, symbol=symbol, base=base,
        quote="USDT" if symbol.endswith("USDT") else "TMN", qty=Decimal("1"), entry_price=Decimal("1"), entry_ts=T0,
    )


def _ctx(
    cfg: Config,
    *,
    now: float = T0,
    positions: tuple[Position, ...] = (),
    orders: tuple[Order, ...] = (),
    flatten_phase: int = 0,
) -> StrategyContext:
    view = PortfolioView(
        equity_tmn=Decimal("5000000"), free={}, total={}, positions=positions, open_orders=orders,
        usdt_share=Decimal("0"), coin_exposure_pct=Decimal("0"), u_bid=Decimal("266720"),
    )
    return StrategyContext(now=now, books={}, features=FeatureSnapshot(ts=now), candles={}, markets={},
                           portfolio=view, plan=None, cfg=cfg, flatten_phase=flatten_phase)


def _ids(intents: list[Intent]) -> list[str]:
    return [i.id for i in intents]


# ---------------------------------------------------------------- exits


def test_exits_always_pass_and_come_first(cfg: Config) -> None:
    s1 = FakeStrategy("S1", [_intent("S1", "ETHTMN", 1), _intent("S1", "BTCTMN", 2, "stop", linked="p_S1_BTCTMN")])
    s3 = FakeStrategy("S3", [_intent("S3", "BTCUSDT", 1, "exit", linked="p_S3_BTCUSDT")])
    router = StrategyRouter([s3, s1], cfg)
    router.note_fill("BTCUSDT", T0 - 1)  # cooldown never blocks an exit
    pm_exits = [
        _intent("S3", "BTCUSDT", 9, "exit", linked="p_S3_BTCUSDT"),
        _intent("S1", "BTCTMN", 8, "stop", linked="p_S1_BTCTMN"),
    ]
    ctx = _ctx(
        cfg,
        positions=(_position("S1", "BTCTMN"), _position("S3", "BTCUSDT")),
        orders=(_order("S1", "BTCTMN", "stop"), _order("S2", "BTCUSDT")),  # BTCUSDT worked by S2 too
    )
    out = router.tick(ctx, pm_exits)
    # position-manager exits first, sorted by (strategy, symbol, id) ...
    assert _ids(out[:2]) == [pm_exits[1].id, pm_exits[0].id]
    # ... then each strategy in id order; strategy exits pass despite open orders / other owners
    assert _ids(out[2:]) == [s1.intents[0].id, s1.intents[1].id, s3.intents[0].id]
    assert router.dropped == []


@pytest.mark.parametrize("purpose", sorted(EXIT_PURPOSES))
def test_every_exit_purpose_bypasses_the_filters(cfg: Config, purpose: Purpose) -> None:
    it = _intent("S1", "BTCUSDT", 1, purpose, linked="p_S1_BTCTMN")
    router = StrategyRouter([FakeStrategy("S1", [it])], cfg)
    router.note_fill("BTCUSDT", T0)
    ctx = _ctx(cfg, positions=(_position("S3", "BTCUSDT"),), orders=(_order("S1", "BTCUSDT"),))
    assert router.tick(ctx) == [it]


# ---------------------------------------------------------------- one order per (strategy, symbol)


def test_one_open_order_per_strategy_and_symbol(cfg: Config) -> None:
    s2 = FakeStrategy("S2", [_intent("S2", "USDTTMN", 1)])
    s1 = FakeStrategy("S1", [_intent("S1", "BTCTMN", 1), _intent("S1", "BTCTMN", 2), _intent("S1", "ETHTMN", 3)])
    router = StrategyRouter([s1, s2], cfg)
    out = router.tick(_ctx(cfg, orders=(_order("S2", "USDTTMN"),)), [])
    assert _ids(out) == [s1.intents[0].id, s1.intents[2].id]  # the 2nd BTCTMN intent waits
    assert router.dropped == [
        {"intent": s1.intents[1].id, "strategy": "S1", "symbol": "BTCTMN",
         "why": "open order exists for (strategy, symbol)"},
        {"intent": s2.intents[0].id, "strategy": "S2", "symbol": "USDTTMN",
         "why": "open order exists for (strategy, symbol)"},
    ]


def test_dropped_is_reset_every_tick(cfg: Config) -> None:
    s2 = FakeStrategy("S2", [_intent("S2", "USDTTMN", 1)])
    router = StrategyRouter([s2], cfg)
    router.tick(_ctx(cfg, orders=(_order("S2", "USDTTMN"),)))
    assert len(router.dropped) == 1
    assert router.tick(_ctx(cfg)) == s2.intents
    assert router.dropped == []


# ---------------------------------------------------------------- symbol ownership


def test_symbol_owned_by_another_strategy_is_skipped(cfg: Config) -> None:
    s1 = FakeStrategy("S1", [_intent("S1", "BTCTMN", 1), _intent("S1", "BTCUSDT", 2)])
    s3 = FakeStrategy("S3", [_intent("S3", "BTCTMN", 1), _intent("S3", "SOLUSDT", 2)])
    router = StrategyRouter([s1, s3], cfg)
    ctx = _ctx(cfg, positions=(_position("S1", "BTCTMN"),), orders=(_order("S3", "BTCUSDT", "stop"),))
    out = router.tick(ctx)
    # S1 may add on its own symbol; S1 is kept off BTCUSDT (S3's stop order), S3 off BTCTMN (S1's position)
    assert _ids(out) == [s1.intents[0].id, s3.intents[1].id]
    assert [(d["intent"], d["why"]) for d in router.dropped] == [
        (s1.intents[1].id, "symbol owned by S3"),
        (s3.intents[0].id, "symbol owned by S1"),
    ]


def test_first_strategy_in_a_tick_claims_the_symbol(cfg: Config) -> None:
    s1 = FakeStrategy("S1", [_intent("S1", "USDTTMN", 1)])
    s2 = FakeStrategy("S2", [_intent("S2", "USDTTMN", 1)])
    router = StrategyRouter([s2, s1], cfg)
    out = router.tick(_ctx(cfg))
    assert _ids(out) == [s1.intents[0].id]  # S1 runs before S2
    assert router.dropped[0]["why"] == "symbol owned by S1"


def test_an_exit_does_not_claim_a_symbol(cfg: Config) -> None:
    s1 = FakeStrategy("S1", [_intent("S1", "BTCUSDT", 1, "leg2", linked="p_S1_BTCTMN")])
    s3 = FakeStrategy("S3", [_intent("S3", "BTCUSDT", 1)])
    router = StrategyRouter([s1, s3], cfg)
    assert _ids(router.tick(_ctx(cfg))) == [s1.intents[0].id, s3.intents[0].id]


# ---------------------------------------------------------------- flatten


@pytest.mark.parametrize("phase", [1, 2, 3])
def test_flatten_phase_returns_only_exits(cfg: Config, phase: int) -> None:
    s1 = FakeStrategy("S1", [_intent("S1", "BTCTMN", 1)])
    s2 = FakeStrategy("S2", [_intent("S2", "USDTTMN", 1)])
    router = StrategyRouter([s1, s2], cfg)
    exits = [_intent("S1", "BTCTMN", 7, "stop", linked="p_S1_BTCTMN")]
    assert router.tick(_ctx(cfg, flatten_phase=phase), exits) == exits
    assert s1.calls == 0 and s2.calls == 0  # strategies are not even asked
    assert router.tick(_ctx(cfg, flatten_phase=phase)) == []


# ---------------------------------------------------------------- cooldown


def test_cooldown_after_fill(cfg: Config) -> None:
    assert cfg.engine.symbol_cooldown_s == 30
    s2 = FakeStrategy("S2", [_intent("S2", "USDTTMN", 1)])
    exit_it = _intent("S1", "USDTTMN", 2, "exit", linked="p_S1_USDTTMN")
    router = StrategyRouter([s2], cfg)
    router.note_fill("USDTTMN", T0 - 10)
    assert router.tick(_ctx(cfg), [exit_it]) == [exit_it]  # the entry waits, the exit does not
    assert router.dropped == [{"intent": s2.intents[0].id, "strategy": "S2", "symbol": "USDTTMN",
                               "why": "symbol cooldown 30s"}]
    assert router.tick(_ctx(cfg, now=T0 + 19.9)) == []  # 29.9 s after the fill
    assert router.tick(_ctx(cfg, now=T0 + 20)) == s2.intents  # 30 s: cooldown over
    router.note_fill("USDTTMN", T0 + 20)
    assert router.tick(_ctx(cfg, now=T0 + 21)) == []  # a new fill restarts it


def test_cooldown_is_per_symbol_and_configurable() -> None:
    cfg = load_config(overrides={"engine.symbol_cooldown_s": 5}, env={})
    s1 = FakeStrategy("S1", [_intent("S1", "BTCTMN", 1), _intent("S1", "ETHTMN", 2)])
    router = StrategyRouter([s1], cfg)
    router.note_fill("BTCTMN", T0 - 4)
    assert _ids(router.tick(_ctx(cfg))) == [s1.intents[1].id]
    assert router.dropped[0]["why"] == "symbol cooldown 5s"
    assert _ids(router.tick(_ctx(cfg, now=T0 + 1))) == _ids(s1.intents)


def test_apply_cooldown_for_hook_intents(cfg: Config) -> None:
    router = StrategyRouter([], cfg)
    router.note_fill("BTCTMN", T0 - 5)
    entry = _intent("S1", "BTCTMN", 1)
    leg2 = _intent("S1", "BTCTMN", 2, "leg2", linked="p_S1_BTCTMN")
    other = _intent("S1", "ETHTMN", 3)
    kept, dropped = router.apply_cooldown([entry, leg2, other], T0)
    assert kept == [leg2, other]
    assert dropped == [{"intent": entry.id, "symbol": "BTCTMN", "why": "symbol cooldown 30s"}]
    assert router.apply_cooldown([entry], T0 + 25) == ([entry], [])


# ---------------------------------------------------------------- determinism


def _strategies() -> list[FakeStrategy]:
    return [
        FakeStrategy("S3", [_intent("S3", "SOLUSDT", 1), _intent("S3", "BTCUSDT", 2)]),
        FakeStrategy("S1", [_intent("S1", "XRPTMN", 1), _intent("S1", "BTCTMN", 2), _intent("S1", "BTCTMN", 3)]),
        FakeStrategy("S2", [_intent("S2", "USDTTMN", 1)]),
    ]


def test_deterministic_order(cfg: Config) -> None:
    exits = [_intent("S3", "BTCUSDT", 5, "exit"), _intent("S1", "XRPTMN", 4, "stop"), _intent("S1", "BTCTMN", 6, "exit")]
    outputs = []
    for order in ([0, 1, 2], [2, 1, 0], [1, 0, 2]):  # strategies registered in any order
        strats = _strategies()
        router = StrategyRouter([strats[i] for i in order], cfg)
        assert [s.id for s in router.strategies] == ["S1", "S2", "S3"]
        outputs.append((router.tick(_ctx(cfg), list(reversed(exits))), router.dropped))
    assert outputs[0] == outputs[1] == outputs[2]
    out, dropped = outputs[0]
    assert [(i.strategy, i.symbol) for i in out] == [
        ("S1", "BTCTMN"), ("S1", "XRPTMN"), ("S3", "BTCUSDT"),  # exits sorted
        ("S1", "XRPTMN"), ("S1", "BTCTMN"),  # S1 in its own order (2nd BTCTMN intent dropped)
        ("S2", "USDTTMN"),
        ("S3", "SOLUSDT"), ("S3", "BTCUSDT"),
    ]
    assert [d["strategy"] for d in dropped] == ["S1"]


# ---------------------------------------------------------------- state


def test_state_roundtrip(cfg: Config) -> None:
    strats = _strategies()
    strats[1].counter = 7
    router = StrategyRouter(strats, cfg)
    router.note_fill("USDTTMN", T0 - 3)
    router.note_fill("BTCTMN", T0 - 9)
    state = json.loads(json.dumps(router.state()))
    assert state == {
        "last_fill_ts": {"BTCTMN": T0 - 9, "USDTTMN": T0 - 3},
        "strategies": {"S1": {"counter": 7, "id": "S1"}, "S2": {"counter": 0, "id": "S2"},
                       "S3": {"counter": 0, "id": "S3"}},
    }
    assert list(state["last_fill_ts"]) == ["BTCTMN", "USDTTMN"]  # sorted

    fresh = _strategies()
    restored = StrategyRouter(fresh, cfg)
    restored.load_state(state)
    assert restored.last_fill_ts == router.last_fill_ts
    assert restored.state() == router.state()
    assert restored.by_id("S1").loaded == {"counter": 7, "id": "S1"}  # type: ignore[union-attr]
    # the restored cooldown still applies after the restart
    assert all(i.symbol != "BTCTMN" or i.purpose in EXIT_PURPOSES for i in restored.tick(_ctx(cfg)))


def test_load_empty_state_and_by_id(cfg: Config) -> None:
    strats = _strategies()
    router = StrategyRouter(strats, cfg)
    router.load_state(None)
    assert router.last_fill_ts == {}
    assert all(s.loaded is None for s in strats)
    assert router.by_id("S2") is strats[2]
    assert router.by_id("S9") is None

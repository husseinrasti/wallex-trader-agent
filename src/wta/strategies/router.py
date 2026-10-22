"""Strategy router (PRD-03 §6): which strategies run this tick and which of their intents survive.

* Evaluation order each tick: position exits (PositionManager) -> S1 -> S2 -> S3.
* Enabled set = config.enabled AND plan.enabled_strategies (fresh plan only) AND not flattening.
  Each strategy checks its own flag and the plan in ``on_tick``; while flattening the router does
  not call the strategies at all and passes only the exit intents.
* One open order per (strategy, symbol); one strategy per symbol at a time (a symbol held or worked by
  another strategy is skipped); per-symbol cooldown after any fill (engine.symbol_cooldown_s).
  Exits (exit/stop/leg2/flatten) are never filtered: they only reduce what a strategy already holds.
* Output order is deterministic: exits first, sorted by (strategy id, symbol, intent id); then the
  strategies in id order, each strategy's intents in the order the strategy returned them (a
  strategy may rank its own intents, e.g. best edge first).
"""

from __future__ import annotations

from typing import Any

from wta.config import Config
from wta.models import Intent
from wta.strategies.base import Strategy, StrategyContext

EXIT_PURPOSES = frozenset({"exit", "stop", "leg2", "flatten"})


class StrategyRouter:
    def __init__(self, strategies: list[Strategy], cfg: Config) -> None:
        self.strategies = sorted(strategies, key=lambda s: s.id)
        self.cfg = cfg
        self.last_fill_ts: dict[str, float] = {}
        self.dropped: list[dict[str, Any]] = []  # last tick's filtered intents (for diagnostics)

    def by_id(self, sid: str) -> Strategy | None:
        return next((s for s in self.strategies if s.id == sid), None)

    def note_fill(self, symbol: str, ts: float) -> None:
        self.last_fill_ts[symbol] = ts

    def tick(self, ctx: StrategyContext, exit_intents: list[Intent] | None = None) -> list[Intent]:
        self.dropped = []
        exits = sorted(exit_intents or [], key=lambda i: (i.strategy, i.symbol, i.id))
        out: list[Intent] = list(exits)
        if ctx.flatten_phase > 0:
            return out
        # symbol ownership: positions and open orders belong to one strategy each
        owner: dict[str, str] = {}
        for p in ctx.portfolio.positions:
            owner.setdefault(p.symbol, p.strategy)
        for o in ctx.portfolio.open_orders:
            owner.setdefault(o.req.symbol, o.req.strategy)
        open_pairs = {(o.req.strategy, o.req.symbol) for o in ctx.portfolio.open_orders}
        for strat in self.strategies:
            for it in strat.on_tick(ctx):
                why = self._reject(it, owner, open_pairs, ctx.now)
                if why is not None:
                    self.dropped.append({"intent": it.id, "strategy": it.strategy, "symbol": it.symbol, "why": why})
                    continue
                out.append(it)
                if it.purpose not in EXIT_PURPOSES:
                    owner.setdefault(it.symbol, it.strategy)
                    open_pairs.add((it.strategy, it.symbol))
        return out

    def _reject(
        self, it: Intent, owner: dict[str, str], open_pairs: set[tuple[str, str]], now: float,
    ) -> str | None:
        """Why a strategy intent is dropped this tick (None = it goes on to the RiskManager)."""
        if it.purpose in EXIT_PURPOSES:
            return None  # exits of a strategy's own positions always pass to risk
        if (it.strategy, it.symbol) in open_pairs:
            return "open order exists for (strategy, symbol)"
        o = owner.get(it.symbol)
        if o is not None and o != it.strategy:
            return f"symbol owned by {o}"
        return self._cooldown_reason(it, now)

    def _cooldown_reason(self, it: Intent, now: float) -> str | None:
        """Non-exit intents wait ``engine.symbol_cooldown_s`` after any fill on their symbol."""
        cooldown = self.cfg.engine.symbol_cooldown_s
        last = self.last_fill_ts.get(it.symbol)
        if it.purpose not in EXIT_PURPOSES and last is not None and now - last < cooldown:
            return f"symbol cooldown {cooldown:.0f}s"
        return None

    def apply_cooldown(self, intents: list[Intent], now: float) -> tuple[list[Intent], list[dict[str, Any]]]:
        """Drop new entries/rebalances on symbols that filled within the cooldown window.

        ``tick`` already applies the cooldown to strategy intents; use this for intents produced
        elsewhere (e.g. by ``on_fill``/``on_order_done`` hooks).
        """
        kept: list[Intent] = []
        dropped: list[dict[str, Any]] = []
        for it in intents:
            why = self._cooldown_reason(it, now)
            if why is not None:
                dropped.append({"intent": it.id, "symbol": it.symbol, "why": why})
                continue
            kept.append(it)
        return kept, dropped

    def state(self) -> dict[str, Any]:
        return {
            "last_fill_ts": dict(sorted(self.last_fill_ts.items())),
            "strategies": {s.id: s.state() for s in self.strategies},
        }

    def load_state(self, state: dict[str, Any] | None) -> None:
        state = state or {}
        self.last_fill_ts = {k: float(v) for k, v in (state.get("last_fill_ts") or {}).items()}
        for s in self.strategies:
            s.load_state((state.get("strategies") or {}).get(s.id))

"""Inputs the RiskManager (and flatten scheduler) need, bundled in one immutable-ish object.

The engine builds a fresh ``RiskContext`` every fast tick from its own state; the RiskManager
never reads the wall clock or any global — only this context.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from wta.config import Config
from wta.execution.positions import PortfolioState
from wta.models import BookSnapshot, MarketInfo, MarketPlan
from wta.utils.timeutil import parse_iso


@dataclass
class EngineFlags:
    """Mutable engine-level switches (persisted via the store ``kv`` table and ``events``)."""

    paused: bool = False
    pause_reason: str = ""
    paused_until_ts: float | None = None
    killed: bool = False
    kill_reason: str = ""
    flatten_phase: int = 0  # 0 normal, 1 after flatten_start, 2 after flatten_final, 3 after end

    def to_state(self) -> dict[str, object]:
        return {
            "paused": self.paused,
            "pause_reason": self.pause_reason,
            "paused_until_ts": self.paused_until_ts,
            "killed": self.killed,
            "kill_reason": self.kill_reason,
            "flatten_phase": self.flatten_phase,
        }

    def load_state(self, state: dict[str, object] | None) -> None:
        if not state:
            return
        self.paused = bool(state.get("paused", False))
        self.pause_reason = str(state.get("pause_reason", ""))
        pu = state.get("paused_until_ts")
        self.paused_until_ts = float(pu) if pu is not None else None  # type: ignore[arg-type]
        self.killed = bool(state.get("killed", False))
        self.kill_reason = str(state.get("kill_reason", ""))
        self.flatten_phase = int(state.get("flatten_phase", 0))  # type: ignore[arg-type]


@dataclass(frozen=True)
class Deadline:
    """Challenge deadline schedule (unix seconds); all None when the end time is not configured."""

    end_ts: float | None
    flatten_start_ts: float | None
    flatten_final_ts: float | None
    final_state_ts: float | None

    @classmethod
    def from_config(cls, cfg: Config) -> Deadline:
        end = parse_iso(cfg.engine.challenge_end_utc)
        if end is None:
            return cls(None, None, None, None)
        e = cfg.engine
        return cls(
            end_ts=end,
            flatten_start_ts=end - e.flatten_start_before_end_s,
            flatten_final_ts=end - e.flatten_final_before_end_s,
            final_state_ts=end - e.final_state_before_end_s,
        )

    def phase(self, now: float) -> int:
        """0 normal, 1 flatten_start reached, 2 flatten_final reached, 3 challenge over."""
        if self.end_ts is None:
            return 0
        if now >= self.end_ts:
            return 3
        if self.flatten_final_ts is not None and now >= self.flatten_final_ts:
            return 2
        if self.flatten_start_ts is not None and now >= self.flatten_start_ts:
            return 1
        return 0

    def seconds_to_end(self, now: float) -> float | None:
        return None if self.end_ts is None else self.end_ts - now


@dataclass(frozen=True)
class RecentIntent:
    ts: float
    strategy: str
    symbol: str
    side: str
    purpose: str


@dataclass
class RiskContext:
    now: float
    portfolio: PortfolioState
    books: dict[str, BookSnapshot]
    markets: dict[str, MarketInfo]
    cfg: Config
    flags: EngineFlags
    deadline: Deadline
    plan: MarketPlan | None = None
    recent_intents: list[RecentIntent] = field(default_factory=list)  # approved, newest last
    api_consecutive_errors: int = 0

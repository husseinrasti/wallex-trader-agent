"""Decision, order and event logs (PRD-01 §8).

* ``logs/decisions.jsonl`` — one ``DecisionRecord`` per decision (intents, vetoes, orders, plans,
  no-trade ticks). Outcomes arrive later as separate ``OutcomeUpdate`` lines: earlier lines are
  never rewritten, so judges can trust the file is append-only. ``merge_decisions`` joins them.
* ``logs/orders.jsonl`` — every order request/response/cancel/fill, raw (``OrderLogRecord``).
* ``logs/events.jsonl`` — start/stop/pause/resume/kill/flatten/config_change (``EventRecord``).

Every record is validated by its pydantic model before it is written; ``ts`` comes from the
injected Clock, so backtest and replay produce the same lines as the live run would.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from wta.logging_.jsonl import JsonlWriter
from wta.models import DecisionKind, DecisionRecord, EventRecord, OrderLogRecord, OutcomeUpdate
from wta.utils.timeutil import Clock, iso

Payload = BaseModel | Mapping[str, Any] | None
RecordListener = Callable[[dict[str, Any]], None]

log = logging.getLogger(__name__)


def _as_dict(value: Payload) -> dict[str, Any] | None:
    """Pydantic models become JSON-ready dicts (Decimals as strings); mappings are copied."""
    if value is None:
        return None
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    return dict(value)


class DecisionLog:
    """Writer for ``decisions.jsonl``; stamps every record with mode, git commit and config hash."""

    def __init__(
        self,
        log_dir: Path,
        mode: str,
        git_commit: str,
        config_hash: str,
        clock: Clock,
        secrets: list[str] | None = None,
        *,
        base: str = "decisions",
        rotate_daily: bool = True,
        fsync: bool = False,
    ) -> None:
        self.mode = mode
        self.git_commit = git_commit
        self.config_hash = config_hash
        self.clock = clock
        self.writer = JsonlWriter(
            log_dir, base, clock, rotate_daily=rotate_daily, secrets=secrets, fsync=fsync
        )
        # hook: called with every written line as a JSON dict (decision records and outcome
        # updates), e.g. the engine's ring buffer of recent decisions for the AI tools
        self.listeners: list[RecordListener] = []

    def _notify(self, rec: BaseModel) -> None:
        if not self.listeners:
            return
        data = rec.model_dump(mode="json")
        for listener in list(self.listeners):
            try:
                listener(data)
            except Exception:  # noqa: BLE001 - a listener must never break the decision log
                log.exception("decision log listener failed")

    def set_config_hash(self, h: str) -> None:
        """Called when the Reviewer's accepted changes produce a new config version."""
        self.config_hash = h

    def set_mode(self, m: str) -> None:
        self.mode = m

    def record(
        self,
        kind: DecisionKind,
        decision_id: str,
        strategy: str,
        symbol: str,
        features: Mapping[str, Any] | None = None,
        plan_snapshot: Payload = None,
        intent: Payload = None,
        risk: Payload = None,
        order: Payload = None,
        notes: str = "",
        config_hash: str | None = None,
    ) -> DecisionRecord:
        """Validate and append one decision. ``intent``/``risk``/``order`` may be models or dicts.
        ``config_hash`` overrides the log's current hash for a decision made under an earlier config
        (an analyst plan whose cycle started before a reviewer reload)."""
        if kind == "outcome":
            raise ValueError("outcomes are appended with DecisionLog.outcome(), not record()")
        rec = DecisionRecord(
            kind=kind,
            decision_id=decision_id,
            ts=iso(self.clock.now()),
            mode=self.mode,
            git_commit=self.git_commit,
            config_hash=config_hash or self.config_hash,
            strategy=strategy,
            symbol=symbol,
            features=dict(features or {}),
            plan_snapshot=_as_dict(plan_snapshot),
            intent=_as_dict(intent),
            risk=_as_dict(risk) or {},
            order=_as_dict(order),
            notes=notes,
        )
        self.writer.write(rec)
        self._notify(rec)
        return rec

    def outcome(self, decision_id: str, outcome: Mapping[str, Any]) -> OutcomeUpdate:
        """Append an outcome update (pnl_tmn, pnl_pct, hold_s, closed_ts, exit_reason) for a decision."""
        upd = OutcomeUpdate(decision_id=decision_id, ts=iso(self.clock.now()), outcome=dict(outcome))
        self.writer.write(upd)
        self._notify(upd)
        return upd

    def close(self) -> None:
        self.writer.close()


class MergedDecisions(dict[str, dict[str, Any]]):
    """``decision_id -> merged record`` (log order), plus ``orphans``: outcome updates whose
    ``decision_id`` never appeared as a decision record."""

    def __init__(self) -> None:
        super().__init__()
        self.orphans: list[dict[str, Any]] = []


def merge_decisions(records: Iterable[Mapping[str, Any]]) -> MergedDecisions:
    """Apply outcome updates to their decisions (input: e.g. ``iter_family(log_dir, "decisions")``).

    * Several records with the same ``decision_id`` (e.g. ``intent`` then ``order``): the later
      record's non-empty fields overlay the earlier ones.
    * Outcome updates replace ``outcome`` wholesale; the later update wins.
    * Outcome updates for unknown ids are kept, unchanged, in ``result.orphans``.

    Input dicts are not modified.
    """
    merged = MergedDecisions()
    updates: list[Mapping[str, Any]] = []
    for rec in records:
        if rec.get("kind") == "outcome":
            updates.append(rec)
            continue
        decision_id = str(rec.get("decision_id"))
        if decision_id not in merged:
            merged[decision_id] = dict(rec)
        else:
            merged[decision_id].update({k: v for k, v in rec.items() if v not in (None, "", {}, [])})
    for upd in updates:
        target = merged.get(str(upd.get("decision_id")))
        if target is None:
            merged.orphans.append(dict(upd))
        else:
            target["outcome"] = upd.get("outcome")
    return merged


class OrdersLog:
    """Writer for ``orders.jsonl``: raw exchange traffic, one ``OrderLogRecord`` per line."""

    def __init__(
        self,
        log_dir: Path,
        clock: Clock,
        secrets: list[str] | None = None,
        *,
        base: str = "orders",
        rotate_daily: bool = True,
        fsync: bool = False,
    ) -> None:
        self.clock = clock
        self.writer = JsonlWriter(
            log_dir, base, clock, rotate_daily=rotate_daily, secrets=secrets, fsync=fsync
        )

    def log(self, action: str, client_id: str, symbol: str = "", data: Payload = None) -> OrderLogRecord:
        rec = OrderLogRecord(
            ts=iso(self.clock.now()),
            action=action,  # type: ignore[arg-type]  # validated against the Literal by pydantic
            client_id=client_id,
            symbol=symbol,
            data=_as_dict(data) or {},
        )
        self.writer.write(rec)
        return rec

    def close(self) -> None:
        self.writer.close()


class EventsLog:
    """Writer for ``events.jsonl``: engine-level events with a human reason."""

    def __init__(
        self,
        log_dir: Path,
        clock: Clock,
        secrets: list[str] | None = None,
        *,
        base: str = "events",
        rotate_daily: bool = True,
        fsync: bool = False,
    ) -> None:
        self.clock = clock
        self.writer = JsonlWriter(
            log_dir, base, clock, rotate_daily=rotate_daily, secrets=secrets, fsync=fsync
        )

    def log(self, kind: str, reason: str = "", data: Payload = None) -> EventRecord:
        rec = EventRecord(ts=iso(self.clock.now()), kind=kind, reason=reason, data=_as_dict(data) or {})
        self.writer.write(rec)
        return rec

    def close(self) -> None:
        self.writer.close()

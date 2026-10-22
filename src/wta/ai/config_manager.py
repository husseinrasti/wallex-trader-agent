"""Bounded, versioned config changes proposed by the Post-trade Reviewer (PRD-04 §7).

Only paths listed in ``configs/llm.yaml -> reviewer.editable`` may change, only within their
bounds, at most ``max_changes_per_review`` per review and not more often than ``cooldown_s``.
Risk parameters are never editable (enforced twice: by the allowlist and by a hard prefix check).
Accepted changes produce ``configs/runtime/config-<ts>.yaml`` (an overlay), a new ``config_hash``
and a ``config_change`` event ``{old_hash, new_hash, diff, source}``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from wta.config import CONFIG_DIR, Config, apply_changes, config_hash, get_path

FORBIDDEN_PREFIXES = ("risk.", "execution.", "markets.", "engine.initial_equity", "engine.challenge",
                      "engine.flatten", "ai.replay_strict", "llm.")


@dataclass
class ChangeResult:
    accepted: list[dict[str, Any]] = field(default_factory=list)
    rejected: list[dict[str, Any]] = field(default_factory=list)
    old_hash: str = ""
    new_hash: str = ""
    overlay_path: str | None = None
    config: Config | None = None

    @property
    def changed(self) -> bool:
        return bool(self.accepted)


def _within(value: Any, bounds: list[Any]) -> tuple[bool, Any, str]:
    """Validate ``value`` against ``bounds`` ([lo, hi] numeric, or an explicit list of allowed values)."""
    if len(bounds) == 2 and all(isinstance(b, bool) for b in bounds):
        if isinstance(value, bool):
            return True, value, ""
        return False, value, "expected a boolean"
    if len(bounds) == 2 and all(isinstance(b, (int, float)) and not isinstance(b, bool) for b in bounds):
        try:
            v = Decimal(str(value))
        except (InvalidOperation, ValueError):
            return False, value, "not a number"
        if not v.is_finite():  # NaN would raise InvalidOperation on the comparison below
            return False, value, "not a finite number"
        lo, hi = Decimal(str(bounds[0])), Decimal(str(bounds[1]))
        if not lo <= v <= hi:
            return False, value, f"outside bounds [{lo}, {hi}]"
        return True, v, ""
    if value in bounds:
        return True, value, ""
    return False, value, f"not one of {bounds}"


def _same(old: Any, new: Any) -> bool:
    """True when ``new`` would not change ``old`` (numbers compare by value: 300 == 300.0)."""
    if isinstance(new, Decimal):
        try:
            return Decimal(str(old)) == new
        except InvalidOperation:
            return False
    return str(old) == str(new)


class ConfigManager:
    def __init__(self, cfg: Config, runtime_dir: Path = CONFIG_DIR / "runtime") -> None:
        self.cfg = cfg
        self.runtime_dir = runtime_dir
        self.last_apply_ts: float | None = None
        self.overlay: dict[str, Any] = {}

    def apply(self, suggestions: list[dict[str, Any]], now: float, source: str = "reviewer") -> ChangeResult:
        rc = self.cfg.llm.reviewer
        res = ChangeResult(old_hash=config_hash(self.cfg))
        if self.last_apply_ts is not None and now - self.last_apply_ts < rc.cooldown_s:
            res.rejected = [{**s, "why": f"cooldown {rc.cooldown_s:.0f}s"} for s in suggestions]
            res.new_hash = res.old_hash
            return res
        changes: dict[str, Any] = {}
        new_cfg: Config | None = None
        tree = self.cfg.model_dump(mode="json")
        for s in suggestions:
            path = str(s.get("path", ""))
            if len(changes) >= rc.max_changes_per_review:
                res.rejected.append({**s, "why": "max_changes_per_review reached"})
                continue
            if path.startswith(FORBIDDEN_PREFIXES) or path not in rc.editable:
                res.rejected.append({**s, "why": "path not editable"})
                continue
            if path in changes:
                res.rejected.append({**s, "why": "duplicate path in this review"})
                continue
            ok, val, why = _within(s.get("value"), rc.editable[path])
            if not ok:
                res.rejected.append({**s, "why": why})
                continue
            try:
                old = get_path(tree, path)
            except (KeyError, TypeError):
                res.rejected.append({**s, "why": "unknown path"})
                continue
            if _same(old, val):
                res.rejected.append({**s, "why": "no change"})
                continue
            new_value = float(val) if isinstance(val, Decimal) else val
            try:  # the field's own type still applies (e.g. 5.5 for an int field)
                trial = apply_changes(self.cfg, {**changes, path: new_value})
            except ValueError as exc:  # pydantic ValidationError is a ValueError
                msg = exc.errors()[0]["msg"] if isinstance(exc, ValidationError) else str(exc)
                res.rejected.append({**s, "why": f"invalid value: {msg}"})
                continue
            changes[path] = new_value
            new_cfg = trial
            res.accepted.append({"path": path, "old": old, "new": str(val), "rationale": s.get("rationale", "")})
        if new_cfg is None:
            res.new_hash = res.old_hash
            return res
        res.config = new_cfg
        res.new_hash = config_hash(new_cfg)
        for k, v in changes.items():
            self.overlay[k] = v
        res.overlay_path = str(self._write_overlay(now, source, res))
        self.cfg = new_cfg
        self.last_apply_ts = now
        return res

    def _write_overlay(self, now: float, source: str, res: ChangeResult) -> Path:
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        nested: dict[str, Any] = {}
        for path, value in sorted(self.overlay.items()):
            node = nested
            parts = path.split(".")
            for p in parts[:-1]:
                node = node.setdefault(p, {})
            node[parts[-1]] = value
        stamp = datetime.fromtimestamp(now, tz=UTC).strftime("%Y%m%dT%H%M%SZ")
        path = self.runtime_dir / f"config-{stamp}.yaml"
        header = (f"# runtime overlay written by {source} at {stamp}\n# old_hash {res.old_hash} -> new_hash "
                  f"{res.new_hash}\n")
        path.write_text(header + yaml.safe_dump(nested, sort_keys=True))
        latest = self.runtime_dir / "latest.yaml"
        latest.write_text(header + yaml.safe_dump(nested, sort_keys=True))
        return path

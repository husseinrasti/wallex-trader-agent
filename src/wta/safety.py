"""Live-mode guard (Overview §8, PRD-01 1.9): shared by the CLI, the engine and any other entry point.

Live trading starts only when every check passes, in this order:

1. ``--i-understand-live`` was given (``i_understand_live=True``);
2. ``WTA_LIVE=1`` is in the process environment (a value in ``.env`` does not count, so live is
   always armed explicitly on the command line);
3. ``WALLEX_API_KEY`` is set;
4. the git working tree is clean, untracked files included (``allow_dirty`` overrides, dev only).

``live_guard_reason`` returns why live mode must not start (or None); ``check_live_guard`` raises
``LiveGuardError`` with that reason. Callers run it before any task starts.

Live *trading* (not ``flatten_now``, which sells everything at once) also needs the challenge end:
``deadline_guard_reason`` refuses a missing or past ``engine.challenge_end_utc``
(``CHALLENGE_END_UTC``). Without it the deadline phases never run — no stop to new entries at
T-2h, no flatten to 100 % TMN before the end — and the final valuation counts TMN only.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Mapping
from pathlib import Path

from wta.config import Settings
from wta.utils import gitinfo
from wta.utils.timeutil import iso, parse_iso


class LiveGuardError(RuntimeError):
    """Live mode was requested but a guard failed; nothing was started."""


def _git(args: list[str], root: Path | None) -> str | None:
    """stdout of ``git <args>`` in ``root`` (default: the repository), or None when git fails."""
    try:
        out = subprocess.run(
            ["git", *args], cwd=root or gitinfo.ROOT, capture_output=True, text=True, timeout=5, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout if out.returncode == 0 else None


def tree_is_dirty(root: Path | None = None) -> bool:
    """True when ``git status --porcelain`` lists anything, untracked files included; True as well
    when git cannot tell (fail closed).

    ``gitinfo.get()["dirty"]`` runs with ``--untracked-files=no``, so new modules that are running
    but not committed would pass as clean there. Files matched by ``.gitignore`` (``.env``,
    ``data/``, ``logs/``, ...) never count.
    """
    status = _git(["status", "--porcelain"], root)
    return status is None or bool(status.strip())


def live_guard_reason(
    i_understand_live: bool,
    allow_dirty: bool,
    settings: Settings,
    env: Mapping[str, str] | None = None,
) -> str | None:
    """Why live mode must not start, or None when every guard passes."""
    env = os.environ if env is None else env
    if not i_understand_live:
        return "pass --i-understand-live"
    if env.get("WTA_LIVE") != "1":
        return "set WTA_LIVE=1 in the environment of this command (.env does not count)"
    if not settings.wallex_api_key.get_secret_value():
        return "WALLEX_API_KEY is not set"
    if not allow_dirty and tree_is_dirty():
        commit = str(gitinfo.get()["commit"])[:12]
        return (
            f"the git working tree is dirty or has untracked files (HEAD {commit}); "
            "commit and tag first, or pass --allow-dirty (dev only)"
        )
    return None


def deadline_guard_reason(challenge_end_utc: str | None, now: float) -> str | None:
    """Why live trading must not start for this challenge end (or None when it may)."""
    try:
        end = parse_iso(challenge_end_utc)
    except ValueError:
        return f"engine.challenge_end_utc {challenge_end_utc!r} is not an ISO-8601 time"
    if end is None:
        return (
            "engine.challenge_end_utc (CHALLENGE_END_UTC) is not set: the flatten to 100 % TMN "
            "before the end would never run"
        )
    if now >= end:
        return f"the challenge ended at {iso(end)} (engine.challenge_end_utc)"
    return None


def check_live_guard(
    i_understand_live: bool,
    allow_dirty: bool,
    settings: Settings,
    env: Mapping[str, str] | None = None,
) -> None:
    """Raise ``LiveGuardError`` unless every live guard passes."""
    reason = live_guard_reason(i_understand_live, allow_dirty, settings, env)
    if reason is not None:
        raise LiveGuardError(f"refusing to start live mode: {reason}")

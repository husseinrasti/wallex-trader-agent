"""Live-mode guard (Overview §8, PRD-01 1.9): shared by the CLI, the engine and any other entry point.

Live trading starts only when every check passes, in this order:

1. ``--i-understand-live`` was given (``i_understand_live=True``);
2. ``WTA_LIVE=1`` is in the process environment (a value in ``.env`` does not count, so live is
   always armed explicitly on the command line);
3. ``WALLEX_API_KEY`` is set;
4. the git working tree is clean, untracked files included (``allow_dirty`` overrides, dev only).

``live_guard_reason`` returns why live mode must not start (or None); ``check_live_guard`` raises
``LiveGuardError`` with that reason. Callers run it before any task starts.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Mapping
from pathlib import Path

from wta.config import Settings
from wta.utils import gitinfo


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

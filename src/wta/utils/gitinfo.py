"""Git provenance for every decision record. Live mode refuses a dirty tree."""

from __future__ import annotations

import functools
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]


def _git(*args: str) -> str | None:
    try:
        out = subprocess.run(
            ["git", *args], cwd=ROOT, capture_output=True, text=True, timeout=5, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    return out.stdout.strip()


@functools.lru_cache(maxsize=1)
def get() -> dict[str, object]:
    """``{"commit": str, "dirty": bool, "tag": str | None}`` (cached for the process lifetime)."""
    commit = _git("rev-parse", "HEAD") or "unknown"
    status = _git("status", "--porcelain")  # untracked (non-ignored) files count as dirty
    dirty = bool(status) if status is not None else True
    tag = _git("describe", "--tags", "--exact-match", "HEAD")
    return {"commit": commit, "dirty": dirty, "tag": tag or None}


def commit_date(rev: str = "HEAD") -> str | None:
    """``YYYY-MM-DD`` (UTC) of ``rev``'s committer date; None without git. Reports use it as their
    date when none is given: deterministic for a commit, and never after the day a report is made."""
    from datetime import UTC, datetime

    out = _git("log", "-1", "--format=%ct", rev)
    try:
        return datetime.fromtimestamp(int(out or ""), UTC).strftime("%Y-%m-%d")
    except ValueError:
        return None


def short_commit() -> str:
    c = str(get()["commit"])
    return c[:12] if c != "unknown" else c


def refresh() -> dict[str, object]:
    get.cache_clear()
    return get()

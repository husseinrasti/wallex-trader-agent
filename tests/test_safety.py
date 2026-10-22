"""Live guard (``wta.safety``): the clean-tree check counts untracked files; guard order and messages.

The git tests use a scratch repository under ``tmp_path``; nothing touches this checkout's history.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from wta import safety
from wta.config import Settings
from wta.safety import LiveGuardError, check_live_guard, live_guard_reason, tree_is_dirty

needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A committed scratch repository with a .gitignore like the project's."""
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "test@example.invalid")
    _git(tmp_path, "config", "user.name", "test")
    (tmp_path / ".gitignore").write_text(".env\nlogs/\ndata/\n")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "engine.py").write_text("x = 1\n")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-q", "-m", "init")
    return tmp_path


@needs_git
def test_untracked_source_file_makes_the_tree_dirty(repo: Path) -> None:
    assert tree_is_dirty(repo) is False
    (repo / "src" / "wallex_rest.py").write_text("new = True\n")  # running code that is not committed
    assert tree_is_dirty(repo) is True


@needs_git
def test_ignored_files_do_not_count_but_modified_files_do(repo: Path) -> None:
    (repo / ".env").write_text("WALLEX_API_KEY=x\n")
    (repo / "logs").mkdir()
    (repo / "logs" / "health.jsonl").write_text("{}\n")
    assert tree_is_dirty(repo) is False
    (repo / "src" / "engine.py").write_text("x = 2\n")
    assert tree_is_dirty(repo) is True


def test_not_a_repository_counts_as_dirty(tmp_path: Path) -> None:
    assert tree_is_dirty(tmp_path / "missing") is True  # fail closed


def test_guard_order_and_messages(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = Settings(_env_file=None, wallex_api_key="safety-test-key-0000")
    live_env = {"WTA_LIVE": "1"}
    monkeypatch.setattr(safety, "tree_is_dirty", lambda root=None: True)
    assert live_guard_reason(False, True, settings, live_env) == "pass --i-understand-live"
    assert "WTA_LIVE=1" in str(live_guard_reason(True, True, settings, {"WTA_LIVE": "0"}))
    no_key = Settings(_env_file=None, wallex_api_key="")
    assert live_guard_reason(True, True, no_key, live_env) == "WALLEX_API_KEY is not set"
    assert "untracked files" in str(live_guard_reason(True, False, settings, live_env))
    assert live_guard_reason(True, True, settings, live_env) is None  # --allow-dirty (dev only)
    with pytest.raises(LiveGuardError, match="refusing to start live mode: the git working tree is dirty"):
        check_live_guard(True, False, settings, live_env)
    monkeypatch.setattr(safety, "tree_is_dirty", lambda root=None: False)
    check_live_guard(True, False, settings, live_env)  # every guard passes: no exception

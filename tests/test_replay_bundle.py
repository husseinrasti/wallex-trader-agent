"""Replay bundles (``deliverables/replay/<run>/``) and ``scripts/make_deliverables.py``.

A tiny deterministic recording first (``tests/test_replay.py``'s ``record``: the real shadow engine on
the SYNTHETIC feed, a SimClock, the OFFLINE RULES PROVIDER — not an LLM — and two injected intents;
60 ticks). Then:

* ``make_replay_bundle`` writes ``logs.tar.xz``, ``state.sqlite.xz`` and a ``sha256sum``-format
  ``SHA256SUMS``; the state DB copy is an online backup (a writer in the middle of a transaction is
  neither blocked nor copied half-way), VACUUMed, a single file, and holds the same rows under the same
  rowids;
* ``extract_replay_bundle`` gives back the identical log directory and state DB, and refuses a bundle
  whose files do not match SHA256SUMS, a malformed SHA256SUMS, and archive members that would leave
  the target directory;
* ``scripts/replay_check.py --bundle`` replays the bundle (PASS, 0 network), removes its temporary
  directory and writes no report; a tampered bundle is exit 2;
* ``scripts/make_deliverables.py`` copies the run (decision/order/event files byte for byte, health
  sampled hourly, journal, LLM cache), writes the bundle and the REPORT.md index, and aborts before
  writing anything when the secrets scan finds a key.

SYNTHETIC data throughout — never evidence of performance. Nothing here may open a socket.
"""

from __future__ import annotations

import asyncio
import functools
import importlib.util
import io
import json
import lzma
import shutil
import socket
import sqlite3
import tarfile
import tempfile
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from wta.logging_.checker import check_logs
from wta.logging_.jsonl import family_files
from wta.replay.runner import (
    BUNDLE_LOGS,
    BUNDLE_STATE,
    BUNDLE_SUMS,
    BundleError,
    extract_replay_bundle,
    make_replay_bundle,
    read_sha256sums,
    sha256_file,
    snapshot_sqlite,
    sqlite_digest,
    verify_replay_bundle,
)

from .test_replay import no_keys, record

ROOT = Path(__file__).resolve().parents[1]
TICKS = 60
FAST_XZ = 1  # xz preset for the tests (the deliverable uses 9 = ``xz -9``; same format)


@pytest.fixture(autouse=True)
def _no_network(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[Any]]:
    """Every socket connection attempt is recorded and fails the test."""
    attempts: list[Any] = []

    def refuse(*args: Any, **kwargs: Any) -> Any:
        attempts.append(args)
        raise AssertionError(f"network access attempted: {args!r}")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket.socket, "connect_ex", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket, "getaddrinfo", refuse)
    yield attempts
    assert attempts == []


@pytest.fixture(scope="module")
def tiny(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    root = tmp_path_factory.mktemp("tiny_run")
    return asyncio.run(record(root, ticks=TICKS))


@pytest.fixture(scope="module")
def bundle(tiny: dict[str, Any], tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    out = tmp_path_factory.mktemp("bundle") / "tiny"
    info = make_replay_bundle(tiny["logs"], tiny["db"], out, preset=FAST_XZ)
    return {"dir": out, "info": info}


def _copy_bundle(bundle: dict[str, Any], dest: Path) -> Path:
    shutil.copytree(bundle["dir"], dest)
    return dest


def _script(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _files(root: Path) -> dict[str, bytes | str]:
    """{relative path: content (regular files) or 'symlink -> target'}."""
    out: dict[str, bytes | str] = {}
    for p in sorted(root.rglob("*")):
        rel = p.relative_to(root).as_posix()
        if p.is_symlink():
            out[rel] = f"symlink -> {p.readlink()}"
        elif p.is_file():
            out[rel] = p.read_bytes()
    return out


def _write_sums(d: Path) -> None:
    (d / BUNDLE_SUMS).write_text("".join(f"{sha256_file(d / n)}  {n}\n" for n in (BUNDLE_LOGS, BUNDLE_STATE)))


def _tiny_sqlite(path: Path) -> Path:
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE t(x)")
    conn.execute("INSERT INTO t VALUES (1)")
    conn.commit()
    conn.close()
    return path


def _xz_bytes(data: bytes) -> bytes:
    return lzma.compress(data, preset=FAST_XZ)


# ---------------------------------------------------------------- the recording


def test_the_tiny_recording_has_decisions_to_replay(tiny: dict[str, Any]) -> None:
    assert family_files(tiny["logs"], "decisions")
    assert (tiny["logs"] / "llm_cache").is_dir()
    assert (tiny["logs"] / "decisions.jsonl").is_symlink()  # the current-day link a bundle must keep
    assert check_logs(tiny["logs"]).ok


# ---------------------------------------------------------------- creation


def test_the_bundle_holds_two_xz_files_and_their_sha256sums(bundle: dict[str, Any], tiny: dict[str, Any]) -> None:
    d: Path = bundle["dir"]
    assert sorted(p.name for p in d.iterdir()) == sorted([BUNDLE_LOGS, BUNDLE_STATE, BUNDLE_SUMS])
    for name in (BUNDLE_LOGS, BUNDLE_STATE):
        assert (d / name).read_bytes()[:6] == b"\xfd7zXZ\x00"  # xz container (what `xz -d` reads)
        assert bundle["info"][name]["sha256"] == sha256_file(d / name)
        assert bundle["info"][name]["bytes"] == (d / name).stat().st_size
    # sha256sum text format, in a fixed order: `sha256sum -c SHA256SUMS` / `shasum -a 256 -c` check it
    assert (d / BUNDLE_SUMS).read_text().splitlines() == [
        f"{sha256_file(d / BUNDLE_LOGS)}  {BUNDLE_LOGS}",
        f"{sha256_file(d / BUNDLE_STATE)}  {BUNDLE_STATE}",
    ]
    assert verify_replay_bundle(d) == read_sha256sums(d / BUNDLE_SUMS)

    with tarfile.open(d / BUNDLE_LOGS, "r:xz") as tf:
        members = tf.getmembers()
    top = tiny["logs"].name
    names = [m.name for m in members]
    assert names[0] == top and all(n == top or n.startswith(f"{top}/") for n in names)
    assert names[1:] == sorted(names[1:])  # a stable member order
    assert all(m.uid == 0 and m.gid == 0 and m.uname == "" and m.gname == "" for m in members)
    link = next(m for m in members if m.name == f"{top}/decisions.jsonl")
    assert link.issym() and "/" not in link.linkname
    assert bundle["info"][BUNDLE_LOGS]["members"] == len(members)


def test_make_replay_bundle_never_overwrites(bundle: dict[str, Any], tiny: dict[str, Any]) -> None:
    with pytest.raises(BundleError, match="already holds"):
        make_replay_bundle(tiny["logs"], tiny["db"], bundle["dir"], preset=FAST_XZ)
    with pytest.raises(BundleError, match="state DB not found"):
        make_replay_bundle(tiny["logs"], bundle["dir"] / "nope.sqlite", bundle["dir"].parent / "other")


def test_the_state_snapshot_is_an_online_single_file_copy(tiny: dict[str, Any], tmp_path: Path) -> None:
    src = tmp_path / "live.sqlite"
    shutil.copyfile(tiny["db"], src)
    before = sqlite_digest(src)
    writer = sqlite3.connect(src, isolation_level=None)
    writer.execute("PRAGMA journal_mode=WAL")
    writer.execute("BEGIN IMMEDIATE")  # a writer in the middle of a transaction
    writer.execute("INSERT INTO ticks(ts, mode, json) VALUES (?, ?, ?)", (9e9, "shadow", "{}"))
    dst = snapshot_sqlite(src, tmp_path / "copy" / "state.sqlite")
    writer.execute("COMMIT")  # neither blocked nor changed by the backup
    writer.close()

    assert sqlite_digest(dst) == before  # the committed rows, same rowids; nothing half-written
    after = sqlite_digest(src)
    assert after["ticks"][0] == before["ticks"][0] + 1
    conn = sqlite3.connect(dst)
    try:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        conn.close()
    assert sorted(p.name for p in dst.parent.iterdir()) == ["state.sqlite"]  # no -wal / -shm side files
    assert before["ticks"][0] == TICKS
    with pytest.raises(BundleError, match="exists"):
        snapshot_sqlite(src, dst)


# ---------------------------------------------------------------- extraction and checksums


def test_extract_gives_back_the_same_run(bundle: dict[str, Any], tiny: dict[str, Any], tmp_path: Path) -> None:
    log_dir, state = extract_replay_bundle(bundle["dir"], tmp_path / "x")
    assert log_dir == tmp_path / "x" / "logs" / tiny["logs"].name
    assert state == tmp_path / "x" / "state.sqlite"
    assert _files(log_dir) == _files(tiny["logs"])  # every file byte for byte, symlinks as symlinks
    assert sqlite_digest(state) == sqlite_digest(tiny["db"])
    with pytest.raises(BundleError, match="not empty"):
        extract_replay_bundle(bundle["dir"], tmp_path / "x")


def test_a_changed_or_missing_file_is_refused(bundle: dict[str, Any], tmp_path: Path) -> None:
    d = _copy_bundle(bundle, tmp_path / "b")
    raw = bytearray((d / BUNDLE_LOGS).read_bytes())
    raw[len(raw) // 2] ^= 0x01
    (d / BUNDLE_LOGS).write_bytes(bytes(raw))
    with pytest.raises(BundleError, match=r"does not match its SHA256SUMS: logs\.tar\.xz: sha256"):
        verify_replay_bundle(d)
    with pytest.raises(BundleError, match="does not match"):
        extract_replay_bundle(d, tmp_path / "out")
    assert not (tmp_path / "out").exists()  # checked before anything is written

    d2 = _copy_bundle(bundle, tmp_path / "b2")
    (d2 / BUNDLE_STATE).unlink()
    with pytest.raises(BundleError, match=r"state\.sqlite\.xz: missing"):
        verify_replay_bundle(d2)


@pytest.mark.parametrize(
    ("sums", "message"),
    [
        (None, "not a replay bundle"),
        ("logs_only", r"no checksum for state\.sqlite\.xz"),
        ("not a checksum line\n", "not a sha256sum line"),
        ("outside", "outside the bundle directory"),
        ("twice", "listed twice"),
    ],
)
def test_a_bad_sha256sums_is_refused(bundle: dict[str, Any], tmp_path: Path, sums: str | None, message: str) -> None:
    d = _copy_bundle(bundle, tmp_path / "b")
    lines = (d / BUNDLE_SUMS).read_text().splitlines()
    if sums is None:
        (d / BUNDLE_SUMS).unlink()
    elif sums == "logs_only":
        (d / BUNDLE_SUMS).write_text(lines[0] + "\n")
    elif sums == "outside":
        (d / BUNDLE_SUMS).write_text("\n".join([*lines, "0" * 64 + "  ../evil"]) + "\n")
    elif sums == "twice":
        (d / BUNDLE_SUMS).write_text("\n".join([*lines, lines[0]]) + "\n")
    else:
        (d / BUNDLE_SUMS).write_text(sums)
    with pytest.raises(BundleError, match=message):
        verify_replay_bundle(d)


def test_binary_mode_sha256sum_lines_are_read(bundle: dict[str, Any], tmp_path: Path) -> None:
    d = _copy_bundle(bundle, tmp_path / "b")
    sums = read_sha256sums(d / BUNDLE_SUMS)
    (d / BUNDLE_SUMS).write_text("".join(f"{h.upper()} *{n}\n" for n, h in sums.items()))  # `sha256sum -b` style
    assert verify_replay_bundle(d) == sums


def _evil_bundle(tmp_path: Path, members: list[tuple[tarfile.TarInfo, bytes | None]], state: bytes | None = None) -> Path:
    d = tmp_path / "evil"
    d.mkdir()
    with tarfile.open(d / BUNDLE_LOGS, "w:xz", preset=FAST_XZ) as tf:
        for info, data in members:
            tf.addfile(info, io.BytesIO(data) if data is not None else None)
    if state is None:
        state = _tiny_sqlite(tmp_path / "tiny.sqlite").read_bytes()
    (d / BUNDLE_STATE).write_bytes(_xz_bytes(state))
    _write_sums(d)
    return d


def _member(name: str, data: bytes = b"x\n", *, kind: bytes = tarfile.REGTYPE, link: str = "") -> tuple[
        tarfile.TarInfo, bytes | None]:
    info = tarfile.TarInfo(name)
    info.type = kind
    if kind == tarfile.REGTYPE:
        info.size = len(data)
        return info, data
    if kind == tarfile.DIRTYPE:
        info.mode = 0o755
    info.linkname = link
    return info, None


@pytest.mark.parametrize(
    ("members", "message"),
    [
        ([_member("run", kind=tarfile.DIRTYPE), _member("run/../../evil.txt")], "unsafe member name"),
        ([_member("/abs/evil.txt")], "unsafe member name"),
        ([_member("run", kind=tarfile.DIRTYPE), _member("run/x", kind=tarfile.SYMTYPE, link="/etc/passwd")],
         "points outside"),
        ([_member("run", kind=tarfile.DIRTYPE), _member("run/x", kind=tarfile.SYMTYPE, link="../../up")],
         "points outside"),
        ([_member("run", kind=tarfile.DIRTYPE), _member("run/dev", kind=tarfile.CHRTYPE)], "not a file"),
        ([_member("a", kind=tarfile.DIRTYPE), _member("b", kind=tarfile.DIRTYPE)], "exactly one log directory"),
    ],
)
def test_unsafe_or_odd_archives_are_refused(tmp_path: Path, members: list[Any], message: str) -> None:
    d = _evil_bundle(tmp_path, members)
    verify_replay_bundle(d)  # the checksums are right: the archive itself is the problem
    with pytest.raises(BundleError, match=message):
        extract_replay_bundle(d, tmp_path / "out")
    assert not (tmp_path / "evil.txt").exists() and not (tmp_path / "out" / "evil.txt").exists()


def test_a_state_file_that_is_not_sqlite_is_refused(tmp_path: Path) -> None:
    d = _evil_bundle(tmp_path, [_member("run", kind=tarfile.DIRTYPE), _member("run/events.jsonl")],
                     state=b"not a database\n")
    with pytest.raises(BundleError, match="does not hold a SQLite database"):
        extract_replay_bundle(d, tmp_path / "out")


# ---------------------------------------------------------------- scripts/replay_check.py --bundle


def test_replay_check_bundle_passes_with_zero_network_and_cleans_up(
        bundle: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str]) -> None:
    scratch = tmp_path / "tmp"
    scratch.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(scratch))  # where the script's temporary directory goes
    assert _script("replay_check").main(["--bundle", str(bundle["dir"])]) == 0
    out = capsys.readouterr().out
    assert "SHA256SUMS verified" in out
    assert "0 socket attempts, 0 LLM provider calls" in out
    assert "decisions reproduced -> PASS" in out and "SYNTHETIC" in out
    assert "not written (--bundle without --reports-dir)" in out  # docs/REPORTS is left as committed
    assert list(scratch.iterdir()) == []  # the unpacked bundle and the replay output are gone


def test_replay_check_bundle_refuses_bad_input(bundle: dict[str, Any], tmp_path: Path,
                                               capsys: pytest.CaptureFixture[str]) -> None:
    script = _script("replay_check")
    d = _copy_bundle(bundle, tmp_path / "b")
    raw = bytearray((d / BUNDLE_STATE).read_bytes())
    raw[-20] ^= 0xFF
    (d / BUNDLE_STATE).write_bytes(bytes(raw))
    assert script.main(["--bundle", str(d)]) == 2
    assert "does not match its SHA256SUMS: state.sqlite.xz" in capsys.readouterr().err
    assert script.main(["--bundle", str(tmp_path / "missing")]) == 2
    assert script.main(["--bundle", str(bundle["dir"]), "--log-dir", str(tmp_path)]) == 2
    assert "drop --log-dir" in capsys.readouterr().err


def test_replay_check_bundle_keeps_an_explicit_output_dir(bundle: dict[str, Any], tmp_path: Path,
                                                          capsys: pytest.CaptureFixture[str]) -> None:
    out = tmp_path / "replay_out"
    assert _script("replay_check").main(["--bundle", str(bundle["dir"]), "--out", str(out),
                                         "--reports-dir", str(tmp_path / "reports"), "--no-index"]) == 0
    assert "-> PASS" in capsys.readouterr().out
    assert (out / "replay_report.md").is_file()
    assert len(list((tmp_path / "reports").glob("replay_*.md"))) == 1


# ---------------------------------------------------------------- scripts/make_deliverables.py


def _deliverables_script(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    mod = _script("make_deliverables")
    monkeypatch.setattr(mod, "Settings", no_keys)  # the scan uses the key patterns, not this machine's .env
    monkeypatch.setattr(mod, "make_replay_bundle", functools.partial(make_replay_bundle, preset=FAST_XZ))
    return mod


def _report(out: Path) -> Path:
    out.mkdir(parents=True, exist_ok=True)
    report = out / "REPORT.md"
    report.write_text("# Final report\n\nAssembled by a test.\n\n> banner\n\n| a | b |\n| --- | --- |\n"
                      "| 1 | [self](REPORT.md) |\n")
    return report


def test_health_is_sampled_hourly_plus_first_and_last(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    mod = _deliverables_script(monkeypatch)
    times = ["2026-10-22T22:54:00.000Z", "2026-10-22T22:55:00.000Z", "2026-10-22T23:00:01.000Z",
             "2026-10-22T23:30:00.000Z", "2026-10-23T01:10:00.000Z", "2026-10-23T01:20:00.000Z"]
    (tmp_path / "health-20261022.jsonl").write_text("".join(json.dumps({"ts": t, "n": i}) + "\n"
                                                            for i, t in enumerate(times[:4])))
    (tmp_path / "health-20261023.jsonl").write_text("".join(json.dumps({"ts": t, "n": i + 4}) + "\n"
                                                            for i, t in enumerate(times[4:])))
    (tmp_path / "health.jsonl").symlink_to("health-20261023.jsonl")  # the current-day link is not read twice
    lines, total = mod.sample_health(tmp_path)
    assert total == 6
    assert [json.loads(line)["n"] for line in lines] == [0, 2, 4, 5]  # 22h first, 23h first, 01h first, last


def test_make_deliverables_copies_the_run_writes_the_bundle_and_the_index(
        tiny: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str]) -> None:
    mod = _deliverables_script(monkeypatch)
    out = tmp_path / "deliverables"
    report = _report(out)
    args = ["--run-dir", str(tiny["logs"]), "--name", "tiny_run", "--out", str(out), "--bundle", "--replay"]
    assert mod.main(args) == 0, capsys.readouterr()
    printed = capsys.readouterr().out
    assert "secrets:  clean" in printed and "-> PASS" in printed

    run = out / "tiny_run"
    for base in ("decisions", "orders", "events"):
        src = family_files(tiny["logs"], base)
        assert [p.name for p in family_files(run, base)] == [p.name for p in src]
        assert all((run / p.name).read_bytes() == p.read_bytes() for p in src)  # byte for byte
    assert not any(p.is_symlink() for p in run.rglob("*"))
    health = (run / "health.jsonl").read_text().splitlines()
    beats = [line for p in family_files(tiny["logs"], "health") for line in p.read_text().splitlines() if line]
    assert beats and health == list(dict.fromkeys([beats[0], beats[-1]]))  # a 2-minute run: first (= its hour's) + last
    src_cache = sorted(p.relative_to(tiny["logs"] / "llm_cache") for p in (tiny["logs"] / "llm_cache").rglob("*.json"))
    assert sorted(p.relative_to(run / "llm_cache") for p in (run / "llm_cache").rglob("*.json")) == src_cache
    assert check_logs(run).ok
    readme = (run / "README.md").read_text()
    assert readme.startswith("# Shadow run `tiny_run` — SYNTHETIC feed")
    assert "**Summary**\n1. " in readme and "\n5. " in readme  # the 5-line summary (PRD-06 §4)
    assert mod.MARKER in readme and "not evidence of performance" in readme
    assert "make replay-deliverable" in readme

    bdir = out / "replay" / "tiny_run"
    assert sorted(p.name for p in bdir.iterdir()) == sorted([BUNDLE_LOGS, BUNDLE_STATE, BUNDLE_SUMS, "README.md"])
    verify_replay_bundle(bdir)
    breadme = (bdir / "README.md").read_text()
    assert "decisions reproduced -> PASS" in breadme and "0 socket attempts, 0 LLM provider calls" in breadme
    assert sha256_file(bdir / BUNDLE_STATE) in breadme

    text = report.read_text()
    assert text.count(mod.INDEX_START) == 1 and text.count(mod.INDEX_END) == 1
    assert text.index("Assembled by a test.") < text.index(mod.INDEX_START) < text.index("> banner")
    for rel in ("tiny_run/", "tiny_run/health.jsonl", "tiny_run/llm_cache/", "replay/tiny_run/",
                f"replay/tiny_run/{BUNDLE_STATE}", f"replay/tiny_run/{BUNDLE_SUMS}", "REPORT.md"):
        assert f"[`{rel}`]" in text
    assert mod.broken_links(report) == []
    assert mod.main(["--out", str(out), "--index-only"]) == 0  # idempotent: the block is replaced, not added
    assert report.read_text() == text

    # rerun: a folder this script wrote is replaced; one it did not write is left alone
    (out / "other").mkdir()
    assert mod.main(["--run-dir", str(tiny["logs"]), "--name", "other", "--out", str(out)]) == 2
    assert "not written by this script" in capsys.readouterr().err


def test_a_broken_link_in_report_md_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    mod = _deliverables_script(monkeypatch)
    report = _report(tmp_path / "deliverables")
    report.write_text(report.read_text() + "\n[gone](nowhere/file.md) [web](https://example.org) [anchor](#x)\n")
    assert mod.broken_links(report) == ["nowhere/file.md"]
    assert mod.main(["--out", str(tmp_path / "deliverables"), "--index-only"]) == 1


def test_make_deliverables_aborts_on_a_secret_before_writing(
        tiny: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str]) -> None:
    mod = _deliverables_script(monkeypatch)
    logs = tmp_path / "logs"
    shutil.copytree(tiny["logs"], logs, symlinks=True)
    fake = "sk" + "-or-v1-" + "0123456789abcdef" * 4  # assembled at runtime: no key-like literal in this file
    (logs / "journal.md").write_text(f"# Trading journal\n\nleaked {fake}\n")
    out = tmp_path / "deliverables"
    _report(out)
    assert mod.main(["--run-dir", str(logs), "--name", "leaky", "--out", str(out), "--bundle"]) == 1
    err = capsys.readouterr().err
    assert "ABORT: secrets scan" in err and "journal.md:3: OpenRouter key" in err
    assert fake not in err
    assert not (out / "leaky").exists() and not (out / "replay").exists()

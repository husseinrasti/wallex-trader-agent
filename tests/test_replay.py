"""PRD-05 §6 / §8: ``wta.replay.runner`` (``run_replay``) and ``scripts/replay_check.py``.

A deterministic recording first: the real engine (``build_engine``, shadow mode, ``PaperExchange``)
on the synthetic feed, driven tick by tick on a ``SimClock``, with the AI layer on the OFFLINE RULES
PROVIDER (no key: deterministic rules, NOT an LLM, so plans exist and are cached), acceptance-test
intents injected (an S3-style entry with a server stop and a time stop, a vetoed second entry) and
the startup reconcile 200 ms after the start event (as a real run waits for its books). Then:

* the replay reproduces 100 % of the decisions, writes the same orders and balance rows, makes zero
  network calls (sockets refused and counted, LLM providers replaced by counting sentinels) and
  writes ``replay_<date>.md``;
* a mutated cached plan, a mutated recorded order answer and a mutated recorded decision are each
  detected as mismatches;
* a missing cache entry is a ``CacheMissError`` surfaced as a failed replay (strict), or a noted gap
  the replay runs past (lenient);
* the recorded config is recovered from the analyst tape when the given config differs;
* ``scripts/replay_check.py`` exits 0 / 1 / 2 and finds the newest live, else shadow, run.

SYNTHETIC data throughout — never evidence of performance. Nothing here may open a socket.
"""

from __future__ import annotations

import asyncio
import dataclasses
import importlib.util
import json
import shutil
import socket
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from wta.ai.tools import to_jsonable
from wta.config import Config, Settings, load_config
from wta.engine import InjectedIntent, build_engine, state_db_path
from wta.exchange.replay import load_balance_snapshots, load_recorded_fills
from wta.logging_.jsonl import family_files, iter_family
from wta.replay import compare_decisions
from wta.replay.runner import (
    ReplayClock,
    ReplayInputError,
    ReplayRun,
    find_run_dirs,
    load_recorded_run,
    network_guard,
    pick_run_dir,
    report_day,
    run_replay_async,
)
from wta.utils.decimalutil import D
from wta.utils.timeutil import iso

from .conftest import T0

ROOT = Path(__file__).resolve().parents[1]
SYMBOLS = ["USDTTMN", "BTCTMN", "BTCUSDT", "ETHTMN", "ETHUSDT", "SOLUSDT"]
TICKS = 400
DT = 2.0
STARTUP_LAG_S = 0.2  # the startup reconcile runs this long after the start event (books wait)


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


def no_keys() -> Settings:
    """No LLM key (offline rules provider), no Telegram, whatever the developer's environment holds."""
    empty = {"openrouter_api_key": "", "gemini_api_key": "", "telegram_bot_token": "", "telegram_chat_id": "",
             "wallex_api_key": ""}
    return Settings(_env_file=None, **empty)


def rec_cfg(root: Path, **over: Any) -> Config:
    values: dict[str, Any] = {
        "markets.watchlist": list(SYMBOLS),
        "data.sqlite_path": str(root / "market.sqlite"),
        "data.state_sqlite_path": str(root / "state_{mode}.sqlite"),
        "data.ws_enabled": False,
        "engine.reconcile_interval_s": 60,
        "engine.slow_loop_interval_s": 120,
        "strategies.s1_crossbook.coins": ["BTC", "ETH"],
        **over,
    }
    return load_config(overrides=values, env={})


INJECT = [
    {"at_s": 20, "strategy": "S3", "symbol": "BTCUSDT", "side": "BUY", "order_type": "LIMIT", "qty": "0.00003",
     "stop_price": "81000", "purpose": "entry", "taker": True, "expected_edge_pct": "0.008",
     "reason": "replay test: entry with a server-side stop and a 60 s time stop", "features": {"time_stop_s": 60}},
    {"at_s": 30, "strategy": "S3", "symbol": "ETHUSDT", "side": "BUY", "order_type": "LIMIT", "qty": "0.001",
     "purpose": "entry", "taker": True, "expected_edge_pct": "0.001",
     "reason": "replay test: second S3 entry while one is open (expected veto)"},
]


def _injected() -> list[InjectedIntent]:
    from wta.engine import parse_injected_line

    return [parse_injected_line(obj, n) for n, obj in enumerate(INJECT, start=2)]


REVIEW_CHANGES = [
    {"path": "strategies.s1_crossbook.margin_min_pct", "value": 0.0008, "rationale": "replay test"},
    {"path": "strategies.s2_usdtalloc.rebalance_threshold_pp", "value": 0.1, "rationale": "replay test"},
]


def review_change(eng: Any, now: float) -> None:
    """What ``Engine.review_cycle`` does with an accepted review (``PostTradeReviewer._apply`` /
    ``_emit``): ConfigManager.apply -> ``config_change`` event at the review time -> reload_config.
    The offline rules provider never suggests changes, so the suggestion is given here."""
    change = eng.config_manager.apply(REVIEW_CHANGES, now, source="reviewer")
    assert change.accepted and change.config is not None
    eng._event_at(now, "config_change", "reviewer", to_jsonable(  # noqa: SLF001 - the reviewer's event sink
        {"old_hash": change.old_hash, "new_hash": change.new_hash, "diff": change.accepted,
         "rejected": change.rejected, "overlay_path": change.overlay_path, "source": "reviewer"}))
    eng.reload_config(change.config)


async def record(root: Path, *, review_at: int | None = None, kill_at: int | None = None, logs: str = "logs",
                 t0: float = T0, ticks: int = TICKS, inject: bool = True, **over: Any) -> dict[str, Any]:
    """The recording: shadow engine on the synthetic feed, SimClock, offline AI provider; optionally
    a reviewer config change after tick ``review_at`` and a KILL file during tick ``kill_at``.
    ``logs`` / ``t0`` / ``ticks``: another log dir, start time and length (a second run on the same
    state DB); ``inject=False``: no acceptance-test intents; ``over``: config overrides."""
    cfg = rec_cfg(root, **over)
    clock = ReplayClock(t0)  # a SimClock; ``jump_when`` puts the startup reconcile 200 ms after the start
    kill_file = root / "KILL"
    eng = await build_engine(cfg, no_keys(), "shadow", feed="synthetic", log_dir=root / logs, clock=clock,
                             kill_file=kill_file if kill_at is not None else None, runtime_dir=root / "runtime",
                             mcp=None, telegram=False, paper_balances={"TMN": D("5000000"), "USDT": D("8")},
                             injected=_injected() if inject else [])
    assert eng.synthetic is not None
    assert eng.ai_source == ("offline-rules" if cfg.ai.enabled else "disabled")
    feed = eng.synthetic
    feed.params = dataclasses.replace(feed.params, jump_rate_per_hour=40.0)  # cross-book dislocations for S1
    try:
        feed.publish(t0)
        clock.jump_when(lambda: eng.started_ts is not None, t0 + STARTUP_LAG_S)
        await eng.startup()
        now = t0 + STARTUP_LAG_S + 0.003  # the first tick follows the startup at once, on the same books
        for k in range(ticks):
            if k:
                now = round(now + DT + 0.0013 * (k % 7), 6)  # uneven tick spacing, like a real loop
                feed.step(DT)
                feed.publish(now)
            clock.set(now)
            if k == kill_at:
                kill_file.write_text("test\n")
            await eng.safe_tick(now)
            kill_file.unlink(missing_ok=True)
            await eng.slow_step(now)  # the slow loop between two ticks: its plan is used from the next one
            if k == review_at:
                review_change(eng, now)
    finally:
        eng.shutdown("recording done")
        await eng.aclose()
    return {"cfg": cfg, "logs": root / logs, "db": Path(state_db_path(cfg, "shadow", "synthetic"))}


@pytest.fixture(scope="module")
def recorded(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    root = tmp_path_factory.mktemp("recording")
    return asyncio.run(record(root))


@pytest.fixture(scope="module")
def recorded_ops(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    root = tmp_path_factory.mktemp("recording_ops")
    return asyncio.run(record(root, review_at=120, kill_at=300))


def copy_recording(recorded: dict[str, Any], dest: Path) -> dict[str, Any]:
    """A private copy of the recording (to mutate): logs, LLM cache and state DB."""
    shutil.copytree(recorded["logs"], dest / "logs", symlinks=True)
    shutil.copyfile(recorded["db"], dest / "state.sqlite")
    return {"cfg": recorded["cfg"], "logs": dest / "logs", "db": dest / "state.sqlite"}


async def replay(rec: dict[str, Any], out: Path, *, cfg: Config | None = None, **kw: Any) -> ReplayRun:
    return await run_replay_async(cfg or rec["cfg"], log_dir=rec["logs"], state_db=rec["db"], out_dir=out / "rep",
                                  reports_dir=out / "reports", settings=no_keys(), **kw)


def _lines(log_dir: Path, family: str) -> list[bytes]:
    return [line for p in family_files(log_dir, family) for line in p.read_bytes().splitlines() if line.strip()]


def plan_records(log_dir: Path) -> list[dict[str, Any]]:
    return [r for r in iter_family(log_dir, "decisions") if r.get("kind") == "plan"]


# ---------------------------------------------------------------- the recording itself


def test_the_recording_covers_what_a_replay_must_reproduce(recorded: dict[str, Any]) -> None:
    decisions = list(iter_family(recorded["logs"], "decisions"))
    kinds = {d["kind"] for d in decisions}
    assert {"plan", "order", "veto", "outcome"} <= kinds
    assert len(plan_records(recorded["logs"])) >= 4
    assert all(p["features"]["ai_source"] == "offline-rules" for p in plan_records(recorded["logs"]))
    orders = [r["action"] for r in iter_family(recorded["logs"], "orders")]
    assert orders.count("place") >= 3 and "fill" in orders and "cancel" in orders
    assert any(d.get("plan_snapshot") for d in decisions if d["kind"] != "plan")  # decisions under a plan
    (start,) = [e for e in iter_family(recorded["logs"], "events") if e["kind"] == "start"]
    assert start["data"]["synthetic"] is True and start["data"]["ai"]["source"] == "offline-rules"
    snaps = load_balance_snapshots(recorded["db"])
    assert snaps[0][0] == pytest.approx(T0 + STARTUP_LAG_S)  # the startup reconcile's row, not at the start


def test_load_recorded_run_reads_one_segment(recorded: dict[str, Any]) -> None:
    run = load_recorded_run(recorded["logs"], recorded["db"])
    assert run.mode == "shadow" and run.feed == "synthetic" and run.synthetic
    assert len(run.ticks) == TICKS and run.segments == 1 and not run.in_progress
    assert run.start_ts == T0 and run.first_ts > T0
    assert run.tick_index(run.tick_ts[5] - 0.0004) == 5 and run.tick_index(T0) == -1
    with pytest.raises(ReplayInputError, match="no start event"):
        load_recorded_run(recorded["logs"] / "llm_cache", recorded["db"])
    with pytest.raises(ReplayInputError, match="state DB not found"):
        load_recorded_run(recorded["logs"], recorded["logs"] / "nope.sqlite")


# ---------------------------------------------------------------- 100 % reproduction, zero network


async def test_replay_reproduces_every_decision_with_zero_network(recorded: dict[str, Any], tmp_path: Path) -> None:
    result = await replay(recorded, tmp_path)
    assert result.ok, result.to_markdown()
    assert result.n_reference == result.n_replayed == result.n_matched > 10
    assert result.summary == f"{result.n_reference}/{result.n_reference} decisions reproduced"
    assert result.strict_check["ok"], result.strict_check
    assert result.errors == [] and result.divergences == []
    run = result.run
    assert run["network"]["socket_attempts"] == 0 and run["network"]["provider_calls"] == 0
    assert run["network"]["guarded"] is True
    assert run["config"]["equal"] and run["config"]["source"] == "the given config"
    assert run["ai"]["plans_recorded"] == run["ai"]["plans_reproduced"] >= 4 and run["ai"]["cache_misses"] == 0
    assert run["inputs"]["ticks"] == TICKS and run["inputs"]["injected"] == 2
    assert set(run["exchange"]["answers"]) <= {"recorded", "repeat", "fallback"}  # nothing derived or diverged
    assert all(p["reproduced"] for p in result.plans)

    rep_logs = tmp_path / "rep" / "logs"
    assert _lines(rep_logs, "orders") == _lines(recorded["logs"], "orders")  # byte for byte
    assert load_balance_snapshots(tmp_path / "rep" / "replay_state.sqlite") == load_balance_snapshots(recorded["db"])
    assert load_recorded_fills(tmp_path / "rep" / "replay_state.sqlite") == load_recorded_fills(recorded["db"])
    assert {r["mode"] for r in iter_family(rep_logs, "decisions") if "mode" in r} == {"replay"}

    # the report: the judge-facing line, the SYNTHETIC label, inputs, no wall clock
    path = Path(result.report_file or "")
    assert path.name.startswith("replay_2026") and path.is_file()
    text = path.read_text()
    assert result.summary in text and "(PASS)" in text
    assert "SYNTHETIC — not evidence of performance" in text and "SYNTHETIC DATA — not evidence of performance" in text
    assert "0 socket attempts, 0 LLM provider calls" in text and "## Limitations" in text
    assert f"| segment complete | yes: the stop event counts {TICKS} ticks = {TICKS} in the state DB |" in text
    assert json.loads((tmp_path / "rep" / "replay_report.json").read_text())["ok"] is True


async def test_the_replay_is_itself_deterministic(recorded: dict[str, Any], tmp_path: Path) -> None:
    a = await replay(recorded, tmp_path / "a")
    b = await replay(recorded, tmp_path / "b")
    assert a.ok and b.ok
    assert _lines(tmp_path / "a" / "rep" / "logs", "decisions") == _lines(tmp_path / "b" / "rep" / "logs", "decisions")


async def test_the_recorded_config_comes_back_from_the_analyst_tape(recorded: dict[str, Any], tmp_path: Path) -> None:
    other = load_config(env={})  # the plain repo config: another watchlist, other paths
    assert other.config_hash != recorded["cfg"].config_hash
    result = await replay(recorded, tmp_path, cfg=other)
    assert result.ok, result.to_markdown()
    assert result.run["config"]["equal"] and "taped" in result.run["config"]["source"]


async def test_an_unmatched_config_fails_a_strict_replay(recorded: dict[str, Any], tmp_path: Path) -> None:
    rec = copy_recording(recorded, tmp_path)
    shutil.rmtree(rec["logs"] / "llm_cache" / "mcp_tape")  # no taped config to recover it from
    result = await replay(rec, tmp_path, cfg=load_config(env={}))
    assert not result.ok
    assert any("config mismatch" in e for e in result.errors)


# ---------------------------------------------------------------- mismatches are detected


def _final_answer(log_dir: Path, plan: dict[str, Any]) -> Path:
    key = plan["features"]["calls"][-1]["cache_key"]
    return log_dir / "llm_cache" / f"{key}.json"


async def test_a_mutated_cached_plan_is_detected(recorded: dict[str, Any], tmp_path: Path) -> None:
    rec = copy_recording(recorded, tmp_path)
    plans = plan_records(rec["logs"])
    target = plans[1]
    path = _final_answer(rec["logs"], target)
    entry = json.loads(path.read_text())
    body = json.loads(entry["response"]["text"])
    body["usdt_target_share"] = 0.69 if float(body.get("usdt_target_share", 0.5)) != 0.69 else 0.31
    body["notes"] = "MUTATED " + str(body.get("notes", ""))
    entry["response"]["text"] = json.dumps(body)
    path.write_text(json.dumps(entry))

    result = await replay(rec, tmp_path)
    assert not result.ok
    fields = {(m.decision_id, m.field) for m in result.mismatches}
    assert (target["decision_id"], "plan.usdt_target_share") in fields
    assert (target["decision_id"], "plan.notes") in fields
    info = {p["decision_id"]: p for p in result.plans}
    assert info[target["decision_id"]]["reproduced"] is False
    assert "usdt_target_share" in info[target["decision_id"]]["error"]
    assert any(e.startswith(f"plan {target['decision_id']} re-derived differently") for e in result.errors)
    assert result.run["network"]["provider_calls"] == 0  # still served from the cache, never a provider


async def test_a_mutated_recorded_order_answer_is_detected(recorded: dict[str, Any], tmp_path: Path) -> None:
    rec = copy_recording(recorded, tmp_path)
    path = family_files(rec["logs"], "orders")[0]
    lines = path.read_text().splitlines()
    i = next(n for n, line in enumerate(lines) if json.loads(line)["action"] == "response")
    rec_line = json.loads(lines[i])
    lines[i] = json.dumps({**rec_line, "action": "error",
                           "data": {"code": "mutated", "status": 400, "message": "mutated answer"}})
    path.write_text("\n".join(lines) + "\n")

    result = await replay(rec, tmp_path)
    assert not result.ok
    assert any(m.field == "order.accepted" and m.reference is True and m.replayed is False for m in result.mismatches)


async def test_a_mutated_recorded_decision_is_detected(recorded: dict[str, Any], tmp_path: Path) -> None:
    rec = copy_recording(recorded, tmp_path)
    path = family_files(rec["logs"], "decisions")[0]
    lines = path.read_text().splitlines()
    i = next(n for n, line in enumerate(lines) if json.loads(line)["kind"] == "order")
    obj = json.loads(lines[i])
    obj["intent"]["reason"] = "MUTATED " + obj["intent"]["reason"]
    lines[i] = json.dumps(obj)
    path.write_text("\n".join(lines) + "\n")

    result = await replay(rec, tmp_path)
    assert not result.ok and len(result.mismatches) == 1
    assert result.mismatches[0].field == "intent.reason" and result.mismatches[0].decision_id == obj["decision_id"]
    assert result.n_matched == result.n_reference - 1


def _mutate_decisions(log_dir: Path, change: Any) -> list[str]:
    """Apply ``change(record) -> bool`` to every recorded decision line; the ids it changed."""
    changed: list[str] = []
    for path in family_files(log_dir, "decisions"):
        lines = path.read_text().splitlines()
        for n, line in enumerate(lines):
            obj = json.loads(line)
            if change(obj):
                changed.append(obj["decision_id"])
                lines[n] = json.dumps(obj)
        path.write_text("\n".join(lines) + "\n")
    return changed


async def test_the_strict_pass_is_part_of_the_verdict(recorded: dict[str, Any], tmp_path: Path,
                                                      capsys: pytest.CaptureFixture[str]) -> None:
    rec = copy_recording(recorded, tmp_path)
    done = {"n": 0}

    def change(obj: dict[str, Any]) -> bool:  # fields the PRD list does not cover: fees, fills, features
        if obj["kind"] != "order" or done["n"] or not (obj.get("order") or {}).get("response"):
            return False
        done["n"] = 1
        obj["order"]["response"]["fee_paid"] = "123456"
        obj["order"]["response"]["filled_qty"] = "42"
        obj["intent"].setdefault("features", {})["equity_tmn"] = "1"
        return True

    (target,) = _mutate_decisions(rec["logs"], change)
    result = await replay(rec, tmp_path)
    assert result.mismatches == [] and result.n_matched == result.n_reference  # the PRD fields still agree
    assert not result.ok and not result.strict_check["ok"]
    fields = {m["field"] for m in result.strict_check["mismatches"] if m["decision_id"] == target}
    assert {"order.response.fee_paid", "order.response.filled_qty", "intent.features.equity_tmn"} <= fields
    assert any(e.startswith("strict pass failed") for e in result.errors)
    assert "(FAIL)" in Path(result.report_file or "").read_text()
    code = await asyncio.to_thread(_run_script, ["--log-dir", str(rec["logs"]), "--state-db", str(rec["db"]),
                                                 "--no-index", "--reports-dir", "", "--out", str(tmp_path / "script")])
    assert code == 1 and "-> FAIL" in capsys.readouterr().out


async def test_time_stamps_are_compared(recorded: dict[str, Any], tmp_path: Path) -> None:
    rec = copy_recording(recorded, tmp_path)
    shifted: list[str] = []

    def change(obj: dict[str, Any]) -> bool:  # a fill seen an hour later, a position closed an hour later
        hit = False
        for part, key in (("outcome", "done_ts"), ("outcome", "closed_ts"), ("order", "response")):
            sub = obj.get(part)
            if part == "order" and isinstance(sub, dict) and isinstance(sub.get("response"), dict):
                resp = sub["response"]
                if isinstance(resp.get("updated_ts"), int | float):
                    resp["updated_ts"] += 3600.0
                    shifted.append("order.response.updated_ts")
                    hit = True
            elif isinstance(sub, dict) and isinstance(sub.get(key), int | float):
                sub[key] += 3600.0
                shifted.append(f"{part}.{key}")
                hit = True
        return hit

    assert _mutate_decisions(rec["logs"], change) and shifted
    result = await replay(rec, tmp_path)
    assert not result.ok
    fields = {m["field"] for m in result.strict_check["mismatches"]}
    assert fields & {"outcome.done_ts", "outcome.closed_ts", "order.response.updated_ts"}, fields


async def test_a_crash_only_in_the_replay_fails_it(recorded: dict[str, Any], tmp_path: Path,
                                                   monkeypatch: pytest.MonkeyPatch) -> None:
    from wta.engine import Engine

    calls = {"n": 0}
    original = Engine._scheduled  # noqa: SLF001 - the tick's last step: the decisions are already written

    async def flaky(self: Any, ctx: Any) -> None:
        calls["n"] += 1
        if calls["n"] in (100, 101):
            raise RuntimeError("replay-only crash (test)")
        await original(self, ctx)

    monkeypatch.setattr(Engine, "_scheduled", flaky)
    result = await replay(recorded, tmp_path)
    assert result.n_matched == result.n_reference  # the decisions alone would not show it
    assert not result.ok
    assert any("crashed on other ticks than the recorded one" in e and "recorded on none" in e
               for e in result.errors), result.errors


async def test_a_window_without_decisions_is_nothing_to_compare(tmp_path: Path,
                                                               capsys: pytest.CaptureFixture[str]) -> None:
    rec = await record(tmp_path, ticks=60, inject=False, **{"ai.enabled": False,
                       "strategies.s1_crossbook.enabled": False, "strategies.s2_usdtalloc.enabled": False})
    assert not list(iter_family(rec["logs"], "decisions"))
    result = await replay(rec, tmp_path / "out")
    assert result.n_reference == result.n_replayed == 0 and result.summary == "0/0 decisions reproduced"
    assert not result.ok and any(e.startswith("nothing to compare") for e in result.errors)
    assert "(FAIL)" in Path(result.report_file or "").read_text()
    code = await asyncio.to_thread(_run_script, ["--log-dir", str(rec["logs"]), "--state-db", str(rec["db"]),
                                                 "--no-index", "--reports-dir", "", "--out", str(tmp_path / "script")])
    assert code == 2 and "-> FAIL" in capsys.readouterr().out


# ---------------------------------------------------------------- a cache miss is a failed replay


async def test_a_missing_cache_entry_fails_a_strict_replay(recorded: dict[str, Any], tmp_path: Path) -> None:
    rec = copy_recording(recorded, tmp_path)
    plans = plan_records(rec["logs"])
    target = plans[2]
    (rec["logs"] / "llm_cache" / f"{target['plan_snapshot']['prompt_hash']}.json").unlink()

    result = await replay(rec, tmp_path)
    assert not result.ok
    assert any("cache miss" in e.lower() and target["decision_id"] in e for e in result.errors)
    assert result.run["inputs"]["ticks"] < TICKS  # strict: the replay stopped at the miss
    assert any(d["decision_id"] == target["decision_id"] for d in result.missing)
    assert result.run["network"]["provider_calls"] == 0 and result.run["network"]["socket_attempts"] == 0
    text = Path(result.report_file or "").read_text()
    assert "(FAIL)" in text and "cache miss" in text.lower()


async def test_a_missing_cache_entry_in_a_lenient_replay_is_reported(recorded: dict[str, Any], tmp_path: Path) -> None:
    rec = copy_recording(recorded, tmp_path)
    target = plan_records(rec["logs"])[2]
    (rec["logs"] / "llm_cache" / f"{target['plan_snapshot']['prompt_hash']}.json").unlink()
    result = await replay(rec, tmp_path, strict=False)
    assert not result.ok  # lenient keeps going; the verdict still fails on the missing plan
    assert not any("cache miss" in e.lower() for e in result.errors)  # the miss itself is a note, not a stop
    assert all(e.startswith("strict pass failed") for e in result.errors)
    assert result.run["inputs"]["ticks"] == TICKS  # lenient: ran past the gap
    assert any(target["decision_id"] in n and "cache miss" in n.lower() for n in result.notes)


def _cut_ticks(db: Path, keep_until: float) -> None:
    import sqlite3

    conn = sqlite3.connect(db)
    with conn:
        conn.execute("DELETE FROM ticks WHERE ts > ?", (keep_until,))
    conn.close()


def _drop_stop_event(log_dir: Path) -> None:
    path = family_files(log_dir, "events")[0]
    path.write_text("".join(line + "\n" for line in path.read_text().splitlines() if json.loads(line)["kind"] != "stop"))


async def test_a_run_still_being_written_fails_unless_a_partial_replay_is_allowed(
    recorded: dict[str, Any], tmp_path: Path
) -> None:
    rec = copy_recording(recorded, tmp_path)
    ticks = load_recorded_run(rec["logs"], rec["db"]).tick_ts
    _cut_ticks(rec["db"], ticks[int(len(ticks) * 0.6)])  # the engine has not committed these yet
    _drop_stop_event(rec["logs"])

    failed = await replay(rec, tmp_path / "strict")  # a prefix is not a reproduction of the run
    assert not failed.ok and failed.n_matched == failed.n_reference  # ... although every replayed decision matches
    assert any("no stop event" in e and "--allow-partial" in e for e in failed.errors), failed.errors
    assert "(FAIL)" in Path(failed.report_file or "").read_text()

    result = await replay(rec, tmp_path / "partial", allow_partial=True)
    assert result.ok, result.to_markdown()
    recd = result.run["recorded"]
    assert recd["in_progress"] is True and recd["partial"] is True and recd["stop"] is False
    assert result.run["window"]["ticks"] == int(len(ticks) * 0.6) + 1
    assert recd["excluded"]["decision lines after the last tick"] > 0
    assert any(n.startswith("PARTIAL replay (allowed with --allow-partial)") for n in result.notes)
    assert "PARTIAL: only the ticks committed so far" in Path(result.report_file or "").read_text()


async def test_a_state_db_with_fewer_ticks_than_the_stop_event_counts_fails(
    recorded: dict[str, Any], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rec = copy_recording(recorded, tmp_path)
    run = load_recorded_run(rec["logs"], rec["db"])
    assert run.stop is not None and run.stop_ticks == len(run.ticks) == TICKS  # the complete recording
    _cut_ticks(rec["db"], run.tick_ts[int(TICKS * 0.3)])  # a stale / partial copy; the stop event is kept
    cut = load_recorded_run(rec["logs"], rec["db"])
    assert cut.stop_ticks == TICKS and len(cut.ticks) == int(TICKS * 0.3) + 1 and not cut.in_progress

    result = await replay(rec, tmp_path)
    assert not result.ok
    assert any(f"but its stop event counts {TICKS}" in e for e in result.errors), result.errors
    assert any("written after its last committed tick" in e for e in result.errors), result.errors
    assert (f"| segment complete | NO: the stop event counts {TICKS} ticks, the state DB holds {int(TICKS * 0.3) + 1}"
            in Path(result.report_file or "").read_text())
    code = await asyncio.to_thread(_run_script, ["--log-dir", str(rec["logs"]), "--state-db", str(rec["db"]),
                                                 "--no-index", "--reports-dir", "", "--out", str(tmp_path / "script")])
    assert code == 1 and "-> FAIL" in capsys.readouterr().out


async def test_a_segment_ends_at_its_own_stop_event_when_runs_share_the_state_db(tmp_path: Path) -> None:
    # run A, then run B on the SAME state DB (one file per mode) with another log dir, 1000 s later
    a = await record(tmp_path, logs="logs_a", ticks=150)
    last_a = load_recorded_run(a["logs"], a["db"]).tick_ts[-1]
    b = await record(tmp_path, logs="logs_b", t0=last_a + 1000.0, ticks=150)
    assert a["db"] == b["db"]
    run_a = load_recorded_run(a["logs"], a["db"])
    assert len(run_a.ticks) == run_a.stop_ticks == 150  # B's ticks are not part of A
    assert run_a.tick_ts[-1] == last_a and run_a.segment_end is not None and run_a.segment_end < last_a + 1000.0
    assert all(ts <= run_a.segment_end for ts, _ in run_a.balances)
    import sqlite3

    conn = sqlite3.connect(a["db"])
    try:  # B continued A's candle numbering (its pre-startup arrivals did not restart at seq 1)
        n, distinct, top = conn.execute("SELECT COUNT(*), COUNT(DISTINCT seq), MAX(seq) FROM engine_candles").fetchone()
        assert n == distinct == top
    finally:
        conn.close()
    result = await replay(a, tmp_path / "rep_a")
    assert result.ok, result.to_markdown()
    assert result.run["window"]["ticks"] == 150 and result.errors == []

    # a state DB written by an engine before that fix: a later run's arrivals reuse seq 1..6
    conn = sqlite3.connect(a["db"])
    with conn:
        conn.execute("INSERT INTO engine_candles(seq, mode, symbol, resolution, ts, o, h, l, c, v) "
                     "SELECT seq, mode, symbol, resolution, ts + 2000, o, h, l, c, v FROM engine_candles "
                     "WHERE seq <= 6")
    conn.close()
    again = await replay(a, tmp_path / "rep_a_dup")
    assert again.ok, again.to_markdown()  # the later bars (opening after A's last tick) are skipped


# ---------------------------------------------------------------- reviewer changes and a kill file


async def test_reviewer_changes_and_a_kill_file_are_replayed(recorded_ops: dict[str, Any], tmp_path: Path) -> None:
    events = list(iter_family(recorded_ops["logs"], "events"))
    (change_ev,) = [e for e in events if e["kind"] == "config_change"]
    assert [e["data"]["source"] for e in events if e["kind"] == "kill"] == ["kill_file"]
    flatten = [d for d in iter_family(recorded_ops["logs"], "decisions")
               if (d.get("intent") or {}).get("purpose") == "flatten"]
    assert flatten  # the kill-switch sold to TMN

    result = await replay(recorded_ops, tmp_path)
    assert result.ok, result.to_markdown()
    (change,) = result.config_changes
    assert change["error"] == "" and change["applied_hash"] == change["new_hash"] == change_ev["data"]["new_hash"]
    assert result.provenance["config_hash"]["equal"]  # both hashes, before and after the change
    rep_events = list(iter_family(tmp_path / "rep" / "logs", "events"))
    assert [e["data"]["source"] for e in rep_events if e["kind"] == "kill"] == ["kill_file"]
    assert not (tmp_path / "rep" / "KILL").exists()
    assert "config_change events" in " ".join(result.notes)


async def test_without_the_recorded_change_the_hashes_differ(recorded_ops: dict[str, Any], tmp_path: Path) -> None:
    rec = copy_recording(recorded_ops, tmp_path)
    path = family_files(rec["logs"], "events")[0]
    path.write_text("".join(line + "\n" for line in path.read_text().splitlines()
                            if json.loads(line)["kind"] != "config_change"))
    result = await replay(rec, tmp_path)
    assert result.config_changes == []

    def hashes(log_dir: Path) -> dict[str, str]:  # the engine's hash on each decision (plans: the cycle's)
        return {d["decision_id"]: d["config_hash"] for d in iter_family(log_dir, "decisions")
                if d["kind"] not in ("plan", "outcome")}

    ref, rep = hashes(rec["logs"]), hashes(tmp_path / "rep" / "logs")
    assert any(rep.get(k) != v for k, v in ref.items())  # decisions after the change: the old hash


# ---------------------------------------------------------------- pieces


def _fake_run(tick_ts: list[float], decisions: list[dict[str, Any]]) -> Any:
    from wta.replay.runner import RecordedRun

    return RecordedRun(
        log_dir=Path("."), state_db=Path("."), start={"data": {}}, start_ts=tick_ts[0] - 1, mode="shadow",
        feed="live", config_hash="", segment=1, segments=1, segment_end=None,
        ticks=[{"ts": t, "book_ts": {}} for t in tick_ts], tick_ts=tick_ts, decisions=decisions, orders=[],
        events=[], excluded={}, in_progress=False, balances=[], fills=[],
    )


def _plan_rec(ts: float, ph: str, latency_ms: int, calls: list[str], cached: bool = False) -> dict[str, Any]:
    from wta.utils.timeutil import iso

    return {"kind": "plan", "decision_id": f"d_{int(ts * 1000)}_AI_PLAN_x", "ts": iso(ts),
            "plan_snapshot": {"ts": ts, "prompt_hash": ph, "latency_ms": latency_ms},
            "features": {"cached": cached, "latency_ms": latency_ms,
                         "calls": [{"cache_key": k, "provider": "p", "model": "m"} for k in calls]}}


def _order_rec(ts: float, ph: str | None) -> dict[str, Any]:
    from wta.utils.timeutil import iso

    return {"kind": "order", "decision_id": f"d_{int(ts * 1000)}_S2_USDTTMN_1", "ts": iso(ts),
            "plan_snapshot": {"prompt_hash": ph} if ph else None}


def test_plan_swap_ticks_from_cache_times_latency_and_decision_bounds(tmp_path: Path) -> None:
    from wta.replay.runner import schedule_plans

    ticks = [1000.0 + 2 * i for i in range(20)]  # 1000, 1002, ..., 1038
    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / "a1.json").write_text(json.dumps({"ts": 1001.5}))
    (cache / "a2.json").write_text(json.dumps({"ts": 1004.7}))  # the cycle's last answer: swapped at 1004.7
    plans = [
        _plan_rec(1000.1, "A", 3000, ["a1", "a2"]),  # cache time -> first tick after 1004.7 = #3 (1006)
        _plan_rec(1010.1, "B", 2500, ["b1"]),  # no cache entry: 1010.1 + 2.5 s -> #7 (1014)
        _plan_rec(1020.1, "C", 100, ["c1"], cached=True),  # 1020.2 -> #11 (1022), but evidence says ...
    ]
    evidence = [_order_rec(1002.0, None), _order_rec(1006.0, "A"), _order_rec(1022.0, "B"), _order_rec(1026.0, "C")]
    slots = schedule_plans(_fake_run(ticks, plans + evidence), cache)
    assert [(s.swap_source, s.index) for s in slots] == [("cache", 3), ("latency", 7), ("latency", 12)]
    assert slots[2].lower == 11 and slots[2].upper == 13 and not slots[2].conflict  # tick #11 still showed B
    assert slots[0].upper == 3  # tick #3 already showed A


def test_network_guard_refuses_and_counts_internet_connections() -> None:
    with network_guard() as attempts:
        with pytest.raises(OSError, match="network access refused"):
            socket.create_connection(("192.0.2.1", 9))
        with pytest.raises(OSError, match="network access refused"):
            socket.getaddrinfo("api.wallex.ir", 443)
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            with pytest.raises(OSError, match="network access refused"):
                s.connect(("192.0.2.1", 9))
        finally:
            s.close()
    assert len(attempts) == 3


def test_replay_clock_jumps_once_its_condition_holds() -> None:
    flag = {"on": False}
    c = ReplayClock(10.0)
    c.jump_when(lambda: flag["on"], 10.2)
    assert c.now() == 10.0
    flag["on"] = True
    assert c.now() == 10.2 and c.now() == 10.2
    c.set(12.0)
    assert c.now() == 12.0


async def test_output_dir_must_be_fresh(recorded: dict[str, Any], tmp_path: Path) -> None:
    (tmp_path / "rep").mkdir()
    (tmp_path / "rep" / "old.txt").write_text("x")
    with pytest.raises(ReplayInputError, match="not empty"):
        await replay(recorded, tmp_path)


def test_compare_of_the_replay_with_itself_is_trivially_ok(recorded: dict[str, Any]) -> None:
    rep = compare_decisions(recorded["logs"], recorded["logs"])
    assert rep.ok


# ---------------------------------------------------------------- scripts/replay_check.py


def _script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("replay_check", ROOT / "scripts" / "replay_check.py")
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _run_script(args: list[str]) -> int:
    return int(_script().main(args))  # a sync test: ``main`` runs its own event loop (``asyncio.run``)


def test_replay_check_script_exit_codes(recorded: dict[str, Any], tmp_path: Path,
                                        capsys: pytest.CaptureFixture[str]) -> None:
    common = ["--log-dir", str(recorded["logs"]), "--state-db", str(recorded["db"]), "--no-index",
              "--reports-dir", str(tmp_path / "reports")]
    assert _run_script([*common, "--out", str(tmp_path / "ok")]) == 0
    out = capsys.readouterr().out
    assert "decisions reproduced -> PASS" in out and "0 socket attempts, 0 LLM provider calls" in out
    assert "final-heartbeat values equal" in out
    assert "SYNTHETIC" in out

    rec = copy_recording(recorded, tmp_path / "bad")
    target = plan_records(rec["logs"])[1]
    (rec["logs"] / "llm_cache" / f"{target['plan_snapshot']['prompt_hash']}.json").unlink()
    code = _run_script(["--log-dir", str(rec["logs"]), "--state-db", str(rec["db"]), "--no-index",
                        "--reports-dir", "", "--out", str(tmp_path / "bad_out")])
    assert code == 1
    assert "-> FAIL" in capsys.readouterr().out

    assert _run_script(["--log-dir", str(tmp_path / "nothing"), "--no-index", "--out", str(tmp_path / "x")]) == 2
    assert _run_script(["--decisions", str(tmp_path / "no" / "decisions.jsonl"), "--logs-root",
                        str(tmp_path / "empty_root"), "--no-index"]) == 2


def test_run_dirs_newest_live_first_else_newest_shadow(tmp_path: Path) -> None:
    def make(name: str, mode: str, ts: str) -> Path:
        d = tmp_path / name
        d.mkdir(parents=True)
        (d / "events-20261022.jsonl").write_text(json.dumps(
            {"kind": "start", "ts": ts, "reason": "x", "data": {"mode": mode, "feed": "live"}}) + "\n")
        (d / "decisions-20261022.jsonl").write_text("")
        return d

    make("shadow_old", "shadow", "2026-10-22T10:00:00.000Z")
    newest_shadow = make("shadow_new", "shadow", "2026-10-22T12:00:00.000Z")
    make("replayed", "replay", "2026-10-22T13:00:00.000Z")  # a replay's own output never counts
    picked = pick_run_dir(tmp_path)
    assert picked is not None and picked["path"] == newest_shadow
    live = make("live_run", "live", "2026-10-22T11:00:00.000Z")
    picked = pick_run_dir(tmp_path)
    assert picked is not None and picked["path"] == live and picked["mode"] == "live"
    assert [r["path"].name for r in find_run_dirs(tmp_path)] == ["shadow_new", "live_run", "shadow_old"]
    assert pick_run_dir(tmp_path / "missing") is None


# ---------------------------------------------------------------- report date, end state, analyst gaps


def test_the_report_is_filed_under_the_day_of_the_last_tick(recorded: dict[str, Any]) -> None:
    run = load_recorded_run(recorded["logs"], recorded["db"])
    midnight = 1_792_713_600.0  # 2026-10-23T00:00:00Z
    assert report_day(dataclasses.replace(run, tick_ts=[midnight - 30.0, midnight - 1.0, midnight + 5.0])) == "20261023"
    assert report_day(dataclasses.replace(run, tick_ts=[midnight - 30.0, midnight - 1.0])) == "20261022"


async def test_the_end_state_and_the_analyst_gaps_are_reported(recorded: dict[str, Any], tmp_path: Path) -> None:
    result = await replay(recorded, tmp_path / "plain")
    end = result.run["end_state"]
    assert end["compared"] and end["equal"], end
    fields = [r["field"] for r in end["rows"]]
    assert fields[:2] == ["equity_tmn", "fees_paid_tmn"] and "S1 BTC counters" in fields
    assert "## End state (informational, not part of the verdict)" in Path(result.report_file or "").read_text()
    assert not any(n.startswith("The recorded analyst") for n in result.notes)  # the offline provider never failed

    rec = copy_recording(recorded, tmp_path)
    run = load_recorded_run(rec["logs"], rec["db"])
    t1, t2 = iso(run.tick_ts[100]), iso(run.tick_ts[200])
    with family_files(rec["logs"], "events")[0].open("a") as fh:
        fh.write(json.dumps({"kind": "ai_degraded", "ts": t1, "reason": "analyst cycle failed: llm unavailable"}) + "\n")
        fh.write(json.dumps({"kind": "plan_stale", "ts": t2, "reason": "plan is old"}) + "\n")
    result = await replay(rec, tmp_path / "gaps")
    assert result.ok, result.to_markdown()
    (note,) = [n for n in result.notes if n.startswith("The recorded analyst")]
    assert f"1 failure streak(s) from {t1}; not recovered before the end" in note
    assert f"plan_stale event(s) ({t2}) both runs trade rules-only" in note
    assert note in Path(result.report_file or "").read_text()

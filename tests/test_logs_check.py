"""``check_logs``: every log family validates; invalid lines and orphan outcomes are flagged."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import orjson

from wta.logging_.checker import MAX_ERRORS_PER_FILE, check_logs
from wta.logging_.decision_log import DecisionLog, EventsLog, OrdersLog
from wta.logging_.health_log import HealthLog
from wta.models import HealthRecord
from wta.utils.timeutil import SimClock, iso, parse_iso

BEFORE_MIDNIGHT = parse_iso("2026-10-22T23:59:00Z") or 0.0


def write_valid_logs(log_dir: Path) -> SimClock:
    clock = SimClock(BEFORE_MIDNIGHT)
    decisions = DecisionLog(log_dir, "shadow", "abc", "cfg", clock)
    orders = OrdersLog(log_dir, clock)
    events = EventsLog(log_dir, clock)
    health = HealthLog(log_dir, clock)
    events.log("start", "boot")
    decisions.record("order", "d_1", "S1", "BTCUSDT", order={"client_id": "S1_BTCUSDT_1_AAAA"})
    orders.log("place", "S1_BTCUSDT_1_AAAA", "BTCUSDT", {"price": Decimal("82845.01")})
    health.write(HealthRecord(ts=iso(clock.now()), mode="shadow", git_commit="abc", config_hash="cfg"))
    clock.advance(120)  # next UTC day: the outcome lands in the next file and must still resolve
    decisions.outcome("d_1", {"pnl_tmn": Decimal("12"), "exit_reason": "target"})
    for w in (decisions, orders, events, health):
        w.close()
    return clock


def append(path: Path, *lines: bytes) -> None:
    with path.open("ab") as fh:
        for line in lines:
            fh.write(line + b"\n")


def test_empty_or_missing_dir_is_ok(tmp_path: Path) -> None:
    report = check_logs(tmp_path)
    assert report.ok and report.files == {}
    assert check_logs(tmp_path / "does-not-exist").ok
    assert "ok" in report.summary()


def test_empty_files_are_ok(tmp_path: Path) -> None:
    for name in ("decisions-20261022.jsonl", "health.jsonl", "events-20261022.jsonl", "orders.jsonl"):
        (tmp_path / name).write_bytes(b"\n\n")
    report = check_logs(tmp_path)
    assert report.ok
    assert all(r == {"lines": 0, "errors": []} for r in report.files.values())


def test_valid_logs_pass(tmp_path: Path) -> None:
    write_valid_logs(tmp_path)
    report = check_logs(tmp_path)
    assert report.ok, report.summary()
    assert report.files["decisions-20261022.jsonl"]["lines"] == 1
    assert report.files["decisions-20261023.jsonl"]["lines"] == 1
    assert report.files["orders-20261022.jsonl"]["lines"] == 1
    assert report.files["events-20261022.jsonl"]["lines"] == 1
    assert report.files["health-20261022.jsonl"]["lines"] == 1
    assert "decisions.jsonl" not in report.files  # the current-day symlink is not checked twice
    assert report.summary().endswith("OK")


def test_invalid_lines_are_flagged_with_line_numbers(tmp_path: Path) -> None:
    write_valid_logs(tmp_path)
    good = orjson.loads((tmp_path / "decisions-20261022.jsonl").read_bytes())
    append(
        tmp_path / "decisions-20261022.jsonl",
        b'{"kind": "order", "decision_id": "d_2"',  # line 2: torn
        orjson.dumps({**good, "kind": "teleport"}),  # line 3: bad kind
        orjson.dumps({**good, "ts": "yesterday-ish"}),  # line 4: bad ts
        b"[1, 2, 3]",  # line 5: not an object
    )
    append(tmp_path / "health-20261022.jsonl", orjson.dumps({"ts": iso(0), "mode": "shadow"}))
    append(
        tmp_path / "orders-20261022.jsonl", orjson.dumps({"ts": iso(0), "action": "nope", "client_id": "c"})
    )

    report = check_logs(tmp_path)
    assert not report.ok
    errors = report.files["decisions-20261022.jsonl"]["errors"]
    assert [e.split(":")[0] for e in errors] == ["line 2", "line 3", "line 4", "line 5"]
    assert "not valid JSON" in errors[0] and "kind" in errors[1] and "ISO-8601" in errors[2]
    assert report.files["decisions-20261022.jsonl"]["lines"] == 5
    [health_err] = report.files["health-20261022.jsonl"]["errors"]
    assert health_err.startswith("line 2:") and "git_commit" in health_err
    assert "action" in report.files["orders-20261022.jsonl"]["errors"][0]
    assert report.files["events-20261022.jsonl"]["errors"] == []
    assert report.summary().endswith("FAILED")


def test_orphan_outcome_is_flagged(tmp_path: Path) -> None:
    clock = write_valid_logs(tmp_path)
    log = DecisionLog(tmp_path, "shadow", "abc", "cfg", clock)
    log.outcome("d_never_recorded", {"pnl_tmn": "0"})
    log.close()
    report = check_logs(tmp_path)
    assert not report.ok
    [err] = report.files["decisions-20261023.jsonl"]["errors"]
    assert err.startswith("line 2:") and "d_never_recorded" in err


def test_unknown_fields_are_rejected_without_echoing_values(tmp_path: Path) -> None:
    clock = write_valid_logs(tmp_path)
    leaked = "sk-or-v1-" + "a" * 40
    append(
        tmp_path / "events-20261022.jsonl", orjson.dumps({"ts": iso(clock.now()), "kind": "x", "key": leaked})
    )
    report = check_logs(tmp_path)
    assert not report.ok
    assert leaked not in report.summary()
    assert "key" in report.files["events-20261022.jsonl"]["errors"][0]


def test_error_list_is_capped(tmp_path: Path) -> None:
    f = tmp_path / "events-20261022.jsonl"
    f.write_bytes(b"not json\n" * (MAX_ERRORS_PER_FILE + 7))
    res = check_logs(tmp_path).files[f.name]
    assert res["lines"] == MAX_ERRORS_PER_FILE + 7
    assert len(res["errors"]) == MAX_ERRORS_PER_FILE + 1
    assert res["errors"][-1] == "... 7 more errors"

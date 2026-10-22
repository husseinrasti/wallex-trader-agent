"""Decision/order/event/health JSONL logs: schema, append-only outcomes, rotation, scrubbing."""

from __future__ import annotations

import os
from decimal import Decimal
from pathlib import Path

import orjson
import pytest
from pydantic import ValidationError

from wta.config import canonical_json
from wta.logging_ import jsonl
from wta.logging_.decision_log import DecisionLog, EventsLog, OrdersLog, merge_decisions
from wta.logging_.health_log import HealthLog, latest
from wta.logging_.jsonl import JsonlWriter, family_files, iter_family
from wta.models import (
    DecisionRecord,
    EventRecord,
    HealthRecord,
    Intent,
    OrderLogRecord,
    OutcomeUpdate,
    RiskCheck,
    RiskDecision,
)
from wta.utils.timeutil import SimClock, iso, parse_iso

T0 = parse_iso("2026-10-22T10:00:00Z") or 0.0
BEFORE_MIDNIGHT = parse_iso("2026-10-22T23:59:59Z") or 0.0


def lines_of(path: Path) -> list[dict]:
    return [orjson.loads(line) for line in path.read_bytes().splitlines() if line.strip()]


def make_log(tmp_path: Path, clock: SimClock, **kw) -> DecisionLog:
    return DecisionLog(tmp_path, "shadow", "abc123def456", "cfg000000001", clock, **kw)


def make_intent() -> Intent:
    return Intent(
        id="i1",
        ts=T0,
        strategy="S1",
        symbol="BTCUSDT",
        side="BUY",
        order_type="LIMIT",
        qty=Decimal("0.00012"),
        price=Decimal("82845.01"),
        purpose="entry",
        reason="TMN book 0.31% cheaper than USDT book",
        expected_edge_pct=Decimal("0.0031"),
    )


# ---------------------------------------------------------------- schema


def test_decision_record_written_and_schema_valid(tmp_path: Path) -> None:
    clock = SimClock(T0)
    log = make_log(tmp_path, clock)
    risk = RiskDecision(approved=True, checks=[RiskCheck(code="EDGE_VS_COST", passed=True, detail="ok")])
    rec = log.record(
        "intent",
        "d_1",
        "S1",
        "BTCUSDT",
        features={"dev_pct": Decimal("0.0031"), "book_age_s": 0.4},
        intent=make_intent(),
        risk=risk,
        notes="cheap TMN book",
    )
    log.close()

    assert isinstance(rec, DecisionRecord)
    assert rec.ts == "2026-10-22T10:00:00.000Z"
    [line] = lines_of(tmp_path / "decisions-20261022.jsonl")
    back = DecisionRecord.model_validate(line)
    assert back.kind == "intent" and back.git_commit == "abc123def456" and back.config_hash == "cfg000000001"
    # money stays exact: Decimals are written as strings, never floats
    assert line["features"]["dev_pct"] == "0.0031"
    assert line["intent"]["price"] == "82845.01"
    assert line["intent"]["qty"] == "0.00012"
    assert line["risk"]["checks"][0]["code"] == "EDGE_VS_COST"


def test_lines_are_canonical_json(tmp_path: Path) -> None:
    log = make_log(tmp_path, SimClock(T0))
    rec = log.record("plan", "d_plan", "analyst", "*", features={"b": 1, "a": 2}, notes="range day")
    log.close()
    raw = (tmp_path / "decisions-20261022.jsonl").read_bytes()
    assert raw == canonical_json(rec.model_dump(mode="json")) + b"\n"
    assert raw.index(b'"config_hash"') < raw.index(b'"decision_id"') < raw.index(b'"kind"')  # sorted keys


def test_every_record_type_validates(tmp_path: Path) -> None:
    clock = SimClock(T0)
    orders = OrdersLog(tmp_path, clock)
    events = EventsLog(tmp_path, clock)
    health = HealthLog(tmp_path, clock)
    o = orders.log("place", "S1_BTCUSDT_1729591234567_A1B2", "BTCUSDT", {"price": Decimal("82845.01")})
    e = events.log("start", "engine boot", {"mode": "shadow"})
    h = health.write(
        HealthRecord(
            ts=iso(clock.now()),
            mode="shadow",
            git_commit="abc",
            config_hash="cfg",
            equity_tmn=Decimal("5000000.5"),
            drawdown_pct=Decimal("0.001"),
            reasons=["stale_data"],
        )
    )
    for w in (orders, events, health):
        w.close()

    assert isinstance(o, OrderLogRecord) and isinstance(e, EventRecord) and isinstance(h, HealthRecord)
    [ol] = lines_of(tmp_path / "orders-20261022.jsonl")
    assert OrderLogRecord.model_validate(ol).model_dump(mode="json") == o.model_dump(mode="json")
    assert ol["data"]["price"] == "82845.01"
    assert EventRecord.model_validate(lines_of(tmp_path / "events-20261022.jsonl")[0]) == e
    [hl] = lines_of(tmp_path / "health-20261022.jsonl")
    assert hl["equity_tmn"] == "5000000.5"
    assert HealthRecord.model_validate(hl) == h


def test_invalid_records_are_rejected_before_writing(tmp_path: Path) -> None:
    clock = SimClock(T0)
    log = make_log(tmp_path, clock)
    with pytest.raises(ValidationError):
        log.record("bogus", "d_1", "S1", "BTCUSDT")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="outcome"):
        log.record("outcome", "d_1", "S1", "BTCUSDT")
    with pytest.raises(ValidationError):
        OrdersLog(tmp_path, clock).log("teleport", "cid")
    with pytest.raises(TypeError):
        HealthLog(tmp_path, clock).write({"ts": "x"})  # type: ignore[arg-type]
    assert family_files(tmp_path, "decisions") == []
    assert family_files(tmp_path, "orders") == []


def test_mode_and_config_hash_can_change(tmp_path: Path) -> None:
    log = make_log(tmp_path, SimClock(T0))
    log.record("no_trade", "d_1", "S2", "USDTTMN")
    log.set_config_hash("cfg000000002")
    log.set_mode("live")
    log.record("no_trade", "d_2", "S2", "USDTTMN")
    log.close()
    first, second = lines_of(tmp_path / "decisions-20261022.jsonl")
    assert (first["mode"], first["config_hash"]) == ("shadow", "cfg000000001")
    assert (second["mode"], second["config_hash"]) == ("live", "cfg000000002")


# ---------------------------------------------------------------- outcomes


def test_outcome_is_appended_and_earlier_lines_never_change(tmp_path: Path) -> None:
    clock = SimClock(T0)
    log = make_log(tmp_path, clock)
    log.record("order", "d_1", "S1", "BTCUSDT", order={"client_id": "S1_BTCUSDT_1_AAAA"})
    log.record("veto", "d_2", "S1", "ETHUSDT", risk={"approved": False, "veto_reason": "RATE_HOUR"})
    path = tmp_path / "decisions-20261022.jsonl"
    before = path.read_bytes()

    clock.advance(600)
    upd = log.outcome(
        "d_1", {"pnl_tmn": Decimal("1234.5"), "pnl_pct": Decimal("0.0004"), "exit_reason": "target"}
    )
    clock.advance(60)
    log.outcome("d_1", {"pnl_tmn": Decimal("1300"), "exit_reason": "target"})  # a correction: later wins
    log.outcome("d_missing", {"pnl_tmn": "0"})
    log.close()

    after = path.read_bytes()
    assert after.startswith(before)  # append-only: the old bytes are untouched
    assert len(after.splitlines()) == 5
    assert isinstance(upd, OutcomeUpdate) and upd.ts == "2026-10-22T10:10:00.000Z"
    on_disk = OutcomeUpdate.model_validate(lines_of(path)[2])
    assert on_disk.model_dump(mode="json") == upd.model_dump(mode="json")

    merged = merge_decisions(iter_family(tmp_path, "decisions"))
    assert list(merged) == ["d_1", "d_2"]
    assert merged["d_1"]["outcome"] == {"pnl_tmn": "1300", "exit_reason": "target"}
    assert merged["d_1"]["order"] == {"client_id": "S1_BTCUSDT_1_AAAA"}
    assert merged["d_2"]["outcome"] is None
    assert [o["decision_id"] for o in merged.orphans] == ["d_missing"]


def test_merge_overlays_repeated_records_and_keeps_inputs_intact() -> None:
    intent_rec = {
        "kind": "intent",
        "decision_id": "d_1",
        "intent": {"side": "BUY"},
        "order": None,
        "notes": "x",
    }
    order_rec = {
        "kind": "order",
        "decision_id": "d_1",
        "intent": None,
        "order": {"status": "NEW"},
        "notes": "",
    }
    early_outcome = {"kind": "outcome", "decision_id": "d_1", "ts": "t", "outcome": {"pnl_tmn": "5"}}
    merged = merge_decisions([early_outcome, intent_rec, order_rec])
    rec = merged["d_1"]
    assert rec["kind"] == "order"
    assert rec["intent"] == {"side": "BUY"} and rec["order"] == {"status": "NEW"} and rec["notes"] == "x"
    assert rec["outcome"] == {"pnl_tmn": "5"}
    assert merged.orphans == []
    assert intent_rec["order"] is None and "outcome" not in intent_rec


# ---------------------------------------------------------------- rotation + symlink


def test_daily_rotation_across_utc_midnight(tmp_path: Path) -> None:
    clock = SimClock(BEFORE_MIDNIGHT)
    log = make_log(tmp_path, clock)
    log.record("no_trade", "d_1", "S2", "USDTTMN")
    assert os.readlink(tmp_path / "decisions.jsonl") == "decisions-20261022.jsonl"
    clock.advance(2)  # 2026-10-23T00:00:01Z
    log.record("no_trade", "d_2", "S2", "USDTTMN")
    log.outcome("d_1", {"pnl_tmn": "0"})
    log.close()

    day1, day2 = tmp_path / "decisions-20261022.jsonl", tmp_path / "decisions-20261023.jsonl"
    assert [r["decision_id"] for r in lines_of(day1)] == ["d_1"]
    assert [r["decision_id"] for r in lines_of(day2)] == ["d_2", "d_1"]
    assert lines_of(day2)[0]["ts"] == "2026-10-23T00:00:01.000Z"

    link = tmp_path / "decisions.jsonl"
    assert link.is_symlink()
    assert os.readlink(link) == "decisions-20261023.jsonl"  # relative, points at the current day
    assert link.resolve() == day2.resolve()
    assert family_files(tmp_path, "decisions") == [day1, day2]  # the symlink is not read twice
    assert [r["decision_id"] for r in iter_family(tmp_path, "decisions")] == ["d_1", "d_2", "d_1"]
    assert merge_decisions(iter_family(tmp_path, "decisions"))["d_1"]["outcome"] == {"pnl_tmn": "0"}


def test_no_symlink_when_platform_refuses(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*args: object, **kwargs: object) -> None:
        raise OSError("symlinks not permitted")

    monkeypatch.setattr(jsonl.os, "symlink", refuse)
    w = JsonlWriter(tmp_path, "events", SimClock(T0))
    w.write({"ts": iso(T0), "kind": "start"})
    w.close()
    assert not w.symlink_ok
    assert not (tmp_path / "events.jsonl").exists()
    assert len(lines_of(tmp_path / "events-20261022.jsonl")) == 1


def test_regular_plain_file_is_never_clobbered(tmp_path: Path) -> None:
    plain = tmp_path / "decisions.jsonl"  # e.g. a copied deliverable
    plain.write_bytes(b'{"decision_id": "old", "kind": "plan"}\n\n')
    log = make_log(tmp_path, SimClock(T0))
    log.record("plan", "new", "analyst", "*")
    log.close()
    assert not plain.is_symlink()
    assert plain.read_bytes() == b'{"decision_id": "old", "kind": "plan"}\n\n'
    assert [r["decision_id"] for r in iter_family(tmp_path, "decisions")] == ["old", "new"]


def test_rotate_daily_false_writes_one_plain_file(tmp_path: Path) -> None:
    clock = SimClock(BEFORE_MIDNIGHT)
    log = make_log(tmp_path, clock, rotate_daily=False)
    log.record("no_trade", "d_1", "S2", "USDTTMN")
    clock.advance(86400)
    log.record("no_trade", "d_2", "S2", "USDTTMN")
    log.close()
    plain = tmp_path / "decisions.jsonl"
    assert plain.is_file() and not plain.is_symlink()
    assert [p.name for p in tmp_path.iterdir()] == ["decisions.jsonl"]
    assert len(lines_of(plain)) == 2


def test_iter_family_skips_blank_and_broken_lines(tmp_path: Path) -> None:
    f = tmp_path / "orders-20261022.jsonl"
    f.write_bytes(b'{"a": 1}\n\n   \n{"a": 2\n[1, 2]\n{"a": 3}\n')
    (tmp_path / "orders-backup.jsonl").write_bytes(b'{"a": 99}\n')  # not part of the family
    assert [r["a"] for r in iter_family(tmp_path, "orders")] == [1, 3]
    with pytest.raises(ValueError, match="orders-20261022.jsonl:4"):
        list(iter_family(tmp_path, "orders", strict=True))
    assert list(iter_family(tmp_path / "missing", "orders")) == []


# ---------------------------------------------------------------- scrubbing


def test_secrets_are_scrubbed_from_every_line(tmp_path: Path) -> None:
    secret = "wallexKEY-1234567890abcdef"
    quoted = 'pa"ss\\word99'  # needs JSON escaping: must still be caught
    clock = SimClock(T0)
    orders = OrdersLog(tmp_path, clock, secrets=[secret, quoted, "", "ab"])
    rec = orders.log(
        "error",
        "S1_BTCUSDT_1_AAAA",
        "BTCUSDT",
        {"headers": {"X-API-Key": secret}, "message": f"bad key {secret}", "other": quoted},
    )
    orders.log("status", "S1_BTCUSDT_1_AAAA", data={"status": "ab"})  # too short to be a secret: untouched
    orders.close()

    raw = (tmp_path / "orders-20261022.jsonl").read_text()
    assert secret not in raw and 'pa\\"ss' not in raw and "1234567890abcdef" not in raw
    assert orders.writer.scrubbed == 3
    first, second = lines_of(tmp_path / "orders-20261022.jsonl")  # still valid JSON
    assert first["data"]["headers"]["X-API-Key"] == "***"
    assert first["data"]["message"] == "bad key ***"
    assert first["data"]["other"] == "***"
    assert second["data"]["status"] == "ab"
    assert rec.data["message"] == f"bad key {secret}"  # only the disk copy is scrubbed


def test_writer_flushes_each_line_immediately(tmp_path: Path) -> None:
    w = JsonlWriter(tmp_path, "events", SimClock(T0), fsync=True)
    w.write({"ts": iso(T0), "kind": "start"})
    assert len(lines_of(tmp_path / "events-20261022.jsonl")) == 1  # readable before close()
    w.write({"ts": iso(T0), "kind": "pause"})
    assert len(lines_of(tmp_path / "events-20261022.jsonl")) == 2
    w.close()


# ---------------------------------------------------------------- health latest


def test_health_latest_reads_newest_line(tmp_path: Path) -> None:
    assert latest(tmp_path) is None
    clock = SimClock(BEFORE_MIDNIGHT - 120)
    health = HealthLog(tmp_path, clock)

    def beat(n: int) -> None:
        health.write(
            HealthRecord(
                ts=iso(clock.now()), mode="shadow", git_commit="abc", config_hash="cfg", open_orders=n
            )
        )
        clock.advance(60)

    for n in range(3):
        beat(n)
    assert latest(tmp_path)["open_orders"] == 2  # type: ignore[index]
    beat(7)  # crosses midnight into the next file
    health.close()
    assert len(family_files(tmp_path, "health")) == 2
    assert latest(tmp_path)["open_orders"] == 7  # type: ignore[index]


def test_health_latest_handles_big_files_and_torn_lines(tmp_path: Path) -> None:
    f = tmp_path / "health-20261022.jsonl"
    body = b"".join(orjson.dumps({"n": i, "pad": "x" * 300}) + b"\n" for i in range(200))
    f.write_bytes(body + b'{"n": 200, "pad": "torn' + b"\n\n")  # crash mid-line, then blank lines
    assert latest(tmp_path)["n"] == 199  # type: ignore[index]
    f.write_bytes(b'{"only": true}')  # single line, no trailing newline
    assert latest(tmp_path) == {"only": True}

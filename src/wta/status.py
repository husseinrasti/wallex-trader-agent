"""One-screen status summary (PRD-06 §2): ``python -m wta status`` and Telegram ``/status``.

``build_status`` collects everything from files other processes write, read-only and in a separate
process from the engine; ``format_status`` turns it into plain text (no colour codes).

Sources, each optional (a missing or unreadable file is reported, never raised):

* ``<log_dir>/health*.jsonl`` — the latest heartbeat: mode, feed, git commit, config hash, equity,
  drawdown, daily PnL, fees, USDT share, coin exposure, paused/killed/flatten, health reasons.
* ``<log_dir>/events*.jsonl`` — the last ``start`` event (git tag, pid, deadline) and the last event.
* ``<log_dir>/decisions*.jsonl`` — the last decisions (newest first, one line per decision id;
  ``outcome`` updates are attached to their decision, ``no_trade`` ticks are hidden) and the last
  ``plan`` record.
* ``state_db`` (engine state SQLite) — kv flags (pause/kill reasons, flatten phase) and portfolio
  counters, open positions, open orders, equity rows, plans, books for the current bid.
* ``market_db`` (``data/wta.sqlite``, the recorder) — books in the last 10 minutes per symbol.
* ``<pid_dir>/*.pid`` — processes, alive or dead.

Nothing here reads the wall clock: ``now`` is passed in. Decimals in the returned dict are strings
(exact, JSON-safe, like the logs). Log files are read backwards from their end with a line cap, so
the command stays fast on a three-day run.
"""

from __future__ import annotations

import os
import sqlite3
import textwrap
from collections.abc import Callable, Iterator
from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation
from pathlib import Path
from typing import Any, TypeVar

import orjson

from wta.data.store import Store
from wta.logging_.health_log import _reverse_lines, latest
from wta.logging_.jsonl import family_files
from wta.models import BookSnapshot, Level
from wta.utils.timeutil import iso, parse_iso

T = TypeVar("T")

HEARTBEAT_WARN_S = 180.0  # PRD-06 §2 watchdog: no heartbeat for 3 min
RECORDER_WINDOW_S = 600.0  # recorder freshness: books in the last 10 minutes
PLAN_STALE_S = 1200.0  # ai.plan_stale_s default: older plans are ignored by the strategies
MAX_SCAN_LINES = 50_000  # per log family and call: how far back we look for decisions/plans/events
DEFAULT_INITIAL_EQUITY_TMN = Decimal("5000000")  # engine.initial_equity_tmn default (challenge capital)
DEFAULT_FEE_BUDGET_PCT = Decimal("0.02")  # risk.fee_budget_pct default
DEFAULT_MARKET_DB = Path("data/wta.sqlite")

OFFLINE_MODEL = "offline-rules-v1"  # wta.ai.offline_provider: the rules provider used without an LLM key
OFFLINE_TEXT = "offline rules (no LLM key)"
SYNTHETIC_WARNING = "SYNTHETIC market data: an offline exercise of the engine, never evidence of performance"
KV_FLAGS = "engine_flags"  # wta.execution.reconcile.KV_FLAGS
KV_PORTFOLIO = "portfolio_state"  # wta.execution.reconcile.KV_PORTFOLIO
FLATTEN_PHASES = {
    0: "normal",
    1: "flatten start, maker exits",
    2: "flatten final, market exits",
    3: "challenge over",
}
COMPACT_WIDTH = 80  # narrower output (Telegram) drops the aligned table columns
HIDDEN_DECISION_KINDS = frozenset({"outcome", "no_trade"})


# ---------------------------------------------------------------- small helpers


def _dec(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        d = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return d if d.is_finite() else None


def _s(value: Decimal | None) -> str | None:
    return None if value is None else str(value)


def _ratio(num: Decimal | None, den: Decimal | None) -> Decimal | None:
    if num is None or den is None or den == 0:
        return None
    return num / den


def _round(value: Decimal, places: str = "0.000001") -> Decimal:
    return value.quantize(Decimal(places), rounding=ROUND_HALF_EVEN)


def _ts_of(value: Any) -> float | None:
    """A record's time: unix seconds (number) or ISO-8601 text."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return parse_iso(value)
        except ValueError:
            return None
    return None


def _age(now: float, ts: float | None) -> float | None:
    return None if ts is None else round(now - ts, 3)


def _safe(errors: list[str], label: str, fn: Callable[[], T], default: T) -> T:
    """Run one read; a failure is recorded in ``errors`` (no traceback, no file contents)."""
    try:
        return fn()
    except Exception as exc:  # status must never crash because one source is broken
        errors.append(f"{label}: {type(exc).__name__}: {str(exc)[:160]}")
        return default


def _iter_reversed(log_dir: Path, base: str, max_lines: int = MAX_SCAN_LINES) -> Iterator[dict[str, Any]]:
    """Records of a log family, newest first (files newest first, lines from the end)."""
    seen = 0
    for path in reversed(family_files(log_dir, base)):
        for line in _reverse_lines(path):
            seen += 1
            if seen > max_lines:
                return
            try:
                obj = orjson.loads(line)
            except orjson.JSONDecodeError:
                continue  # e.g. a line cut short by a crash
            if isinstance(obj, dict):
                yield obj


def pid_alive(pid: int) -> bool:
    """``kill -0``: True if a process with this pid exists (also when it belongs to another user)."""
    if pid <= 0:
        return False  # 0 / negative would signal a whole process group
    try:
        os.kill(pid, 0)
    except PermissionError:
        return True
    except (ProcessLookupError, OSError):
        return False
    return True


def _open_ro(path: Path | None, label: str, errors: list[str]) -> Store | None:
    if path is None or not Path(path).is_file():
        return None
    return _safe(errors, f"{label} {path}", lambda: Store.open_readonly(path), None)


# ---------------------------------------------------------------- sections


def _heartbeat(log_dir: Path, now: float, warn_s: float, errors: list[str]) -> dict[str, Any] | None:
    rec = _safe(errors, "health log", lambda: latest(log_dir, "health"), None)
    if rec is None:
        return None
    extra = rec.get("extra") if isinstance(rec.get("extra"), dict) else {}
    ts = _ts_of(rec.get("ts"))
    age = _age(now, ts)
    return {
        "ts": rec.get("ts"),
        "age_s": age,
        "stale": age is None or age > warn_s,
        "mode": rec.get("mode"),
        "feed": extra.get("feed"),
        "git_commit": rec.get("git_commit"),
        "git_dirty": extra.get("git_dirty"),
        "config_hash": rec.get("config_hash"),
        "uptime_s": extra.get("uptime_s"),
        "ws_ok": rec.get("ws_ok"),
        "rest_ok": rec.get("rest_ok"),
        "api_errors_1m": rec.get("api_errors_1m"),
        "llm_ok": rec.get("llm_ok"),
        "paused": bool(rec.get("paused")),
        "killed": bool(rec.get("killed")),
        "reasons": [str(r) for r in rec.get("reasons") or []],
        "open_orders": rec.get("open_orders"),
        "positions": rec.get("positions"),
        "equity_tmn": rec.get("equity_tmn"),
        "drawdown_pct": rec.get("drawdown_pct"),
        "extra": extra,
    }


def _events(log_dir: Path, now: float, errors: list[str]) -> dict[str, Any]:
    def scan() -> dict[str, Any]:
        last: dict[str, Any] | None = None
        start: dict[str, Any] | None = None
        for rec in _iter_reversed(log_dir, "events"):
            if last is None:
                last = rec
            if rec.get("kind") == "start":
                start = rec
                break
        out: dict[str, Any] = {"last": None, "start": None}
        if last is not None:
            out["last"] = {
                "ts": last.get("ts"),
                "age_s": _age(now, _ts_of(last.get("ts"))),
                "kind": last.get("kind"),
                "reason": last.get("reason", ""),
            }
        if start is not None:
            data = start.get("data") if isinstance(start.get("data"), dict) else {}
            out["start"] = {
                "ts": start.get("ts"),
                "age_s": _age(now, _ts_of(start.get("ts"))),
                "reason": data.get("reason"),
                "mode": data.get("mode"),
                "feed": data.get("feed"),
                "synthetic": bool(data.get("synthetic")) or data.get("feed") == "synthetic",
                "git_commit": data.get("git_commit"),
                "git_tag": data.get("git_tag"),
                "git_dirty": data.get("git_dirty"),
                "config_hash": data.get("config_hash"),
                "pid": data.get("pid"),
                "challenge_end_utc": data.get("challenge_end_utc"),
            }
        return out

    return _safe(errors, "events log", scan, {"last": None, "start": None})


def _decision_result(rec: dict[str, Any]) -> str:
    kind = rec.get("kind")
    risk = rec.get("risk") if isinstance(rec.get("risk"), dict) else {}
    order = rec.get("order") if isinstance(rec.get("order"), dict) else {}
    if kind == "veto" or risk.get("approved") is False:
        codes = [
            str(c.get("code"))
            for c in risk.get("checks") or []
            if isinstance(c, dict) and c.get("passed") is False
        ]
        return "VETO " + (",".join(codes) if codes else str(risk.get("veto_reason") or "?"))
    if risk.get("approved") is True:
        if kind == "order" and order:
            return "approved, order sent" if order.get("accepted") else "approved, order not accepted"
        return "approved"
    if kind == "plan":
        snap = rec.get("plan_snapshot") if isinstance(rec.get("plan_snapshot"), dict) else {}
        features = rec.get("features") if isinstance(rec.get("features"), dict) else {}
        model = snap.get("model") or features.get("model") or "?"
        return "model " + (OFFLINE_TEXT if model == OFFLINE_MODEL else str(model))
    return "-"


def _plan_from_record(rec: dict[str, Any]) -> dict[str, Any]:
    """A ``kind="plan"`` decision record -> plan dict (snapshot first, then meta from features)."""
    snap = rec.get("plan_snapshot") if isinstance(rec.get("plan_snapshot"), dict) else {}
    features = rec.get("features") if isinstance(rec.get("features"), dict) else {}
    plan = {**features, **snap}
    if not plan.get("notes") and rec.get("notes"):
        plan["notes"] = rec.get("notes")
    if _ts_of(plan.get("ts")) is None:
        plan["ts"] = rec.get("ts")
    return plan


def _decisions(
    log_dir: Path, n: int, errors: list[str]
) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    """The last ``n`` decisions (newest first) and the last plan record of the decision log."""

    def scan() -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
        outcomes: dict[str, Any] = {}
        seen: set[str] = set()
        rows: list[dict[str, Any]] = []
        plan: dict[str, Any] | None = None
        for rec in _iter_reversed(log_dir, "decisions"):
            kind = rec.get("kind")
            did = str(rec.get("decision_id"))
            if kind == "outcome":
                outcomes.setdefault(did, rec.get("outcome"))  # newest update wins
                continue
            if kind == "plan" and plan is None:
                plan = _plan_from_record(rec)
            if len(rows) < n and kind not in HIDDEN_DECISION_KINDS and did not in seen:
                seen.add(did)
                intent = rec.get("intent") if isinstance(rec.get("intent"), dict) else {}
                outcome = outcomes.get(did)
                rows.append(
                    {
                        "ts": rec.get("ts"),
                        "kind": kind,
                        "decision_id": did,
                        "strategy": rec.get("strategy", ""),
                        "symbol": rec.get("symbol", ""),
                        "side": intent.get("side") or "",
                        "purpose": intent.get("purpose") or "",
                        "result": _decision_result(rec),
                        "outcome_pnl_pct": outcome.get("pnl_pct") if isinstance(outcome, dict) else None,
                        "notes": str(rec.get("notes") or intent.get("reason") or ""),
                    }
                )
            if len(rows) >= n and plan is not None:
                break
        return rows, plan

    return _safe(errors, "decision log", scan, ([], None))


def _db_plan(store: Store | None, errors: list[str]) -> dict[str, Any] | None:
    if store is None:
        return None

    def read() -> dict[str, Any] | None:
        _lo, hi = store.time_range("plans")
        if hi is None:
            return None
        rows = store.load_plans(from_ts=hi)
        if not rows:
            return None
        row = rows[-1]
        plan = dict(row["plan"]) if isinstance(row.get("plan"), dict) else {}
        if _ts_of(plan.get("ts")) is None:
            plan["ts"] = row["ts"]
        plan.setdefault("prompt_hash", row.get("prompt_hash"))
        return plan

    return _safe(errors, "state db plans", read, None)


def _plan_summary(
    plan: dict[str, Any] | None, source: str, now: float, stale_s: float
) -> dict[str, Any] | None:
    if plan is None:
        return None
    ts = _ts_of(plan.get("ts"))
    age = _age(now, ts)
    model = str(plan.get("model") or "")
    regimes: dict[str, dict[str, Any]] = {}
    raw_regimes = plan.get("regimes") if isinstance(plan.get("regimes"), dict) else {}
    for sym in sorted(raw_regimes):
        call = raw_regimes[sym]
        if isinstance(call, dict):
            regimes[sym] = {"regime": call.get("regime"), "confidence": call.get("confidence")}
        else:
            regimes[sym] = {"regime": str(call), "confidence": None}
    return {
        "source": source,
        "ts": iso(ts) if ts is not None else None,
        "age_s": age,
        "stale": age is None or age > stale_s,
        "model": model,
        "offline": model == OFFLINE_MODEL,
        "model_text": OFFLINE_TEXT if model == OFFLINE_MODEL else (model or "?"),
        "cached": plan.get("cached"),
        "regimes": regimes,
        "enabled_strategies": list(plan.get("enabled_strategies") or []),
        "size_multiplier": _s(_dec(plan.get("size_multiplier"))),
        "usdt_target_share": _s(_dec(plan.get("usdt_target_share"))),
        "risk_flags": list(plan.get("risk_flags") or []),
        "notes": str(plan.get("notes") or ""),
    }


def _latest_engine_book(path: Path, symbol: str) -> BookSnapshot | None:
    """Newest row of ``engine_books`` (books the engine traded on) for one symbol, read-only."""
    uri = Path(path).resolve().as_uri() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=5)
    try:
        row = conn.execute(
            "SELECT ts, symbol, source, bids_json, asks_json FROM engine_books"
            " WHERE symbol = ? ORDER BY ts DESC, rowid DESC LIMIT 1",
            (symbol,),
        ).fetchone()
    except sqlite3.OperationalError:
        return None  # an older schema without the table
    finally:
        conn.close()
    if row is None:
        return None
    ts, sym, source, bids, asks = row

    def levels(text: str) -> list[Level]:
        return [Level(price=Decimal(p), qty=Decimal(q)) for p, q in orjson.loads(text)]

    return BookSnapshot(symbol=sym, ts=ts, source=source, bids=levels(bids), asks=levels(asks))


def _best_bid(
    symbol: str, stores: list[tuple[str, Store, Path]], errors: list[str]
) -> tuple[Decimal | None, float | None, str | None]:
    """Newest best bid of ``symbol`` over every database we have (books and engine_books)."""
    best: BookSnapshot | None = None
    origin: str | None = None
    for label, store, path in stores:
        candidates = [
            _safe(errors, f"{label} books {symbol}", lambda s=store: s.latest_book(symbol), None),
            _safe(
                errors, f"{label} engine_books {symbol}", lambda p=path: _latest_engine_book(p, symbol), None
            ),
        ]
        for book in candidates:
            if book is not None and book.best_bid is not None and (best is None or book.ts > best.ts):
                best, origin = book, label
    if best is None:
        return None, None, None
    return best.best_bid, best.ts, origin


def _recorder(market_db: Path | None, now: float, window_s: float, errors: list[str]) -> dict[str, Any]:
    out: dict[str, Any] = {
        "db": None if market_db is None else str(market_db),
        "exists": market_db is not None and Path(market_db).is_file(),
        "window_s": window_s,
        "books_in_window": 0,
        "symbols": {},
        "last_book_ts": None,
        "last_book_age_s": None,
    }
    store = _open_ro(market_db, "market db", errors) if out["exists"] else None
    if store is None:
        return out
    try:
        counts = _safe(
            errors, "market db books", lambda: store.book_counts_by_symbol(since_ts=now - window_s), {}
        )
        _lo, hi = _safe(errors, "market db books range", lambda: store.time_range("books"), (None, None))
        symbols: dict[str, dict[str, Any]] = {}
        for sym in sorted(counts):
            _a, last = _safe(
                errors, f"market db {sym}", lambda s=sym: store.time_range("books", s), (None, None)
            )
            symbols[sym] = {"count": counts[sym], "last_age_s": _age(now, last)}
        out.update(
            books_in_window=sum(counts.values()),
            symbols=symbols,
            last_book_ts=None if hi is None else iso(hi),
            last_book_age_s=_age(now, hi),
        )
    finally:
        store.close()
    return out


def _processes(pid_dir: Path, alive: Callable[[int], bool], errors: list[str]) -> list[dict[str, Any]]:
    def read() -> list[dict[str, Any]]:
        if not Path(pid_dir).is_dir():
            return []
        rows = []
        for path in sorted(Path(pid_dir).glob("*.pid")):
            text = path.read_text(encoding="utf-8", errors="replace")
            digits = "".join(ch for ch in text if ch.isdigit())
            pid = int(digits) if digits else None
            rows.append(
                {
                    "name": path.stem,
                    "file": str(path),
                    "pid": pid,
                    "alive": bool(pid is not None and alive(pid)),
                }
            )
        return rows

    return _safe(errors, f"pid files {pid_dir}", read, [])


# ---------------------------------------------------------------- build


def build_status(
    log_dir: Path,
    state_db: Path | None,
    now: float,
    pid_dir: Path = Path("run"),
    *,
    market_db: Path | None = DEFAULT_MARKET_DB,
    initial_equity_tmn: Decimal | None = None,
    fee_budget_pct: Decimal | None = None,
    heartbeat_warn_s: float = HEARTBEAT_WARN_S,
    recorder_window_s: float = RECORDER_WINDOW_S,
    plan_stale_s: float = PLAN_STALE_S,
    decisions_n: int = 5,
    is_alive: Callable[[int], bool] = pid_alive,
) -> dict[str, Any]:
    """Collect the status from logs, the engine state DB, the recorder DB and pid files.

    Never raises because a file is missing or broken: every problem lands in ``errors`` and the
    affected section is ``None``/empty. ``warnings`` holds what an operator must see first.
    """
    log_dir = Path(log_dir)
    errors: list[str] = []
    warnings: list[str] = []
    initial = initial_equity_tmn if initial_equity_tmn is not None else DEFAULT_INITIAL_EQUITY_TMN
    budget_pct = fee_budget_pct if fee_budget_pct is not None else DEFAULT_FEE_BUDGET_PCT

    hb = _heartbeat(log_dir, now, heartbeat_warn_s, errors)
    events = _events(log_dir, now, errors)
    start = events.get("start") or {}
    extra: dict[str, Any] = hb["extra"] if hb else {}

    # ------------------------------------------------ engine identity
    feed = (hb or {}).get("feed") or start.get("feed")
    synthetic = feed == "synthetic" or bool(start.get("synthetic") and not (hb or {}).get("feed"))
    end_ts = _ts_of(start.get("challenge_end_utc"))
    commit = (hb or {}).get("git_commit") or start.get("git_commit")
    engine = {
        "mode": (hb or {}).get("mode") or start.get("mode"),
        "feed": feed,
        "synthetic": synthetic,
        "heartbeat_ts": (hb or {}).get("ts"),
        "heartbeat_age_s": (hb or {}).get("age_s"),
        "heartbeat_stale": hb is None or bool(hb["stale"]),
        "git_commit": commit,
        # the tag is only known from the start event: never show it for another commit's heartbeat
        "git_tag": start.get("git_tag") if start.get("git_commit") in (None, commit) else None,
        "git_dirty": extra.get("git_dirty", start.get("git_dirty")),
        "config_hash": (hb or {}).get("config_hash") or start.get("config_hash"),
        "uptime_s": (hb or {}).get("uptime_s"),
        "pid": start.get("pid"),
        "started_ts": start.get("ts"),
        "start_reason": start.get("reason"),
        "ws_ok": (hb or {}).get("ws_ok"),
        "rest_ok": (hb or {}).get("rest_ok"),
        "api_errors_1m": (hb or {}).get("api_errors_1m"),
        "llm_ok": (hb or {}).get("llm_ok"),
        "health_reasons": (hb or {}).get("reasons", []),
        "challenge_end_utc": start.get("challenge_end_utc"),
        "deadline_in_s": None if end_ts is None else round(end_ts - now, 3),
        "last_event": events.get("last"),
    }

    # ------------------------------------------------ engine state DB
    state: Store | None = _open_ro(state_db, "state db", errors)
    flags: dict[str, Any] | None = None
    portfolio: dict[str, Any] | None = None
    equity_row: dict[str, Any] | None = None
    positions_raw: list[Any] = []
    open_orders: int | None = None
    db_plan: dict[str, Any] | None = None
    if state is not None:
        flags = _safe(errors, "state db kv flags", lambda: state.kv_get(KV_FLAGS), None)
        portfolio = _safe(errors, "state db kv portfolio", lambda: state.kv_get(KV_PORTFOLIO), None)
        equity_row = _safe(errors, "state db equity", state.latest_equity, None)
        positions_raw = _safe(errors, "state db positions", state.load_open_positions, [])
        orders = _safe(errors, "state db orders", lambda: state.load_orders(non_terminal_only=True), None)
        open_orders = None if orders is None else len(orders)
        db_plan = _db_plan(state, errors)

    # ------------------------------------------------ equity
    equity = _dec((hb or {}).get("equity_tmn"))
    equity_source = "health" if equity is not None else None
    drawdown = _dec((hb or {}).get("drawdown_pct"))
    peak = _dec(extra.get("peak_equity_tmn"))
    usdt_share = _dec(extra.get("usdt_share"))
    coin_exposure = _dec(extra.get("coin_exposure_pct"))
    equity_ts = _ts_of((hb or {}).get("ts"))
    if equity_row is not None and equity_row.get("equity_tmn") is not None:
        row_ts = _ts_of(equity_row.get("ts"))
        if equity is None or (row_ts is not None and equity_ts is not None and row_ts > equity_ts):
            eq = equity_row["equity_tmn"]
            equity, equity_source, equity_ts = eq, "state_db", row_ts
            drawdown = equity_row.get("drawdown_pct")
            peak = equity_row.get("peak_tmn") or peak
            usdt_share = _ratio(equity_row.get("usdt_tmn_value"), eq)
            coin_exposure = _ratio(equity_row.get("coins_tmn_value"), eq)
    port = portfolio if isinstance(portfolio, dict) else {}
    if peak is None:
        peak = _dec(port.get("peak_equity_tmn"))
    if drawdown is None and equity is not None and peak:
        drawdown = max((peak - equity) / peak, Decimal(0))
    day_start = _dec(port.get("day_start_equity_tmn"))
    daily_pct = _dec(extra.get("daily_pnl_pct")) if equity_source == "health" else None
    if daily_pct is None and equity is not None and day_start:
        daily_pct = equity / day_start - 1
    fees_paid = _dec(extra.get("fees_paid_tmn"))
    if fees_paid is None:
        fees_paid = _dec(port.get("fees_paid_tmn_total"))
    fee_budget = budget_pct * initial
    realized = _dec(extra.get("realized_pnl_tmn"))
    if realized is None and isinstance(port.get("realized_pnl_tmn"), dict):
        values = [_dec(v) for v in port["realized_pnl_tmn"].values()]
        realized = sum((v for v in values if v is not None), Decimal(0))
    equity_sec = {
        "source": equity_source,
        "ts": None if equity_ts is None else iso(equity_ts),
        "equity_tmn": _s(equity),
        "initial_equity_tmn": _s(initial),
        "return_pct": _s(None if equity is None or not initial else _round(equity / initial - 1)),
        "peak_tmn": _s(peak),
        "drawdown_pct": _s(None if drawdown is None else _round(drawdown)),
        "daily_pnl_pct": _s(None if daily_pct is None else _round(daily_pct)),
        "daily_pnl_tmn": _s(
            None if equity is None or day_start is None else _round(equity - day_start, "0.01")
        ),
        "fees_paid_tmn": _s(fees_paid),
        "fee_budget_tmn": _s(fee_budget),
        "fee_budget_used_pct": _s(
            None if fees_paid is None or not fee_budget else _round(fees_paid / fee_budget)
        ),
        "usdt_share": _s(None if usdt_share is None else _round(usdt_share)),
        "coin_exposure_pct": _s(None if coin_exposure is None else _round(coin_exposure)),
        "realized_pnl_tmn": _s(realized),
    }

    # ------------------------------------------------ risk switches (kv flags are the persisted truth)
    if isinstance(flags, dict):
        phase_raw = flags.get("flatten_phase", 0)
        risk = {
            "source": "state_db",
            "paused": bool(flags.get("paused")),
            "pause_reason": str(flags.get("pause_reason") or ""),
            "killed": bool(flags.get("killed")),
            "kill_reason": str(flags.get("kill_reason") or ""),
        }
    else:
        phase_raw = extra.get("flatten_phase", 0)
        reasons = engine["health_reasons"]
        risk = {
            "source": "health" if hb else None,
            "paused": bool((hb or {}).get("paused")),
            "pause_reason": next((r[len("paused: ") :] for r in reasons if r.startswith("paused: ")), ""),
            "killed": bool((hb or {}).get("killed")),
            "kill_reason": next((r[len("killed: ") :] for r in reasons if r.startswith("killed: ")), ""),
        }
    try:
        phase = int(phase_raw or 0)
    except (TypeError, ValueError):
        phase = 0
    risk.update(flatten_phase=phase, flatten_phase_name=FLATTEN_PHASES.get(phase, "?"))

    # ------------------------------------------------ positions (PnL at the newest bid we have)
    books_from: list[tuple[str, Store, Path]] = []
    market: Store | None = None
    if state is not None and state_db is not None:
        books_from.append(("state db", state, Path(state_db)))
    positions: list[dict[str, Any]] = []
    if positions_raw:
        same_file = (
            market_db is not None
            and state_db is not None
            and Path(market_db).resolve() == Path(state_db).resolve()
        )
        if not same_file:
            market = _open_ro(market_db, "market db", errors)
            if market is not None and market_db is not None:
                books_from.append(("market db", market, Path(market_db)))
        for pos in positions_raw:
            bid, bid_ts, origin = _best_bid(pos.symbol, books_from, errors)
            pnl = None if bid is None or not pos.entry_price else _round(bid / pos.entry_price - 1)
            positions.append(
                {
                    "id": pos.id,
                    "strategy": pos.strategy,
                    "symbol": pos.symbol,
                    "qty": str(pos.qty),
                    "entry_price": str(pos.entry_price),
                    "entry_ts": iso(pos.entry_ts),
                    "stop_price": _s(pos.stop_price),
                    "bid": _s(bid),
                    "bid_age_s": _age(now, bid_ts),
                    "bid_source": origin,
                    "pnl_pct": _s(pnl),
                }
            )
    if market is not None:
        market.close()
    if state is not None:
        state.close()
    positions_count = len(positions) if state is not None else (hb or {}).get("positions")
    if open_orders is None and hb is not None:
        open_orders = hb.get("open_orders")

    # ------------------------------------------------ decisions, plan, processes, recorder
    decisions, log_plan = _decisions(log_dir, decisions_n, errors)
    plan_src, plan_raw = None, None
    for src, candidate in (("decisions", log_plan), ("state_db", db_plan)):
        if candidate is None:
            continue
        if plan_raw is None or (_ts_of(candidate.get("ts")) or 0) > (_ts_of(plan_raw.get("ts")) or 0):
            plan_src, plan_raw = src, candidate
    plan = _plan_summary(plan_raw, plan_src or "", now, plan_stale_s)
    processes = _processes(Path(pid_dir), is_alive, errors)
    recorder = _recorder(market_db, now, recorder_window_s, errors)

    # ------------------------------------------------ warnings, most important first
    if risk["killed"]:
        warnings.append(
            f"KILLED: {risk['kill_reason'] or 'no reason recorded'} (manual reset: wta reset-kill)"
        )
    if synthetic:
        warnings.append(SYNTHETIC_WARNING)
    if hb is None:
        warnings.append(f"no heartbeat in {log_dir}/health*.jsonl: the agent has not run with these logs")
    elif hb["stale"]:
        age = hb["age_s"]
        shown = "unknown time" if age is None else _dur(age)
        warnings.append(f"last heartbeat {shown} ago (> {heartbeat_warn_s:g}s): the agent may be down")
    if risk["paused"]:
        warnings.append(f"PAUSED: {risk['pause_reason'] or 'no reason recorded'} (no new entries)")
    if risk["flatten_phase"]:
        warnings.append(f"flatten phase {risk['flatten_phase']}: {risk['flatten_phase_name']}")
    for proc in processes:
        if not proc["alive"]:
            warnings.append(
                f"process {proc['name']} (pid {proc['pid'] or '?'}) is DEAD: stale {proc['file']}"
            )
    if plan is not None and plan["stale"]:
        age = plan["age_s"]
        warnings.append(
            f"plan is {'of unknown age' if age is None else _dur(age) + ' old'} (> {plan_stale_s:g}s):"
            " strategies use rules-only defaults"
        )
    if recorder["exists"] and not recorder["books_in_window"]:
        last = recorder["last_book_age_s"]
        warnings.append(
            f"recorder: no books in the last {_dur(recorder_window_s)}"
            + (f" (newest {_dur(last)} ago)" if last is not None else " (no books recorded at all)")
        )
    if state_db is not None and not Path(state_db).is_file():
        errors.append(f"state db {state_db}: not found")

    return {
        "now": iso(now),
        "now_ts": now,
        "warnings": warnings,
        "engine": engine,
        "equity": equity_sec,
        "risk": risk,
        "positions": positions,
        "positions_count": positions_count,
        "open_orders": open_orders,
        "decisions": decisions,
        "plan": plan,
        "processes": processes,
        "recorder": recorder,
        "sources": {
            "log_dir": str(log_dir),
            "state_db": None if state_db is None else str(state_db),
            "state_db_found": state_db is not None and Path(state_db).is_file(),
            "market_db": None if market_db is None else str(market_db),
            "pid_dir": str(pid_dir),
        },
        "errors": errors,
    }


# ---------------------------------------------------------------- format


def _dur(seconds: float | None) -> str:
    if seconds is None:
        return "?"
    s = int(abs(seconds))
    if s < 60:
        text = f"{s}s"
    elif s < 3600:
        text = f"{s // 60}m {s % 60:02d}s"
    elif s < 86400:
        text = f"{s // 3600}h {s % 3600 // 60:02d}m"
    else:
        text = f"{s // 86400}d {s % 86400 // 3600:02d}h"
    return "-" + text if seconds < 0 else text


def _tmn(value: Any, signed: bool = False) -> str:
    d = _dec(value)
    if d is None:
        return "n/a"
    q = d.quantize(Decimal(1), rounding=ROUND_HALF_EVEN)
    return f"{q:+,}" if signed else f"{q:,}"


def _pct(value: Any, signed: bool = False, places: int = 2) -> str:
    d = _dec(value)
    if d is None:
        return "n/a"
    return f"{d * 100:+.{places}f}%" if signed else f"{d * 100:.{places}f}%"


def _num(value: Any) -> str:
    d = _dec(value)
    return "n/a" if d is None else f"{d:,}"


def _fit(text: str, width: int) -> str:
    return text if len(text) <= width else text[: max(width - 3, 1)] + "..."


def _rows(row: str, width: int, compact: bool) -> list[str]:
    """A table row: cut to ``width``, or wrapped with a 4-space continuation in compact mode."""
    if not compact:
        return [_fit(row, width)]
    wrapped = textwrap.wrap(row, width, initial_indent="", subsequent_indent="    ", break_on_hyphens=False)
    return [_fit(x, width) for x in wrapped] or [""]


def _line(label: str, parts: list[str], width: int) -> list[str]:
    """``label`` + parts joined by `` | ``, wrapped between parts; a part longer than a whole line
    is itself wrapped over several lines."""
    indent = " " * 11
    head = f"{label:<10} "
    room = max(width - len(indent), 10)
    chunks: list[str] = []
    for part in parts:
        if part:
            chunks.extend(textwrap.wrap(part, room, break_on_hyphens=False) if len(part) > room else [part])
    lines: list[str] = []
    cur = head
    for part in chunks:
        sep = "" if cur in (head, indent) else " | "
        if cur not in (head, indent) and len(cur) + len(sep) + len(part) > width:
            lines.append(cur)
            cur, sep = indent, ""
        cur += sep + part
    lines.append(cur)
    return [_fit(x.rstrip(), width) for x in lines]


def _ok(value: Any) -> str:
    return "n/a" if value is None else ("ok" if value else "DOWN")


def format_status(status: dict[str, Any], width: int = 100) -> str:
    """Plain-text one-screen summary of ``build_status`` output (no colour codes)."""
    width = max(int(width), 40)
    eng = status.get("engine") or {}
    eq = status.get("equity") or {}
    risk = status.get("risk") or {}
    out: list[str] = [_fit(f"WTA status at {status.get('now', '?')} (UTC)", width)]
    for w in status.get("warnings") or []:
        out.extend(
            _fit(x, width)
            for x in textwrap.wrap(f"!! {w}", width, subsequent_indent="   ", break_on_hyphens=False)
        )

    hb_age = eng.get("heartbeat_age_s")
    heartbeat = "no heartbeat" if eng.get("heartbeat_ts") is None else f"heartbeat {_dur(hb_age)} ago"
    if eng.get("heartbeat_stale") and eng.get("heartbeat_ts") is not None:
        heartbeat += " (STALE)"
    feed = eng.get("feed") or "?"
    out += _line(
        "Engine",
        [
            f"mode {eng.get('mode') or '?'}",
            f"feed {'SYNTHETIC' if eng.get('synthetic') else feed}",
            heartbeat,
            f"uptime {_dur(eng['uptime_s'])}" if eng.get("uptime_s") is not None else "",
            f"pid {eng['pid']}" if eng.get("pid") else "",
        ],
        width,
    )
    commit = str(eng.get("git_commit") or "unknown")
    out += _line(
        "Build",
        [
            f"commit {commit[:12]}" + (" (dirty)" if eng.get("git_dirty") else ""),
            f"tag {eng['git_tag']}" if eng.get("git_tag") else "no tag",
            f"config {eng.get('config_hash') or '?'}",
        ],
        width,
    )
    if eng.get("heartbeat_ts") is not None:
        llm = eng.get("llm_ok")
        out += _line(
            "Health",
            [
                f"rest {_ok(eng.get('rest_ok'))}",
                f"ws {_ok(eng.get('ws_ok'))}",
                f"api errors {eng.get('api_errors_1m') or 0}/min",
                f"llm {'n/a' if llm is None else _ok(llm)}",
                *[str(r) for r in eng.get("health_reasons") or []],
            ],
            width,
        )

    state = "KILLED" if risk.get("killed") else ("PAUSED" if risk.get("paused") else "running")
    reason = risk.get("kill_reason") if risk.get("killed") else risk.get("pause_reason")
    deadline = eng.get("deadline_in_s")
    out += _line(
        "Risk",
        [
            state + (f": {reason}" if reason and state != "running" else ""),
            f"flatten phase {risk.get('flatten_phase', 0)} ({risk.get('flatten_phase_name', 'normal')})",
            "no deadline set"
            if deadline is None
            else (
                f"deadline in {_dur(deadline)}" if deadline >= 0 else f"deadline passed {_dur(-deadline)} ago"
            ),
        ],
        width,
    )

    if eq.get("equity_tmn") is None:
        out += _line("Equity", ["no equity yet (no trading heartbeat or equity row)"], width)
    else:
        daily = _pct(eq.get("daily_pnl_pct"), signed=True)
        if eq.get("daily_pnl_tmn") is not None:
            daily += f" ({_tmn(eq['daily_pnl_tmn'], signed=True)} TMN)"
        out += _line(
            "Equity",
            [
                f"{_tmn(eq['equity_tmn'])} TMN",
                f"return {_pct(eq.get('return_pct'), signed=True)} vs {_tmn(eq.get('initial_equity_tmn'))}",
                f"peak {_tmn(eq.get('peak_tmn'))}",
                f"drawdown {_pct(eq.get('drawdown_pct'))}",
                f"daily PnL {daily}",
            ],
            width,
        )
    out += _line(
        "Fees",
        [
            f"{_tmn(eq.get('fees_paid_tmn'))} / {_tmn(eq.get('fee_budget_tmn'))} TMN budget"
            f" ({_pct(eq.get('fee_budget_used_pct'))} used)",
            f"realized PnL {_tmn(eq.get('realized_pnl_tmn'), signed=True)} TMN",
        ],
        width,
    )
    out += _line(
        "Exposure",
        [
            f"USDT share {_pct(eq.get('usdt_share'), places=1)}",
            f"coins {_pct(eq.get('coin_exposure_pct'), places=1)}",
        ],
        width,
    )

    positions = status.get("positions") or []
    n_pos = status.get("positions_count")
    orders = status.get("open_orders")
    out += _line(
        "Positions",
        [
            f"{'?' if n_pos is None else n_pos} open",
            f"{'?' if orders is None else orders} open orders",
            "" if status.get("sources", {}).get("state_db_found") else "details need the engine state db",
        ],
        width,
    )
    compact = width < COMPACT_WIDTH
    if positions and not compact:
        out.append(
            _fit(
                f"  {'STRATEGY':<9}{'SYMBOL':<10}{'QTY':>14}{'ENTRY':>18}{'BID':>18}{'PNL':>9}  BID AGE",
                width,
            )
        )
    for p in positions:
        bid_age = "no book" if p.get("bid_age_s") is None else _dur(p["bid_age_s"])
        pnl = _pct(p.get("pnl_pct"), signed=True)
        if compact:
            row = (
                f"  {p['strategy']} {p['symbol']} {pnl} | qty {p['qty']} @ {_num(p['entry_price'])}"
                f" | bid {_num(p.get('bid'))} ({bid_age})"
            )
        else:
            row = (
                f"  {str(p['strategy'])[:8]:<9}{str(p['symbol'])[:9]:<10}{p['qty']:>14}"
                f"{_num(p['entry_price']):>18}{_num(p.get('bid')):>18}{pnl:>9}  {bid_age}"
            )
        out.extend(_rows(row, width, compact))

    decisions = status.get("decisions") or []
    if not decisions:
        out += _line("Decisions", ["none logged"], width)
    else:
        out += _line("Decisions", [f"last {len(decisions)}, newest first", "no_trade ticks hidden"], width)
        for d in decisions:
            ts = str(d.get("ts") or "")
            when = ts[11:19] if len(ts) >= 19 else ts
            res = str(d.get("result") or "-")
            if d.get("outcome_pnl_pct") is not None:
                res += f", closed {_pct(d['outcome_pnl_pct'], signed=True)}"
            if compact:
                fields = [when, d.get("kind"), d.get("strategy"), d.get("symbol"), d.get("side")]
                row = "  " + " ".join(str(f) for f in fields if f) + f": {res}"
            else:
                row = (
                    f"  {when:<9}{str(d.get('kind'))[:8]:<9}{str(d.get('strategy'))[:8]:<9}"
                    f"{str(d.get('symbol'))[:9]:<10}{str(d.get('side'))[:4]:<5}{res}"
                )
            out.extend(_rows(row, width, compact))

    plan = status.get("plan")
    if plan is None:
        out += _line("Plan", ["no plan yet (rules-only defaults)"], width)
    else:
        share = plan.get("usdt_target_share")
        out += _line(
            "Plan",
            [
                plan.get("model_text") or "?",
                f"{_dur(plan.get('age_s'))} old" + (" (STALE)" if plan.get("stale") else ""),
                f"size x{plan['size_multiplier']}" if plan.get("size_multiplier") is not None else "",
                f"USDT target {_pct(share, places=0)}" if share is not None else "",
                ("strategies " + ",".join(plan["enabled_strategies"]))
                if plan.get("enabled_strategies")
                else "",
            ],
            width,
        )
        regimes = plan.get("regimes") or {}
        if regimes:
            parts = []
            for sym, call in regimes.items():
                conf = _dec(call.get("confidence"))
                parts.append(f"{sym} {call.get('regime')}" + ("" if conf is None else f" {conf:.2f}"))
            out += _line("", parts, width)
        if plan.get("notes"):
            notes = textwrap.wrap(
                " ".join(str(plan["notes"]).split()),
                width,
                initial_indent=" " * 11,
                subsequent_indent=" " * 11,
                max_lines=3,
                placeholder=" ...",
            )
            out.extend(_fit(x, width) for x in notes)

    procs = status.get("processes") or []
    out += _line(
        "Processes",
        [f"{p['name']} {p.get('pid') or '?'} {'alive' if p['alive'] else 'DEAD'}" for p in procs]
        or [f"no pid files in {status.get('sources', {}).get('pid_dir', 'run')}/"],
        width,
    )

    rec = status.get("recorder") or {}
    if not rec.get("db"):
        out += _line("Recorder", ["no market database configured"], width)
    elif not rec.get("exists"):
        out += _line("Recorder", ["not found (recorder never ran here)", str(rec["db"])], width)
    else:
        newest = rec.get("last_book_age_s")
        out += _line(
            "Recorder",
            [
                f"{rec.get('books_in_window', 0):,} books in the last {_dur(rec.get('window_s'))}",
                f"{len(rec.get('symbols') or {})} symbols",
                "no books at all" if newest is None else f"newest {_dur(newest)} ago",
                str(rec["db"]),
            ],
            width,
        )

    errors = status.get("errors") or []
    for e in errors:
        out.append(_fit(f"(error) {e}", width))
    return "\n".join(out)


# ---------------------------------------------------------------- config wiring


def resolve_state_db(state_sqlite_path: str, sqlite_path: str, mode: str | None) -> Path | None:
    """The engine state DB of ``mode``: ``data.state_sqlite_path`` (``{mode}`` filled in) when it
    exists, else ``data.sqlite_path`` when it exists (engines that keep state in the market DB),
    else None."""
    candidates = []
    if mode and "{mode}" in state_sqlite_path:
        candidates.append(Path(state_sqlite_path.format(mode=mode)))
    elif "{mode}" not in state_sqlite_path:
        candidates.append(Path(state_sqlite_path))
    candidates.append(Path(sqlite_path))
    for path in candidates:
        if path.is_file():
            return path
    return None


def status_for_config(
    cfg: Any,
    now: float,
    *,
    log_dir: Path = Path("logs"),
    pid_dir: Path = Path("run"),
    mode: str | None = None,
) -> dict[str, Any]:
    """``build_status`` with paths and limits from a ``wta.config.Config``.

    ``mode`` picks the state DB; by default the mode of the latest heartbeat, else ``shadow``.
    """
    if mode is None:
        hb = None
        try:
            hb = latest(Path(log_dir), "health")
        except OSError:
            hb = None
        mode = str((hb or {}).get("mode") or "shadow")
    state_db = resolve_state_db(cfg.data.state_sqlite_path, cfg.data.sqlite_path, mode)
    return build_status(
        Path(log_dir),
        state_db,
        now,
        Path(pid_dir),
        market_db=Path(cfg.data.sqlite_path),
        initial_equity_tmn=cfg.engine.initial_equity_tmn,
        fee_budget_pct=cfg.risk.fee_budget_pct,
        plan_stale_s=cfg.ai.plan_stale_s,
    )

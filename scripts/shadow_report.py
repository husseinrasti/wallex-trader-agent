"""Shadow-run report (PRD-03 §9, PRD-05 §6): one Markdown file from a run's logs (+ its state DB).

    python scripts/shadow_report.py --log-dir logs --state-db data/state_shadow.sqlite
    python scripts/shadow_report.py --log-dir /tmp/run/logs --out docs/REPORTS/shadow_20261022.md

Reads only files (never the network, never the wall clock): ``events``, ``health``, ``decisions``
and ``orders`` JSONL families in ``--log-dir`` and, when given, the state DB's ``equity`` table
(read-only). Without a state DB the equity curve comes from the heartbeats. Sections:

* window, feed (a loud **SYNTHETIC — not evidence** banner when any run used the synthetic feed),
  heartbeats (count, gaps, reasons), equity (start/end/peak/max drawdown, via
  ``backtest.metrics.compute_metrics``), decisions by kind and strategy, vetoes by risk code,
  fills and closed positions with PnL, fees vs the fee budget, S1 opportunity ticks per coin (the
  engine's counters in ``health.extra.strategies``), S2 rebalances, errors, and ``logs check``.

The default ``--out`` is ``docs/REPORTS/shadow_<YYYYMMDD of the window end>.md``. Exit codes:
0 = report written, 1 = no log records found in ``--log-dir``, 2 = bad arguments.
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from backtest.metrics import compute_metrics, read_equity_table, trades_from_decisions, vetoes_by_code
from wta.logging_.checker import check_logs
from wta.logging_.decision_log import merge_decisions
from wta.logging_.jsonl import iter_family
from wta.utils.timeutil import iso, parse_iso

EXIT_OK, EXIT_NO_DATA, EXIT_BAD_ARGS = 0, 1, 2
HEARTBEAT_GAP_FACTOR = 2.5  # a gap longer than this many median intervals is listed
SYNTHETIC_BANNER = (
    "> **SYNTHETIC — not evidence.** This run used the offline synthetic market feed "
    "(`--feed synthetic`). It exercises the engine end to end; its trades, PnL and opportunity "
    "counts say nothing about Wallex or about strategy performance."
)


# ---------------------------------------------------------------- small helpers


def _ts(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return parse_iso(str(value))


def _dec(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _money(value: Any, places: int = 0) -> str:
    d = _dec(value)
    return "n/a" if d is None else f"{d:,.{places}f}"


def _pct(value: Any, places: int = 2) -> str:
    d = _dec(value)
    return "n/a" if d is None else f"{d * 100:.{places}f} %"


def _when(ts: float | None) -> str:
    return "n/a" if ts is None else iso(ts)


def _table(header: list[str], rows: Iterable[list[Any]]) -> list[str]:
    out = ["| " + " | ".join(header) + " |", "|" + "|".join("---" for _ in header) + "|"]
    out += ["| " + " | ".join(str(c) for c in row) + " |" for row in rows]
    return out


def _hours(seconds: float | None) -> str:
    return "n/a" if seconds is None else f"{seconds / 3600:.2f} h ({seconds:,.0f} s)"


# ---------------------------------------------------------------- data


class Run:
    """Every record of one log directory (+ the optional state DB's equity rows)."""

    def __init__(self, log_dir: Path, state_db: Path | None) -> None:
        self.log_dir = log_dir
        self.events = list(iter_family(log_dir, "events"))
        self.health = list(iter_family(log_dir, "health"))
        self.decisions = list(iter_family(log_dir, "decisions"))
        self.orders = list(iter_family(log_dir, "orders"))
        self.merged = merge_decisions(self.decisions)
        self.state_db = state_db
        self.equity_rows = read_equity_table(state_db) if state_db is not None else []
        self.starts = [e for e in self.events if e.get("kind") == "start"]
        self.stops = [e for e in self.events if e.get("kind") == "stop"]

    @property
    def empty(self) -> bool:
        return not (self.events or self.health or self.decisions)

    def window(self) -> tuple[float | None, float | None]:
        stamps = [_ts(r.get("ts")) for r in (*self.events, *self.health)]
        known = [t for t in stamps if t is not None]
        return (min(known), max(known)) if known else (None, None)

    @property
    def synthetic(self) -> bool:
        for e in self.starts:
            data = e.get("data") or {}
            if data.get("synthetic") or data.get("feed") == "synthetic":
                return True
        return any((h.get("extra") or {}).get("feed") == "synthetic" for h in self.health)

    def equity_points(self) -> tuple[list[dict[str, Any]], str]:
        if self.equity_rows:
            return self.equity_rows, f"state DB `equity` table ({len(self.equity_rows)} rows)"
        points = [
            {"ts": _ts(h.get("ts")), "equity_tmn": h.get("equity_tmn")}
            for h in self.health
            if h.get("equity_tmn") is not None
        ]
        return points, f"heartbeats ({len(points)} with an equity value; no state DB given)"


# ---------------------------------------------------------------- sections


def _section_window(run: Run) -> list[str]:
    start, end = run.window()
    out = ["## Window", ""]
    out += [f"- from {_when(start)} to {_when(end)}: {_hours(end - start if start and end else None)}"]
    out += [f"- engine runs: {len(run.starts)} start event(s), {len(run.stops)} stop event(s)"]
    for e in run.stops:
        data = e.get("data") or {}
        out.append(
            f"  - stop at {e.get('ts')}: {e.get('reason')} (uptime {data.get('uptime_s')} s, "
            f"{data.get('ticks')} ticks, {data.get('heartbeats')} heartbeats)"
        )
    return out


def _section_feed(run: Run) -> list[str]:
    out = ["## Feed", ""]
    if run.synthetic:
        out += ["**Synthetic feed** (see the banner above): not evidence of anything about Wallex.", ""]
    if not run.starts:
        return [*out, "No start event in these logs."]
    rows = []
    for e in run.starts:
        d = e.get("data") or {}
        rows.append(
            [
                e.get("ts"),
                d.get("mode"),
                d.get("feed"),
                "yes" if d.get("synthetic") else "no",
                str(d.get("git_commit") or "")[:12] + (" (dirty)" if d.get("git_dirty") else ""),
                d.get("config_hash"),
                ", ".join(d.get("overlays") or []) or "-",
                ", ".join(d.get("strategies") or []) or "-",
                "yes" if d.get("rules_only", True) else "no",
                d.get("reason"),
            ]
        )
    header = ["start", "mode", "feed", "synthetic", "commit", "config", "overlays", "strategies",
              "rules-only", "reason"]
    return out + _table(header, rows)


def _section_heartbeats(run: Run) -> list[str]:
    out = ["## Heartbeats", ""]
    stamps = [t for t in (_ts(h.get("ts")) for h in run.health) if t is not None]
    if not stamps:
        return [*out, "No heartbeat in these logs."]
    gaps = [b - a for a, b in zip(stamps, stamps[1:], strict=False)]
    median = sorted(gaps)[len(gaps) // 2] if gaps else None
    long_gaps = [(a, b - a) for a, b in zip(stamps, stamps[1:], strict=False) if median and b - a > HEARTBEAT_GAP_FACTOR * median]
    reasons: Counter[str] = Counter()
    for h in run.health:
        for r in h.get("reasons") or []:
            reasons[str(r).split(":")[0]] += 1
    out += [
        f"- {len(stamps)} heartbeats, median interval {median:.0f} s" if median else f"- {len(stamps)} heartbeat(s)",
        f"- gaps longer than {HEARTBEAT_GAP_FACTOR}x the median: {len(long_gaps)}"
        + "".join(f"\n  - {_when(a)}: {g:.0f} s" for a, g in long_gaps[:10]),
        f"- heartbeats killed: {sum(1 for h in run.health if h.get('killed'))}, "
        f"paused: {sum(1 for h in run.health if h.get('paused'))}",
    ]
    if reasons:
        out += ["- health reasons (heartbeats that carried them):"]
        out += [f"  - {k}: {v}" for k, v in sorted(reasons.items(), key=lambda kv: (-kv[1], kv[0]))]
    else:
        out += ["- health reasons: none (every heartbeat healthy)"]
    return out


def _metrics(run: Run) -> tuple[Any, str]:
    points, source = run.equity_points()
    trades = trades_from_decisions(run.merged)
    first = points[0]["equity_tmn"] if points else None
    m = compute_metrics(equity=points, trades=trades, decisions=run.merged, initial_equity=first, label="shadow")
    return m, source


def _section_equity(run: Run, m: Any, source: str) -> list[str]:
    out = ["## Equity", "", f"Source: {source}.", ""]
    if not m.n_equity_points:
        return [*out, "No equity values (no usable valuation was recorded)."]
    peak = None
    points, _ = run.equity_points()
    for p in points:
        e = _dec(p.get("equity_tmn"))
        if e is not None and (peak is None or e > peak):
            peak = e
    rows = [
        ["start equity", f"{_money(m.initial_equity_tmn, 2)} TMN"],
        ["end equity", f"{_money(m.final_equity_tmn, 2)} TMN"],
        ["peak equity", f"{_money(peak, 2)} TMN"],
        ["PnL", f"{_money(m.pnl_tmn, 2)} TMN ({_pct(m.return_pct, 3)})"],
        ["max drawdown", _pct(m.max_drawdown_pct, 3)],
        ["equity points", m.n_equity_points],
    ]
    return out + _table(["metric", "value"], rows)


def _section_decisions(run: Run) -> list[str]:
    out = ["## Decisions", ""]
    counts: Counter[tuple[str, str]] = Counter()
    for rec in run.decisions:
        kind = str(rec.get("kind"))
        if kind == "outcome":
            continue
        counts[(kind, str(rec.get("strategy")))] += 1
    outcomes = sum(1 for rec in run.decisions if rec.get("kind") == "outcome")
    if not counts:
        return [*out, "No decision records."]
    rows = [[k, s, n] for (k, s), n in sorted(counts.items())]
    return out + _table(["kind", "strategy", "records"], rows) + ["", f"Outcome updates: {outcomes}."]


def _section_vetoes(run: Run) -> list[str]:
    out = ["## Vetoes", ""]
    by_code = vetoes_by_code(run.merged)
    if not by_code:
        return [*out, "No vetoes."]
    return out + _table(["risk check", "vetoed decisions"], [[c, n] for c, n in by_code.items()])


def _section_fills(run: Run, m: Any) -> list[str]:
    out = ["## Fills and closed positions", ""]
    fills: Counter[tuple[str, str]] = Counter()
    for rec in run.orders:
        if rec.get("action") == "fill":
            data = rec.get("data") or {}
            fills[(str(rec.get("symbol")), str(data.get("side")))] += 1
    if fills:
        out += _table(["symbol", "side", "fills"], [[s, side, n] for (s, side), n in sorted(fills.items())])
    else:
        out.append("No fills.")
    trades = trades_from_decisions(run.merged)
    out += ["", f"Closed positions: {len(trades)} (win rate {_pct(m.win_rate, 1)}, "
            f"closed-trade PnL {_money(m.closed_trades_pnl_tmn, 2)} TMN)."]
    if trades:
        rows = [
            [
                t.get("strategy"),
                t.get("symbol"),
                t.get("exit_reason"),
                _money(t.get("pnl_tmn"), 2),
                _pct(t.get("pnl_pct"), 3),
                f"{float(t.get('hold_s') or 0):.0f} s",
                _when(_ts(t.get("closed_ts"))),
            ]
            for t in trades
        ]
        out += [""] + _table(["strategy", "symbol", "exit", "PnL TMN", "PnL %", "hold", "closed"], rows)
    return out


def _last_extra(run: Run) -> dict[str, Any]:
    for h in reversed(run.health):
        extra = h.get("extra") or {}
        if "fees_paid_tmn" in extra:
            return extra
    return {}


def _section_fees(run: Run) -> list[str]:
    out = ["## Fees", ""]
    extra = _last_extra(run)
    if not extra:
        return [*out, "No trading heartbeat (fees unknown)."]
    fees, used = _dec(extra.get("fees_paid_tmn")), _dec(extra.get("fee_budget_used_pct"))
    out.append(
        f"- fees paid {_money(fees, 2)} TMN = {_pct(used, 3)} of the fee budget "
        "(risk.fee_budget_pct x initial equity; the RiskManager's FEE_BUDGET check)"
    )
    out.append(f"- orders today (last heartbeat): {extra.get('orders_today')}, last hour: {extra.get('orders_last_hour')}")
    out.append(f"- realized PnL (last heartbeat): {_money(extra.get('realized_pnl_tmn'), 2)} TMN")
    return out


def s1_opportunities(health: Iterable[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    """Per coin: S1 ticks evaluated and opportunity ticks summed over engine runs (the engine's
    counters restart with every process; a counter that goes down marks a new run)."""
    totals: dict[str, dict[str, Any]] = {}
    last: dict[str, dict[str, Any]] = {}
    for h in health:
        coins = (((h.get("extra") or {}).get("strategies") or {}).get("S1") or {}).get("coins") or {}
        for coin in sorted(coins):
            stats = (coins[coin] or {}).get("stats")
            if not stats:
                continue
            prev = last.get(coin)
            if prev is not None and int(stats.get("evaluated", 0)) < int(prev.get("evaluated", 0)):
                _add(totals, coin, prev)  # a new engine run started
            last[coin] = dict(stats)
            totals.setdefault(coin, {"evaluated": 0, "opportunity_ticks": 0, "best_excess_pct": None,
                                     "why": None})
            totals[coin]["why"] = (coins[coin] or {}).get("why_no_trade")
    for coin, stats in last.items():
        _add(totals, coin, stats)
    return totals


def _add(totals: dict[str, dict[str, Any]], coin: str, stats: Mapping[str, Any]) -> None:
    t = totals.setdefault(coin, {"evaluated": 0, "opportunity_ticks": 0, "best_excess_pct": None, "why": None})
    t["evaluated"] += int(stats.get("evaluated", 0))
    t["opportunity_ticks"] += int(stats.get("opportunity_ticks", 0))
    best = _dec(stats.get("best_excess_pct"))
    if best is not None and (t["best_excess_pct"] is None or best > t["best_excess_pct"]):
        t["best_excess_pct"] = best


def _section_s1(run: Run) -> list[str]:
    out = ["## S1 opportunities", ""]
    totals = s1_opportunities(run.health)
    entries = [r for r in run.merged.values() if r.get("strategy") == "S1" and (r.get("intent") or {}).get("purpose") == "entry"]
    placed = [r for r in entries if r.get("kind") == "order" and (r.get("order") or {}).get("accepted")]
    out.append(
        "An opportunity tick is a fast tick where a lock-in edge reached cost + margin on either side "
        "(the engine's counters, sampled by the heartbeats). Best excess = max(edge - need) seen."
    )
    out.append("")
    if not totals:
        out.append("No S1 diagnostics in the heartbeats.")
    else:
        rows = [
            [coin, t["evaluated"], t["opportunity_ticks"], _pct(t["best_excess_pct"], 3), (t["why"] or "-")[:140]]
            for coin, t in sorted(totals.items())
        ]
        out += _table(["coin", "ticks evaluated", "opportunity ticks", "best excess", "last why-no-trade"], rows)
    closed = [t for t in trades_from_decisions(run.merged) if t.get("strategy") == "S1"]
    out += ["", f"S1 entries: {len(entries)} decided, {len(placed)} placed; closed S1 cycles: {len(closed)}."]
    return out


def _section_s2(run: Run) -> list[str]:
    out = ["## S2 rebalances", ""]
    recs = [r for r in run.merged.values() if r.get("strategy") == "S2" and (r.get("intent") or {}).get("purpose") == "rebalance"]
    if not recs:
        return [*out, "No S2 rebalance decisions."]
    rows = []
    for r in sorted(recs, key=lambda x: str(x.get("ts"))):
        it, outcome = r.get("intent") or {}, r.get("outcome") or {}
        feats = it.get("features") or {}
        request = (r.get("order") or {}).get("request") or {}
        rows.append(
            [
                r.get("ts"),
                r.get("kind"),
                it.get("side"),
                f"{it.get('qty')} / {request.get('qty', '-')}",
                request.get("price") or it.get("price"),
                _pct(feats.get("usdt_share"), 1),
                _pct(feats.get("target_share"), 1),
                "yes" if feats.get("initial_allocation") else "no",
                outcome.get("status") or ("vetoed" if r.get("kind") == "veto" else "open/unknown"),
                outcome.get("filled_qty", "-"),
            ]
        )
    header = ["ts", "kind", "side", "qty USDT (intent / order)", "price", "share before", "target", "initial",
              "status", "filled"]
    return out + _table(header, rows)


def _section_errors(run: Run) -> list[str]:
    out = ["## Errors", ""]
    kinds = ("tick_error", "kill", "pause", "resume", "flatten", "reset_kill", "reset_kill_applied")
    events = Counter(str(e.get("kind")) for e in run.events if e.get("kind") in kinds)
    last = run.health[-1].get("extra", {}) if run.health else {}
    out.append("- events: " + (", ".join(f"{k} {v}" for k, v in sorted(events.items())) or "none of " + ", ".join(kinds)))
    out.append(f"- tick crashes (last heartbeat): {last.get('tick_crashes', 'n/a')}, task crashes: {last.get('task_crashes', 'n/a')}")
    api = max((int(h.get("api_errors_1m") or 0) for h in run.health), default=0)
    out.append(f"- max API errors in one minute: {api}")
    order_errors = sum(1 for r in run.orders if r.get("action") == "error")
    out.append(f"- order errors (orders log): {order_errors}")
    for e in [e for e in run.events if e.get("kind") == "tick_error"][:5]:
        out.append(f"  - {e.get('ts')}: {e.get('reason')}")
    return out


def _section_logs_check(run: Run) -> list[str]:
    report = check_logs(run.log_dir)
    lines = sum(f["lines"] for f in report.files.values())
    head = f"logs check: {'OK' if report.ok else 'FAILED'} ({len(report.files)} files, {lines} lines)"
    out = ["## Logs check", "", head]
    if not report.ok:
        out += ["", "```", report.summary(), "```"]
    return out


def render(run: Run) -> str:
    start, end = run.window()
    day = datetime.fromtimestamp(end, tz=UTC).strftime("%Y-%m-%d") if end else "n/a"
    m, source = _metrics(run)
    parts: list[list[str]] = [
        [f"# Shadow report — {day}", ""],
        [SYNTHETIC_BANNER, ""] if run.synthetic else [],
        [
            f"Generated by `scripts/shadow_report.py` from `{run.log_dir.name}/`"
            + (f" and the state DB `{run.state_db.name}`" if run.state_db is not None else "")
            + ". Every number below comes from those files.",
            "",
        ],
        _section_window(run),
        _section_feed(run),
        _section_heartbeats(run),
        _section_equity(run, m, source),
        _section_decisions(run),
        _section_vetoes(run),
        _section_fills(run, m),
        _section_fees(run),
        _section_s1(run),
        _section_s2(run),
        _section_errors(run),
        _section_logs_check(run),
    ]
    lines: list[str] = []
    for part in parts:
        if part:
            lines += part
            if part[-1] != "":
                lines.append("")
    return "\n".join(lines).rstrip() + "\n"


# ---------------------------------------------------------------- entry point


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="shadow_report.py", description="Markdown report of a shadow run.", allow_abbrev=False)
    p.add_argument("--log-dir", required=True, help="the run's JSONL log directory")
    p.add_argument("--state-db", default=None, help="the run's engine state DB (equity table), optional")
    p.add_argument("--out", default=None, help="output file (default docs/REPORTS/shadow_<YYYYMMDD>.md)")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    log_dir = Path(args.log_dir)
    if not log_dir.is_dir():
        print(f"error: {log_dir} is not a directory", file=sys.stderr)
        return EXIT_BAD_ARGS
    state_db = Path(args.state_db) if args.state_db else None
    if state_db is not None and not state_db.is_file():
        print(f"error: no state DB at {state_db}", file=sys.stderr)
        return EXIT_BAD_ARGS
    run = Run(log_dir, state_db)
    if run.empty:
        print(f"error: no events, heartbeats or decisions in {log_dir}", file=sys.stderr)
        return EXIT_NO_DATA
    text = render(run)
    if args.out:
        out = Path(args.out)
    else:
        end = run.window()[1]
        day = datetime.fromtimestamp(end, tz=UTC).strftime("%Y%m%d") if end else "undated"
        out = Path("docs/REPORTS") / f"shadow_{day}.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")
    print(f"wrote {out}")
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())

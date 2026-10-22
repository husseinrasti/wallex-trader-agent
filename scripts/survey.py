"""Market survey over the recorded order books (PRD-05 §2; ``make survey``).

    python scripts/survey.py --since 24h
    python scripts/survey.py --since 6h --db data/wta.sqlite --out docs/REPORTS --png
    python scripts/survey.py --since 12 --source ws,rest --json     # hours; only recorded Wallex books

Reads the recorder's SQLite store (read-only, never the network) for the last N hours ending at the
newest recorded order book (or ``--until``), prints a short summary and writes
``<out>/survey_<YYYYMMDD>.md`` (+ ``_edges.png`` with ``--png``, ``.json`` with ``--json``).
The computation lives in ``wta.research.survey``; the report header names the data source, the
window, the snapshot count, the git commit, and carries a SYNTHETIC warning when the books come
from the offline demo feed.

Exit codes: 0 = report written (also when there is no recorded data in the window: the report then
says so), 1 = the database could not be read, 2 = bad arguments.
"""

from __future__ import annotations

import argparse
import logging
import sqlite3
import sys
import time
from pathlib import Path

from wta.config import load_config
from wta.research.survey import (
    SurveyParams,
    open_store,
    parse_duration,
    run_survey,
    summary_text,
    write_report,
)
from wta.utils.timeutil import parse_iso


def _duration(text: str) -> float:
    try:
        return parse_duration(text)
    except ValueError as e:
        raise argparse.ArgumentTypeError(str(e)) from None


def _until(text: str) -> float | str:
    if text.strip().lower() == "now":
        return "now"  # resolved in main() from the wall clock
    try:
        ts = parse_iso(text)
    except ValueError:
        ts = None
    if ts is None:
        raise argparse.ArgumentTypeError(f"not an ISO-8601 time: {text!r} (e.g. 2026-10-22T10:00Z or now)")
    return ts


def _sources(text: str) -> tuple[str, ...]:
    items = tuple(sorted({s.strip() for s in text.split(",") if s.strip()}))
    if not items:
        raise argparse.ArgumentTypeError("--source needs at least one source, e.g. ws,rest")
    return items


def _positive(text: str) -> float:
    try:
        value = float(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a number: {text!r}") from None
    if not value > 0:
        raise argparse.ArgumentTypeError(f"must be > 0: {text!r}")
    return value


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Survey the recorded Wallex order books (PRD-05 §2).")
    p.add_argument("--since", type=_duration, default=parse_duration("24h"), metavar="DURATION",
                   help="window length: 24h, 6h, <N>h, 90m, 2d or a bare number of hours (default 24h)")
    p.add_argument("--until", type=_until, default=None, metavar="ISO|now",
                   help="window end (default: the newest recorded order book)")
    p.add_argument("--db", type=Path, default=None, help="SQLite store (default: data.sqlite_path, data/wta.sqlite)")
    p.add_argument("--out", type=Path, default=Path("docs/REPORTS"), help="report directory (default docs/REPORTS)")
    p.add_argument("--png", action="store_true", help="also write the cross-book edge chart (PNG)")
    p.add_argument("--json", action="store_true", help="also write every number as JSON")
    p.add_argument("--source", type=_sources, default=None, metavar="LIST",
                   help="only books with these sources, e.g. ws,rest (default: all)")
    p.add_argument("--grid", type=_positive, default=2.0, metavar="S", help="cross-book grid step in s (default 2)")
    p.add_argument("--set", action="append", default=[], metavar="KEY=VALUE",
                   help="config override, e.g. strategies.s1_crossbook.coins=[BTC,ETH]")
    p.add_argument("--log-level", default="WARNING")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(level=args.log_level.upper(), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = load_config(args.set)
    db = args.db if args.db is not None else Path(cfg.data.sqlite_path)
    until = time.time() if args.until == "now" else args.until
    params = SurveyParams(since_s=args.since, until=until, sources=args.source, grid_s=args.grid)
    try:
        store, note = open_store(db)
    except sqlite3.Error as e:
        print(f"survey: cannot read {db}: {e}", file=sys.stderr)
        return 1
    try:
        result = run_survey(store, cfg, params, db_path=str(db), store_note=note)
    except sqlite3.Error as e:
        print(f"survey: cannot read {db}: {e}", file=sys.stderr)
        return 1
    finally:
        if store is not None:
            store.close()
    print(summary_text(result))
    for path in write_report(result, args.out, png=args.png, json_out=args.json):
        print(f"wrote {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

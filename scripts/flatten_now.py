"""Flatten the account to 100 % TMN now (PRD-02 §7; ``make flatten``).

    python scripts/flatten_now.py                                   # paper account of the store
    python scripts/flatten_now.py --feed synthetic --paper-balances '{"TMN": 1000000, "USDT": 5}'
    WTA_LIVE=1 python scripts/flatten_now.py --i-understand-live     # the real account

It runs the engine's own flatten routine (``FlattenManager`` -> ``RiskManager`` ->
``OrderManager``), with no strategy:

* phase 1 for ``--phase1-seconds``: maker-first LIMIT sells, re-priced up to
  ``execution.flatten_max_reprices`` times, then crossing the spread;
* phase 2 afterwards: MARKET sells where the market allows them, else a LIMIT crossing the bid;
* coins go to their TMN market (or USDT, then USDT -> TMN); balances below the market's minimum
  order are dust and are left alone. Nothing is ever bought.

It stops when every non-TMN balance is dust (exit 0) or after ``--timeout`` seconds (exit 1, the
leftovers are listed). Before and after it prints the balances and the equity in TMN.

Paper (default): the account is the store's kv ``paper_state`` (what the shadow engine left), or
``--paper-balances`` on a store without one; books come from ``--feed live`` (Wallex REST/WS) or
``--feed synthetic`` (offline exercise only). Live: only with ``--i-understand-live`` *and*
``WTA_LIVE=1`` in the environment, an API key and a clean git tree (``wta.safety``).

Two processes must never trade the same account (paper or real): the engine's account lock
(``<sqlite_path>.engine.lock``) refuses to run while ``wta run`` (or another flatten_now) holds it —
stop the engine first (exit 2 otherwise). Live flattening does not need ``CHALLENGE_END_UTC``.
Every order has a decision record in ``--log-dir`` like any engine order.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from decimal import Decimal
from typing import Any

from wta.config import Config, Settings, load_config, load_settings
from wta.engine import EngineLockError, build_engine, parse_paper_balances
from wta.safety import LiveGuardError, live_guard_reason

EXIT_FLAT, EXIT_NOT_FLAT, EXIT_CANNOT_RUN = 0, 1, 2


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="flatten_now.py", description="Sell every non-TMN balance to TMN now.", allow_abbrev=False
    )
    p.add_argument(
        "--i-understand-live", action="store_true", help="flatten the REAL account (needs WTA_LIVE=1)"
    )
    p.add_argument("--allow-dirty", action="store_true", help="live on a dirty git tree (dev only)")
    p.add_argument("--feed", choices=("live", "synthetic"), default="live", help="book source (paper only)")
    p.add_argument("--paper-balances", metavar="JSON", help="paper account for a store without one")
    p.add_argument("--phase1-seconds", type=float, default=60.0, help="maker-first phase length (default 60)")
    p.add_argument(
        "--timeout", type=float, default=600.0, help="give up after this many seconds (default 600)"
    )
    p.add_argument("--log-dir", default="logs", help="JSONL log directory (default logs)")
    p.add_argument("--set", action="append", default=[], metavar="KEY=VALUE", help="config override")
    p.add_argument("--log-level", default="WARNING", help="python log level (default WARNING)")
    return p


def _err(message: str) -> None:
    print(f"error: {message}", file=sys.stderr)


def _print_report(title: str, report: dict[str, Any] | None) -> None:
    print(f"{title}:")
    if report is None:
        print("  (no fully priced snapshot: some held asset had no book)")
        return
    for asset, total in report["balances"].items():
        print(f"  {asset:<6} {total}")
    equity = report["equity_tmn"]
    tmn = report["tmn"]
    share = f"{Decimal(tmn) / Decimal(equity) * 100:.4f} %" if equity and Decimal(equity) > 0 else "n/a"
    print(f"  equity {equity} TMN, of which TMN {tmn} ({share})")
    if report["missing_prices"]:
        print(f"  no price for: {', '.join(report['missing_prices'])}")


async def flatten_now(
    cfg: Config,
    settings: Settings,
    *,
    live: bool,
    feed: str,
    paper_balances: dict[str, Decimal] | None,
    phase1_s: float,
    timeout_s: float,
    log_dir: str,
    allow_dirty: bool = False,
) -> int:
    """Run the engine in manual-flatten mode until flat or ``timeout_s``; print before/after."""
    engine = await build_engine(
        cfg,
        settings,
        "live" if live else "shadow",
        feed=feed,
        log_dir=log_dir,
        paper_balances=paper_balances,
        i_understand_live=live,
        allow_dirty=allow_dirty,
        require_deadline=False,  # flattening sells everything now: no deadline schedule involved
    )
    engine.enable_manual_flatten(phase1_s)
    try:
        reason = await engine.run(seconds=timeout_s)
    finally:
        await engine.aclose()
    mode = "LIVE account" if live else f"paper account (feed {feed})"
    print(f"flatten_now: {mode}; stopped: {reason}")
    _print_report("before", engine.flatten_report["before"])
    _print_report("after", engine.flatten_report["after"])
    left = [s.as_dict() for s in engine.flatten.last_scan if s.state != "dust"]
    dust = [s.as_dict() for s in engine.flatten.last_scan if s.state == "dust"]
    for item in dust:
        print(f"  dust left as is: {item['total']} {item['asset']} ({item['detail']})")
    if reason == "flatten complete":
        print("result: 100 % TMN (dust excepted)")
        return EXIT_FLAT
    for item in left:
        print(f"  NOT FLAT: {item['total']} {item['asset']}: {item['state']} ({item['detail']})")
    print("result: not flat before the timeout; run it again or check the books/API")
    return EXIT_NOT_FLAT


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.WARNING),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        cfg = load_config(args.set)
        settings = load_settings()
    except (ValueError, OSError) as exc:
        _err(f"invalid configuration: {exc}")
        return EXIT_CANNOT_RUN
    live = bool(args.i_understand_live)
    if live:
        reason = live_guard_reason(True, args.allow_dirty, settings)
        if reason is not None:
            _err(f"refusing to flatten the live account: {reason}")
            return EXIT_CANNOT_RUN
        if args.feed != "live" or args.paper_balances:
            _err("the live account is flattened on the live feed only, without --paper-balances")
            return EXIT_CANNOT_RUN
    balances = None
    if args.paper_balances:
        try:
            balances = parse_paper_balances(args.paper_balances)
        except (OSError, ValueError) as exc:
            _err(f"--paper-balances: {exc}")
            return EXIT_CANNOT_RUN
    try:
        return asyncio.run(
            flatten_now(
                cfg,
                settings,
                live=live,
                feed=args.feed,
                paper_balances=balances,
                phase1_s=args.phase1_seconds,
                timeout_s=args.timeout,
                log_dir=args.log_dir,
                allow_dirty=args.allow_dirty,
            )
        )
    except (LiveGuardError, EngineLockError) as exc:
        _err(str(exc))
        return EXIT_CANNOT_RUN
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())

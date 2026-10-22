"""Command line interface: ``python -m wta <command>`` or ``wta <command>`` (PRD-01 §9).

Commands::

    config show|hash          resolved config (secrets masked) and its config_hash
    markets                   watchlist market rules from /v1/markets; flags banned symbols
    feed SYMBOL               best bid/ask once per second from the WebSocket or REST, labelled
    record                    market recorder: books, trades and candles into SQLite
    backfill                  historical candles from /v1/udf/history into SQLite
    account                   balances and fees (needs WALLEX_API_KEY)
    run --mode shadow|live    the engine; live needs --i-understand-live, WTA_LIVE=1 and a clean tree
        --feed live|synthetic     market data: Wallex REST/WS (default) or the offline synthetic feed
        --inject-intents FILE     JSONL intents sent through risk -> orders (shadow acceptance tests)
        --paper-balances JSON     starting paper account, e.g. '{"TMN": 3000000, "USDT": 10}'
        --synthetic-shock-at S --synthetic-shock-pct P    one-off synthetic crash (P = -0.08 is -8 %)
    reset-kill --reason TEXT  clear the kill-switch by hand (writes a reset_kill event)
    logs check                validate logs/*.jsonl against their schemas
    db vacuum                 compact the SQLite database
    backtest, replay, report, tui, status, export     later PRDs (exit 3)

Options such as ``--set key=value`` go after the command: ``wta run --mode shadow --set a.b=1``.

Exit codes: 0 ok · 1 a check failed (banned watchlist symbol, invalid logs, API refused)
· 2 cannot run (API unreachable, live guard, bad arguments) · 3 not implemented yet.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sqlite3
import sys
import time
from collections.abc import Callable
from contextlib import closing
from decimal import Decimal
from pathlib import Path
from typing import Any

import yaml
from pydantic import SecretStr, ValidationError
from rich import box
from rich.console import Console
from rich.table import Table

from wta import engine as engine_mod
from wta.config import Config, Settings, load_config, load_settings, mask_secrets
from wta.data.collector import RestPoller, default_ws_factory, recordable_symbols, run_recorder
from wta.data.store import Store
from wta.engine import (
    EngineLockError,
    load_injected_intents,
    parse_paper_balances,
    request_kill_reset,
    run_engine,
)
from wta.exchange.base import MarketDataBus, WallexAPIError
from wta.exchange.wallex_rest import WallexREST
from wta.logging_.checker import check_logs, scan_for_secrets
from wta.logging_.decision_log import EventsLog
from wta.models import BookSnapshot, MarketInfo
from wta.safety import LiveGuardError, deadline_guard_reason, live_guard_reason
from wta.utils.timeutil import SystemClock, iso

EXIT_OK, EXIT_FAILED, EXIT_CANNOT_RUN, EXIT_NOT_IMPLEMENTED = 0, 1, 2, 3

# command -> (PRD that delivers it, help text)
PLACEHOLDERS: dict[str, tuple[str, str]] = {
    "backtest": ("PRD-05", "backtest over recorded/historical data"),
    "replay": ("PRD-05", "re-run recorded inputs and cached LLM answers"),
    "report": ("PRD-05", "backtest and shadow reports"),
    "tui": ("PRD-06", "terminal dashboard"),
    "status": ("PRD-06", "one-screen summary of the latest heartbeat"),
    "export": ("PRD-06", "copy logs and reports into deliverables/"),
}

Handler = Callable[[argparse.Namespace, Config, Settings], int]


# ---------------------------------------------------------------- helpers


def _console() -> Console:
    """Rich console; a fixed width when piped so tables are not squeezed into 80 columns."""
    return Console(width=None if sys.stdout.isatty() else 160)


def _num(value: Decimal | None) -> str:
    """Plain decimal text without exponent or trailing zeros (``1E-6`` -> ``0.000001``)."""
    if value is None:
        return "-"
    return format(value.normalize(), "f") if value else "0"


def _one_line(exc: BaseException) -> str:
    text = getattr(exc, "message", None) or str(exc) or type(exc).__name__
    return str(text).splitlines()[0][:300]


def _err(message: str) -> None:
    print(f"error: {message}", file=sys.stderr)


def _rest(cfg: Config, settings: Settings, timeout: float | None = None, retries: int | None = None) -> WallexREST:
    """A Wallex REST client. One-shot commands pass ``retries=0`` so an outage fails fast."""
    return WallexREST(
        settings.wallex_base_url,
        settings.wallex_api_key.get_secret_value(),
        rps=cfg.data.rate_limit_rps,
        timeout=timeout or cfg.data.http_timeout_s,
        max_retries=cfg.data.max_retries if retries is None else retries,
        sub_account_client_id=settings.wallex_sub_account_client_id,
    )


def _symbols_arg(text: str | None) -> list[str] | None:
    if not text:
        return None
    return [s.strip().upper() for s in text.split(",") if s.strip()]


def _watchlist_file(path: Path) -> list[str]:
    """The ``watchlist:`` list of a markets YAML file (e.g. ``configs/markets.yaml``)."""
    data = yaml.safe_load(path.read_text()) or {}
    return [str(s).upper() for s in data.get("watchlist") or []]


def _secret_fields(settings: Settings) -> dict[str, str]:
    """``{field name: value}`` of every secret setting (for the secrets scan)."""
    out: dict[str, str] = {}
    for name in type(settings).model_fields:
        value = getattr(settings, name)
        if isinstance(value, SecretStr):
            out[name] = value.get_secret_value()
    return out


def _install_stop_signals(stop: asyncio.Event) -> Callable[[], None]:
    """SIGINT/SIGTERM set ``stop``; returns a function that removes the handlers."""
    loop = asyncio.get_running_loop()
    installed = []
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
            installed.append(sig)
        except (NotImplementedError, RuntimeError, ValueError):
            pass

    def remove() -> None:
        for sig in installed:
            loop.remove_signal_handler(sig)

    return remove


# ---------------------------------------------------------------- config


def cmd_config_show(args: argparse.Namespace, cfg: Config, settings: Settings) -> int:
    tree = cfg.model_dump(mode="json")
    if not args.full:
        n = len(tree["markets"]["banned"])
        tree["markets"]["banned"] = f"<{n} symbols; --full lists them>"
    text = "# resolved config (configs/*.yaml + --set overrides)\n"
    text += yaml.safe_dump(tree, sort_keys=False, allow_unicode=True)
    text += "\n# settings (environment / .env; secrets masked)\n"
    text += yaml.safe_dump({"settings": settings.masked()}, sort_keys=False, allow_unicode=True)
    text += f"\nconfig_hash: {cfg.config_hash}\n"
    print(mask_secrets(text, settings.secret_values()), end="")
    return EXIT_OK


def cmd_config_hash(args: argparse.Namespace, cfg: Config, settings: Settings) -> int:
    print(cfg.config_hash)
    return EXIT_OK


# ---------------------------------------------------------------- markets / account


async def _fetch_markets(cfg: Config, settings: Settings, timeout: float | None) -> dict[str, MarketInfo]:
    async with _rest(cfg, settings, timeout=timeout, retries=0) as rest:
        return await rest.get_markets()


def cmd_markets(args: argparse.Namespace, cfg: Config, settings: Settings) -> int:
    watchlist = list(dict.fromkeys(s.upper() for s in cfg.markets.watchlist))
    banned = [s for s in watchlist if cfg.markets.is_banned(s)]
    try:
        markets = asyncio.run(_fetch_markets(cfg, settings, args.timeout))
    except WallexAPIError as exc:
        _err(f"cannot fetch markets from {settings.wallex_base_url}: {_one_line(exc)}")
        if banned:  # a config error we can report without the API
            _err(f"BANNED symbols in the watchlist: {', '.join(banned)}")
            return EXIT_FAILED
        return EXIT_CANNOT_RUN

    table = Table(title=f"Watchlist markets ({len(markets)} markets on the exchange)", box=box.SIMPLE)
    for col in ("symbol", "base", "quote", "tick dec", "step dec", "minQty", "minNotional", "market orders", "banned"):
        table.add_column(col, no_wrap=True)
    missing = []
    for sym in watchlist:
        is_banned = "BANNED" if sym in banned else "no"
        m = markets.get(sym)
        if m is None:
            missing.append(sym)
            table.add_row(sym, "not listed", "-", "-", "-", "-", "-", "-", is_banned)
            continue
        table.add_row(
            sym,
            m.base,
            m.quote,
            str(m.tick_decimals),
            str(m.step_decimals),
            _num(m.min_qty),
            _num(m.min_notional),
            "yes" if m.market_orders_enabled else "NO",
            is_banned,
        )
    console = _console()
    console.print(table)
    if missing:
        console.print(f"warning: not listed on the exchange: {', '.join(missing)}")
    if banned:
        console.print(f"BANNED symbols in the watchlist (remove them from configs/markets.yaml): {', '.join(banned)}")
        return EXIT_FAILED
    return EXIT_OK


async def _fetch_account(cfg: Config, settings: Settings, timeout: float | None) -> tuple[Any, Any]:
    async with _rest(cfg, settings, timeout=timeout, retries=0) as rest:
        balances = await rest.get_balances()
        fees = await rest.get_fees()
    return balances, fees


def cmd_account(args: argparse.Namespace, cfg: Config, settings: Settings) -> int:
    if not settings.wallex_api_key.get_secret_value():
        _err("WALLEX_API_KEY is not set (add it to .env)")
        return EXIT_CANNOT_RUN
    try:
        balances, fees = asyncio.run(_fetch_account(cfg, settings, args.timeout))
    except WallexAPIError as exc:
        _err(f"account request failed: {mask_secrets(_one_line(exc), settings.secret_values())}")
        return EXIT_CANNOT_RUN if exc.retryable else EXIT_FAILED

    console = _console()
    sub = settings.wallex_sub_account_client_id
    console.print(f"API key: set · sub-account-client-id: {'set' if sub else 'not set'}")
    table = Table(title="Balances", box=box.SIMPLE)
    for col in ("asset", "free", "locked", "total"):
        table.add_column(col, no_wrap=True, justify="left" if col == "asset" else "right")
    shown = [b for b in balances.values() if b.total or args.all]
    for b in shown:
        table.add_row(b.asset, _num(b.free), _num(b.locked), _num(b.total))
    console.print(table)
    if len(shown) < len(balances):
        console.print(f"({len(balances) - len(shown)} zero balances hidden; --all shows them)")
    table = Table(title="Fees (watchlist)", box=box.SIMPLE)
    for col in ("symbol", "maker", "taker"):
        table.add_column(col, no_wrap=True)
    for sym in dict.fromkeys(cfg.markets.watchlist):
        if sym in fees:
            maker, taker = fees[sym]
            table.add_row(sym, f"{maker * 100:.3f}%", f"{taker * 100:.3f}%")
        else:
            table.add_row(sym, "-", "-")
    console.print(table)
    return EXIT_OK


# ---------------------------------------------------------------- feed / record / backfill


def _feed_line(now: float, book: BookSnapshot | None, rest: WallexREST) -> str:
    if book is None:
        last = f", last: {rest.last_error[:120]}" if rest.last_error else ""
        return f"{iso(now)} -    waiting for data (REST errors: {rest.errors_total}{last})"
    spread = book.spread_pct
    spread_text = "-" if spread is None else f"{spread * 100:.4f}%"
    return (
        f"{iso(now)} {book.source:<4} {_num(book.best_bid)} {_num(book.best_ask)} {spread_text}"
        f" (age {book.age(now):.1f}s)"
    )


async def _feed(
    cfg: Config, settings: Settings, symbol: str, seconds: float | None, no_ws: bool, interval: float
) -> int:
    from wta.exchange.wallex_ws import WallexWS

    clock = SystemClock()
    bus = MarketDataBus()
    stop = asyncio.Event()
    rest = _rest(cfg, settings)
    poller = RestPoller(
        rest, bus, [symbol], interval_s=cfg.data.rest_poll_interval_s, use_depth_all=False, clock=clock
    )
    tasks = [asyncio.create_task(poller.run(), name="feed-rest")]
    use_ws = cfg.data.ws_enabled and not no_ws
    if use_ws:
        ws = WallexWS(
            settings.wallex_ws_url,
            [symbol],
            bus,
            clock,
            levels=cfg.data.depth_levels_stored,
            reconnect_max_s=cfg.data.ws_reconnect_max_s,
            stale_s=cfg.engine.stale_data_s,
        )
        tasks.append(asyncio.create_task(ws.run(stop), name="feed-ws"))
    remove_handlers = _install_stop_signals(stop)
    print(f"# {symbol}: sources rest{' + ws' if use_ws else ''}; Ctrl-C stops", flush=True)
    print("# ts source bid ask spread%", flush=True)
    got_data = False
    lines = None if seconds is None else max(1, round(seconds / interval))
    try:
        while not stop.is_set() and (lines is None or lines > 0):
            try:
                await asyncio.wait_for(stop.wait(), timeout=interval)
                break
            except TimeoutError:
                pass
            book = bus.latest(symbol)
            got_data = got_data or book is not None
            print(_feed_line(clock.now(), book, rest), flush=True)
            if lines is not None:
                lines -= 1
    finally:
        remove_handlers()
        stop.set()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await rest.close()
    if not got_data:
        _err(f"no data received for {symbol} (API unreachable or unknown symbol?)")
        return EXIT_CANNOT_RUN
    return EXIT_OK


def cmd_feed(args: argparse.Namespace, cfg: Config, settings: Settings) -> int:
    return asyncio.run(_feed(cfg, settings, args.symbol.upper(), args.seconds, args.no_ws, args.interval))


def cmd_record(args: argparse.Namespace, cfg: Config, settings: Settings) -> int:
    symbols = _symbols_arg(args.symbols)
    if symbols is None and args.symbols_from:
        try:
            symbols = _watchlist_file(Path(args.symbols_from))
        except OSError as exc:
            _err(f"cannot read {args.symbols_from}: {exc}")
            return EXIT_CANNOT_RUN
    try:
        symbols = recordable_symbols(cfg, symbols)  # upper-cased, de-duplicated, banned removed
    except ValueError as exc:
        _err(str(exc))
        return EXIT_CANNOT_RUN
    ws_factory = None if args.no_ws or not cfg.data.ws_enabled else default_ws_factory(cfg, settings)

    async def record() -> dict[str, int]:
        stop = asyncio.Event()
        if args.seconds:
            asyncio.get_running_loop().call_later(args.seconds, stop.set)
        return await run_recorder(cfg, settings, symbols=symbols, ws_factory=ws_factory, stop_event=stop)

    written = asyncio.run(record())
    print("rows written: " + ", ".join(f"{k}={v}" for k, v in written.items()))
    return EXIT_OK


async def _backfill(cfg: Config, settings: Settings, symbols: list[str], resolution: str, days: float) -> int:
    from wta.data.candles import backfill  # imported here: it pulls in pandas

    now = time.time()
    try:  # one quick request first, so an outage fails in seconds instead of retrying every window
        async with _rest(cfg, settings, retries=0) as probe:
            await probe.get_candles(symbols[0], resolution, now - 3600, now)
    except WallexAPIError as exc:
        _err(f"cannot fetch candles from {settings.wallex_base_url}: {_one_line(exc)}")
        return EXIT_CANNOT_RUN
    rest = _rest(cfg, settings)
    store = Store(cfg.data.sqlite_path, depth_levels=cfg.data.depth_levels_stored)
    try:
        totals = await backfill(rest, store, symbols, resolution=resolution, days=days)
    finally:
        await rest.close()
        store.close()
    table = Table(title=f"Backfill: resolution {resolution}, {days:g} days -> {cfg.data.sqlite_path}", box=box.SIMPLE)
    table.add_column("symbol")
    table.add_column("candles upserted", justify="right")
    for sym, n in totals.items():
        table.add_row(sym, str(n))
    _console().print(table)
    return EXIT_OK


def cmd_backfill(args: argparse.Namespace, cfg: Config, settings: Settings) -> int:
    try:
        symbols = recordable_symbols(cfg, _symbols_arg(args.symbols))
    except ValueError as exc:
        _err(str(exc))
        return EXIT_CANNOT_RUN
    return asyncio.run(_backfill(cfg, settings, symbols, args.resolution, args.days))


# ---------------------------------------------------------------- run


def _run_options(args: argparse.Namespace) -> dict[str, Any] | str:
    """The Phase-2 ``run`` options as ``run_engine`` keyword arguments, or an error message.

    Only options that differ from their defaults are returned, so a plain ``wta run`` calls the
    engine exactly as before.
    """
    shock = (args.synthetic_shock_at, args.synthetic_shock_pct)
    if args.mode == "live":
        if args.feed != "live":
            return "live mode trades on the live feed only (drop --feed synthetic)"
        if args.inject_intents or args.paper_balances:
            return "--inject-intents and --paper-balances are for shadow mode only"
    if any(v is not None for v in shock):
        if None in shock:
            return "give both --synthetic-shock-at and --synthetic-shock-pct"
        if args.feed != "synthetic":
            return "a synthetic shock needs --feed synthetic"
        if not -1 < args.synthetic_shock_pct < 1:
            return "--synthetic-shock-pct is a fraction between -1 and 1 (e.g. -0.08 for -8 %)"
    options: dict[str, Any] = {}
    if args.feed != "live":
        options["feed"] = args.feed
    if args.inject_intents:
        try:
            options["injected"] = load_injected_intents(args.inject_intents)
        except (OSError, ValueError) as exc:
            return f"--inject-intents {args.inject_intents}: {exc}"
    if args.paper_balances:
        try:
            options["paper_balances"] = parse_paper_balances(args.paper_balances)
        except (OSError, ValueError) as exc:
            return f"--paper-balances: {exc}"
    if shock[0] is not None:
        options["synthetic_shock"] = (float(shock[0]), float(shock[1]))
    return options


def cmd_run(args: argparse.Namespace, cfg: Config, settings: Settings) -> int:
    """The guards (``wta.safety``) are checked here for a friendly message, and again by the engine."""
    if args.mode == "live":
        reason = live_guard_reason(args.i_understand_live, args.allow_dirty, settings)
        if reason is None:
            reason = deadline_guard_reason(cfg.engine.challenge_end_utc, SystemClock().now())
        if reason is not None:
            _err(f"refusing to start live mode: {reason}")
            return EXIT_CANNOT_RUN
    options = _run_options(args)
    if isinstance(options, str):
        _err(options)
        return EXIT_CANNOT_RUN
    try:
        return asyncio.run(
            run_engine(
                cfg,
                settings,
                args.mode,
                seconds=args.seconds,
                log_dir=args.log_dir,
                i_understand_live=args.i_understand_live,
                allow_dirty=args.allow_dirty,
                **options,
            )
        )
    except (LiveGuardError, EngineLockError) as exc:
        _err(str(exc))
        return EXIT_CANNOT_RUN


def cmd_reset_kill(args: argparse.Namespace, cfg: Config, settings: Settings) -> int:
    """Manual kill-switch reset (PRD-02 §6). A running engine picks it up on its next tick."""
    reason = args.reason.strip()
    if not reason:
        _err("--reason must say why the kill-switch is reset (it is logged for the judges)")
        return EXIT_CANNOT_RUN
    kill_file = engine_mod.KILL_FILE
    if kill_file.exists():
        _err(f"the kill file {kill_file} is present: remove it first, or the engine kills again at once")
        return EXIT_FAILED
    path = Path(cfg.data.sqlite_path)
    if not path.exists():
        _err(f"no database at {path} (nothing was ever killed there)")
        return EXIT_FAILED
    clock = SystemClock()
    events = EventsLog(Path(args.log_dir), clock, settings.secret_values())
    store = Store(path)
    try:
        record = request_kill_reset(store, events, reason, clock.now())
    finally:
        store.close()
        events.close()
    if record is None:
        print("kill-switch is not active (engine_flags.killed is false): nothing to reset")
        return EXIT_OK
    peak = record["peak_equity_tmn"] or "unchanged (no equity row yet)"
    print(f"kill-switch reset: {reason}")
    print(f"  previous kill reason: {record['previous_kill_reason'] or '-'}")
    print(f"  peak equity moved to the latest equity: {peak}")
    print(f"  reset_kill event written to {Path(args.log_dir) / 'events.jsonl'} and {path}")
    print("  a running engine applies it on its next tick; otherwise the next start does")
    return EXIT_OK


# ---------------------------------------------------------------- logs / db / placeholders


def cmd_logs_check(args: argparse.Namespace, cfg: Config, settings: Settings) -> int:
    log_dir = Path(args.dir)
    report = check_logs(log_dir)
    print(report.summary())
    ok = report.ok
    if args.secrets:
        paths = [log_dir, Path(args.deliverables), Path(args.reports)]
        findings = scan_for_secrets(paths, _secret_fields(settings))
        if findings:
            print(f"secrets scan: {len(findings)} finding(s)")
            for line in findings:
                print(f"  {line}")
            ok = False
        else:
            print("secrets scan: clean")
    return EXIT_OK if ok else EXIT_FAILED


def cmd_db_vacuum(args: argparse.Namespace, cfg: Config, settings: Settings) -> int:
    path = Path(args.path or cfg.data.sqlite_path)
    if not path.exists():
        _err(f"no database at {path}")
        return EXIT_FAILED
    before = path.stat().st_size
    try:
        with closing(sqlite3.connect(path, timeout=10)) as conn:
            conn.execute("VACUUM")
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    except sqlite3.Error as exc:
        _err(f"vacuum failed (is a writer busy?): {exc}")
        return EXIT_FAILED
    after = path.stat().st_size
    print(f"vacuumed {path}: {before / 1e6:.2f} MB -> {after / 1e6:.2f} MB")
    return EXIT_OK


def _placeholder(name: str) -> Handler:
    prd = PLACEHOLDERS[name][0]

    def handler(args: argparse.Namespace, cfg: Config, settings: Settings) -> int:
        print(f"wta {name}: not implemented yet ({prd})")
        return EXIT_NOT_IMPLEMENTED

    return handler


# ---------------------------------------------------------------- parser


def build_parser() -> argparse.ArgumentParser:
    # allow_abbrev=False everywhere: an acknowledgement such as --i-understand-live must be typed in
    # full, never matched from a prefix like --i.
    common = argparse.ArgumentParser(add_help=False, allow_abbrev=False)
    common.add_argument(
        "--set",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="config override, repeatable (e.g. --set risk.max_order_pct_equity=0.2)",
    )
    common.add_argument("--log-level", default="INFO", help="DEBUG, INFO, WARNING, ... (default INFO)")

    parser = argparse.ArgumentParser(prog="wta", description="Wallex Trader Agent", allow_abbrev=False)
    sub = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")

    def add(name: str, handler: Handler | None, help_text: str) -> argparse.ArgumentParser:
        """A command; group commands (handler None) get their options on their sub-commands."""
        parents = [common] if handler is not None else []
        p = sub.add_parser(name, parents=parents, help=help_text, description=help_text, allow_abbrev=False)
        if handler is not None:
            p.set_defaults(handler=handler)
        return p

    p = add("config", None, "show the resolved config or its hash")
    config_sub = p.add_subparsers(dest="config_command", required=True, metavar="show|hash")
    show = config_sub.add_parser(
        "show", parents=[common], help="resolved config, masked settings and hash", allow_abbrev=False
    )
    show.add_argument("--full", action="store_true", help="also list every banned symbol")
    show.set_defaults(handler=cmd_config_show)
    config_hash = config_sub.add_parser("hash", parents=[common], help="config_hash only", allow_abbrev=False)
    config_hash.set_defaults(handler=cmd_config_hash)

    p = add("markets", cmd_markets, "watchlist market rules from /v1/markets; exit 1 if a symbol is banned")
    p.add_argument("--timeout", type=float, default=None, help="HTTP timeout in seconds")

    p = add("feed", cmd_feed, "print best bid/ask of SYMBOL once per second (ws or rest)")
    p.add_argument("symbol")
    p.add_argument("--seconds", type=float, default=None, help="stop after this many seconds")
    p.add_argument("--interval", type=float, default=1.0, help="seconds between lines (default 1)")
    p.add_argument("--no-ws", action="store_true", help="REST polling only")

    p = add("record", cmd_record, "record books, trades and candles into SQLite")
    p.add_argument("--symbols-from", help="YAML file with a `watchlist:` list (e.g. configs/markets.yaml)")
    p.add_argument("--symbols", help="comma-separated symbols (overrides --symbols-from)")
    p.add_argument("--no-ws", action="store_true", help="REST polling only")
    p.add_argument("--seconds", type=float, default=None, help="stop after this many seconds")

    p = add("backfill", cmd_backfill, "fetch historical candles into SQLite")
    p.add_argument("--symbols", help="comma-separated symbols (default: the watchlist)")
    p.add_argument("--resolution", default="1", help="UDF resolution: 1, 15, 60, 240, 1D, ... (default 1)")
    p.add_argument("--days", type=float, default=30, help="days of history (default 30)")

    p = add("account", cmd_account, "balances and fee rates (needs WALLEX_API_KEY)")
    p.add_argument("--timeout", type=float, default=None, help="HTTP timeout in seconds")
    p.add_argument("--all", action="store_true", help="also show zero balances")

    p = add("run", cmd_run, "run the engine (shadow by default)")
    p.add_argument("--mode", choices=("shadow", "live"), default="shadow")
    p.add_argument("--i-understand-live", action="store_true", help="required for --mode live")
    p.add_argument("--allow-dirty", action="store_true", help="allow live mode on a dirty git tree (dev only)")
    p.add_argument("--seconds", type=float, default=None, help="stop after this many seconds")
    p.add_argument("--log-dir", default="logs", help="JSONL log directory (default logs)")
    p.add_argument(
        "--feed",
        choices=("live", "synthetic"),
        default="live",
        help="market data: Wallex REST/WS (default) or the offline synthetic feed (never evidence)",
    )
    p.add_argument(
        "--inject-intents", metavar="FILE", help="JSONL intents for acceptance tests (shadow only)"
    )
    p.add_argument(
        "--paper-balances",
        metavar="JSON",
        help='starting paper account (inline JSON or a file), e.g. \'{"TMN": 3000000, "USDT": 10}\'',
    )
    p.add_argument(
        "--synthetic-shock-at", type=float, default=None, metavar="S", help="apply the synthetic shock S s after start"
    )
    p.add_argument(
        "--synthetic-shock-pct",
        type=float,
        default=None,
        metavar="P",
        help="synthetic shock size as a fraction (-0.08 = -8 %%) of USDTTMN and every coin mid",
    )

    p = add("reset-kill", cmd_reset_kill, "clear the kill-switch by hand (writes a reset_kill event)")
    p.add_argument("--reason", required=True, help="why the kill-switch is reset (logged)")
    p.add_argument("--log-dir", default="logs", help="JSONL log directory of the engine (default logs)")

    p = add("logs", None, "log tools")
    logs_sub = p.add_subparsers(dest="logs_command", required=True, metavar="check")
    check = logs_sub.add_parser("check", parents=[common], help="validate the JSONL logs", allow_abbrev=False)
    check.add_argument("--dir", default="logs", help="log directory (default logs)")
    check.add_argument(
        "--secrets", action="store_true", help="also scan logs, deliverables and docs/REPORTS for secrets"
    )
    check.add_argument("--deliverables", default="deliverables", help=argparse.SUPPRESS)
    check.add_argument("--reports", default="docs/REPORTS", help=argparse.SUPPRESS)
    check.set_defaults(handler=cmd_logs_check)

    p = add("db", None, "database tools")
    db_sub = p.add_subparsers(dest="db_command", required=True, metavar="vacuum")
    vacuum = db_sub.add_parser("vacuum", parents=[common], help="compact the SQLite database", allow_abbrev=False)
    vacuum.add_argument("--path", default=None, help="database file (default data.sqlite_path)")
    vacuum.set_defaults(handler=cmd_db_vacuum)

    for name, (prd, help_text) in PLACEHOLDERS.items():
        add(name, _placeholder(name), f"{help_text} ({prd}, not implemented yet)")
    return parser


def _setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    for noisy in ("httpx", "httpcore", "websockets"):
        logging.getLogger(noisy).setLevel(logging.WARNING)  # one line per request otherwise


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    try:
        args, extra = parser.parse_known_args(argv)
    except SystemExit as exc:  # --help, or a usage error argparse already printed
        return exc.code if isinstance(exc.code, int) else EXIT_CANNOT_RUN
    if extra and args.command not in PLACEHOLDERS:  # placeholders accept their future options
        parser.print_usage(sys.stderr)
        _err(f"unrecognized arguments: {' '.join(extra)}")
        return EXIT_CANNOT_RUN
    _setup_logging(args.log_level)
    try:
        cfg = load_config(args.set)
    except (ValueError, OSError, yaml.YAMLError) as exc:  # pydantic's ValidationError is a ValueError
        _err(f"invalid configuration: {_describe_error(exc)}")
        return EXIT_CANNOT_RUN
    try:
        settings = load_settings()
    except ValidationError as exc:
        _err(f"invalid environment / .env: {_describe_error(exc)}")
        return EXIT_CANNOT_RUN
    try:
        return int(args.handler(args, cfg, settings))
    except KeyboardInterrupt:
        return 130


def _describe_error(exc: BaseException) -> str:
    """Field paths and messages only; pydantic's full text would echo input values."""
    if isinstance(exc, ValidationError):
        return "; ".join(f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors()[:5])
    return _one_line(exc)


if __name__ == "__main__":
    raise SystemExit(main())

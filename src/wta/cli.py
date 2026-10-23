"""Command line interface: ``python -m wta <command>`` or ``wta <command>`` (PRD-01 §9).

Commands::

    config show|hash          resolved config (secrets masked) and its config_hash (--overlay too)
    markets                   watchlist market rules from /v1/markets; flags banned symbols
    feed SYMBOL               best bid/ask once per second from the WebSocket or REST, labelled
    record                    market recorder: books, trades and candles into SQLite
    backfill                  historical candles from /v1/udf/history into SQLite
    account                   balances and fees (needs WALLEX_API_KEY)
    run --mode shadow|live    the engine with S1 + S2 (+ S3 when enabled); live needs --i-understand-live,
                              WTA_LIVE=1 and a clean tree. State DB per mode and feed:
                              data.state_sqlite_path (data/state_shadow.sqlite, data/state_live.sqlite,
                              data/state_shadow_synthetic.sqlite), see below. Logs: --log-dir, default
                              logs (live) / logs/shadow (shadow), so a shadow run next to live never
                              writes into the live decision log
        --overlay FILE            YAML overlay merged over configs/*.yaml (repeatable, in order; --set
                                  wins), e.g. configs/live_halfsize.yaml; named in the start event
        --feed live|synthetic     market data: Wallex REST/WS (default) or the offline synthetic feed
        --inject-intents FILE     JSONL intents sent through risk -> orders (shadow acceptance tests)
        --paper-balances JSON     starting paper account, e.g. '{"TMN": 3000000, "USDT": 10}'
        --synthetic-shock-at S --synthetic-shock-pct P    one-off synthetic crash (P = -0.08 is -8 %)
        --no-runtime-overlay      do not re-apply the reviewer's <runtime-dir>/latest.yaml at startup
        --runtime-dir DIR         the reviewer's overlay directory (default configs/runtime/<account>:
                                  live, shadow or shadow_synthetic; one per account, like the state DB)
    reset-kill --reason TEXT  clear the kill-switch by hand (writes a reset_kill event); --mode
                              shadow|live and --feed live|synthetic pick the state DB (default: the
                              one that is killed)
    status [--mode] [--feed] [--log-dir] [--json]   one-screen summary (latest heartbeat, state DB,
                              plan, AI source, processes); read-only
    tui [--mode] [--feed] [--log-dir] [--once]      rich terminal dashboard (read-only process)
    plan [--mode shadow] [--feed live|synthetic]    ONE analyst cycle on the shadow account's data,
                              prints the plan JSON (nothing traded, nothing stored but the LLM cache)
    alert TEXT                one Telegram message (a no-op without TELEGRAM_* and
                              alerts.telegram_enabled)
    logs check                validate logs/*.jsonl against their schemas
    db vacuum                 compact the SQLite database
    backtest                  PRD-05 §3: the engine over stored data (backtest/runner.py)
        --driver candles|books --from ISO --to ISO --ai off|on|offline --strategies S2,S3 --out DIR
        [--db PATH] [--param-grid KEY=V1,V2 (walk-forward 70/30, backtest/walkforward.py)]
        [--ai-max-calls N] [--synthetic-db PATH --synthetic-hours H (SYNTHETIC data, never evidence)]
    report --source LOGDIR [--state-db PATH] --out DIR [--compare LOGDIR]   a shadow/live run's
                              report (metrics, PNGs; paper-vs-live divergence with --compare)
    export --parquet DIR [--db PATH]   books, candles, trades to Parquet (CSV without pyarrow)
    replay [--log-dir DIR] [--state-db PATH]   wta.replay.runner.run_replay: N/N decisions reproduced

Options such as ``--set key=value`` go after the command: ``wta run --mode shadow --set a.b=1``.

Databases: the recorder (``record``/``backfill``) writes market data to ``data.sqlite_path``; the
engine (``run``, ``reset-kill``, ``scripts/flatten_now.py``) keeps its state in the per-mode state
DB ``data.state_sqlite_path`` with ``{mode}`` replaced (by ``shadow``, ``live``, or
``shadow_synthetic`` on the synthetic feed), so shadow, synthetic and live never share state. A
``--set data.sqlite_path=DIR/x.sqlite`` alone derives the state DB from it
(``DIR/x.state_{mode}.sqlite``); ``--set data.state_sqlite_path=...`` sets it directly (without
``{mode}``, ``.<mode>`` is inserted before the suffix).

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
from wta.ai.cache import CacheMissError
from wta.config import (
    Config,
    Settings,
    apply_changes,
    canonical_json,
    load_config,
    load_settings,
    mask_secrets,
    parse_overrides,
)
from wta.data.collector import RestPoller, default_ws_factory, recordable_symbols, run_recorder
from wta.data.store import Store
from wta.engine import (
    EngineLockError,
    load_injected_intents,
    parse_paper_balances,
    request_kill_reset,
    run_engine,
    state_db_path,
)
from wta.exchange.base import MarketDataBus, WallexAPIError
from wta.exchange.wallex_rest import WallexREST
from wta.execution.reconcile import KV_FLAGS
from wta.logging_.checker import check_logs, scan_for_secrets
from wta.logging_.decision_log import EventsLog
from wta.models import BookSnapshot, MarketInfo
from wta.safety import LiveGuardError, deadline_guard_reason, live_guard_reason
from wta.utils.timeutil import SystemClock, iso

EXIT_OK, EXIT_FAILED, EXIT_CANNOT_RUN, EXIT_NOT_IMPLEMENTED = 0, 1, 2, 3

# command -> (PRD that delivers it, help text): commands announced but not built yet (exit 3).
# Empty since PRD-05: backtest, report, export and replay are real commands now.
PLACEHOLDERS: dict[str, tuple[str, str]] = {}

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


def overlay_pairs(tree: Any, prefix: str = "") -> dict[str, Any]:
    """A YAML overlay as dotted ``--set`` pairs: ``{"risk": {"max_order_pct_equity": 0.125}}`` ->
    ``{"risk.max_order_pct_equity": 0.125}``. Mappings are walked (deep merge: keys the overlay
    does not name keep their value); anything else, lists included, replaces the value."""
    if not isinstance(tree, dict):
        raise ValueError(f"{prefix or 'overlay'}: expected a mapping of config keys")
    out: dict[str, Any] = {}
    for key, value in tree.items():
        path = f"{prefix}.{key}" if prefix else str(key)
        if isinstance(value, dict) and value:
            out.update(overlay_pairs(value, path))
        elif not isinstance(value, dict):
            out[path] = value
    return out


def load_cli_config(overrides: list[str] | None, overlays: list[str] | None = None) -> Config:
    """``configs/*.yaml`` + YAML overlays (in order) + ``--set`` overrides (last, they win).

    The overlays are merged the way ``load_config(runtime_overlay=...)`` merges one file (deep
    merge of mappings); several files apply in the order given.
    """
    if not overlays:
        return load_config(overrides)
    pairs: dict[str, Any] = {}
    for path in overlays:
        try:
            tree = yaml.safe_load(Path(path).read_text()) or {}
        except yaml.YAMLError as exc:
            raise ValueError(f"overlay {path}: not valid YAML ({_one_line(exc)})") from None
        try:
            pairs.update(overlay_pairs(tree))
        except ValueError as exc:
            raise ValueError(f"overlay {path}: {exc}") from None
    pairs.update(parse_overrides(overrides))
    return load_config(pairs)


def _secret_fields(settings: Settings) -> dict[str, str]:
    """``{field name: value}`` for the secrets scan: every secret setting plus the challenge
    sub-account id (``Settings.scan_values``)."""
    return settings.scan_values()


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
    if args.overlay:
        options["overlays"] = [str(p) for p in args.overlay]
    if getattr(args, "no_runtime_overlay", False):
        options["runtime_overlay"] = False
    if getattr(args, "runtime_dir", None):
        options["runtime_dir"] = args.runtime_dir
    pinned = _pinned_keys(args)
    if pinned:
        options["pinned_keys"] = pinned
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


def _pinned_keys(args: argparse.Namespace) -> list[str]:
    """Config keys the operator set by hand (``--set`` and ``--overlay``): the reviewer's runtime
    overlay never overrides them at startup."""
    keys = set(parse_overrides(getattr(args, "set", None) or []))
    for path in getattr(args, "overlay", None) or []:
        try:
            keys.update(overlay_pairs(yaml.safe_load(Path(path).read_text()) or {}))
        except (OSError, ValueError, yaml.YAMLError):
            continue  # load_cli_config already refused a broken overlay
    return sorted(keys)


AI_CHOICES = ("on", "off", "offline")


def apply_ai_choice(choice: str | None, cfg: Config, settings: Settings) -> tuple[Config, Settings]:
    """``--ai`` of ``wta run`` / ``wta plan``.

    * ``on`` (default): the analyst as configured — the LLM when an OpenRouter/Gemini key is set, else the
      offline rules provider.
    * ``off``: rules only (``ai.enabled=false``, the ablation; changes the config hash).
    * ``offline``: the offline rules provider even when keys are set (it is labelled "offline rules (no LLM
      key)", never an LLM) — for a network that reaches Wallex but not OpenRouter/Gemini.
    """
    if choice in (None, "on"):
        return cfg, settings
    if choice == "off":
        return apply_changes(cfg, {"ai.enabled": False}), settings
    if choice == "offline":
        blank = {"openrouter_api_key": SecretStr(""), "gemini_api_key": SecretStr("")}
        return cfg, settings.model_copy(update=blank)
    raise ValueError(f"--ai must be one of {', '.join(AI_CHOICES)}, got {choice!r}")


def cmd_run(args: argparse.Namespace, cfg: Config, settings: Settings) -> int:
    """The guards (``wta.safety``) are checked here for a friendly message, and again by the engine."""
    cfg, settings = apply_ai_choice(getattr(args, "ai", None), cfg, settings)
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
                log_dir=run_log_dir(args.mode, args.log_dir),
                i_understand_live=args.i_understand_live,
                allow_dirty=args.allow_dirty,
                **options,
            )
        )
    except (LiveGuardError, EngineLockError) as exc:
        _err(str(exc))
        return EXIT_CANNOT_RUN


def run_log_dir(mode: str, given: str | None = None) -> str:
    """``--log-dir`` of ``wta run``: as given, else ``logs`` for live and ``logs/shadow`` for shadow
    (the ``make shadow`` layout): a shadow engine next to live never appends to the live logs."""
    if given:
        return given
    return "logs" if mode == "live" else "logs/shadow"


def _killed_state_db(cfg: Config, mode: str | None, feed: str | None = None) -> Path | None | int:
    """The engine state DB to reset: ``mode``'s (and ``feed``'s), else the one that is killed among
    live, shadow and shadow on the synthetic feed.

    Returns the path, None when no candidate is killed, or an exit code after an error message.
    """
    accounts = [("live", "live"), ("shadow", "live"), ("shadow", "synthetic")]  # live trades the live feed only
    accounts = [(m, f) for m, f in accounts if (mode is None or m == mode) and (feed is None or f == feed)]
    paths = list(dict.fromkeys(Path(state_db_path(cfg, m, f)) for m, f in accounts))
    existing = [p for p in paths if p.exists()]
    if not existing:
        where = " or ".join(str(p) for p in paths)
        _err(f"no engine state database at {where} (nothing was ever killed there)")
        return EXIT_FAILED
    killed: list[Path] = []
    for path in existing:
        try:
            store = Store.open_readonly(path)
            try:
                flags = store.kv_get(KV_FLAGS) or {}
            finally:
                store.close()
        except sqlite3.Error as exc:
            _err(f"cannot read {path}: {exc}")
            return EXIT_FAILED
        if flags.get("killed"):
            killed.append(path)
    if len(killed) > 1:
        _err(
            f"the kill-switch is active in {' and '.join(map(str, killed))}: pass --mode shadow|live "
            "(and --feed live|synthetic)"
        )
        return EXIT_CANNOT_RUN
    return killed[0] if killed else None


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
    found = _killed_state_db(cfg, args.mode, args.feed)
    if isinstance(found, int):
        return found
    if found is None:
        print("kill-switch is not active (engine_flags.killed is false): nothing to reset")
        return EXIT_OK
    path = found
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


# ---------------------------------------------------------------- status / tui / plan / alert


def _feed_of_heartbeat(log_dir: Path) -> tuple[str | None, str | None]:
    """(mode, feed) of the newest heartbeat in ``log_dir``, if any."""
    from wta.logging_.health_log import latest

    try:
        hb = latest(log_dir, "health") or {}
    except OSError:
        return None, None
    extra = hb.get("extra") if isinstance(hb.get("extra"), dict) else {}
    feed = extra.get("feed")
    return hb.get("mode"), feed if feed in ("live", "synthetic") else None


def _heartbeat_ts(log_dir: Path) -> float:
    """``ts`` of the newest heartbeat in ``log_dir`` (-inf when there is none)."""
    from wta.logging_.health_log import latest
    from wta.utils.timeutil import parse_iso

    try:
        hb = latest(log_dir, "health") or {}
        ts = parse_iso(hb.get("ts")) if isinstance(hb.get("ts"), str) else None
    except (OSError, ValueError):
        ts = None
    return float("-inf") if ts is None else ts


def _default_ops_dir(mode: str | None, given: str | None) -> Path:
    """``--log-dir`` as given; with ``--mode`` the ``wta run`` default of that mode; with neither,
    the live (``logs``) or the shadow (``logs/shadow``) directory, whichever holds the newer
    heartbeat (shadow when neither has one): a live engine is never hidden behind the shadow one."""
    if given or mode:
        return Path(run_log_dir(mode or "shadow", given))
    live, shadow = Path(run_log_dir("live")), Path(run_log_dir("shadow"))
    return live if _heartbeat_ts(live) > _heartbeat_ts(shadow) else shadow


def _ops_paths(args: argparse.Namespace, cfg: Config) -> tuple[Path, str, str, Path | None]:
    """(log dir, mode, feed, state DB) for ``status`` / ``tui``: ``--log-dir`` as given, else the
    ``wta run`` default of ``--mode``, else the live or shadow directory with the newer heartbeat
    (``_default_ops_dir``); mode and feed as given, else those of the latest heartbeat there (else
    shadow / live); the state DB of that mode and feed (``config.state_db_path``)."""
    mode = args.mode
    log_dir = _default_ops_dir(mode, args.log_dir)
    hb_mode, hb_feed = _feed_of_heartbeat(log_dir)
    mode = mode or (hb_mode if hb_mode in ("shadow", "live") else "shadow")
    feed = args.feed or hb_feed or "live"
    path = state_db_path(cfg, mode, feed)
    return log_dir, mode, feed, None if path == ":memory:" else Path(path)


def cmd_status(args: argparse.Namespace, cfg: Config, settings: Settings) -> int:
    from wta.status import format_status, status_for_config

    log_dir, mode, feed, _db = _ops_paths(args, cfg)
    st = status_for_config(cfg, time.time(), log_dir=log_dir, pid_dir=Path(args.pid_dir), mode=mode, feed=feed)
    if args.json:
        print(canonical_json(st).decode())
        return EXIT_OK
    width = args.width or (_console().width if sys.stdout.isatty() else 100)
    print(mask_secrets(format_status(st, width=width), settings.secret_values()))
    return EXIT_OK


def cmd_tui(args: argparse.Namespace, cfg: Config, settings: Settings) -> int:
    from wta.dashboard import tui

    log_dir, _mode, _feed, state_db = _ops_paths(args, cfg)
    console = Console(width=args.width) if args.width else None
    return tui.run(log_dir, state_db, refresh_s=args.refresh, once=args.once, cfg=cfg, console=console)


def cmd_web(args: argparse.Namespace, cfg: Config, settings: Settings) -> int:
    from wta.dashboard import web

    def resolve() -> tuple[Path, Path | None]:
        log_dir, _mode, _feed, state_db = _ops_paths(args, cfg)
        return log_dir, state_db

    log_dir, state_db = resolve()
    dashboard = web.Dashboard(log_dir, state_db, cfg=cfg, equity_hours=args.equity_hours, resolve=resolve,
                              controls=not args.read_only, secret_values=settings.secret_values())
    web.serve(dashboard, host=args.host, port=args.port)
    return 0


def cmd_plan(args: argparse.Namespace, cfg: Config, settings: Settings) -> int:
    """One analyst cycle (``engine.plan_once``); prints ``{"plan", "analyst", "ai_source", ...}``."""
    cfg, settings = apply_ai_choice(getattr(args, "ai", None), cfg, settings)
    log_dir = run_log_dir("shadow", args.log_dir)
    try:
        out = asyncio.run(
            engine_mod.plan_once(
                cfg, settings, feed=args.feed, log_dir=log_dir, warmup_s=args.warmup,
                runtime_overlay=not args.no_runtime_overlay, pinned_keys=_pinned_keys(args),
                runtime_dir=args.runtime_dir,
            )
        )
    except (LiveGuardError, EngineLockError) as exc:
        _err(f"{exc} (the running engine's latest plan: wta status)")
        return EXIT_CANNOT_RUN
    print(mask_secrets(canonical_json(out).decode(), settings.secret_values()))
    return EXIT_OK if out.get("plan") is not None else EXIT_FAILED


def cmd_alert(args: argparse.Namespace, cfg: Config, settings: Settings) -> int:
    """``wta alert TEXT``: one Telegram message; a no-op (exit 0) without TELEGRAM_BOT_TOKEN,
    TELEGRAM_CHAT_ID and ``alerts.telegram_enabled``."""
    from wta.utils.telegram import send_alert_cli

    text = " ".join(args.text).strip()
    if not text:
        _err("nothing to send")
        return EXIT_CANNOT_RUN
    configured = bool(settings.telegram_bot_token.get_secret_value() and settings.telegram_chat_id.strip())
    if not (configured and cfg.alerts.telegram_enabled):
        print("telegram: not configured (TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, alerts.telegram_enabled); nothing sent",
              file=sys.stderr)
        return EXIT_OK
    return EXIT_OK if send_alert_cli(settings, text, prefix="[wta]") else EXIT_FAILED


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


# ---------------------------------------------------------------- PRD-05: backtest / report / export / replay

SYNTHETIC_DEFAULT_HOURS = {"books": 6.0, "candles": 72.0}  # --synthetic-db without --synthetic-hours
SYNTHETIC_STEP_S = {"books": 2.0, "candles": 10.0}


def _pct_text(value: float | None, places: int = 3) -> str:
    return "n/a" if value is None else f"{value * 100:+.{places}f}%"


def _default_bt_out(driver: str, from_ts: float, to_ts: float) -> str:
    return f"docs/REPORTS/bt_{driver}_{iso(from_ts)[:10].replace('-', '')}_{iso(to_ts)[:10].replace('-', '')}"


def _print_backtest(res: Any) -> None:
    m, meta = res.metrics, res.meta
    if meta.get("synthetic"):
        print("SYNTHETIC — not evidence of performance (generated market data)")
    if m.insufficient_data:
        print("INSUFFICIENT DATA: fewer than two equity points in the window (see Limitations in the report)")
    print(
        f"backtest {meta.get('driver')} {','.join(meta.get('strategies') or [])} ai={meta.get('ai')}"
        f" ({meta.get('ai_source')}): {meta.get('window_from')} -> {meta.get('window_to')},"
        f" data {meta.get('coverage_hours')} h, {meta.get('ticks')} ticks"
    )
    print(
        f"  return {_pct_text(m.return_pct)}  max drawdown {_pct_text(m.max_drawdown_pct)}  trades {m.n_trades}"
        f"  orders {m.n_orders}  vetoes {m.n_vetoes}  fees {m.fees_paid_tmn:,.0f} TMN"
    )
    if meta.get("ai") != "off":
        print(f"  analyst cycles {meta.get('analyst_cycles')} (cap {meta.get('ai_max_calls')}), plans"
              f" {meta.get('analyst_plans')}, model calls {meta.get('model_calls')} ({meta.get('cached_calls')} cached)")
    report = res.paths.get("report")
    if report is not None:
        print(f"  report: {report}")


def cmd_backtest(args: argparse.Namespace, cfg: Config, settings: Settings) -> int:
    """``wta backtest`` (PRD-05 §3.3): ``backtest.runner.run_backtest``, or the walk-forward of
    ``backtest.walkforward`` with ``--param-grid``. ``--synthetic-db`` generates (or reuses) an
    explicitly synthetic market DB first and runs on it (every report says SYNTHETIC)."""
    from backtest import runner as bt  # lazy: pandas/matplotlib are heavy for the other commands

    driver = args.driver
    db = args.db
    try:
        a = bt.parse_ts(args.from_ts, "--from") if args.from_ts else None
        b = bt.parse_ts(args.to_ts, "--to") if args.to_ts else None
        if args.synthetic_db:
            hours = args.synthetic_hours or SYNTHETIC_DEFAULT_HOURS[driver]
            seed = args.seed if args.seed is not None else cfg.engine.rng_seed
            info = bt.make_synthetic_market_db(
                args.synthetic_db, hours, seed, step_s=SYNTHETIC_STEP_S[driver], with_books=driver == "books",
            )
            print(f"synthetic market DB {info['path']} ({'reused' if info.get('reused') else 'generated'}): "
                  f"{info['from']} -> {info['to']}, {info.get('books', 0)} books, {info.get('candles', 0)} candles "
                  "(SYNTHETIC — not evidence of performance)")
            db = info["path"]
            a = info["from_ts"] if a is None else a
            b = info["to_ts"] if b is None else b
        elif args.synthetic_hours is not None:
            raise ValueError("--synthetic-hours needs --synthetic-db PATH")
        if a is None or b is None:
            raise ValueError("--from and --to are required (or --synthetic-db PATH)")
        balances = parse_paper_balances(args.balances) if args.balances else None
        common: dict[str, Any] = {
            "driver": driver,
            "from_ts": a,
            "to_ts": b,
            "strategies": args.strategies,
            "ai": args.ai,
            "ai_max_calls": args.ai_max_calls,
            "market_db": db,
            "seed": args.seed,
            "step_s": args.step_s,
            "initial_equity_tmn": args.initial_equity,
            "initial_balances": balances,
            "flatten_at_end": args.flatten_at_end,
            "settings": settings,
            "title": args.title,
            "report_date": args.report_date,
        }
        if args.state_db:
            common["state_db"] = args.state_db
        if args.verbose:
            common["quiet"] = False
        out = args.out or _default_bt_out(driver, a, b)
        if args.param_grid:
            from backtest import walkforward as wf

            if args.ai == "cache":
                raise ValueError("--ai cache cannot run a --param-grid walk-forward (its runs ask questions the cache "
                                 "never saw); use --ai on or off")

            grid = wf.parse_param_grid(args.param_grid, cfg)
            result = wf.run_walkforward(cfg, grid=grid, out_dir=out, **common)
            print(f"walk-forward: chosen {wf.label(result.chosen.params)}; {result.reason}; final "
                  f"{wf.label(result.final_params)} ({result.paths.get('markdown')})")
            res = result.final
        else:
            res = bt.run_backtest(cfg, out_dir=out, **common)
    except (ValueError, FileNotFoundError, OSError) as exc:
        _err(_one_line(exc))
        return EXIT_CANNOT_RUN
    except EngineLockError as exc:
        _err(f"{exc} (another backtest writes into the same --out)")
        return EXIT_CANNOT_RUN
    except CacheMissError as exc:
        _err(f"--ai cache: {_one_line(exc)}: this run asked a question the recorded cache does not hold (another "
             "window, config, strategies or code than the cached run); no provider was called")
        return EXIT_FAILED
    if res is not None:
        _print_backtest(res)
    return EXIT_OK


def cmd_report(args: argparse.Namespace, cfg: Config, settings: Settings) -> int:
    """``wta report --source LOGDIR --out DIR`` (PRD-05 §5, §7): metrics + report of a shadow/live
    run from its logs and state DB; ``--compare OTHER_LOGDIR`` adds the paper-vs-live divergence."""
    from backtest import runner as bt
    from backtest.report import update_index

    source = Path(args.source)
    out = Path(args.out) if args.out else Path("docs/REPORTS") / f"report_{source.name or 'logs'}"
    try:
        a = bt.parse_ts(args.from_ts, "--from") if args.from_ts else None
        b = bt.parse_ts(args.to_ts, "--to") if args.to_ts else None
        res = bt.report_from_logs(
            source=source, out_dir=out, state_db=args.state_db, compare=args.compare,
            compare_state_db=args.compare_state_db, title=args.title, from_ts=a, to_ts=b,
            report_date=args.report_date,
        )
    except (ValueError, FileNotFoundError, OSError) as exc:
        _err(_one_line(exc))
        return EXIT_FAILED if "no log records" in str(exc) else EXIT_CANNOT_RUN
    m, meta = res.metrics, res.meta
    if meta.get("synthetic"):
        print("SYNTHETIC — not evidence of performance (the run used the synthetic feed)")
    print(f"report {meta.get('mode')} {meta.get('window_from')} -> {meta.get('window_to')}: return "
          f"{_pct_text(m.return_pct)}, max drawdown {_pct_text(m.max_drawdown_pct)}, trades {m.n_trades}, "
          f"orders {m.n_orders} (equity from {meta.get('equity_source')})")
    if res.divergence is not None:
        d = res.divergence
        print(f"divergence: {d['matched']} matched, {d['only_a']} only in {args.source}, {d['only_b']} only in "
              f"{args.compare}")
    print(f"  report: {res.report}")
    if args.update_index:
        print(f"  index: {update_index(out.parent)}")
    return EXIT_OK


def cmd_export(args: argparse.Namespace, cfg: Config, settings: Settings) -> int:
    """``wta export --parquet DIR [--db PATH]``: books, candles and trades of the market DB as
    Parquet (CSV with a note when pyarrow is not installed)."""
    from backtest import runner as bt

    db = Path(args.db or cfg.data.sqlite_path)
    try:
        manifest = bt.export_tables(db, args.parquet)
    except (FileNotFoundError, OSError, sqlite3.Error, ValueError) as exc:
        _err(_one_line(exc))
        return EXIT_FAILED
    if manifest.get("note"):
        print(f"note: {manifest['note']}")
    for table, info in sorted(manifest["tables"].items()):
        print(f"  {table}: {info['rows']} rows -> {info.get('file') or info.get('note')}")
    print(f"exported {db} to {args.parquet} ({manifest['format']})")
    return EXIT_OK


def cmd_replay(args: argparse.Namespace, cfg: Config, settings: Settings) -> int:
    """``wta replay`` (PRD-05 §6): ``wta.replay.runner.run_replay`` over a run's logs + state DB;
    exit 0 when every decision is reproduced (PRD fields and the strict pass), 1 on a mismatch or a
    partial recording, 2 when there is nothing to replay or compare."""
    import inspect

    try:
        from wta.replay.runner import run_replay
    except ImportError as exc:
        print(f"wta replay: the replay runner (wta.replay.runner) is not available: {_one_line(exc)}")
        return EXIT_NOT_IMPLEMENTED
    log_dir = Path(args.log_dir)
    if not log_dir.is_dir():
        _err(f"no log directory {log_dir}")
        return EXIT_CANNOT_RUN
    # without --state-db the replay runner takes the one its start event names
    state_db = Path(args.state_db) if args.state_db else None
    try:
        result = run_replay(
            cfg, log_dir=log_dir, state_db=state_db, out_dir=Path(args.out) if args.out else None,
            reports_dir=args.reports_dir or None, strict=not args.lenient, allow_partial=args.allow_partial,
        )
        if inspect.isawaitable(result):
            result = asyncio.run(result)
    except (ValueError, FileNotFoundError, OSError) as exc:
        _err(_one_line(exc))
        return EXIT_CANNOT_RUN
    summary = getattr(result, "summary", None)
    summary = summary() if callable(summary) else summary
    ok = bool(getattr(result, "ok", False))
    print(f"replay: {summary or result} ({'PASS' if ok else 'FAIL'})")
    for err in getattr(result, "errors", None) or []:
        print(f"replay error: {err}")
    if not ok and getattr(result, "n_reference", None) == 0:
        return EXIT_CANNOT_RUN  # nothing to compare: a window without decisions proves nothing
    return EXIT_OK if ok else EXIT_FAILED


def _placeholder(name: str) -> Handler:
    prd = PLACEHOLDERS[name][0]

    def handler(args: argparse.Namespace, cfg: Config, settings: Settings) -> int:
        print(f"wta {name}: not implemented yet ({prd})")
        return EXIT_NOT_IMPLEMENTED

    return handler


# ---------------------------------------------------------------- parser


def _add_overlay_option(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--overlay",
        action="append",
        default=[],
        metavar="FILE",
        help="YAML overlay merged over configs/*.yaml, repeatable (e.g. configs/live_halfsize.yaml); "
        "--set still wins",
    )


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
    _add_overlay_option(show)
    show.set_defaults(handler=cmd_config_show)
    config_hash = config_sub.add_parser("hash", parents=[common], help="config_hash only", allow_abbrev=False)
    _add_overlay_option(config_hash)
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
    _add_overlay_option(p)
    p.add_argument("--i-understand-live", action="store_true", help="required for --mode live")
    p.add_argument("--allow-dirty", action="store_true", help="allow live mode on a dirty git tree (dev only)")
    p.add_argument("--seconds", type=float, default=None, help="stop after this many seconds")
    p.add_argument(
        "--log-dir", default=None, help="JSONL log directory (default: logs for live, logs/shadow for shadow)"
    )
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
    p.add_argument(
        "--no-runtime-overlay",
        action="store_true",
        help="do not re-apply the reviewer's <runtime-dir>/latest.yaml at startup",
    )
    p.add_argument(
        "--runtime-dir",
        default=None,
        metavar="DIR",
        help="where the reviewer writes (and startup reads) its overlays "
             "(default configs/runtime/<account>: live, shadow or shadow_synthetic)",
    )
    p.add_argument(
        "--ai",
        choices=AI_CHOICES,
        default="on",
        help="on: analyst as configured (LLM if a key is set); off: rules only (ai.enabled=false); "
             "offline: the labelled offline rules provider even with keys (network without OpenRouter)",
    )

    p = add("reset-kill", cmd_reset_kill, "clear the kill-switch by hand (writes a reset_kill event)")
    p.add_argument("--reason", required=True, help="why the kill-switch is reset (logged)")
    p.add_argument("--log-dir", default="logs", help="JSONL log directory of the engine (default logs)")
    p.add_argument(
        "--mode",
        choices=("shadow", "live"),
        default=None,
        help="the engine whose state DB is reset (default: the one that is killed)",
    )
    p.add_argument(
        "--feed",
        choices=("live", "synthetic"),
        default=None,
        help="the feed of that engine's account (synthetic runs keep their own state DB)",
    )

    def ops_options(q: argparse.ArgumentParser) -> None:
        q.add_argument("--mode", choices=("shadow", "live"), default=None,
                       help="engine mode (default: the latest heartbeat's, else shadow)")
        q.add_argument("--feed", choices=("live", "synthetic"), default=None,
                       help="engine feed (default: the latest heartbeat's, else live)")
        q.add_argument("--log-dir", default=None,
                       help="engine log directory (default: logs for live, logs/shadow for shadow)")

    p = add("status", cmd_status, "one-screen summary of the engine (latest heartbeat, state DB, plan, AI)")
    ops_options(p)
    p.add_argument("--pid-dir", default="run", help="directory of *.pid files (default run)")
    p.add_argument("--width", type=int, default=None, help="text width (default: the terminal's)")
    p.add_argument("--json", action="store_true", help="print the status as JSON")

    p = add("tui", cmd_tui, "terminal dashboard (read-only, separate process)")
    ops_options(p)
    p.add_argument("--refresh", type=float, default=2.0, help="seconds between refreshes (default 2)")
    p.add_argument("--once", action="store_true", help="print one frame and exit")
    p.add_argument("--width", type=int, default=None, help="screen width (default: the terminal's; 200 fits all)")

    p = add("web", cmd_web, "web dashboard on http://localhost:8080 (Kill / Reset-kill buttons; never goes live)")
    ops_options(p)
    p.add_argument("--host", default="127.0.0.1", help="bind address (default 127.0.0.1; 0.0.0.0 inside Docker)")
    p.add_argument("--port", type=int, default=8080, help="port (default 8080)")
    p.add_argument("--equity-hours", type=float, default=6.0, help="hours of equity history in the chart (default 6)")
    p.add_argument("--read-only", action="store_true", help="no Kill / Reset-kill buttons (the page only reads)")

    p = add("plan", cmd_plan, "ONE analyst cycle on the shadow account's data; prints the plan JSON")
    p.add_argument("--mode", choices=("shadow",), default="shadow", help="shadow only (nothing is traded)")
    p.add_argument("--feed", choices=("live", "synthetic"), default="live", help="market data source")
    p.add_argument("--log-dir", default=None, help="log directory whose llm_cache is used (default logs/shadow)")
    p.add_argument("--warmup", type=float, default=15.0, help="seconds to wait for the regime books (default 15)")
    p.add_argument("--no-runtime-overlay", action="store_true", help="ignore the reviewer's <runtime-dir>/latest.yaml")
    p.add_argument("--runtime-dir", default=None, metavar="DIR",
                   help="reviewer overlay directory (default configs/runtime/<account>)")
    p.add_argument("--ai", choices=("on", "offline"), default="on",
                   help="on: the configured LLM (offline rules without a key); offline: the offline rules provider")
    _add_overlay_option(p)

    p = add("alert", cmd_alert, "send one Telegram message (no-op without TELEGRAM_* and alerts.telegram_enabled)")
    p.add_argument("text", nargs="+", help="message text (put it after -- when it starts with a dash)")
    _add_overlay_option(p)  # scripts/run_forever.sh forwards the wrapped command's --set / --overlay

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

    p = add("backtest", cmd_backtest, "backtest the engine over stored books or candles (PRD-05 §3)")
    p.add_argument("--driver", choices=("candles", "books"), default="candles",
                   help="candles: 1-minute bars, S2/S3 (default); books: recorded order books, S1/S2")
    p.add_argument("--from", dest="from_ts", default=None, metavar="ISO", help="window start, e.g. 2026-09-22")
    p.add_argument("--to", dest="to_ts", default=None, metavar="ISO", help="window end, e.g. 2026-10-22T10:00Z")
    p.add_argument("--ai", choices=("off", "on", "offline", "cache"), default="off",
                   help="off: rules only (default); on: the configured LLM (cached, capped); offline: rules provider; "
                        "cache: an ai=on run's recorded LLM answers (--set ai.cache_dir=<run>/llm_cache), no key, "
                        "no provider call, a cache miss is an error")
    p.add_argument("--strategies", default=None, metavar="S2,S3",
                   help="strategies to run (others disabled; default S2,S3 for candles, S1,S2 for books)")
    p.add_argument("--out", default=None, metavar="DIR",
                   help="output folder (default docs/REPORTS/bt_<driver>_<from>_<to>)")
    p.add_argument("--db", default=None, metavar="PATH", help="market DB, read-only (default data.sqlite_path)")
    p.add_argument("--param-grid", action="append", default=[], metavar="KEY=V1,V2",
                   help="walk-forward grid (<= 2 keys, <= 8 values each; 70/30 split), repeatable")
    p.add_argument("--ai-max-calls", type=int, default=150, metavar="N",
                   help="at most N analyst cycles per run with --ai on/offline (default 150)")
    p.add_argument("--synthetic-db", default=None, metavar="PATH",
                   help="generate (or reuse) a SYNTHETIC market DB at PATH and run on it (never evidence)")
    p.add_argument("--synthetic-hours", type=float, default=None, metavar="H",
                   help="hours of synthetic data (default 6 for books, 72 for candles)")
    p.add_argument("--seed", type=int, default=None, help="engine.rng_seed (and the synthetic generator's seed)")
    p.add_argument("--step-s", type=float, default=None, help="books driver tick grid (default engine.fast_loop_interval_s)")
    p.add_argument("--initial-equity", default=None, metavar="TMN", help="starting equity in TMN (default engine.initial_equity_tmn)")
    p.add_argument("--balances", default=None, metavar="JSON", help='starting balances, e.g. \'{"TMN": 2500000, "USDT": 9.4}\'')
    p.add_argument("--flatten-at-end", action=argparse.BooleanOptionalAction, default=None,
                   help="run the deadline flatten in the window's last 2 h (default: on for windows >= 8 h)")
    p.add_argument("--title", default=None, help="report title")
    p.add_argument("--report-date", default=None, metavar="YYYY-MM-DD",
                   help="the report's date (default: the commit date of the code; never the data's end)")
    p.add_argument("--state-db", default=None, metavar="PATH",
                   help="engine state DB of the run (default <out>/state.sqlite; ~4 MB per simulated day)")
    p.add_argument("--verbose", action="store_true", help="engine log lines during the run (default: errors only)")

    p = add("report", cmd_report, "report of a shadow/live run from its logs (+ state DB) (PRD-05 §5, §7)")
    p.add_argument("--source", default="logs/shadow", metavar="LOGDIR", help="the run's log directory (default logs/shadow)")
    p.add_argument("--state-db", default=None, metavar="PATH",
                   help="the run's state DB for the equity curve (default: the start event's; else heartbeats)")
    p.add_argument("--out", default=None, metavar="DIR", help="output folder (default docs/REPORTS/report_<logdir>)")
    p.add_argument("--compare", default=None, metavar="LOGDIR",
                   help="another run's log dir (e.g. live next to shadow): adds the paper-vs-live divergence")
    p.add_argument("--compare-state-db", default=None, metavar="PATH", help=argparse.SUPPRESS)
    p.add_argument("--from", dest="from_ts", default=None, metavar="ISO", help="cut the window (start)")
    p.add_argument("--to", dest="to_ts", default=None, metavar="ISO", help="cut the window (end)")
    p.add_argument("--title", default=None, help="report title")
    p.add_argument("--report-date", default=None, metavar="YYYY-MM-DD",
                   help="the report's date (default: the commit date of the code; never the data's end)")
    p.add_argument("--update-index", action="store_true", help="regenerate README.md of the --out folder's parent")

    p = add("export", cmd_export, "market tables (books, candles, trades) to Parquet (CSV without pyarrow)")
    p.add_argument("--parquet", default="data/export", metavar="DIR", help="output folder (default data/export)")
    p.add_argument("--db", default=None, metavar="PATH", help="database (default data.sqlite_path)")

    p = add("replay", cmd_replay, "re-run a recorded run and compare the decisions (PRD-05 §6)")
    p.add_argument("--log-dir", default="logs", metavar="DIR", help="the run's log directory (default logs)")
    p.add_argument("--state-db", default=None, metavar="PATH",
                   help="the run's state DB (default: the one its start event names)")
    p.add_argument("--out", default=None, metavar="DIR", help="where the replayed logs go")
    p.add_argument("--reports-dir", default="docs/REPORTS", metavar="DIR",
                   help="where replay_<date>.md goes ('' = nowhere)")
    p.add_argument("--lenient", action="store_true",
                   help="keep going after an LLM cache miss / unmatched config hash (the verdict still FAILs on "
                        "any mismatch; every field is always compared)")
    p.add_argument("--allow-partial", action="store_true",
                   help="replay the committed prefix of a run still being written, crashed or with an incomplete "
                        "state DB copy (a PARTIAL replay; without it that is a failure)")

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
        cfg = load_cli_config(args.set, getattr(args, "overlay", None))
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

"""Standalone market recorder (same as ``python -m wta record``).

    python scripts/record_market.py --symbols-from configs/markets.yaml
    python scripts/record_market.py --symbols BTCTMN,BTCUSDT --no-ws

Runs until SIGINT/SIGTERM: polls REST books + 1-minute candles (and the WebSocket feed when
enabled) into ``data.sqlite_path`` and prints a health line every minute. API outages are retried
quietly with capped backoff, so it is safe to leave running for days.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
from pathlib import Path

import yaml

from wta.config import load_config, load_settings
from wta.data.collector import default_ws_factory, run_recorder


def symbols_from_file(path: Path) -> list[str]:
    """The ``watchlist:`` list of a markets YAML file (e.g. ``configs/markets.yaml``)."""
    data = yaml.safe_load(path.read_text()) or {}
    return [str(s).upper() for s in data.get("watchlist") or []]


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Record Wallex market data into SQLite.")
    p.add_argument("--symbols-from", type=Path, help="YAML file with a `watchlist:` list")
    p.add_argument("--symbols", help="comma-separated symbols (overrides --symbols-from)")
    p.add_argument("--no-ws", action="store_true", help="REST polling only")
    p.add_argument("--set", action="append", default=[], metavar="KEY=VALUE", help="config override")
    p.add_argument("--log-level", default="INFO")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(level=args.log_level.upper(), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    for noisy in ("httpx", "httpcore", "websockets"):
        logging.getLogger(noisy).setLevel(logging.WARNING)  # one line per request otherwise

    cfg = load_config(args.set)
    settings = load_settings()
    symbols: list[str] | None = None
    if args.symbols:
        symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    elif args.symbols_from:
        symbols = symbols_from_file(args.symbols_from)
    ws_factory = None if args.no_ws or not cfg.data.ws_enabled else default_ws_factory(cfg, settings)
    asyncio.run(run_recorder(cfg, settings, symbols=symbols, ws_factory=ws_factory))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

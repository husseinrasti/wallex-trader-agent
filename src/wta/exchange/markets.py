"""Market metadata loading: live ``/v1/markets`` + fees, with an offline snapshot fallback.

Live trading always uses the API values; the snapshot (``configs/markets_snapshot.yaml``) only
keeps backtests and synthetic-feed shadow runs working on machines without API access.
"""

from __future__ import annotations

import logging
from decimal import Decimal
from pathlib import Path
from typing import Any

import yaml

from wta.config import CONFIG_DIR
from wta.models import MarketInfo
from wta.utils.decimalutil import D

log = logging.getLogger(__name__)

DEFAULT_FEES = {"TMN": (Decimal("0.0025"), Decimal("0.003")), "USDT": (Decimal("0.001"), Decimal("0.0013"))}


def load_fee_snapshot(path: Path = CONFIG_DIR / "fees_snapshot.yaml") -> dict[str, Any]:
    if not path.exists():
        return {"default_by_quote": {}, "symbols": {}}
    return yaml.safe_load(path.read_text()) or {}


def default_fees(quote: str, snapshot: dict[str, Any] | None = None) -> tuple[Decimal, Decimal]:
    snap = (snapshot or {}).get("default_by_quote", {}).get(quote)
    if snap:
        return D(snap["maker"]), D(snap["taker"])
    return DEFAULT_FEES.get(quote, DEFAULT_FEES["TMN"])


def load_markets_snapshot(path: Path = CONFIG_DIR / "markets_snapshot.yaml") -> dict[str, MarketInfo]:
    raw = yaml.safe_load(path.read_text()) or {}
    fees = load_fee_snapshot()
    out: dict[str, MarketInfo] = {}
    for sym in sorted(raw.get("markets", {})):
        m = raw["markets"][sym]
        maker, taker = default_fees(m["quote"], fees)
        sym_fee = (fees.get("symbols") or {}).get(sym)
        if sym_fee:
            maker, taker = D(sym_fee["maker"]), D(sym_fee["taker"])
        out[sym] = MarketInfo(
            symbol=sym,
            base=m["base"],
            quote=m["quote"],
            tick_decimals=int(m["tick_decimals"]),
            step_decimals=int(m["step_decimals"]),
            min_qty=D(m.get("min_qty", "0")),
            min_notional=D(m.get("min_notional", "0")),
            market_orders_enabled=bool(m.get("market_orders_enabled", True)),
            maker_fee=maker,
            taker_fee=taker,
        )
    return out


def snapshot_reference(path: Path = CONFIG_DIR / "markets_snapshot.yaml") -> dict[str, dict[str, Decimal]]:
    raw = yaml.safe_load(path.read_text()) or {}
    return {
        "mids": {k: D(v) for k, v in (raw.get("reference_mids") or {}).items()},
        "spreads": {k: D(v) for k, v in (raw.get("reference_spreads_pct") or {}).items()},
    }


async def load_markets(rest: Any | None, with_fees: bool = True) -> tuple[dict[str, MarketInfo], str]:
    """Return ``(markets, source)`` where source is "api" or "snapshot".

    ``rest`` is a WallexREST (or None for offline). Fees come from ``/v1/account/fee`` when an API
    key is configured, otherwise from the fee snapshot.
    """
    if rest is not None:
        try:
            markets = await rest.get_markets()
            if with_fees and getattr(rest, "has_api_key", False):
                try:
                    from wta.exchange.wallex_rest import apply_fees

                    markets = apply_fees(markets, await rest.get_fees())
                except Exception as exc:  # noqa: BLE001 - fees are optional, defaults apply
                    log.warning("fee fetch failed, using default fees: %s", exc)
            return markets, "api"
        except Exception as exc:  # noqa: BLE001 - fall back to the snapshot
            log.warning("markets fetch failed (%s); using configs/markets_snapshot.yaml", exc)
    return load_markets_snapshot(), "snapshot"

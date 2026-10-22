"""Async REST client for Wallex (PRD-01 §5).

* ``TokenBucket`` rate limiter (``rps`` tokens/s, burst ``rps``) with an injectable clock and sleep.
* Retries on HTTP 429/5xx, timeouts and transport errors with backoff 1, 2, 4, 8 s (+ jitter),
  honouring ``Retry-After``. Other 4xx answers and ``success: false`` bodies raise at once.
* ``place_order`` is never retried blindly: after an ambiguous failure (timeout, 5xx, 429, a 2xx
  whose body cannot be read) it first looks the order up by ``client_id`` and only re-sends when
  the exchange does not know it. ``OrderRejected`` is raised only when the first send of a
  ``client_id`` is refused; every other error means "state unknown, reconcile".
* Pure, module-level parsers turn the documented payloads (``docs/WALLEX_API_NOTES.md``) into the
  models of ``wta.models``. They are tolerant: unknown shapes keep the raw payload.

JSON is decoded with ``parse_float=Decimal`` so no price or quantity ever passes through a float.
The API key is only sent on account endpoints and is scrubbed from every error message, every
``raw`` payload and every ``on_order_event`` record.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import time
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, cast
from urllib.parse import quote

import httpx

from wta.config import mask_secrets
from wta.exchange.base import Exchange, OrderRejected, WallexAPIError
from wta.models import (
    Balance,
    BookSnapshot,
    Candle,
    Fill,
    Level,
    MarketInfo,
    Order,
    OrderRequest,
    OrderStatus,
    OrderType,
    Side,
    Trade,
)
from wta.utils.ids import is_ours
from wta.utils.timeutil import Clock, SystemClock, parse_iso

log = logging.getLogger(__name__)

D0 = Decimal("0")
USER_AGENT = "wta/0.1"
MAX_BACKOFF_S = 60.0
ORDERS_PATH = "/v1/account/orders"
OPEN_ORDERS_PATH = "/v1/account/openOrders"
OPEN_ORDERS_PER_PAGE = 100
OPEN_ORDERS_MAX_PAGES = 20
MARKETS_PATHS = ("/v1/markets", "/hector/web/v1/markets")
ALL_DEPTH_PATHS = ("/v2/depth/all", "/depth/all")
SUB_ACCOUNT_PATHS = ("/sub-accounts", "/v1/sub-accounts", "/v1/account/sub-accounts")

# Used when /v1/markets or /v1/account/fee do not say (Overview §2).
DEFAULT_MIN_NOTIONAL = {"TMN": Decimal("50000"), "USDT": Decimal("1")}
DEFAULT_FEES = {"TMN": (Decimal("0.0025"), Decimal("0.003")), "USDT": (Decimal("0.001"), Decimal("0.0013"))}
QUOTE_ASSETS = ("USDT", "TMN", "USDC")

ORDER_TYPES: frozenset[str] = frozenset({"LIMIT", "MARKET", "STOP_LIMIT", "STOP_MARKET"})
STOP_TYPES: frozenset[str] = frozenset({"STOP_LIMIT", "STOP_MARKET"})
_OPEN = frozenset({"NEW", "OPEN", "ACTIVE", "PENDING"})
_PARTIAL = frozenset({"PARTIALLY_FILLED", "PARTIAL", "PARTIALLY_FILL"})
_FILLED = frozenset({"FILLED", "DONE"})
_CANCELED = frozenset({"CANCELED", "CANCELLED", "CANCEL", "CANCELLED_BY_USER", "CANCELED_BY_USER"})

OrderEventCallback = Callable[[str, str, str, dict[str, Any]], None]


# ---------------------------------------------------------------- value helpers


def _dec(value: object) -> Decimal | None:
    """Tolerant Decimal: None for missing, blank or non-numeric values (floats go through ``str``)."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, Decimal):
        d = value
    elif isinstance(value, int):
        d = Decimal(value)
    else:
        text = str(value).strip()
        if not text:
            return None
        try:
            d = Decimal(text)
        except InvalidOperation:
            return None
    return d if d.is_finite() else None


def _bool(value: object) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value != 0
    if isinstance(value, str) and value.strip().lower() in ("true", "false", "1", "0"):
        return value.strip().lower() in ("true", "1")
    return None


def _ts(value: object) -> float | None:
    """Unix seconds from ISO-8601, epoch seconds or epoch milliseconds (numbers or strings)."""
    if value is None or isinstance(value, bool) or value == "":
        return None
    if isinstance(value, int | float | Decimal):
        x = float(value)
    else:
        text = str(value).strip()
        try:
            x = float(text)
        except ValueError:
            try:
                return parse_iso(text)
            except ValueError:
                return None
    if x <= 0:
        return None
    while x > 1e11:  # milliseconds (or finer) -> seconds
        x /= 1000
    return x


def _first(d: dict[str, Any], *keys: str) -> Any:
    """The first key of ``keys`` present in ``d`` with a non-None value."""
    for k in keys:
        if d.get(k) is not None:
            return d[k]
    return None


def _result(payload: Any) -> Any:
    """Unwrap the ``{"result": ..., "success": ...}`` envelope if present."""
    if isinstance(payload, dict) and "result" in payload:
        return payload["result"]
    return payload


def _side(value: object) -> Side | None:
    s = str(value or "").strip().upper()
    if s == "BUY":
        return "BUY"
    if s == "SELL":
        return "SELL"
    return None


def _norm(value: object) -> str:
    return str(value or "").strip().upper().replace("-", "_").replace(" ", "_")


def _plain(d: Decimal) -> str:
    """Canonical text for keys: no exponent, no trailing zeros."""
    return format(d.normalize(), "f")


def decimals_count(value: object) -> int | None:
    """Precision as a decimal count: ``2``/``"2"`` -> 2, and a step value ``"0.01"`` -> 2."""
    d = _dec(value)
    if d is None or d < 0:
        return None
    text = format(value, "f") if isinstance(value, Decimal) else str(value).strip()
    if d == d.to_integral_value() and "." not in text and "e" not in text.lower():
        return int(d)  # already a count of decimals
    if d == 0:
        return 0
    return max(0, -d.normalize().as_tuple().exponent)  # a step size such as 0.01


def split_symbol(symbol: str) -> tuple[str, str]:
    """``BTCUSDT`` -> ``("BTC", "USDT")`` using the known quote assets; ``("", "")`` if unknown."""
    for q in QUOTE_ASSETS:
        if symbol.endswith(q) and len(symbol) > len(q):
            return symbol[: -len(q)], q
    return "", ""


def json_safe(obj: Any) -> Any:
    """Recursively convert Decimals (and other odd values) so ``json.dumps`` always works."""
    if isinstance(obj, Decimal):
        return format(obj, "f")
    if isinstance(obj, dict):
        return {str(k): json_safe(v) for k, v in obj.items()}
    if isinstance(obj, list | tuple):
        return [json_safe(v) for v in obj]
    if obj is None or isinstance(obj, str | int | float | bool):
        return obj
    return str(obj)


def _redact(obj: Any, secret: str) -> Any:
    """Replace ``secret`` in every string (and key) of a decoded JSON value."""
    if not secret:
        return obj
    if isinstance(obj, str):
        return mask_secrets(obj, [secret])
    if isinstance(obj, dict):
        return {_redact(k, secret): _redact(v, secret) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_redact(v, secret) for v in obj]
    return obj


# ---------------------------------------------------------------- market data parsers


def _market_defaults(quote: str) -> tuple[Decimal, Decimal, Decimal]:
    maker, taker = DEFAULT_FEES.get(quote, DEFAULT_FEES["TMN"])
    return DEFAULT_MIN_NOTIONAL.get(quote, D0), maker, taker


def _market(
    symbol: str,
    base: str,
    quote: str,
    tick: int | None,
    step: int | None,
    min_qty: Decimal | None,
    min_notional: Decimal | None,
    market_orders: bool | None,
) -> MarketInfo | None:
    if not symbol or tick is None or step is None:
        return None
    if not base or not quote:
        base, quote = split_symbol(symbol)
    default_notional, maker, taker = _market_defaults(quote)
    return MarketInfo(
        symbol=symbol,
        base=base,
        quote=quote,
        tick_decimals=tick,
        step_decimals=step,
        min_qty=min_qty if min_qty is not None else D0,
        min_notional=min_notional if min_notional else default_notional,
        market_orders_enabled=True if market_orders is None else market_orders,
        maker_fee=maker,
        taker_fee=taker,
    )


def parse_markets(payload: Any) -> dict[str, MarketInfo]:
    """Markets from ``/v1/markets`` (``result.symbols``) or ``/hector/web/v1/markets`` (``result.markets``).

    ``tickSize``/``stepSize`` are decimal counts (int or str); a step value like ``"0.01"`` is
    converted to its count. Missing ``minNotional`` and fees fall back to the per-quote defaults.
    """
    res = _result(payload)
    out: dict[str, MarketInfo] = {}
    if not isinstance(res, dict):
        return out
    symbols = res.get("symbols")
    if isinstance(symbols, dict | list):
        if isinstance(symbols, dict):
            entries: Iterable[tuple[Any, Any]] = symbols.items()
        else:
            entries = ((e.get("symbol"), e) for e in symbols if isinstance(e, dict))
        for key, m in entries:
            if not isinstance(m, dict):
                continue
            sym = str(m.get("symbol") or key or "").upper()
            info = _market(
                sym,
                str(m.get("baseAsset") or "").upper(),
                str(m.get("quoteAsset") or "").upper(),
                decimals_count(m.get("tickSize")),
                decimals_count(m.get("stepSize")),
                _dec(m.get("minQty")),
                _dec(m.get("minNotional")),
                _bool(m.get("isMarketTypeEnable")),
            )
            if info is not None:
                out[sym] = info
    markets = res.get("markets")
    if isinstance(markets, list):
        for m in markets:
            if not isinstance(m, dict) or _bool(m.get("is_spot")) is False:
                continue
            sym = str(m.get("symbol") or "").upper().replace("-", "").replace("/", "").replace("_", "")
            if sym in out:
                continue
            info = _market(
                sym,
                str(m.get("base_asset") or "").upper(),
                str(m.get("quote_asset") or "").upper(),
                decimals_count(m.get("price_precision")),
                decimals_count(m.get("amount_precision")),
                _dec(_first(m, "min_qty", "min_amount")),
                _dec(_first(m, "min_notional", "min_total")),
                _bool(_first(m, "is_market_type_enable", "isMarketTypeEnable")),
            )
            if info is not None:
                out[sym] = info
    return dict(sorted(out.items()))


def apply_fees(
    markets: dict[str, MarketInfo], fees: dict[str, tuple[Decimal, Decimal]]
) -> dict[str, MarketInfo]:
    """A new markets dict with ``maker_fee``/``taker_fee`` taken from ``fees`` where known."""
    out: dict[str, MarketInfo] = {}
    for sym, m in markets.items():
        if sym in fees:
            maker, taker = fees[sym]
            out[sym] = m.model_copy(update={"maker_fee": maker, "taker_fee": taker})
        else:
            out[sym] = m
    return out


def _fee_rate(value: object) -> Decimal | None:
    d = _dec(value)
    if d is None or d < 0:
        return None
    # The docs do not say whether rates are fractions ("0.0025") or percents ("0.25"). No spot
    # fee is above 2 %, so a larger number can only be a percent.
    return d / 100 if d > Decimal("0.02") else d


def parse_fees(payload: Any) -> dict[str, tuple[Decimal, Decimal]]:
    """``/v1/account/fee`` -> ``{SYMBOL: (maker, taker)}`` as fractions (0.0025 = 0.25 %)."""
    res = _result(payload)
    if isinstance(res, dict):
        entries: Iterable[tuple[Any, Any]] = res.items()
    elif isinstance(res, list):
        entries = ((e.get("symbol"), e) for e in res if isinstance(e, dict))
    else:
        return {}
    out: dict[str, tuple[Decimal, Decimal]] = {}
    for sym, f in entries:
        if not isinstance(f, dict) or not sym:
            continue
        maker = _fee_rate(_first(f, "makerFeeRate", "maker_fee_rate", "maker"))
        taker = _fee_rate(_first(f, "takerFeeRate", "taker_fee_rate", "taker"))
        if maker is not None and taker is not None:
            out[str(sym).upper()] = (maker, taker)
    return dict(sorted(out.items()))


def _levels(raw: Any, levels: int, descending: bool) -> list[Level]:
    out: list[Level] = []
    for entry in raw if isinstance(raw, list) else []:
        if isinstance(entry, dict):
            price, qty = _dec(entry.get("price")), _dec(_first(entry, "quantity", "qty", "amount"))
        elif isinstance(entry, list | tuple) and len(entry) >= 2:
            price, qty = _dec(entry[0]), _dec(entry[1])
        else:
            continue
        if price is None or qty is None or price <= 0 or qty <= 0:
            continue
        out.append(Level(price=price, qty=qty))
    out.sort(key=lambda lv: lv.price, reverse=descending)
    return out[:levels]


def parse_depth(symbol: str, payload: Any, ts: float, levels: int = 10) -> BookSnapshot:
    """``/v1/depth`` (``result.ask[]``/``result.bid[]``) -> BookSnapshot; bids desc, asks asc, no zero qty."""
    res = _result(payload)
    res = res if isinstance(res, dict) else {}
    bids = _first(res, "bid", "bids", "buy", "buyDepth")
    asks = _first(res, "ask", "asks", "sell", "sellDepth")
    return BookSnapshot(
        symbol=symbol,
        ts=ts,
        source="rest",
        bids=_levels(bids, levels, descending=True),
        asks=_levels(asks, levels, descending=False),
    )


def parse_all_depth(payload: Any, ts: float, levels: int = 10) -> dict[str, BookSnapshot]:
    """``/v2/depth/all`` or ``/depth/all`` -> ``{SYMBOL: BookSnapshot}`` (mapping or list of books)."""
    res = _result(payload)
    if isinstance(res, dict):
        entries: Iterable[tuple[Any, Any]] = res.items()
    elif isinstance(res, list):
        entries = ((e.get("symbol"), e) for e in res if isinstance(e, dict))
    else:
        return {}
    out: dict[str, BookSnapshot] = {}
    for sym, book in entries:
        if not sym or not isinstance(book, dict):
            continue
        snap = parse_depth(str(sym).upper(), book, ts, levels)
        if snap.bids or snap.asks:
            out[snap.symbol] = snap
    return dict(sorted(out.items()))


def parse_trades(symbol: str, payload: Any) -> list[Trade]:
    """``/v1/trades`` -> trades sorted by time. ``isBuyOrder=true`` (maker was BUY) -> taker side SELL."""
    res = _result(payload)
    items = _first(res, "latestTrades", "trades") if isinstance(res, dict) else res
    out: list[Trade] = []
    for t in items if isinstance(items, list) else []:
        if not isinstance(t, dict):
            continue
        price, qty = _dec(t.get("price")), _dec(_first(t, "quantity", "qty", "amount"))
        ts = _ts(_first(t, "timestamp", "time", "created_at"))
        if price is None or qty is None or ts is None or qty <= 0:
            continue
        maker_was_buy = _bool(t.get("isBuyOrder"))
        if maker_was_buy is None:
            side = _side(t.get("side"))
        else:
            side = "SELL" if maker_was_buy else "BUY"
        out.append(Trade(symbol=symbol, ts=ts, price=price, qty=qty, side=side))
    out.sort(key=lambda tr: tr.ts)
    return out


def parse_candles(symbol: str, resolution: str, payload: Any) -> list[Candle]:
    """TradingView UDF ``{s, t[], o[], h[], l[], c[], v[]}`` -> candles; ``s != "ok"`` -> ``[]``."""
    res = payload
    if isinstance(res, dict) and "s" not in res:
        res = _result(res)
    if not isinstance(res, dict) or res.get("s") != "ok":
        return []
    cols = [res.get(k) or [] for k in ("t", "o", "h", "l", "c", "v")]
    out: list[Candle] = []
    for t, o, h, lo, c, v in zip(*cols, strict=False):
        values = [_dec(x) for x in (o, h, lo, c, v)]
        ts = _dec(t)
        if ts is None or any(x is None for x in values):
            continue
        o_, h_, l_, c_, v_ = values
        out.append(
            Candle(symbol=symbol, resolution=str(resolution), ts=int(ts), o=o_, h=h_, l=l_, c=c_, v=v_)
        )
    return out


# ---------------------------------------------------------------- account parsers


def parse_balances(payload: Any) -> dict[str, Balance]:
    """Balances from either documented shape.

    * main wallet ``/v1/account/balances``: ``result.balances[ASSET] = {asset, value, locked}`` with
      ``value`` the total, so ``free = value - locked``;
    * credit sub-account ``/v1/account/balances-detail``: ``result[ASSET] = {total, freeze, available}``
      with ``free = available`` and ``locked = freeze``.
    """
    res = _result(payload)
    if isinstance(res, dict) and isinstance(res.get("balances"), dict | list):
        res = res["balances"]
    if isinstance(res, dict):
        entries: Iterable[tuple[Any, Any]] = res.items()
    elif isinstance(res, list):
        entries = ((_first(e, "asset", "symbol"), e) for e in res if isinstance(e, dict))
    else:
        return {}
    out: dict[str, Balance] = {}
    for key, b in entries:
        if not isinstance(b, dict):
            continue
        asset = str(key or _first(b, "asset", "symbol") or "").upper()
        if not asset:
            continue
        if "available" in b or "freeze" in b:
            locked = _dec(b.get("freeze")) or D0
            free = _dec(b.get("available"))
            if free is None:
                free = (_dec(b.get("total")) or D0) - locked
        else:
            locked = _dec(b.get("locked")) or D0
            free = (_dec(_first(b, "value", "total")) or D0) - locked
        out[asset] = Balance(asset=asset, free=max(free, D0), locked=max(locked, D0))
    return dict(sorted(out.items()))


def map_status(
    raw_status: object,
    executed_qty: Decimal | None,
    orig_qty: Decimal | None,
    active: bool | None = None,
) -> OrderStatus:
    """Wallex status string -> ``OrderStatus``. Fully executed orders are FILLED whatever the label.

    ``active`` is only used when the status string is missing; unknown strings map to UNKNOWN.
    """
    executed = executed_qty or D0
    orig = orig_qty or D0
    if orig > 0 and executed >= orig:
        return "FILLED"
    s = _norm(raw_status)
    if not s and active is not None:
        if active:
            return "PARTIALLY_FILLED" if executed > 0 else "NEW"
        return "CANCELED"
    if s in _OPEN:
        return "PARTIALLY_FILLED" if executed > 0 else "NEW"
    if s in _PARTIAL:
        return "PARTIALLY_FILLED"
    if s in _FILLED:
        return "FILLED"
    if s == "FINISHED":  # Wallex: done; fully filled, or the rest was canceled
        return "FILLED" if executed > 0 and executed >= orig else "CANCELED"
    if s in _CANCELED:
        return "CANCELED"
    if s == "REJECTED":
        return "REJECTED"
    if s == "EXPIRED":
        return "EXPIRED"
    return "UNKNOWN"


def _is_maker(f: dict[str, Any]) -> bool | None:
    flag = _bool(_first(f, "isMaker", "is_maker"))
    if flag is not None:
        return flag
    coef, maker, taker = (
        _dec(f.get(k)) for k in ("feeCoefficient", "makerFeeCoefficient", "takerFeeCoefficient")
    )
    if coef is None or maker is None or maker == taker:
        return None
    return coef == maker


def _fill_key(client_id: str, ts: float, price: Decimal, qty: Decimal) -> str:
    """Content-based de-duplication key (the same fill seen via the order or via my-trades)."""
    return f"{client_id}:{int(round(ts * 1000))}:{_plain(price)}:{_plain(qty)}"


def _to_fill(
    f: dict[str, Any], client_id: str, symbol: str, side: Side | None, fallback_ts: float
) -> Fill | None:
    price, qty = _dec(f.get("price")), _dec(_first(f, "quantity", "qty", "executedQty", "amount"))
    if side is None:
        is_buyer = _bool(f.get("isBuyer"))
        side = _side(f.get("side")) or (None if is_buyer is None else ("BUY" if is_buyer else "SELL"))
    if price is None or qty is None or qty <= 0 or side is None:
        return None
    ts = _ts(_first(f, "timestamp", "time", "created_at", "createdAt")) or fallback_ts
    trade_id = _first(f, "id", "tradeId", "trade_id")
    return Fill(
        order_client_id=client_id,
        symbol=symbol,
        side=side,
        price=price,
        qty=qty,
        fee=_dec(f.get("fee")) or D0,
        fee_asset=str(_first(f, "feeAsset", "fee_asset", "commissionAsset") or ""),
        ts=ts,
        is_maker=_is_maker(f),
        trade_id=str(trade_id) if trade_id is not None else _fill_key(client_id, ts, price, qty),
    )


def parse_fills(order_payload: Any) -> list[Fill]:
    """The ``fills[]`` of an order object. ``is_maker`` from ``feeCoefficient == makerFeeCoefficient``."""
    o = _result(order_payload)
    if not isinstance(o, dict):
        return []
    client_id = str(_first(o, "clientOrderId", "client_id") or "")
    symbol = str(o.get("symbol") or "")
    side = _side(o.get("side"))
    fallback_ts = _ts(_first(o, "updated_at", "created_at", "transactTime")) or 0.0
    out: list[Fill] = []
    for f in o.get("fills") or []:
        if isinstance(f, dict):
            fill = _to_fill(f, client_id, str(f.get("symbol") or symbol), side, fallback_ts)
            if fill is not None:
                out.append(fill)
    return out


def parse_my_trades(payload: Any, symbol: str | None = None, since_ts: float | None = None) -> list[Fill]:
    """``/v1/account/trades`` (shape unverified) -> fills at or after ``since_ts``, oldest first.

    Accepts a bare list, ``{trades: [...]}``, ``{latestTrades: [...]}`` or a mapping of lists.
    """
    res = _result(payload)
    named = _first(res, "trades", "latestTrades") if isinstance(res, dict) else None
    if isinstance(named, list):
        items = named
    else:
        items = _dict_items(res)
    out: list[Fill] = []
    for t in items:
        if not isinstance(t, dict):
            continue
        sym = str(t.get("symbol") or symbol or "")
        if symbol and sym != symbol:
            continue
        client_id = str(_first(t, "clientOrderId", "client_id", "clientId", "orderClientId") or "")
        fill = _to_fill(t, client_id, sym, None, 0.0)
        if fill is None or (since_ts is not None and fill.ts < since_ts):
            continue
        out.append(fill)
    out.sort(key=lambda f: f.ts)
    return out


def _synthetic_request(o: dict[str, Any]) -> OrderRequest:
    """An OrderRequest for an order we did not create in this process (reconciliation needs one)."""
    client_id = str(_first(o, "clientOrderId", "client_id") or "")
    otype = _norm(o.get("type"))
    price = _dec(o.get("price"))
    stop = _dec(_first(o, "stopPrice", "stop_Price", "stop_price"))
    return OrderRequest(
        client_id=client_id,
        symbol=str(o.get("symbol") or ""),
        side=_side(o.get("side")) or "BUY",
        type=cast(OrderType, otype if otype in ORDER_TYPES else "LIMIT"),
        qty=_dec(_first(o, "origQty", "quantity")) or D0,
        price=price if price and price > 0 else None,
        stop_price=stop if stop and stop > 0 else None,
        strategy="unknown" if is_ours(client_id) else "foreign",
        purpose="exit",
        decision_id="",
    )


def _avg_price(o: dict[str, Any], executed: Decimal, fills: list[Fill]) -> Decimal | None:
    if executed <= 0:
        return None
    price = _dec(o.get("executedPrice"))
    if price and price > 0:
        return price
    total = _dec(o.get("executedSum"))
    if total and total > 0:
        return total / executed
    qty = sum((f.qty for f in fills), D0)
    if qty > 0:
        return sum((f.price * f.qty for f in fills), D0) / qty
    return None


def parse_order(payload: Any, req: OrderRequest | None, now: float) -> Order:
    """A Wallex order object (or its envelope) -> ``Order``.

    ``req`` is our request when we know it; otherwise a synthetic one is built from the payload
    (strategy ``foreign`` for client ids we did not generate, ``unknown`` for ours). The full
    payload is kept in ``Order.raw``.
    """
    o = _result(payload)
    o = o if isinstance(o, dict) else {}
    side_known = req is not None or _side(o.get("side")) is not None
    req = req if req is not None else _synthetic_request(o)
    fills = parse_fills(o)
    orig = _dec(_first(o, "origQty", "quantity")) or req.qty
    executed = _dec(o.get("executedQty"))
    if executed is None:
        executed = sum((f.qty for f in fills), D0)
    status = map_status(o.get("status"), executed, orig, _bool(o.get("active")))
    if not side_known:
        status = "UNKNOWN"
    if status == "UNKNOWN":  # PRD-01 §5: unknown -> UNKNOWN and log the raw answer
        log.warning(
            "unknown Wallex order status %r for %s; raw=%s",
            o.get("status"),
            req.client_id,
            json.dumps(json_safe(o))[:500],
        )
    fee = _dec(o.get("fee"))
    created = _ts(_first(o, "created_at", "createdAt", "transactTime")) or now
    exchange_id = _first(o, "orderId", "id")
    return Order(
        req=req,
        exchange_id=str(exchange_id) if exchange_id is not None else None,
        status=status,
        filled_qty=executed,
        avg_price=_avg_price(o, executed, fills),
        fee_paid=fee if fee is not None else sum((f.fee for f in fills), D0),
        fee_asset=(fills[0].fee_asset or None) if fills else (str(o.get("feeAsset") or "") or None),
        created_ts=created,
        updated_ts=_ts(_first(o, "updated_at", "updatedAt")) or now,
        raw=json_safe(o),
    )


def _dict_items(res: Any) -> list[dict[str, Any]]:
    """A bare list, ``{orders: [...]}`` or a mapping of lists (``additionalProp1..``) -> flat list of dicts."""
    if isinstance(res, list):
        return [x for x in res if isinstance(x, dict)]
    if isinstance(res, dict):
        return [x for v in res.values() if isinstance(v, list) for x in v if isinstance(x, dict)]
    return []


def order_body(req: OrderRequest) -> dict[str, str]:
    """``POST /v1/account/orders`` body: all strings; ``stop_Price`` only for STOP_* orders."""
    body = {"symbol": req.symbol, "type": req.type, "side": req.side, "quantity": format(req.qty, "f")}
    if req.price is not None:
        body["price"] = format(req.price, "f")
    body["client_id"] = req.client_id
    if req.type in STOP_TYPES:
        if req.stop_price is None:
            raise ValueError(f"{req.type} order {req.client_id} needs a stop_price")
        body["stop_Price"] = format(req.stop_price, "f")
    return body


# ---------------------------------------------------------------- rate limiter


class TokenBucket:
    """``rate`` tokens per second, at most ``burst`` stored; ``acquire`` waits for one token."""

    def __init__(
        self,
        rate: float,
        burst: float | None = None,
        sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.rate = float(rate)
        self.capacity = max(1.0, float(burst if burst is not None else rate))
        self._sleep = sleep
        self._monotonic = monotonic
        self._tokens = self.capacity
        self._last = monotonic()
        self._lock = asyncio.Lock()

    def _refill(self) -> None:
        now = self._monotonic()
        self._tokens = min(self.capacity, self._tokens + max(0.0, now - self._last) * self.rate)
        self._last = now

    async def acquire(self) -> float:
        """Take one token, sleeping as needed. Returns the seconds waited (0 when disabled)."""
        if self.rate <= 0:
            return 0.0
        waited = 0.0
        async with self._lock:
            while True:
                self._refill()
                if self._tokens >= 1 - 1e-9:
                    self._tokens = max(0.0, self._tokens - 1)
                    return waited
                delay = (1 - self._tokens) / self.rate
                waited += delay
                await self._sleep(delay)


# ---------------------------------------------------------------- client


@dataclass
class _Attempt:
    """Outcome of one HTTP attempt: a decoded body, or an error (+ the server's Retry-After)."""

    body: Any = None
    error: WallexAPIError | None = None
    retry_after: float | None = None
    http_status: int | None = None


def _retry_after_s(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:  # HTTP-date form: not worth parsing, use our own backoff
        return None


def _needs_auth(path: str) -> bool:
    return path.startswith("/v1/account") or "sub-accounts" in path


class WallexREST(Exchange):
    """Wallex spot REST API. All methods are async and return ``wta.models`` objects.

    Health counters (``consecutive_errors``, ``errors_total``, ``last_ok_ts``, ``last_error``) count
    failed HTTP attempts; a 404 on a lookup is a normal answer. ``last_error`` never holds the key.
    """

    name = "wallex"

    def __init__(
        self,
        base_url: str = "https://api.wallex.ir",
        api_key: str = "",
        rps: float = 6,
        timeout: float = 8,
        max_retries: int = 4,
        sub_account_client_id: str = "",
        clock: Clock | None = None,
        sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        transport: httpx.AsyncBaseTransport | None = None,
        on_order_event: OrderEventCallback | None = None,
        rng_seed: int = 0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._api_key = api_key
        self.max_retries = max(0, int(max_retries))
        self.sub_account_client_id = sub_account_client_id
        self._clock: Clock = clock or SystemClock()
        self._sleep = sleep
        self._bucket = TokenBucket(rps, sleep=sleep, monotonic=monotonic)
        self._rng = random.Random(rng_seed)
        self._on_order_event = on_order_event
        self._http = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=httpx.Timeout(timeout),
            transport=transport,
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
        )
        self._known: dict[str, OrderRequest] = {}  # requests placed (or registered) in this process
        self._all_depth_path: str | None = None
        self.consecutive_errors = 0
        self.errors_total = 0
        self.last_ok_ts: float | None = None
        self.last_error = ""

    def __repr__(self) -> str:
        return f"WallexREST(base_url={self.base_url!r}, api_key={'<set>' if self._api_key else '<empty>'})"

    # ------------------------------------------------------------ health

    @property
    def ok(self) -> bool:
        """True while the last HTTP attempt succeeded (or none has failed yet)."""
        return self.consecutive_errors == 0

    @property
    def has_api_key(self) -> bool:
        return bool(self._api_key)

    @property
    def api_key(self) -> str:
        """Masked: ``"***"`` when a key is configured, ``""`` otherwise. The key itself is never exposed."""
        return "***" if self._api_key else ""

    def health(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "consecutive_errors": self.consecutive_errors,
            "errors_total": self.errors_total,
            "last_ok_ts": self.last_ok_ts,
            "last_error": self.last_error,
        }

    def _note_ok(self) -> None:
        self.consecutive_errors = 0
        self.last_ok_ts = self._clock.now()

    def _note_error(self, err: WallexAPIError) -> None:
        self.consecutive_errors += 1
        self.errors_total += 1
        self.last_error = str(err)

    # ------------------------------------------------------------ http plumbing

    def _mask(self, text: str) -> str:
        return mask_secrets(text, [self._api_key]) if self._api_key else text

    def _headers(self, auth: bool) -> dict[str, str]:
        if not auth:
            return {}
        headers = {"X-API-Key": self._api_key}
        if self.sub_account_client_id:
            headers["sub-account-client-id"] = self.sub_account_client_id
        return headers

    def _backoff(self, attempt: int, retry_after: float | None) -> float:
        if retry_after is not None:
            return min(retry_after, MAX_BACKOFF_S)
        return min(2.0**attempt + self._rng.uniform(0, 0.25), MAX_BACKOFF_S)

    def _api_error(self, status: int, body: Any, method: str, path: str) -> WallexAPIError:
        code: str | None = None
        message = f"HTTP {status} on {method} {path}"
        if isinstance(body, dict):
            raw_code = body.get("code")
            if raw_code is None and isinstance(body.get("result"), dict):
                raw_code = body["result"].get("code")
            code = None if raw_code is None else str(raw_code)
            if body.get("message"):
                message = str(body["message"])
        elif isinstance(body, str) and body:
            message = f"{message}: {body}"
        return WallexAPIError(status, code, self._mask(message), payload=body)

    async def _attempt(
        self, method: str, path: str, params: dict[str, Any] | None, json_body: Any, auth: bool
    ) -> _Attempt:
        """One rate-limited HTTP attempt. Never raises for HTTP/transport failures."""
        if auth and not self._api_key:
            return _Attempt(
                error=WallexAPIError(401, "no_api_key", f"{method} {path}: WALLEX_API_KEY is not set")
            )
        await self._bucket.acquire()
        try:
            resp = await self._http.request(
                method, path, params=params, json=json_body, headers=self._headers(auth)
            )
        except httpx.RequestError as exc:  # transport errors, and bodies that fail to decode (gzip)
            if isinstance(exc, httpx.TimeoutException):
                code = "timeout"
            elif isinstance(exc, httpx.DecodingError):
                code = "bad_body"
            else:
                code = "transport"
            err = WallexAPIError(
                None, code, self._mask(f"{method} {path}: {type(exc).__name__} {exc}".strip())
            )
            self._note_error(err)
            return _Attempt(error=err)
        text = resp.text
        try:
            body = json.loads(text, parse_float=Decimal) if text.strip() else None
            decoded = True
        except ValueError:
            body, decoded = self._mask(text)[:200], False  # mask first: a cut could split the key
        if auth:  # only account calls carry the key, so only their answers could echo it
            body = _redact(body, self._api_key)
        status = resp.status_code
        if 200 <= status < 300 and decoded and not (isinstance(body, dict) and body.get("success") is False):
            self._note_ok()
            return _Attempt(body=body, http_status=status)
        if 200 <= status < 300 and not decoded:
            # A success we cannot read (proxy page, truncated body) may still have done its job, so
            # it is ambiguous like a timeout: status None makes it retryable, and place_order looks
            # the order up before deciding anything.
            err = WallexAPIError(
                None, "bad_json", self._mask(f"HTTP {status} on {method} {path}: response is not JSON"), body
            )
        else:
            err = self._api_error(status, body, method, path)
        if status == 404:
            self._note_ok()  # the API answered; "not found" is a normal result for lookups
        else:
            self._note_error(err)
        return _Attempt(
            error=err, retry_after=_retry_after_s(resp.headers.get("Retry-After")), http_status=status
        )

    async def _call(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: Any = None,
        auth: bool = False,
        retries: int | None = None,
    ) -> Any:
        """Request with retries on 429/5xx/timeouts; returns the decoded body or raises WallexAPIError."""
        retries = self.max_retries if retries is None else retries
        attempt = 0
        while True:
            result = await self._attempt(method, path, params, json_body, auth)
            err = result.error
            if err is None:
                return result.body
            if not err.retryable:
                raise err
            if attempt >= retries:
                if attempt == 0:
                    raise err
                raise WallexAPIError(
                    err.status, err.code, f"{err.message} (gave up after {attempt} retries)", err.payload
                )
            await self._sleep(self._backoff(attempt, result.retry_after))
            attempt += 1

    async def request(
        self,
        method: str,
        path: str,
        params: dict[str, Any] | None = None,
        json: Any = None,
        auth: bool = False,
    ) -> Any:
        """Raw call returning the decoded body (fractional numbers as Decimal; see ``json_safe``).

        Account paths always authenticate. Only GET/DELETE are retried.
        """
        method = method.upper()
        retries = None if method in ("GET", "DELETE") else 0
        return await self._call(
            method, path, params=params, json_body=json, auth=auth or _needs_auth(path), retries=retries
        )

    def _emit(self, action: str, client_id: str, symbol: str, data: dict[str, Any]) -> None:
        """Report order traffic to the OrderManager (orders.jsonl). A failing callback never breaks trading."""
        if self._on_order_event is None:
            return
        try:
            self._on_order_event(action, client_id, symbol, json_safe(_redact(data, self._api_key)))
        except Exception:
            log.exception("on_order_event callback failed (action=%s, client_id=%s)", action, client_id)

    @staticmethod
    def _error_data(err: WallexAPIError) -> dict[str, Any]:
        return {"status": err.status, "code": err.code, "message": err.message, "payload": err.payload}

    async def close(self) -> None:
        await self._http.aclose()

    async def __aenter__(self) -> WallexREST:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    # ------------------------------------------------------------ public market data

    async def get_markets(self, with_fees: bool = False) -> dict[str, MarketInfo]:
        """Markets from ``/v1/markets``, falling back to ``/hector/web/v1/markets``.

        With ``with_fees`` (and an API key) the account fee rates replace the defaults.
        """
        markets: dict[str, MarketInfo] = {}
        last_err: WallexAPIError | None = None
        for path in MARKETS_PATHS:
            try:
                markets = parse_markets(await self._call("GET", path))
            except WallexAPIError as err:
                # An outage is not a reason to switch: the hector shape has no minQty, minNotional
                # or isMarketTypeEnable, so its defaults would silently loosen the market rules.
                if err.retryable:
                    raise
                last_err = err
                continue
            if markets:
                break
        if not markets:
            raise last_err or WallexAPIError(
                None, "empty", "no markets in /v1/markets or /hector/web/v1/markets"
            )
        if with_fees and self._api_key:
            markets = apply_fees(markets, await self.get_fees())
        return markets

    async def get_depth(self, symbol: str, levels: int = 10) -> BookSnapshot:
        body = await self._call("GET", "/v1/depth", params={"symbol": symbol})
        return parse_depth(symbol, body, self._clock.now(), levels)

    async def get_all_depth(self, levels: int = 10) -> dict[str, BookSnapshot]:
        """All books in one call: ``/v2/depth/all`` then ``/depth/all``; the working path is remembered."""
        paths = list(ALL_DEPTH_PATHS)
        if self._all_depth_path in paths:
            paths.remove(self._all_depth_path)
            paths.insert(0, self._all_depth_path)
        last_err: WallexAPIError | None = None
        for path in paths:
            try:
                body = await self._call("GET", path)
            except WallexAPIError as err:
                last_err = err
                continue
            books = parse_all_depth(body, self._clock.now(), levels)
            if books:
                self._all_depth_path = path
                return books
        if last_err is not None:
            raise last_err
        return {}

    async def get_trades(self, symbol: str) -> list[Trade]:
        body = await self._call("GET", "/v1/trades", params={"symbol": symbol})
        return parse_trades(symbol, body)

    async def get_candles(self, symbol: str, resolution: str, from_ts: float, to_ts: float) -> list[Candle]:
        params = {"symbol": symbol, "resolution": str(resolution), "from": int(from_ts), "to": int(to_ts)}
        body = await self._call("GET", "/v1/udf/history", params=params)
        return parse_candles(symbol, str(resolution), body)

    # ------------------------------------------------------------ account

    async def get_fees(self) -> dict[str, tuple[Decimal, Decimal]]:
        return parse_fees(await self._call("GET", "/v1/account/fee", auth=True))

    async def get_balances(self) -> dict[str, Balance]:
        """Sub-account: ``balances-detail``; main account: ``balances``.

        With a sub-account configured, ``/v1/account/balances`` is used only when the detail endpoint
        does not exist (404/405). Any other failure raises, and a successful detail answer is
        returned as it is (even empty): the main wallet is another account, so falling back to it
        after a network blip would size orders and drawdown on the wrong balances.
        """
        if self.sub_account_client_id:
            try:
                return parse_balances(await self._call("GET", "/v1/account/balances-detail", auth=True))
            except WallexAPIError as err:
                if err.retryable or err.status not in (404, 405):
                    raise
                log.warning("GET /v1/account/balances-detail answered %s; using /v1/account/balances", err.status)
        return parse_balances(await self._call("GET", "/v1/account/balances", auth=True))

    async def get_sub_accounts(self) -> list[dict[str, Any]]:
        """``GET /sub-accounts`` (path prefix undocumented, three are tried) -> list of dicts."""
        last_err: WallexAPIError | None = None
        for path in SUB_ACCOUNT_PATHS:
            try:
                res = _result(await self._call("GET", path, auth=True))
            except WallexAPIError as err:
                last_err = err
                continue
            return json_safe(_dict_items(res))
        raise last_err or WallexAPIError(None, "empty", "no sub-accounts endpoint answered")

    # ------------------------------------------------------------ orders

    def register_request(self, req: OrderRequest) -> None:
        """Let lookups attach our real request (e.g. orders restored from the store after a restart)."""
        self._known[req.client_id] = req

    def _parse_order(self, payload: Any, req: OrderRequest | None = None, *, report_unknown: bool = False) -> Order:
        """Parse with our request attached when known. ``report_unknown`` sends an UNKNOWN status,
        with the raw order, to orders.jsonl (callers that log the raw answer anyway leave it off)."""
        o = _result(payload)
        if req is None and isinstance(o, dict):
            req = self._known.get(str(_first(o, "clientOrderId", "client_id") or ""))
        order = parse_order(o, req, self._clock.now())
        if report_unknown and order.status == "UNKNOWN":
            raw_status = order.raw.get("status") if isinstance(order.raw, dict) else None
            self._emit(
                "status",
                order.client_id,
                order.req.symbol,
                {"found": True, "status": "UNKNOWN", "raw_status": raw_status, "order": order.raw},
            )
        return order

    async def _fetch_order_payload(self, client_id: str) -> dict[str, Any] | None:
        try:
            body = await self._call("GET", f"{ORDERS_PATH}/{quote(client_id, safe='')}", auth=True)
        except WallexAPIError as err:
            if err.status == 404:
                return None
            raise
        res = _result(body)
        return res if isinstance(res, dict) and res else None

    async def _lookup(self, req: OrderRequest) -> Order | None:
        """Resolve an ambiguous placement: does the exchange know ``req.client_id``?"""
        payload = await self._fetch_order_payload(req.client_id)
        order = None if payload is None else self._parse_order(payload, req)
        data: dict[str, Any] = {"found": order is not None}
        if order is not None:
            data.update(status=order.status, order=order.raw)
        self._emit("status", req.client_id, req.symbol, data)
        return order

    async def place_order(self, req: OrderRequest) -> Order:
        """Place ``req``; idempotent through its ``client_id``.

        * HTTP 4xx / ``success: false`` on the first send -> ``OrderRejected`` (never retried).
        * timeout, transport error, unreadable 2xx, 5xx or 429 -> back off, ``GET`` the order by
          ``client_id``; return it if it exists, otherwise re-send (at most ``max_retries`` times).
        * A 4xx after an earlier send of the same ``client_id`` (a re-send here, or an earlier
          ``place_order`` call / ``register_request``) is NOT a rejection: it may be the duplicate
          refusal of an order that did register but is not indexed yet. If the lookup still finds
          nothing, a plain ``WallexAPIError`` (code ``ambiguous``) is raised.
        * Every ``WallexAPIError`` that is not ``OrderRejected`` means the order state is unknown:
          the caller must reconcile by ``client_id`` (nothing is ever re-sent blindly).
        """
        sent_before = req.client_id in self._known
        self._known[req.client_id] = req
        body = order_body(req)
        if sent_before:  # an earlier call may have registered it: look before sending again
            existing = await self._lookup(req)
            if existing is not None:
                return existing
        attempt = 0
        while True:
            self._emit(
                "place",
                req.client_id,
                req.symbol,
                {
                    "request": body,
                    "attempt": attempt,
                    "strategy": req.strategy,
                    "purpose": req.purpose,
                    "decision_id": req.decision_id,
                },
            )
            result = await self._attempt("POST", ORDERS_PATH, None, body, True)
            err = result.error
            if err is None:
                res = _result(result.body)
                if isinstance(res, dict) and res:
                    order = self._parse_order(res, req)
                else:  # accepted without an order object: it is open as far as we know
                    now = self._clock.now()
                    order = Order(
                        req=req, status="NEW", created_ts=now, updated_ts=now, raw=json_safe(result.body)
                    )
                self._emit(
                    "response",
                    req.client_id,
                    req.symbol,
                    {"http_status": result.http_status, "status": order.status, "order": order.raw},
                )
                return order
            if not err.retryable:
                if attempt > 0 or sent_before:
                    # A re-send can be refused as a duplicate client_id of an order that did register.
                    existing = await self._lookup(req)
                    if existing is not None:
                        return existing
                    self._emit(
                        "error",
                        req.client_id,
                        req.symbol,
                        {**self._error_data(err), "attempt": attempt, "ambiguous": True},
                    )
                    raise WallexAPIError(
                        err.status,
                        "ambiguous",
                        f"{err.message} (an earlier send of {req.client_id} may have registered; "
                        "order state unknown, reconcile by client_id)",
                        err.payload,
                    )
                self._emit("error", req.client_id, req.symbol, {**self._error_data(err), "attempt": attempt})
                raise OrderRejected(err.status, err.code, err.message, err.payload)
            action = "timeout" if err.status is None else "error"
            self._emit(action, req.client_id, req.symbol, {**self._error_data(err), "attempt": attempt})
            await self._sleep(self._backoff(attempt, result.retry_after))
            existing = await self._lookup(req)  # never re-send blindly
            if existing is not None:
                return existing
            if attempt >= self.max_retries:
                raise WallexAPIError(
                    err.status,
                    err.code,
                    f"{err.message} (no order found after {attempt + 1} attempts; reconcile by client_id)",
                    err.payload,
                )
            attempt += 1

    async def get_order(self, client_id: str) -> Order | None:
        """``GET /v1/account/orders/{client_id}``; ``None`` when the exchange does not know it (404)."""
        payload = await self._fetch_order_payload(client_id)
        return None if payload is None else self._parse_order(payload, report_unknown=True)

    async def cancel_order(self, client_id: str) -> Order | None:
        """``DELETE /v1/account/orders/{client_id}``; ``None`` on 404.

        If the exchange refuses the cancel (e.g. the order already finished), the order's current
        state is fetched and returned, so the caller learns about a fill instead of an error.
        """
        known = self._known.get(client_id)
        symbol = known.symbol if known else ""
        self._emit("cancel", client_id, symbol, {})
        try:
            body = await self._call("DELETE", f"{ORDERS_PATH}/{quote(client_id, safe='')}", auth=True)
        except WallexAPIError as err:
            if err.status == 404:
                self._emit("cancel_response", client_id, symbol, {"found": False})
                return None
            self._emit("error", client_id, symbol, self._error_data(err))
            if err.retryable:
                raise
            order = await self.get_order(client_id)
            if order is None:
                raise
            self._emit(
                "cancel_response",
                client_id,
                symbol,
                {"found": True, "status": order.status, "order": order.raw},
            )
            return order
        res = _result(body)
        if not isinstance(res, dict) or not res:
            order = await self.get_order(client_id)
        else:
            order = self._parse_order(res)
        data: dict[str, Any] = {"found": order is not None}
        if order is not None:
            data.update(status=order.status, order=order.raw)
        self._emit("cancel_response", client_id, symbol, data)
        return order

    async def get_open_orders(self, symbol: str | None = None) -> list[Order]:
        """``GET /v1/account/openOrders`` with ``page``/``per_page`` until a short (or repeated) page."""
        items: list[dict[str, Any]] = []
        seen: set[str] = set()
        for page in range(1, OPEN_ORDERS_MAX_PAGES + 1):
            params: dict[str, Any] = {"page": page, "per_page": OPEN_ORDERS_PER_PAGE}
            if symbol:
                params["symbol"] = symbol
            page_items = _dict_items(
                _result(await self._call("GET", OPEN_ORDERS_PATH, params=params, auth=True))
            )
            new = 0
            for o in page_items:
                key = str(_first(o, "clientOrderId", "client_id") or "") or json.dumps(
                    json_safe(o), sort_keys=True
                )
                if key not in seen:
                    seen.add(key)
                    items.append(o)
                    new += 1
            if len(page_items) < OPEN_ORDERS_PER_PAGE or new == 0:
                break
        if symbol:
            items = [o for o in items if str(o.get("symbol") or symbol) == symbol]
        return [self._parse_order(o, report_unknown=True) for o in items]

    async def get_my_trades(self, symbol: str | None = None, since_ts: float | None = None) -> list[Fill]:
        """``GET /v1/account/trades`` -> our fills at or after ``since_ts`` (tolerant parser)."""
        params = {"symbol": symbol} if symbol else None
        body = await self._call("GET", "/v1/account/trades", params=params, auth=True)
        return parse_my_trades(body, symbol, since_ts)

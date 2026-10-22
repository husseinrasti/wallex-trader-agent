"""Market metadata: the offline snapshot and the API-with-fallback loader.

``configs/markets_snapshot.yaml`` keeps backtests and synthetic shadow runs working without API
access; live runs always use ``/v1/markets``. ``load_markets`` must fall back to the snapshot when
the REST client fails, and say which source it used.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from wta.config import Config
from wta.exchange import markets as mk
from wta.exchange.base import WallexAPIError
from wta.exchange.markets import (
    DEFAULT_FEES,
    default_fees,
    load_fee_snapshot,
    load_markets,
    load_markets_snapshot,
    snapshot_reference,
)
from wta.models import MarketInfo

WATCHLIST = {
    "USDTTMN", "BTCTMN", "BTCUSDT", "ETHTMN", "ETHUSDT", "SOLTMN", "SOLUSDT",
    "XRPTMN", "XRPUSDT", "DOGETMN", "DOGEUSDT",
}


# ---------------------------------------------------------------- snapshot


def test_snapshot_has_the_eleven_watchlist_markets(cfg: Config) -> None:
    markets = load_markets_snapshot()
    assert set(markets) == WATCHLIST == set(cfg.markets.watchlist)
    assert list(markets) == sorted(markets)  # deterministic iteration
    for sym, m in markets.items():
        assert m.symbol == sym
        assert m.base + m.quote == sym
        assert m.quote in ("TMN", "USDT")
        assert not cfg.markets.is_banned(sym)


def test_snapshot_fees_by_quote() -> None:
    for m in load_markets_snapshot().values():
        if m.quote == "TMN":
            assert (m.maker_fee, m.taker_fee) == (Decimal("0.0025"), Decimal("0.003"))
        else:
            assert (m.maker_fee, m.taker_fee) == (Decimal("0.001"), Decimal("0.0013"))


def test_snapshot_precision_and_minimums() -> None:
    markets = load_markets_snapshot()
    assert markets["BTCUSDT"].tick_decimals == 2  # Overview §2.1
    assert markets["BTCUSDT"].tick == Decimal("0.01")
    assert all(m.tick_decimals == 0 for m in markets.values() if m.quote == "TMN")
    assert all(m.min_notional == Decimal("50000") for m in markets.values() if m.quote == "TMN")
    assert all(m.min_notional == Decimal("1") for m in markets.values() if m.quote == "USDT")
    assert markets["DOGETMN"].min_qty == Decimal("1") and markets["DOGETMN"].step_decimals == 0
    assert markets["BTCTMN"].min_qty == Decimal("0.000001")


def test_soltmn_market_orders_disabled() -> None:
    markets = load_markets_snapshot()
    assert markets["SOLTMN"].market_orders_enabled is False  # isMarketTypeEnable: false
    assert all(m.market_orders_enabled for s, m in markets.items() if s != "SOLTMN")


def test_symbol_fee_override_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    snapshot = {
        "default_by_quote": {"TMN": {"maker": "0.002", "taker": "0.0025"}},
        "symbols": {"BTCTMN": {"maker": "0.001", "taker": "0.0015"}},
    }
    monkeypatch.setattr(mk, "load_fee_snapshot", lambda *a, **k: snapshot)
    markets = load_markets_snapshot()
    assert (markets["BTCTMN"].maker_fee, markets["BTCTMN"].taker_fee) == (Decimal("0.001"), Decimal("0.0015"))
    assert (markets["ETHTMN"].maker_fee, markets["ETHTMN"].taker_fee) == (Decimal("0.002"), Decimal("0.0025"))
    # USDT has no default in this snapshot -> built-in defaults
    assert (markets["BTCUSDT"].maker_fee, markets["BTCUSDT"].taker_fee) == DEFAULT_FEES["USDT"]


def test_default_fees_fallbacks(tmp_path: Path) -> None:
    assert default_fees("USDT") == (Decimal("0.001"), Decimal("0.0013"))
    assert default_fees("TMN", {}) == (Decimal("0.0025"), Decimal("0.003"))
    assert default_fees("BTC") == DEFAULT_FEES["TMN"]  # unknown quote: the conservative (higher) TMN fees
    assert load_fee_snapshot(tmp_path / "missing.yaml") == {"default_by_quote": {}, "symbols": {}}


def test_custom_snapshot_file(tmp_path: Path) -> None:
    path = tmp_path / "snap.yaml"
    path.write_text(
        "markets:\n"
        "  ZZZUSDT: {base: ZZZ, quote: USDT, tick_decimals: 3, step_decimals: 1}\n"
        "  AAATMN: {base: AAA, quote: TMN, tick_decimals: 0, step_decimals: 2, min_qty: '0.5',"
        " min_notional: '50000', market_orders_enabled: false}\n"
    )
    markets = load_markets_snapshot(path)
    assert list(markets) == ["AAATMN", "ZZZUSDT"]
    z = markets["ZZZUSDT"]
    assert (z.min_qty, z.min_notional, z.market_orders_enabled) == (Decimal(0), Decimal(0), True)  # defaults
    assert markets["AAATMN"].market_orders_enabled is False
    assert markets["AAATMN"].min_qty == Decimal("0.5")


def test_snapshot_reference_values() -> None:
    ref = snapshot_reference()
    assert ref["mids"]["USDTTMN"] == Decimal("266739")  # Overview §2.1 mid
    assert ref["mids"]["BTCUSDT"] == Decimal("82877")
    assert ref["spreads"]["BTCTMN"] == Decimal("0.00138")
    assert set(ref["spreads"]) == WATCHLIST
    assert all(isinstance(v, Decimal) for d in ref.values() for v in d.values())


# ---------------------------------------------------------------- load_markets (API with fallback)


def _api_markets() -> dict[str, MarketInfo]:
    return {
        "BTCUSDT": MarketInfo(symbol="BTCUSDT", base="BTC", quote="USDT", tick_decimals=2, step_decimals=6,
                              maker_fee=Decimal("0.001"), taker_fee=Decimal("0.0013")),
        "BTCTMN": MarketInfo(symbol="BTCTMN", base="BTC", quote="TMN", tick_decimals=0, step_decimals=6),
    }


class FakeRest:
    def __init__(self, *, fail: bool = False, has_api_key: bool = False, fees_fail: bool = False) -> None:
        self.fail = fail
        self.has_api_key = has_api_key
        self.fees_fail = fees_fail
        self.calls: list[str] = []

    async def get_markets(self) -> dict[str, MarketInfo]:
        self.calls.append("markets")
        if self.fail:
            raise WallexAPIError(None, "transport", "GET /v1/markets: ConnectError")
        return _api_markets()

    async def get_fees(self) -> dict[str, tuple[Decimal, Decimal]]:
        self.calls.append("fees")
        if self.fees_fail:
            raise WallexAPIError(401, "auth", "bad key")
        return {"BTCTMN": (Decimal("0.002"), Decimal("0.0024"))}


async def test_load_markets_offline_uses_snapshot() -> None:
    markets, source = await load_markets(None)
    assert source == "snapshot"
    assert set(markets) == WATCHLIST


async def test_load_markets_falls_back_when_the_client_raises(caplog: pytest.LogCaptureFixture) -> None:
    rest = FakeRest(fail=True)
    markets, source = await load_markets(rest)
    assert source == "snapshot"
    assert markets == load_markets_snapshot()
    assert rest.calls == ["markets"]
    assert "markets_snapshot.yaml" in caplog.text


async def test_load_markets_falls_back_on_any_exception() -> None:
    class Broken:
        async def get_markets(self) -> Any:
            raise RuntimeError("unexpected payload")

    markets, source = await load_markets(Broken())
    assert source == "snapshot" and set(markets) == WATCHLIST


async def test_load_markets_from_api_without_key() -> None:
    rest = FakeRest(has_api_key=False)
    markets, source = await load_markets(rest)
    assert source == "api"
    assert markets == _api_markets()
    assert rest.calls == ["markets"]  # no key: account fees are not requested


async def test_load_markets_applies_account_fees() -> None:
    rest = FakeRest(has_api_key=True)
    markets, source = await load_markets(rest)
    assert source == "api"
    assert rest.calls == ["markets", "fees"]
    assert (markets["BTCTMN"].maker_fee, markets["BTCTMN"].taker_fee) == (Decimal("0.002"), Decimal("0.0024"))
    assert markets["BTCUSDT"] == _api_markets()["BTCUSDT"]  # no fee row: unchanged
    plain, _ = await load_markets(FakeRest(has_api_key=True), with_fees=False)
    assert plain == _api_markets()


async def test_fee_failure_keeps_api_markets(caplog: pytest.LogCaptureFixture) -> None:
    markets, source = await load_markets(FakeRest(has_api_key=True, fees_fail=True))
    assert source == "api"  # fees are optional; markets are still the API's
    assert markets == _api_markets()
    assert "fee fetch failed" in caplog.text

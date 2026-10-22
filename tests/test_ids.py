"""Deterministic ids: Wallex-safe ``client_id`` values that replay regenerates byte-for-byte,
recognition of our own orders vs foreign ones, and decision/position ids."""

from __future__ import annotations

import re

import pytest

from wta.config import Config
from wta.models import MarketInfo
from wta.utils.ids import (
    OUR_PREFIXES,
    is_ours,
    make_client_id,
    make_decision_id,
    make_position_id,
    strategy_prefix,
)

TS = 1_792_663_200.123  # 2026-10-22T10:00:00.123Z
WALLEX_SAFE = re.compile(r"^[A-Z0-9_]+$")  # letters, digits, underscore (we only emit upper case)


def test_client_id_format() -> None:
    cid = make_client_id("S1", "BTCUSDT", TS, "d_1792663200123_S1_BTCUSDT_1")
    assert re.fullmatch(r"S1_BTCUSDT_\d{13}_[0-9A-F]{4}", cid)
    assert cid.split("_")[2] == "1792663200123"


@pytest.mark.parametrize("strategy", ["S1", "S2", "S3", "FLATTEN", "PROBE", "EXIT"])
def test_client_id_is_wallex_safe_and_ours(markets: dict[str, MarketInfo], strategy: str) -> None:
    for symbol in markets:
        cid = make_client_id(strategy, symbol, TS, "d_1", attempt=2)
        assert WALLEX_SAFE.match(cid), cid
        assert len(cid) <= 40
        assert is_ours(cid)


def test_client_id_upper_cases_the_symbol() -> None:
    assert make_client_id("S1", "btcusdt", TS, "d_1") == make_client_id("S1", "BTCUSDT", TS, "d_1")


def test_client_id_fits_40_chars_for_the_longest_symbols(cfg: Config) -> None:
    longest = max([*cfg.markets.watchlist, *cfg.markets.banned], key=len)
    assert longest == "1BBABYDOGEUSDT"
    for symbol in (longest, "A" * 18):  # 18 chars is the most a 40-char id can hold
        cid = make_client_id("FLATTEN", symbol, TS, "d_1", attempt=99)
        assert len(cid) <= 40
        assert is_ours(cid)


def test_client_id_is_deterministic() -> None:
    first = make_client_id("S1", "BTCUSDT", TS, "d_1", attempt=0)
    assert make_client_id("S1", "BTCUSDT", TS, "d_1", attempt=0) == first


def test_client_id_differs_per_attempt_and_decision() -> None:
    attempts = {make_client_id("S1", "BTCUSDT", TS, "d_1", attempt=n) for n in range(5)}
    assert len(attempts) == 5
    assert make_client_id("S1", "BTCUSDT", TS, "d_2") != make_client_id("S1", "BTCUSDT", TS, "d_1")


@pytest.mark.parametrize(
    "client_id",
    [
        "S1_BTCUSDT_1792663200123_977A",
        "S2_USDTTMN_1792663200123_0000",
        "S3_ETHUSDT_1792663200123_FFFF",
        "FL_SOLTMN_1792663200123_1A2B",
        "EX_BTCTMN_1792663200123_ABCD",
        "PR_BTCUSDT_1792663200123_1234",
    ],
)
def test_is_ours_accepts_our_ids(client_id: str) -> None:
    assert is_ours(client_id)


@pytest.mark.parametrize(
    "client_id",
    [
        "web_123",
        "test-clientId",
        "",
        None,
        "S1-BTCUSDT-1792663200123-977A",  # Overview's hyphen form is not what we send
        "s1_btcusdt_1792663200123_977a",
        "S9_BTCUSDT_1792663200123_977A",  # unknown strategy prefix
        "S1_BTCUSDT_179266320012_977A",  # 12-digit timestamp
        "S1_BTCUSDT_1792663200123_977",  # 3-char nonce
        "S1_BTCUSDT_1792663200123_977A_X",
        "1792663200123",
    ],
)
def test_is_ours_rejects_foreign_ids(client_id: str | None) -> None:
    assert not is_ours(client_id)


@pytest.mark.parametrize(
    "strategy,prefix",
    [
        ("FLATTEN", "FL"),
        ("flatten", "FL"),
        ("FLAT", "FL"),
        ("PROBE", "PR"),
        ("probe_order", "PR"),
        ("S1", "S1"),
        ("s1_crossbook", "S1"),
        ("S2", "S2"),
        ("S3", "S3"),
        ("EXIT", "EX"),
    ],
)
def test_strategy_prefix(strategy: str, prefix: str) -> None:
    assert strategy_prefix(strategy) == prefix
    assert prefix in OUR_PREFIXES


def test_make_decision_id_is_deterministic() -> None:
    did = make_decision_id(TS, "S1", "BTCUSDT", 7)
    assert did == "d_1792663200123_S1_BTCUSDT_7"
    assert make_decision_id(TS, "S1", "BTCUSDT", 7) == did
    assert make_decision_id(TS, "S1", "BTCUSDT", 8) != did


def test_make_position_id() -> None:
    assert make_position_id("S1", "BTCTMN", TS, 1) == "p_S1_BTCTMN_1792663200123_1"

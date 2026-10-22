"""Deterministic IDs.

Wallex accepts ``client_id`` made of letters, digits and underscore only (spot-create-order docs),
so the Overview's ``{strategy}-{symbol}-{ts_ms}-{nonce4}`` is written with ``_``:
``S1_BTCUSDT_1729591234567_A1B2`` (<= 40 chars). The nonce is derived from the decision id and the
placement attempt, so replay regenerates exactly the same ids.
"""

from __future__ import annotations

import hashlib
import re

# strategy prefixes we generate: S1/S2/S3 strategies, FL flatten, EX generic exit, PR api probe
OUR_PREFIXES = ("S1", "S2", "S3", "FL", "EX", "PR")
CLIENT_ID_RE = re.compile(r"^(S1|S2|S3|FL|EX|PR)_[A-Z0-9]{2,18}_\d{13}_[A-Z0-9]{4}$")


def _h(*parts: object, n: int = 4) -> str:
    return hashlib.sha1("|".join(str(p) for p in parts).encode()).hexdigest()[:n].upper()


def strategy_prefix(strategy: str) -> str:
    s = strategy.upper().replace("_", "")
    if s.startswith("FLAT"):
        return "FL"
    if s.startswith("PROBE"):
        return "PR"
    return s[:2]


def make_client_id(strategy: str, symbol: str, ts: float, decision_id: str, attempt: int = 0) -> str:
    ts_ms = int(round(ts * 1000))
    cid = f"{strategy_prefix(strategy)}_{symbol.upper()}_{ts_ms:013d}_{_h(decision_id, attempt)}"
    if len(cid) > 40:
        raise ValueError(f"client_id too long ({len(cid)} > 40): {cid}")
    return cid


def is_ours(client_id: str | None) -> bool:
    return bool(client_id) and CLIENT_ID_RE.match(client_id or "") is not None


def make_decision_id(ts: float, strategy: str, symbol: str, seq: int) -> str:
    """``d_<ts_ms>_<strategy>_<symbol>_<seq>`` — deterministic given the simulated clock."""
    return f"d_{int(round(ts * 1000))}_{strategy}_{symbol}_{seq}"


def make_position_id(strategy: str, symbol: str, ts: float, seq: int) -> str:
    return f"p_{strategy}_{symbol}_{int(round(ts * 1000))}_{seq}"

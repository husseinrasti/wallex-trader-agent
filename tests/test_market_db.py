"""``scripts/market_db.py``: the shipped real market DB (pack, verify, unpack; never overwrites)."""

from __future__ import annotations

import importlib.util
import sqlite3
from pathlib import Path

import pytest

from wta.config import ROOT
from wta.data.store import Store
from wta.models import BookSnapshot, Level


@pytest.fixture(scope="module")
def market_db():
    spec = importlib.util.spec_from_file_location("market_db", ROOT / "scripts" / "market_db.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _market(path: Path, n: int = 5) -> Path:
    with Store(path) as store:
        for k in range(n):
            book = BookSnapshot(symbol="USDTTMN", ts=1_792_678_800.0 + k, source="rest",
                                bids=[Level(price="266700", qty="10")], asks=[Level(price="266760", qty="10")])
            store.add_book(book)
        store.add_event(1_792_678_800.0, "recorder_start", {"symbols": ["USDTTMN"]})
        store.flush()
    return path


def test_pack_verify_unpack_round_trip(market_db, tmp_path: Path) -> None:
    src = _market(tmp_path / "wta.sqlite")
    bundle = tmp_path / "market"
    info = market_db.pack(src, bundle)
    assert sorted(p.name for p in bundle.iterdir()) == ["README.md", "SHA256SUMS", "wta.sqlite.xz"]
    assert (bundle / "SHA256SUMS").read_text() == f"{info['sha256']}  wta.sqlite.xz\n"
    readme = (bundle / "README.md").read_text()
    assert readme.splitlines()[4] == "**Summary**" and "| `books` | 5 |" in readme
    out = tmp_path / "data" / "wta.sqlite"
    assert market_db.main(["--bundle", str(bundle), "--out", str(out)]) == 0
    assert market_db.table_summary(out)["books"]["rows"] == 5
    # never overwrites
    assert market_db.main(["--bundle", str(bundle), "--out", str(out)]) == 2
    # a changed archive fails its checksum (exit 1) and writes nothing
    (bundle / "wta.sqlite.xz").write_bytes((bundle / "wta.sqlite.xz").read_bytes()[:-1] + b"\x00")
    other = tmp_path / "other.sqlite"
    assert market_db.main(["--bundle", str(bundle), "--out", str(other)]) == 1 and not other.exists()
    assert market_db.main(["--bundle", str(tmp_path / "none"), "--check"]) == 2


def test_pack_refuses_account_or_engine_rows(market_db, tmp_path: Path) -> None:
    src = _market(tmp_path / "state.sqlite")
    conn = sqlite3.connect(src)
    conn.execute("INSERT INTO balances (ts, asset, free, locked) VALUES (1, 'TMN', '5000000', '0')")
    conn.commit()
    conn.close()
    with pytest.raises(market_db.MarketDBError, match=r"not market data only: balances \(1 rows\)"):
        market_db.pack(src, tmp_path / "market")
    assert not (tmp_path / "market" / "wta.sqlite.xz").exists()


def test_the_committed_bundle_verifies_and_holds_the_recorded_books(market_db, tmp_path: Path) -> None:
    bundle = ROOT / "deliverables" / "market"
    if not (bundle / "SHA256SUMS").exists():
        pytest.skip("no market bundle in this checkout")
    market_db.verify(bundle)
    summary = market_db.unpack(bundle, tmp_path / "wta.sqlite")
    assert summary["books"]["rows"] == 6131 and summary["books"]["from"] == "2026-10-22T14:20:05Z"
    assert all(int(summary.get(t, {}).get("rows", 0) or 0) == 0 for t in market_db.PRIVATE_TABLES)

"""Web dashboard (``wta web``): the JSON view, the routes, the read-only guarantee, the server.

The log/state fixtures are the TUI's (same writers, same numbers): both dashboards read one
``build_snapshot``, so the web view only has to shape it.
"""

from __future__ import annotations

import hashlib
import json
import threading
import urllib.request
from decimal import Decimal
from pathlib import Path

from wta.cli import build_parser
from wta.dashboard import web
from wta.data.store import Store
from wta.utils.timeutil import SimClock

from .conftest import T0
from .test_tui import _write_health, _write_start, populated  # noqa: F401 - fixture re-export


def _dash(log_dir: Path, db: Path | None, reports: Path | None = None) -> web.Dashboard:
    return web.Dashboard(log_dir, db, reports_dir=reports or web.REPORTS_DIR, clock=lambda: T0)


def _get(d: web.Dashboard, path: str) -> tuple[int, str, bytes]:
    return d.handle(path)


def _json(d: web.Dashboard, path: str) -> dict:
    status, ctype, body = _get(d, path)
    assert status == 200 and ctype == "application/json", (path, status, ctype)
    return json.loads(body)


def test_snapshot_view_has_every_panel(populated: tuple[Path, Path]) -> None:  # noqa: F811
    snap = _json(_dash(*populated), "/api/snapshot")
    assert set(snap) >= {"now", "header", "equity", "markets", "plan", "positions", "decisions", "health"}
    h = snap["header"]
    assert h["mode"] == "shadow" and h["synthetic"] is True and h["commit"].startswith("6094408")
    assert snap["equity"]["equity"] and snap["equity"]["kill_at"] == "5.0%"
    edges = {e["coin"]: e for e in snap["markets"]["edges"]}
    assert edges["BTC"]["tmn_hit"] is True and edges["ETH"]["tmn_hit"] is False
    assert snap["plan"]["offline"] is True
    assert snap["decisions"], "decisions must be listed"
    assert snap["decisions"][0]["time"] >= snap["decisions"][-1]["time"], "newest first"
    assert snap["health"]["rest"] in ("ok", "bad", "off")


def test_empty_sources_never_raise(tmp_path: Path) -> None:
    snap = _json(_dash(tmp_path / "nothing", tmp_path / "missing.sqlite"), "/api/snapshot")
    assert snap["plan"] is None and snap["decisions"] == [] and snap["positions"]["db_found"] is False
    assert _json(_dash(tmp_path / "nothing", None), "/api/equity") == {"points": []}


def test_index_page_is_served_and_self_contained(populated: tuple[Path, Path]) -> None:  # noqa: F811
    status, ctype, body = _get(_dash(*populated), "/")
    page = body.decode()
    assert status == 200 and ctype.startswith("text/html")
    assert "/api/snapshot" in page and "/api/decision/" in page and "/api/reports" in page
    assert "<script src=" not in page and "<link" not in page, "no CDN: the page must work offline"


def test_decision_route_returns_the_full_record(populated: tuple[Path, Path]) -> None:  # noqa: F811
    d = _dash(*populated)
    did = _json(d, "/api/snapshot")["decisions"][0]["id"]
    rec = _json(web.Dashboard(*populated, clock=lambda: T0), f"/api/decision/{did}")  # cold cache
    assert rec["decision_id"] == did and "risk" in rec
    assert _get(d, "/api/decision/does-not-exist")[0] == 404


def test_equity_series_window_and_downsampling(tmp_path: Path) -> None:
    db = tmp_path / "state.sqlite"
    store = Store(db)
    for i in range(2000):
        store.add_equity(T0 - 20000 + i * 10, Decimal("5000000") + i)
    store.close()
    pts = web.equity_series(db, hours=2, now=T0)
    assert 2 <= len(pts) <= web.EQUITY_POINTS + 1
    assert pts[-1][1] == 5000000 + 1999, "the newest row is always kept"
    assert pts[-1][0] - pts[0][0] <= 2 * 3600
    # a stopped run: the window ends at its newest row, not at "now"
    assert web.equity_series(db, hours=2, now=T0 + 10 * 86400)


def test_reports_listing_and_path_guard(tmp_path: Path) -> None:
    reports = tmp_path / "REPORTS"
    (reports / "bt_x").mkdir(parents=True)
    (reports / "bt_x" / "report.md").write_text("# Backtest X\n> SYNTHETIC data\n")
    (reports / "bt_x" / "equity.png").write_bytes(b"\x89PNG fake")
    (reports / "survey.md").write_text("# Survey\nreal\n")
    (reports / "README.md").write_text("# index\n")
    (reports / "secret.env").write_text("KEY=1\n")
    (tmp_path / "outside.md").write_text("# outside\n")
    d = _dash(tmp_path / "logs", None, reports)
    listed = {r["name"]: r for r in _json(d, "/api/reports")["reports"]}
    assert set(listed) == {"bt_x", "survey.md"}
    assert listed["bt_x"]["synthetic"] is True and listed["bt_x"]["pngs"] == ["bt_x/equity.png"]
    assert listed["survey.md"]["synthetic"] is False
    assert _get(d, "/reports/bt_x/equity.png")[:2] == (200, "image/png")
    assert _get(d, "/reports/bt_x/report.md")[0] == 200
    for bad in ("/reports/../outside.md", "/reports/%2e%2e/outside.md", "/reports/secret.env",
                "/reports/bt_x", "/reports//etc/passwd"):
        assert _get(d, bad)[0] == 404, bad


def test_dashboard_is_read_only(populated: tuple[Path, Path]) -> None:  # noqa: F811
    log_dir, db = populated

    def digest() -> dict[str, str]:
        # SQLite's -wal/-shm sidecars may appear for any WAL reader; the data files must not change
        files = [*log_dir.rglob("*"), *db.parent.rglob("*")]
        return {str(p): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in files if p.is_file() and not p.name.endswith(("-wal", "-shm"))}

    before = digest()
    d = _dash(log_dir, db)
    for path in ("/", "/api/snapshot", "/api/equity", "/api/reports", "/nope"):
        d.handle(path)
    assert digest() == before


def test_http_server_serves_json(populated: tuple[Path, Path]) -> None:  # noqa: F811
    from http.server import ThreadingHTTPServer

    server = ThreadingHTTPServer(("127.0.0.1", 0), web.make_handler(_dash(*populated)))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        base = f"http://127.0.0.1:{server.server_address[1]}"
        with urllib.request.urlopen(base + "/api/snapshot", timeout=5) as r:
            assert r.status == 200 and r.headers["Cache-Control"] == "no-store"
            assert json.loads(r.read())["header"]["mode"] == "shadow"
        try:
            urllib.request.urlopen(base + "/missing", timeout=5)
            raise AssertionError("expected 404")
        except urllib.error.HTTPError as e:
            assert e.code == 404
    finally:
        server.shutdown()
        server.server_close()


def test_live_feed_header_is_not_synthetic(tmp_path: Path) -> None:
    log_dir = tmp_path / "logs"
    clock = SimClock(T0 - 600)
    _write_start(log_dir, clock, feed="live")
    clock.set(T0 - 20)
    _write_health(log_dir, clock, extra={"feed": "live"})
    h = _json(_dash(log_dir, None), "/api/snapshot")["header"]
    assert h["feed"] == "live" and h["synthetic"] is False


def test_cli_web_command_parses() -> None:
    args = build_parser().parse_args(["web", "--mode", "shadow", "--host", "0.0.0.0", "--port", "9090"])
    assert args.host == "0.0.0.0" and args.port == 9090 and args.equity_hours == 6.0
    assert args.handler.__name__ == "cmd_web"

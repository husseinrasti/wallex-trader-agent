"""Web dashboard (``python -m wta web``): the terminal dashboard's data in a browser.

It never talks to Wallex and cannot place, change or cancel an order. Apart from the Kill and
Reset-kill buttons (below) it only reads. Every request re-reads the engine's logs and state DB through the TUI's
``build_snapshot`` (so both dashboards always show the same numbers) and returns JSON; the page
(``web_static/index.html``, plain HTML/JS, no CDN) polls it every 2 s.

Routes:

* ``/``                       the page (tabs: Live, Evidence)
* ``/api/snapshot``           everything one frame shows (header, equity, markets + S1 edges, plan,
                              positions, open orders, last decisions, health, events)
* ``/api/equity``             equity curve of the last ``--equity-hours`` from the state DB
* ``/api/decision/<id>``      one full decision record (intent, all risk checks, order, outcome)
* ``/api/reports``            the report folders of ``docs/REPORTS`` with their PNGs
* ``/reports/<path>``         a file of ``docs/REPORTS`` (png, md, csv, json; no path escapes)
* ``POST /api/control/kill``        Kill button: ``request_kill`` on this dashboard's state DB
* ``POST /api/control/reset-kill``  Reset-kill button: ``request_kill_reset`` (same as ``wta reset-kill``)

The two POST routes are the only writes. They never place an order themselves: the engine applies
the request on its next tick (a kill cancels entries and flattens to TMN; a reset lets the
strategies trade again). There is no route that starts an engine or switches it to live: going
live stays a terminal command behind its own guards. ``--read-only`` turns the POST routes off.
Each POST needs the per-process token embedded in the page, a localhost ``Host`` and a same-origin
``Origin``, so another web page open in the browser cannot trigger them.

It binds to 127.0.0.1 by default; ``--host 0.0.0.0`` is for the Docker service only.
"""

from __future__ import annotations

import json
import mimetypes
import secrets
import threading
import time
from collections.abc import Callable, Mapping
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse, urlsplit

from wta import engine as engine_mod
from wta.config import Config, canonical_json
from wta.dashboard.tui import (
    DASH,
    HEARTBEAT_STALE_S,
    OFFLINE_PLAN_MODEL,
    PHASE_NAMES,
    SYNTHETIC_BANNER,
    DashboardSettings,
    DashboardSnapshot,
    _dec,
    _dict,
    _dur,
    _hms,
    _money,
    _pct,
    _price,
    _qty,
    _quote,
    _short,
    _ts,
    build_snapshot,
    s1_diagnostics,
    s1_edges,
)
from wta.data.store import Store
from wta.logging_.decision_log import EventsLog
from wta.utils.timeutil import SystemClock

ROOT = Path(__file__).resolve().parents[3]
STATIC = Path(__file__).resolve().parent / "web_static"
REPORTS_DIR = ROOT / "docs" / "REPORTS"
SERVED_SUFFIXES = {".png", ".md", ".csv", ".json", ".jsonl", ".txt"}
EQUITY_POINTS = 400
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}
TOKEN_PLACEHOLDER = "__WTA_CONTROL_TOKEN__"
MAX_POST_BYTES = 4096
MAX_REASON_CHARS = 300


# ---------------------------------------------------------------- the JSON view of one snapshot


def _f(d: Decimal | None) -> float | None:
    return None if d is None else float(d)


def _equity(snap: DashboardSnapshot) -> dict[str, Any]:
    eq_row, extra, s = snap.equity or {}, snap.extra, snap.settings
    equity = _dec(eq_row.get("equity_tmn")) or _dec((snap.health or {}).get("equity_tmn"))
    peak = _dec(eq_row.get("peak_tmn")) or _dec(extra.get("peak_equity_tmn"))
    dd = _dec(eq_row.get("drawdown_pct"))
    if dd is None:
        dd = _dec((snap.health or {}).get("drawdown_pct"))
    if dd is None and equity is not None and peak:
        dd = max((peak - equity) / peak, Decimal(0))
    initial = snap.initial_equity_tmn
    ret = equity / initial - 1 if equity is not None and initial else None
    fees = _dec(extra.get("fees_paid_tmn"))
    used = _dec(extra.get("fee_budget_used_pct"))
    budget = fees / used if fees is not None and used else initial * s.fee_budget_pct
    if used is None and fees is not None and budget:
        used = fees / budget
    usdt_share = coin_exp = None
    if equity:
        u_val, c_val = _dec(eq_row.get("usdt_tmn_value")), _dec(eq_row.get("coins_tmn_value"))
        usdt_share = None if u_val is None else u_val / equity
        coin_exp = None if c_val is None else c_val / equity
    usdt_share = usdt_share if usdt_share is not None else _dec(extra.get("usdt_share"))
    coin_exp = coin_exp if coin_exp is not None else _dec(extra.get("coin_exposure_pct"))
    daily = _dec(extra.get("daily_pnl_pct"))
    realized = _dec(extra.get("realized_pnl_tmn"))
    target = _dec(_dict(snap.plan).get("usdt_target_share"))
    orders_today = extra.get("orders_today")
    return {
        "equity": _money(equity) if equity is not None else None,
        "return": _pct(ret, 2, signed=True), "return_value": _f(ret),
        "initial": _money(initial),
        "peak": _money(peak),
        "drawdown": _pct(dd), "drawdown_value": _f(dd), "kill_at": _pct(s.max_drawdown_kill_pct, 1),
        "drawdown_level": None if dd is None else (
            "bad" if dd >= s.max_drawdown_kill_pct * Decimal("0.6")
            else "warn" if dd >= s.max_drawdown_kill_pct * Decimal("0.3") else "good"),
        "daily": _pct(daily, 2, signed=True), "daily_value": _f(daily),
        "pause_at": f"-{_pct(s.daily_loss_limit_pct, 1)}",
        "realized": f"{_money(realized, signed=True)} TMN" if realized is not None else DASH,
        "realized_value": _f(realized),
        "fees": f"{_money(fees)} / {_money(budget)}", "fees_used": _pct(used, 1), "fees_used_value": _f(used),
        "usdt_share": _pct(usdt_share, 1), "usdt_target": _pct(target, 1) if target is not None else None,
        "coin_exposure": _pct(coin_exp, 1), "coin_cap": _pct(s.max_coin_exposure_pct, 0),
        "orders_today": f"{orders_today if isinstance(orders_today, int) else DASH} / {s.max_orders_per_day}",
    }


def _markets(snap: DashboardSnapshot, now: float) -> dict[str, Any]:
    books = []
    for sym in snap.watchlist:
        b = snap.books.get(sym)
        if b is None:
            books.append({"symbol": sym, "bid": None, "ask": None, "spread": None, "age": None, "age_s": None})
            continue
        age = now - b.ts
        books.append({
            "symbol": sym, "bid": _price(b.best_bid, _quote(sym)), "ask": _price(b.best_ask, _quote(sym)),
            "spread": _pct(b.spread_pct, 3), "age": _dur(age), "age_s": round(age, 1), "source": b.source,
        })
    edges = []
    rows = s1_edges(snap)
    for r in rows:
        edges.append({
            "coin": r.coin,
            "tmn_cheap": None if r.edge_tmn_cheap is None else round(float(r.edge_tmn_cheap) * 100, 3),
            "tmn_need": None if r.need_tmn_cheap is None else round(float(r.need_tmn_cheap) * 100, 3),
            "tmn_hit": r.hit_tmn_cheap,
            "usdt_cheap": None if r.edge_usdt_cheap is None else round(float(r.edge_usdt_cheap) * 100, 3),
            "usdt_need": None if r.need_usdt_cheap is None else round(float(r.need_usdt_cheap) * 100, 3),
            "usdt_hit": r.hit_usdt_cheap,
            "status": r.status,
        })
    return {
        "books": books,
        "edges": edges,
        "edges_estimated": bool(rows and rows[0].estimated),
        "s1_status": (s1_diagnostics(snap.extra) or {}).get("status"),
    }


def _plan(snap: DashboardSnapshot, now: float) -> dict[str, Any] | None:
    plan = snap.plan
    if not plan:
        return None
    model = str(plan.get("model") or "")
    regimes = []
    for sym in sorted(_dict(plan.get("regimes"))):
        call = _dict(plan["regimes"][sym])
        conf = call.get("confidence")
        regimes.append({
            "symbol": sym, "regime": str(call.get("regime") or "?"),
            "confidence": conf if isinstance(conf, (int, float)) and not isinstance(conf, bool) else None,
            "evidence": _short(call.get("evidence") or "", 220),
        })
    age = None if snap.plan_ts is None else now - snap.plan_ts
    return {
        "model": model,
        "offline": model == OFFLINE_PLAN_MODEL,
        "cached": bool(plan.get("cached")),
        "tools_source": plan.get("tools_source") or "",
        "age": _dur(age), "age_s": age,
        "enabled": [str(s) for s in plan.get("enabled_strategies") or []],
        "size_multiplier": _qty(_dec(plan.get("size_multiplier"))),
        "usdt_target": _pct(_dec(plan.get("usdt_target_share")), 1),
        "s1_margin": _pct(_dec(plan.get("s1_margin_min_pct")), 2),
        "risk_flags": [str(f) for f in plan.get("risk_flags") or []],
        "clamped": sorted(_dict(plan.get("clamped"))),
        "regimes": regimes,
        "notes": str(plan.get("notes") or "").strip(),
        "prompt_hash": plan.get("prompt_hash") or "",
    }


def _positions(snap: DashboardSnapshot, now: float) -> dict[str, Any]:
    positions = []
    for p in snap.positions:
        b = snap.books.get(p.symbol)
        bid = b.best_bid if b is not None else None
        upnl = bid / p.entry_price - 1 if bid is not None and p.entry_price else None
        q = _quote(p.symbol)
        positions.append({
            "strategy": p.strategy, "symbol": p.symbol, "qty": _qty(p.qty), "entry": _price(p.entry_price, q),
            "now": _price(bid, q), "upnl": _pct(upnl, 2, signed=True), "upnl_value": _f(upnl),
            "stop": _price(p.stop_price, q), "target": _price(p.target_price, q), "held": _dur(now - p.entry_ts),
            "mode": str(p.meta.get("mode") or ""),
        })
    orders = []
    for o in sorted(snap.open_orders, key=lambda o: (o.created_ts, o.client_id), reverse=True):
        r = o.req
        orders.append({
            "strategy": r.strategy, "symbol": r.symbol, "side": r.side, "type": r.type, "purpose": r.purpose,
            "filled": f"{_qty(o.filled_qty)}/{_qty(r.qty)}",
            "price": _price(r.price if r.price is not None else r.stop_price, _quote(r.symbol)),
            "status": "PARTIAL" if o.status == "PARTIALLY_FILLED" else o.status,
            "age": _dur(now - o.created_ts), "client_id": r.client_id,
        })
    return {"db_found": snap.db_found, "positions": positions, "orders": orders}


def _risk(rec: dict[str, Any]) -> dict[str, Any]:
    risk = _dict(rec.get("risk"))
    checks = [c for c in risk.get("checks") or [] if isinstance(c, dict)]
    failed = [str(c.get("code")) for c in checks if c.get("passed") is False]
    return {"approved": risk.get("approved"), "failed": failed, "n_checks": len(checks),
            "veto_reason": _short(risk.get("veto_reason") or "", 120)}


def _outcome(rec: dict[str, Any]) -> dict[str, Any]:
    out = rec.get("outcome")
    order = _dict(rec.get("order"))
    if isinstance(out, dict) and out:
        pnl = _dec(out.get("pnl_tmn"))
        if pnl is not None:
            pnl_pct = _dec(out.get("pnl_pct"))
            text = f"{_money(pnl, signed=True)} TMN" + (f" ({_pct(pnl_pct, 2, signed=True)})" if pnl_pct is not None else "")
            if out.get("exit_reason"):
                text += f" {out['exit_reason']}"
            return {"text": text, "level": "good" if pnl > 0 else "bad" if pnl < 0 else ""}
        status = str(out.get("status") or "done")
        return {"text": status, "level": "good" if status == "FILLED" else "warn"}
    if rec.get("kind") == "order":
        if order.get("accepted") is False or str(rec.get("notes") or "").startswith("REFUSED"):
            return {"text": "not sent", "level": "warn"}
        return {"text": "working…", "level": "info"}
    return {"text": DASH, "level": ""}


def _decisions(snap: DashboardSnapshot) -> list[dict[str, Any]]:
    rows = []
    for rec in reversed(snap.decisions):  # newest first
        intent = _dict(rec.get("intent"))
        req = _dict(_dict(rec.get("order")).get("request"))
        side = str(intent.get("side") or req.get("side") or "")
        qty = _dec(_dict(rec.get("risk")).get("adjusted_qty")) or _dec(intent.get("qty")) or _dec(req.get("qty"))
        rows.append({
            "id": str(rec.get("decision_id") or ""),
            "time": _hms(_ts(rec.get("ts"))), "kind": str(rec.get("kind") or ""),
            "strategy": str(rec.get("strategy") or ""), "symbol": str(rec.get("symbol") or ""),
            "side": side, "qty": _qty(qty), "risk": _risk(rec),
            "reason": str(intent.get("reason") or rec.get("notes") or ""),
            "outcome": _outcome(rec),
        })
    return rows


def _health(snap: DashboardSnapshot, now: float) -> dict[str, Any]:
    h, extra = snap.health or {}, snap.extra
    ai = _dict(extra.get("ai"))
    source = extra.get("ai_source") or ai.get("ai_source")
    if source == "offline-rules":
        llm = {"state": "warn", "text": "offline rules (no LLM key)"}
    elif source == "disabled":
        llm = {"state": "off", "text": "AI off (rules-only)"}
    else:
        ok = h.get("llm_ok")
        llm = {"state": "off" if ok is None else "ok" if ok else "bad",
               "text": "n/a" if ok is None else "ok" if ok else "down"}
    mcp = extra.get("mcp_ok")
    if mcp is None and isinstance(extra.get("mcp"), dict):
        mcp = extra["mcp"].get("ok")
    age = snap.heartbeat_age(now)
    crashes = extra.get("tick_crashes")
    task = sum(v for v in _dict(extra.get("task_crashes")).values() if isinstance(v, int))
    restarts = extra.get("restarts")

    def ok(v: Any) -> str:
        return "off" if v is None else "ok" if v else "bad"

    return {
        "heartbeat": f"{_dur(age)} ago" if h else "none yet",
        "heartbeat_stale": bool(h) and age is not None and age > HEARTBEAT_STALE_S or not h,
        "rest": ok(h.get("rest_ok")) if h else "off",
        "ws": ok(h.get("ws_ok")) if h and (extra.get("ws") is not None or h.get("ws_ok")) else "off",
        "llm": llm,
        "mcp": ok(mcp),
        "api_errors": h.get("api_errors_1m") if isinstance(h.get("api_errors_1m"), int) else None,
        "restarts": restarts if isinstance(restarts, int) else snap.restarts,
        "crashes": f"tick {crashes if isinstance(crashes, int) else 0} · task {task}",
        "reasons": [r for r in h.get("reasons") or [] if isinstance(r, str)][:6],
        "events": [{"time": _hms(_ts(e.get("ts"))), "kind": e.get("kind"), "reason": _short(e.get("reason") or "", 160)}
                   for e in snap.recent_events[-6:]],
        "problems": snap.problems[:4],
    }


def snapshot_view(snap: DashboardSnapshot, now: float | None = None) -> dict[str, Any]:
    """Everything one page frame shows, JSON-safe (numbers pre-formatted like the TUI)."""
    now = snap.now if now is None else now
    deadline = snap.deadline_ts
    lifecycle = (snap.lifecycle_event or {}).get("kind")
    return {
        "now": now,
        "header": {
            "mode": snap.mode, "feed": snap.feed, "synthetic": snap.synthetic, "banner": SYNTHETIC_BANNER,
            "commit": snap.git_commit[:12], "tag": snap.git_tag,
            "dirty": bool(snap.extra.get("git_dirty") or snap.start_data.get("git_dirty")),
            "config": snap.config_hash, "markets": snap.extra.get("markets_source") or snap.start_data.get("markets_source"),
            "running": lifecycle != "stop" and not (snap.heartbeat_age(now) or 0) > HEARTBEAT_STALE_S,
            "stopped": lifecycle == "stop",
            "killed": snap.killed, "kill_reason": snap.kill_reason,
            "paused": snap.paused, "pause_reason": snap.pause_reason,
            "flatten": PHASE_NAMES.get(snap.flatten_phase, str(snap.flatten_phase)),
            "deadline": _dur(deadline - now) if deadline is not None else "not set",
            "ai_source": snap.extra.get("ai_source") or _dict(snap.extra.get("ai")).get("ai_source"),
        },
        "equity": _equity(snap),
        "markets": _markets(snap, now),
        "plan": _plan(snap, now),
        "positions": _positions(snap, now),
        "decisions": _decisions(snap),
        "health": _health(snap, now),
        "sources": {"log_dir": snap.log_dir, "state_db": snap.state_db},
    }


# ---------------------------------------------------------------- equity curve & reports


def equity_series(state_db: str | Path | None, hours: float, now: float) -> list[list[float]]:
    """``[[ts, equity_tmn], ...]`` of the last ``hours`` of the run (downsampled to <= EQUITY_POINTS).

    The window ends at the newest row, not at ``now``, so a stopped run still shows its curve.
    """
    if not state_db or not Path(state_db).is_file():
        return []
    from backtest.metrics import read_equity_table

    pts = []
    for r in read_equity_table(state_db, from_ts=None, to_ts=now):
        eq = _dec(r.get("equity_tmn"))
        if r.get("ts") is not None and eq is not None:
            pts.append([float(r["ts"]), float(eq)])
    if pts:
        start = pts[-1][0] - hours * 3600
        pts = [p for p in pts if p[0] >= start]
    step = max(1, -(-len(pts) // EQUITY_POINTS))
    out = pts[::step]
    if pts and out[-1] != pts[-1]:
        out.append(pts[-1])
    return out


def list_reports(reports_dir: Path = REPORTS_DIR) -> list[dict[str, Any]]:
    """Report folders and top-level reports of docs/REPORTS, with their PNGs (relative paths)."""
    out: list[dict[str, Any]] = []
    if not reports_dir.is_dir():
        return out
    for p in sorted(reports_dir.iterdir()):
        if p.is_dir() and (p / "report.md").is_file():
            title = _first_heading(p / "report.md") or p.name
            pngs = sorted(f"{p.name}/{f.name}" for f in p.glob("*.png"))
            md = f"{p.name}/report.md"
        elif p.suffix == ".md" and p.name != "README.md":
            title = _first_heading(p) or p.stem
            pngs = sorted(f.name for f in reports_dir.glob(f"{p.stem}*.png"))
            md = p.name
        else:
            continue
        synthetic = "SYNTHETIC" in (reports_dir / md).read_text(encoding="utf-8", errors="replace")[:4000]
        out.append({"name": p.name, "title": title, "md": md, "pngs": pngs, "synthetic": synthetic})
    return out


def _first_heading(path: Path) -> str:
    try:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines()[:20]:
            if line.startswith("#"):
                return line.lstrip("#").strip()
    except OSError:
        pass
    return ""


def safe_report_path(rel: str, reports_dir: Path = REPORTS_DIR) -> Path | None:
    """The file under docs/REPORTS that ``rel`` names, or None (no escape, served types only)."""
    try:
        base = reports_dir.resolve()
        path = (base / unquote(rel)).resolve()
    except (OSError, ValueError):
        return None
    if base not in path.parents or not path.is_file() or path.suffix not in SERVED_SUFFIXES:
        return None
    return path


# ---------------------------------------------------------------- HTTP server


class Dashboard:
    """Holds the paths and settings; ``handle`` maps a GET path to (status, content type, body)."""

    def __init__(self, log_dir: str | Path, state_db: str | Path | None, cfg: Config | None = None,
                 equity_hours: float = 6.0, reports_dir: Path = REPORTS_DIR,
                 clock: Callable[[], float] = time.time,
                 resolve: Callable[[], tuple[Path, Path | None]] | None = None,
                 controls: bool = True, secret_values: list[str] | None = None) -> None:
        self.log_dir = Path(log_dir)
        self.state_db = state_db
        # ``resolve`` re-picks (log dir, state DB) on every request, so a page left open follows the
        # engine when it restarts on another feed (``wta web`` without --mode/--feed/--log-dir)
        self.resolve = resolve
        self.controls = controls
        self.secret_values = secret_values or []
        self.token = secrets.token_urlsafe(24)  # embedded in the page; every POST must echo it
        self.settings = DashboardSettings.from_config(cfg) if cfg is not None else DashboardSettings()
        self.equity_hours = equity_hours
        self.reports_dir = reports_dir
        self.clock = clock
        self._records: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()

    def _refresh_paths(self) -> None:
        if self.resolve is not None:
            log_dir, state_db = self.resolve()
            self.log_dir, self.state_db = Path(log_dir), state_db

    def snapshot(self) -> dict[str, Any]:
        self._refresh_paths()
        snap = build_snapshot(self.log_dir, self.state_db, now=self.clock(), settings=self.settings)
        with self._lock:
            self._records = {str(r.get("decision_id")): r for r in snap.decisions}
        view = snapshot_view(snap)
        view["controls"] = {
            "enabled": self.controls,
            "state_db": str(self.state_db) if self.state_db else None,
            "db_found": snap.db_found,
            "reset_confirm": f"RESET {snap.mode.upper()}",
        }
        return view

    def handle(self, raw_path: str) -> tuple[int, str, bytes]:
        url = urlparse(raw_path)
        path = url.path
        if path in ("/", "/index.html"):
            page = (STATIC / "index.html").read_text(encoding="utf-8").replace(TOKEN_PLACEHOLDER, self.token)
            return 200, "text/html; charset=utf-8", page.encode()
        if path == "/api/snapshot":
            return self._json(self.snapshot())
        if path == "/api/equity":
            self._refresh_paths()
            hours = float((parse_qs(url.query).get("hours") or [self.equity_hours])[0])
            return self._json({"points": equity_series(self.state_db, hours, self.clock())})
        if path.startswith("/api/decision/"):
            did = unquote(path[len("/api/decision/"):])
            with self._lock:
                rec = self._records.get(did)
            if rec is None:
                self.snapshot()
                with self._lock:
                    rec = self._records.get(did)
            return self._json(rec) if rec is not None else (404, "application/json", b'{"error": "not found"}')
        if path == "/api/reports":
            return self._json({"reports": list_reports(self.reports_dir)})
        if path.startswith("/reports/"):
            file = safe_report_path(path[len("/reports/"):], self.reports_dir)
            if file is None:
                return 404, "text/plain; charset=utf-8", b"not found"
            ctype = mimetypes.guess_type(file.name)[0] or "text/plain"
            if ctype.startswith("text/") or file.suffix in (".md", ".jsonl"):
                ctype = "text/plain; charset=utf-8"
            return 200, ctype, file.read_bytes()
        return 404, "text/plain; charset=utf-8", b"not found"

    @staticmethod
    def _json(obj: Any, status: int = 200) -> tuple[int, str, bytes]:
        return status, "application/json", canonical_json(obj)

    # ------------------------------------------------------------ the two control routes

    def request_allowed(self, headers: Mapping[str, str]) -> str | None:
        """Why a POST must be refused (None when it may run): token, localhost Host, same Origin."""
        headers = {k.lower(): v for k, v in headers.items()}  # HTTP header names are case-insensitive
        if not self.controls:
            return "controls are off (wta web --read-only)"
        if not secrets.compare_digest(headers.get("x-wta-token", ""), self.token):
            return "missing or wrong control token: reload the page"
        host = headers.get("host", "")
        hostname = urlsplit(f"http://{host}").hostname or ""
        if hostname not in LOCAL_HOSTS:
            return f"Host {host!r} is not localhost"
        origin = headers.get("origin")
        if origin is not None and urlsplit(origin).netloc != host:
            return f"Origin {origin!r} is not this dashboard"
        if not headers.get("content-type", "").startswith("application/json"):
            return "Content-Type must be application/json"
        return None

    def handle_post(self, raw_path: str, headers: Mapping[str, str], body: bytes) -> tuple[int, str, bytes]:
        path = urlparse(raw_path).path
        if path not in ("/api/control/kill", "/api/control/reset-kill"):
            return self._json({"ok": False, "error": "not found"}, 404)
        refused = self.request_allowed(headers)
        if refused:
            return self._json({"ok": False, "error": refused}, 403)
        try:
            data = json.loads(body or b"{}")
        except ValueError:
            return self._json({"ok": False, "error": "body is not JSON"}, 400)
        reason = " ".join(str((data or {}).get("reason") or "").split())[:MAX_REASON_CHARS]
        if not reason:
            return self._json({"ok": False, "error": "a reason is required (it is logged for the judges)"}, 400)
        self._refresh_paths()
        if not self.state_db or not Path(self.state_db).is_file():
            return self._json({"ok": False, "error": f"no engine state database at {self.state_db}"}, 409)
        if path == "/api/control/kill":
            return self._kill(reason)
        return self._reset_kill(reason, str(data.get("confirm") or ""))

    def _write(self, fn: Callable[[Store, EventsLog, float], Any]) -> Any:
        clock = SystemClock()
        events = EventsLog(self.log_dir, clock, self.secret_values)
        store = Store(self.state_db)
        try:
            return fn(store, events, clock.now())
        finally:
            store.close()
            events.close()

    def _kill(self, reason: str) -> tuple[int, str, bytes]:
        with self._lock:
            self._write(lambda store, events, now: engine_mod.request_kill(store, events, reason, now))
        return self._json({"ok": True, "message": "kill requested: the engine cancels its entries and flattens "
                           "to TMN on its next tick (a stopped engine does it at its next start)"})

    def _reset_kill(self, reason: str, confirm: str) -> tuple[int, str, bytes]:
        snap = build_snapshot(self.log_dir, self.state_db, now=self.clock(), settings=self.settings)
        expected = f"RESET {snap.mode.upper()}"
        if confirm.strip() != expected:
            return self._json({"ok": False, "error": f"type {expected} to confirm"}, 400)
        if engine_mod.KILL_FILE.exists():
            return self._json({"ok": False, "error": f"the kill file {engine_mod.KILL_FILE} is present: remove it "
                               "first, or the engine kills again at once"}, 409)
        with self._lock:
            record = self._write(lambda store, events, now: engine_mod.request_kill_reset(store, events, reason, now))
        if record is None:
            return self._json({"ok": False, "error": "the kill-switch is not active: nothing to reset"}, 409)
        return self._json({"ok": True, "message": "kill-switch reset requested: the engine applies it on its next "
                           "tick; drawdown is measured from the latest equity",
                           "previous_kill_reason": record.get("previous_kill_reason") or ""})


def make_handler(dashboard: Dashboard) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "wta-dashboard"

        def do_GET(self) -> None:  # noqa: N802 - http.server API
            try:
                status, ctype, body = dashboard.handle(self.path)
            except Exception as exc:  # noqa: BLE001 - the page must keep polling whatever breaks
                status, ctype = 500, "application/json"
                body = json.dumps({"error": f"{type(exc).__name__}: {exc}"}).encode()
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self) -> None:  # noqa: N802 - http.server API
            try:
                length = int(self.headers.get("Content-Length") or 0)
                if length > MAX_POST_BYTES:
                    status, ctype, body = dashboard._json({"ok": False, "error": "body too large"}, 413)
                else:
                    raw = self.rfile.read(length) if length > 0 else b""
                    status, ctype, body = dashboard.handle_post(self.path, dict(self.headers.items()), raw)
                    if status == 200:
                        print(f"web dashboard: {self.path} accepted", flush=True)
            except Exception as exc:  # noqa: BLE001 - answer, never drop the connection
                status, ctype = 500, "application/json"
                body = json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"}).encode()
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, fmt: str, *args: Any) -> None:  # quiet: one line per request is noise
            return None

    return Handler


def serve(dashboard: Dashboard, host: str = "127.0.0.1", port: int = 8080) -> None:
    server = ThreadingHTTPServer((host, port), make_handler(dashboard))
    shown = "localhost" if host in ("0.0.0.0", "127.0.0.1") else host
    access = "Kill / Reset-kill buttons on" if dashboard.controls else "read-only"
    print(f"web dashboard: http://{shown}:{port}  ({access}; logs {dashboard.log_dir}, state DB "
          f"{dashboard.state_db or '-'}; Ctrl-C stops)", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()

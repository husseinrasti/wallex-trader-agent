"""CLI (``python -m wta``) and ``scripts/check_account.py``.

Every exchange answer comes from the recorded fixtures through respx; "unreachable" is a mocked
connection error or a closed port on 127.0.0.1. No test sends anything to api.wallex.ir, and the
engine itself is replaced by a stub wherever ``run`` would start it.
"""

from __future__ import annotations

import functools
import importlib.util
import json
import socket
import sqlite3
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from wta import cli, safety
from wta.config import ROOT, Settings, load_config
from wta.data.store import Store
from wta.logging_.checker import check_logs
from wta.logging_.jsonl import iter_family
from wta.models import HealthRecord
from wta.safety import LiveGuardError

API = "https://api.wallex.ir"
FIXTURES = Path(__file__).parent / "fixtures" / "wallex"
FAKE_KEY = "".join(["cli", "-test-", "q" * 8, "7" * 8])  # built at runtime: no key-like literal
SUB_ID = "c0ffee00-1111-2222-3333-444455556666"
ENV_VARS = (
    "WALLEX_API_KEY",
    "WALLEX_BASE_URL",
    "WALLEX_WS_URL",
    "WALLEX_MCP_URL",
    "WALLEX_SUB_ACCOUNT_CLIENT_ID",
    "WTA_LIVE",
    "WTA_MODE",
)


def fixture(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / name).read_text())


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ENV_VARS:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def use_settings(clean_env: None, monkeypatch: pytest.MonkeyPatch):
    """Install test Settings (never the developer's .env) for the CLI; returns a setter."""

    def install(**values: Any) -> Settings:
        settings = Settings(_env_file=None, **{"wallex_api_key": FAKE_KEY, **values})
        monkeypatch.setattr(cli, "load_settings", lambda: settings)
        return settings

    install()
    return install


@pytest.fixture
def engine_calls(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Replace the engine: ``run`` must decide everything before starting it."""
    calls: list[dict[str, Any]] = []

    async def fake_run_engine(cfg: Any, settings: Any, mode: str, **kwargs: Any) -> int:
        calls.append({"mode": mode, **kwargs})
        return 0

    monkeypatch.setattr(cli, "run_engine", fake_run_engine)
    return calls


# ---------------------------------------------------------------- config


def test_config_show_masks_secrets_and_prints_the_hash(use_settings, capsys) -> None:
    assert cli.main(["config", "show"]) == 0
    out = capsys.readouterr().out
    assert FAKE_KEY not in out
    assert "wallex_api_key: <set>" in out and "openrouter_api_key: <empty>" in out
    assert f"config_hash: {load_config([]).config_hash}" in out
    assert "symbols; --full lists them>" in out and "ATMN" not in out
    assert cli.main(["config", "show", "--full"]) == 0
    assert "- ATMN" in capsys.readouterr().out


def test_config_hash_follows_set_overrides(use_settings, capsys) -> None:
    assert cli.main(["config", "hash"]) == 0
    plain = capsys.readouterr().out.strip()
    assert plain == load_config([]).config_hash
    assert cli.main(["config", "hash", "--set", "risk.max_order_pct_equity=0.2"]) == 0
    changed = capsys.readouterr().out.strip()
    assert changed == load_config(["risk.max_order_pct_equity=0.2"]).config_hash != plain


def test_bad_override_and_unknown_arguments_exit_2(use_settings, capsys) -> None:
    assert cli.main(["config", "hash", "--set", "risk.max_order_pct_equity=lots"]) == 2
    assert "invalid configuration" in capsys.readouterr().err
    assert cli.main(["config", "hash", "--bogus"]) == 2
    assert "unrecognized arguments: --bogus" in capsys.readouterr().err
    assert cli.main([]) == 2


# ---------------------------------------------------------------- markets


def test_markets_lists_the_watchlist(use_settings, capsys) -> None:
    with respx.mock(base_url=API, assert_all_called=False) as router:
        route = router.get("/v1/markets").respond(200, json=fixture("rest_markets.json"))
        code = cli.main(["markets", "--set", "markets.watchlist=[BTCTMN,BTCUSDT,SOLTMN,DOGETMN]"])
    assert code == 0
    out = capsys.readouterr().out
    lines = {line.split()[0]: line for line in out.splitlines() if line.strip()[:3] in ("BTC", "SOL", "DOG")}
    assert lines["BTCUSDT"].split()[1:7] == ["BTC", "USDT", "2", "6", "0.000001", "1"]
    assert lines["SOLTMN"].split()[-2:] == ["NO", "no"]  # market orders disabled, not banned
    assert "not listed" in lines["DOGETMN"]
    assert "not listed on the exchange: DOGETMN" in out
    assert "x-api-key" not in route.calls.last.request.headers  # public endpoint: no key sent


def test_markets_exits_1_when_a_watchlist_symbol_is_banned(use_settings, capsys) -> None:
    with respx.mock(base_url=API, assert_all_called=False) as router:
        router.get("/v1/markets").respond(200, json=fixture("rest_markets.json"))
        code = cli.main(["markets", "--set", "markets.watchlist=[BTCTMN,ATMN]"])
    assert code == 1
    out = capsys.readouterr().out
    assert "BANNED symbols in the watchlist" in out and "ATMN" in out


def test_markets_exits_2_when_the_api_is_unreachable(use_settings, capsys) -> None:
    with respx.mock(base_url=API, assert_all_called=False) as router:
        router.get("/v1/markets").mock(side_effect=httpx.ConnectError("connection refused"))
        router.get("/hector/web/v1/markets").mock(side_effect=httpx.ConnectError("connection refused"))
        code = cli.main(["markets"])
    assert code == 2
    err = capsys.readouterr().err.strip().splitlines()
    assert len(err) == 1 and err[0].startswith("error: cannot fetch markets from https://api.wallex.ir")


# ---------------------------------------------------------------- account


def test_account_prints_balances_and_fees_without_the_key(use_settings, capsys) -> None:
    with respx.mock(base_url=API, assert_all_called=True) as router:
        balances = router.get("/v1/account/balances").respond(200, json=fixture("rest_balances.json"))
        router.get("/v1/account/fee").respond(200, json=fixture("rest_fee.json"))
        code = cli.main(["account"])
    assert code == 0
    out = capsys.readouterr().out
    assert FAKE_KEY not in out
    assert balances.calls.last.request.headers["x-api-key"] == FAKE_KEY
    tmn = next(line for line in out.splitlines() if line.strip().startswith("TMN"))
    assert tmn.split() == ["TMN", "4750000", "250000", "5000000"]
    assert "zero balances hidden" in out  # ETH is zero
    btctmn = next(line for line in out.splitlines() if line.strip().startswith("BTCTMN"))
    assert btctmn.split() == ["BTCTMN", "0.250%", "0.300%"]


def test_account_needs_a_key(use_settings, capsys) -> None:
    use_settings(wallex_api_key="")
    assert cli.main(["account"]) == 2
    assert "WALLEX_API_KEY is not set" in capsys.readouterr().err


# ---------------------------------------------------------------- feed / record / backfill


def test_feed_prints_labelled_best_bid_and_ask(use_settings, capsys) -> None:
    with respx.mock(base_url=API, assert_all_called=False) as router:
        router.get("/v1/depth").respond(200, json=fixture("rest_depth.json"))
        code = cli.main(
            ["feed", "btctmn", "--no-ws", "--seconds", "0.4", "--interval", "0.2", "--set", "data.rest_poll_interval_s=0.05"]
        )
    assert code == 0
    lines = [line for line in capsys.readouterr().out.splitlines() if not line.startswith("#")]
    assert len(lines) == 2
    ts, source, bid, ask, spread = lines[-1].split()[:5]
    assert ts.endswith("Z") and source == "rest"
    assert (bid, ask, spread) == ("22075699081", "22106074058", "0.1375%")


def test_feed_without_data_exits_2(use_settings, capsys) -> None:
    with respx.mock(base_url=API, assert_all_called=False) as router:
        router.get("/v1/depth").mock(side_effect=httpx.ConnectError("connection refused"))
        code = cli.main(["feed", "BTCTMN", "--no-ws", "--seconds", "0.2", "--interval", "0.1"])
    assert code == 2
    captured = capsys.readouterr()
    assert "waiting for data" in captured.out and "no data received for BTCTMN" in captured.err


def test_record_writes_books_and_candles(use_settings, capsys, tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)  # data/ and logs/ are relative to the working directory
    symbols_file = tmp_path / "markets.yaml"
    symbols_file.write_text("watchlist: [BTCTMN, USDTTMN]\n")
    with respx.mock(base_url=API, assert_all_called=False) as router:
        router.get("/v2/depth/all").respond(200, json=fixture("rest_depth_all.json"))
        router.get("/v1/udf/history").respond(200, json=fixture("rest_udf.json"))
        code = cli.main(
            ["record", "--symbols-from", str(symbols_file), "--no-ws", "--seconds", "0.5",
             "--set", "data.rest_poll_interval_s=0.1"]
        )
    assert code == 0
    assert "rows written: books=" in capsys.readouterr().out
    store = Store.open_readonly(tmp_path / "data" / "wta.sqlite")
    assert set(store.book_counts_by_symbol()) == {"BTCTMN", "USDTTMN"}
    assert store.counts()["candles"] > 0
    store.close()
    assert list((tmp_path / "logs").glob("recorder-*.jsonl"))


def test_backfill_upserts_candles_and_fails_fast_when_unreachable(use_settings, capsys, tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    with respx.mock(base_url=API, assert_all_called=False) as router:
        history = router.get("/v1/udf/history").respond(200, json=fixture("rest_udf.json"))
        code = cli.main(["backfill", "--symbols", "BTCUSDT", "--days", "1"])
    assert code == 0 and history.call_count == 2  # the reachability probe + one 1-day window
    row = next(line for line in capsys.readouterr().out.splitlines() if line.strip().startswith("BTCUSDT"))
    assert row.split() == ["BTCUSDT", "3"]

    with respx.mock(base_url=API, assert_all_called=False) as router:
        history = router.get("/v1/udf/history").mock(side_effect=httpx.ConnectError("connection refused"))
        code = cli.main(["backfill", "--symbols", "BTCUSDT", "--days", "30"])
    assert code == 2 and history.call_count == 1
    assert "cannot fetch candles" in capsys.readouterr().err


# ---------------------------------------------------------------- run + live guard


NO_LIVE_FLAGS = {"i_understand_live": False, "allow_dirty": False}


def test_run_defaults_to_shadow(use_settings, engine_calls) -> None:
    assert cli.main(["run", "--seconds", "5"]) == 0
    assert engine_calls == [{"mode": "shadow", "seconds": 5.0, "log_dir": "logs", **NO_LIVE_FLAGS}]


def test_run_live_without_flags_exits_2(use_settings, engine_calls, monkeypatch, capsys) -> None:
    monkeypatch.setenv("WTA_LIVE", "1")
    assert cli.main(["run", "--mode", "live"]) == 2
    assert "--i-understand-live" in capsys.readouterr().err
    assert engine_calls == []


def test_run_live_with_flag_but_without_wta_live_exits_2(use_settings, engine_calls, capsys) -> None:
    assert cli.main(["run", "--mode", "live", "--i-understand-live"]) == 2
    assert "WTA_LIVE=1" in capsys.readouterr().err
    assert engine_calls == []


def test_wta_live_from_dotenv_settings_does_not_count(use_settings, engine_calls, capsys) -> None:
    use_settings(wta_live=1)  # as if WTA_LIVE=1 were in .env
    assert cli.main(["run", "--mode", "live", "--i-understand-live"]) == 2
    assert engine_calls == []


def test_run_live_refuses_a_dirty_tree_unless_allowed(use_settings, engine_calls, monkeypatch, capsys) -> None:
    monkeypatch.setenv("WTA_LIVE", "1")
    monkeypatch.setattr(safety, "tree_is_dirty", lambda root=None: True)
    assert cli.main(["run", "--mode", "live", "--i-understand-live"]) == 2
    assert "dirty" in capsys.readouterr().err and engine_calls == []
    assert cli.main(["run", "--mode", "live", "--i-understand-live", "--allow-dirty"]) == 0
    assert engine_calls[-1] == {
        "mode": "live", "seconds": None, "log_dir": "logs", "i_understand_live": True, "allow_dirty": True
    }  # the flags reach the engine, which checks the guards again


def test_run_live_needs_an_api_key(use_settings, engine_calls, monkeypatch, capsys) -> None:
    use_settings(wallex_api_key="")
    monkeypatch.setenv("WTA_LIVE", "1")
    assert cli.main(["run", "--mode", "live", "--i-understand-live", "--allow-dirty"]) == 2
    assert "WALLEX_API_KEY" in capsys.readouterr().err and engine_calls == []


def test_run_live_starts_when_every_guard_passes(use_settings, engine_calls, monkeypatch) -> None:
    monkeypatch.setenv("WTA_LIVE", "1")
    monkeypatch.setattr(safety, "tree_is_dirty", lambda root=None: False)
    assert cli.main(["run", "--mode", "live", "--i-understand-live", "--seconds", "1"]) == 0
    assert engine_calls == [
        {"mode": "live", "seconds": 1.0, "log_dir": "logs", "i_understand_live": True, "allow_dirty": False}
    ]


def test_live_flags_cannot_be_abbreviated(use_settings, engine_calls, monkeypatch, capsys) -> None:
    monkeypatch.setenv("WTA_LIVE", "1")
    monkeypatch.setattr(safety, "tree_is_dirty", lambda root=None: False)
    _, extra = cli.build_parser().parse_known_args(["run", "--mode", "live", "--i", "--al"])
    assert extra == ["--i", "--al"]  # prefixes are not matched to --i-understand-live / --allow-dirty
    assert cli.main(["run", "--mode", "live", "--i", "--al"]) == 2
    assert "unrecognized arguments: --i --al" in capsys.readouterr().err and engine_calls == []
    assert cli.main(["run", "--mode", "live", "--i-understand"]) == 2 and engine_calls == []


def test_run_reports_a_guard_failure_raised_by_the_engine(use_settings, monkeypatch, capsys) -> None:
    async def refusing_engine(*args: Any, **kwargs: Any) -> int:
        raise LiveGuardError("refusing to start live mode: the git working tree is dirty")

    monkeypatch.setenv("WTA_LIVE", "1")
    monkeypatch.setattr(safety, "tree_is_dirty", lambda root=None: False)
    monkeypatch.setattr(cli, "run_engine", refusing_engine)
    assert cli.main(["run", "--mode", "live", "--i-understand-live"]) == 2
    assert "refusing to start live mode" in capsys.readouterr().err


# ---------------------------------------------------------------- logs / db / placeholders


def test_logs_check_on_a_directory(use_settings, capsys, tmp_path) -> None:
    assert cli.main(["logs", "check", "--dir", str(tmp_path)]) == 0
    assert "no log files found" in capsys.readouterr().out

    record = HealthRecord(ts="2026-10-22T10:00:00.000Z", mode="shadow", git_commit="abc", config_hash="def")
    (tmp_path / "health.jsonl").write_text(record.model_dump_json() + "\n")
    assert cli.main(["logs", "check", "--dir", str(tmp_path)]) == 0

    (tmp_path / "decisions.jsonl").write_text('{"kind": "intent", "decision_id": "d1"}\nnot json\n')
    assert cli.main(["logs", "check", "--dir", str(tmp_path)]) == 1
    out = capsys.readouterr().out
    assert "decisions.jsonl: 2 lines, 2 error(s)" in out and out.rstrip().endswith("FAILED")


def test_logs_check_secrets_scan(use_settings, capsys, tmp_path) -> None:
    for name in ("logs", "deliverables", "REPORTS"):
        (tmp_path / name).mkdir()
    args = ["logs", "check", "--dir", str(tmp_path / "logs"), "--secrets", "--deliverables", str(tmp_path / "deliverables"),
            "--reports", str(tmp_path / "REPORTS")]
    assert cli.main(args) == 0
    assert "secrets scan: clean" in capsys.readouterr().out
    (tmp_path / "deliverables" / "notes.md").write_text(f"oops {FAKE_KEY}\n")
    assert cli.main(args) == 1
    out = capsys.readouterr().out
    assert "configured secret wallex_api_key" in out and FAKE_KEY not in out
    (tmp_path / "deliverables" / "notes.md").unlink()
    (tmp_path / "REPORTS" / "api_probe.md").write_text(f"raw payload echoing {FAKE_KEY}\n")  # check_account's report
    assert cli.main(args) == 1
    out = capsys.readouterr().out
    assert "api_probe.md:1: configured secret wallex_api_key" in out and FAKE_KEY not in out


def test_db_vacuum(use_settings, capsys, tmp_path) -> None:
    path = tmp_path / "wta.sqlite"
    with Store(path) as store:
        store.add_event(1.0, "start", {"x": 1})
    assert cli.main(["db", "vacuum", "--path", str(path)]) == 0
    assert "vacuumed" in capsys.readouterr().out
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 1
    assert cli.main(["db", "vacuum", "--path", str(tmp_path / "missing.sqlite")]) == 1


@pytest.mark.parametrize("name", sorted(cli.PLACEHOLDERS))
def test_placeholders_exit_3(use_settings, capsys, name: str) -> None:
    assert cli.main([name, "--from", "2026-09-22"]) == 3  # future options are accepted
    assert f"not implemented yet ({cli.PLACEHOLDERS[name][0]})" in capsys.readouterr().out


# ---------------------------------------------------------------- scripts/check_account.py


@pytest.fixture
def check_account(clean_env: None, monkeypatch: pytest.MonkeyPatch):
    """The script as a module, plus a setter for the Settings it loads."""
    spec = importlib.util.spec_from_file_location("check_account", ROOT / "scripts" / "check_account.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    def install(**values: Any) -> Settings:
        settings = Settings(_env_file=None, **{"wallex_api_key": FAKE_KEY, **values})
        monkeypatch.setattr(module, "load_settings", lambda: settings)
        return settings

    install()
    return module, install


def _mcp_server(balances: dict[str, Any], calls: list[dict[str, Any]]):
    """An MCP mock with the two credit tools; records every tools/call."""
    tool_list = [{"name": n} for n in ("wallex_basic_get_markets", "wallex_spot_post_account_orders")]
    tool_list.append({"name": "wallex_credit_active_sub_accounts_list", "inputSchema": {"type": "object"}})
    tool_list.append(
        {
            "name": "wallex_credit_sub_account_balances_details",
            "inputSchema": {"type": "object", "properties": {"clientId": {"type": "string"}}, "required": ["clientId"]},
        }
    )

    def text(obj: Any) -> dict[str, Any]:
        return {"content": [{"type": "text", "text": json.dumps(obj)}]}

    def handler(request: httpx.Request) -> httpx.Response:
        msg = json.loads(request.content)
        if "id" not in msg:
            return httpx.Response(202)
        method = msg["method"]
        if method == "initialize":
            result: Any = {"protocolVersion": "2025-06-18", "serverInfo": {"name": "wallex", "version": "2"}}
        elif method == "tools/list":
            result = {"tools": tool_list}
        else:
            calls.append(msg["params"])
            if msg["params"]["name"] == "wallex_credit_active_sub_accounts_list":
                result = text({"result": [{"clientId": SUB_ID, "title": "challenge", "isActive": True}]})
            else:
                result = text({"result": balances})
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": msg["id"], "result": result})

    return handler


def _mock_read_only_api(router: respx.MockRouter) -> None:
    usdttmn = {"result": fixture("rest_depth_all.json")["result"]["USDTTMN"], "success": True}
    router.get("/v1/depth", params={"symbol": "USDTTMN"}).respond(200, json=usdttmn)  # before the generic one
    router.get("/v1/markets").respond(200, json=fixture("rest_markets.json"))
    router.get("/v1/account/fee").respond(200, json=fixture("rest_fee.json"))
    router.get("/v1/account/balances").respond(200, json=fixture("rest_balances.json"))
    router.get("/v1/account/balances-detail").respond(200, json=fixture("rest_balances_detail.json"))
    router.get("/v2/depth/all").respond(200, json=fixture("rest_depth_all.json"))
    router.get("/v1/depth").respond(200, json=fixture("rest_depth.json"))
    router.get("/sub-accounts").respond(200, json=fixture("rest_sub_accounts.json"))


def test_check_account_report_warns_about_routing(check_account, tmp_path, capsys) -> None:
    module, _ = check_account
    out_file = tmp_path / "REPORTS" / "api_probe.md"
    mcp_calls: list[dict[str, Any]] = []
    mcp_balances = {"TMN": {"total": "4000000", "freeze": "0", "available": "4000000"}}
    with respx.mock(base_url=API, assert_all_called=False) as router:
        _mock_read_only_api(router)
        orders = router.post("/v1/account/orders").respond(500)
        router.post("/mcp").mock(side_effect=_mcp_server(mcp_balances, mcp_calls))
        code = module.main(["--out", str(out_file), "--timeout", "2"])

    report = out_file.read_text()
    assert code == 1  # warnings were raised
    assert report == capsys.readouterr().out.rstrip("\n") + "\n"
    assert FAKE_KEY not in report and "API key: set (never printed)" in report
    assert "REST reachable: yes (6 markets" in report
    assert "| BTCTMN | 0.250% | 0.300% |" in report  # 1. fees
    # 2. equity at best bids: 5,000,000 TMN + 1.25 USDT x 266,720 + 0.00015 BTC x 22,075,699,081
    assert "**Equity estimate: 8,644,755 TMN** at best bids" in report
    assert "tools/list: 4 tools (1 state-changing, never called)" in report  # 3. MCP
    assert [c["name"] for c in mcp_calls] == [module.MCP_SUB_ACCOUNTS_TOOL, module.MCP_SUB_BALANCES_TOOL]
    assert mcp_calls[1]["arguments"] == {"clientId": SUB_ID}
    assert f"| Wallex trading challenge | {SUB_ID} |" in report
    assert "WARNING: WALLEX_SUB_ACCOUNT_CLIENT_ID IS EMPTY BUT CREDIT SUB-ACCOUNTS EXIST" in report
    assert "WARNING: MCP SUB-ACCOUNT BALANCES DIFFER FROM REST BALANCES" in report and "TMN: REST 5000000 vs MCP 4000000" in report
    assert "| SOLTMN | 0 | 2 | 0.01 | 50000 | NO | no |" in report  # 4. markets
    assert "Not run. Pass `--probe-order`" in report  # 5. no orders without the flag
    assert not orders.called


def test_check_account_survives_an_unreachable_api(check_account, tmp_path) -> None:
    module, install = check_account
    port = free_port()
    install(wallex_base_url=f"http://127.0.0.1:{port}", wallex_mcp_url=f"http://127.0.0.1:{port}/mcp")
    out_file = tmp_path / "api_probe.md"
    assert module.main(["--out", str(out_file), "--timeout", "2"]) == 1
    report = out_file.read_text()
    assert "REST reachable: **NO**" in report and FAKE_KEY not in report
    sections = report.split("\n## ")
    assert [s.split("\n")[0][:2] for s in sections[1:]] == ["1.", "2.", "3.", "4.", "5.", "Su"]
    assert "skipped: REST API unreachable" in sections[1] and "skipped: REST API unreachable" in sections[2]
    assert "MCP server unreachable" in sections[3]
    assert "market rules unavailable" in sections[4]


def _order(body: dict[str, Any], status: str) -> dict[str, Any]:
    return {
        "result": {
            "symbol": body["symbol"], "type": body["type"], "side": body["side"],
            "clientOrderId": body["client_id"], "price": body["price"], "origQty": body["quantity"],
            "executedQty": "0", "status": status, "active": status == "NEW", "fills": [],
            "stopPrice": body.get("stop_Price"), "created_at": "2026-10-22T10:00:00Z",
        },
        "success": True,
    }


def test_check_account_probe_places_fetches_and_always_cancels(check_account, tmp_path) -> None:
    module, install = check_account
    install(wallex_sub_account_client_id=SUB_ID)
    bodies: dict[str, dict[str, Any]] = {}

    def place(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if body["type"] == "STOP_LIMIT":
            return httpx.Response(422, json={"success": False, "code": "invalid_type", "message": "stop orders disabled"})
        bodies[body["client_id"]] = body
        return httpx.Response(201, json=_order(body, "NEW"))

    def lookup(request: httpx.Request, status: str) -> httpx.Response:
        cid = request.url.path.rsplit("/", 1)[-1]
        if cid not in bodies:
            return httpx.Response(404, json={"success": False, "message": "not found"})
        return httpx.Response(200, json=_order(bodies[cid], status))

    with respx.mock(base_url=API, assert_all_called=False) as router:
        _mock_read_only_api(router)
        router.post("/mcp").respond(503)
        posted = router.post("/v1/account/orders").mock(side_effect=place)
        router.get(url__regex=r"/v1/account/orders/\w+$").mock(side_effect=lambda r: lookup(r, "NEW"))
        cancels = router.delete(url__regex=r"/v1/account/orders/\w+$").mock(side_effect=lambda r: lookup(r, "CANCELED"))
        code = module.main(
            ["--out", str(tmp_path / "probe.md"), "--probe-order", "--yes", "--log-dir", str(tmp_path / "logs")]
        )

    assert code == 1  # the MCP section failed
    sent = [json.loads(c.request.content) for c in posted.calls]
    limit, stop = sent
    assert {k: limit[k] for k in ("symbol", "type", "side", "price", "quantity")} == {
        "symbol": "USDTTMN", "type": "LIMIT", "side": "BUY", "price": "240048", "quantity": "0.22"
    }  # 90 % of the 266,720 best bid; 50,000 / 240,048 rounded up to 0.21, plus one step
    assert "stop_Price" not in limit and limit["client_id"].startswith("PR_USDTTMN_")
    assert {k: stop[k] for k in ("type", "side", "stop_Price", "price", "quantity")} == {
        "type": "STOP_LIMIT", "side": "SELL", "stop_Price": "226712", "price": "306772", "quantity": "0.18"
    }  # USDT 1.25 covers a SELL: stop 15 % under the 266,720 bid, limit 15 % over the 266,758 ask
    assert Decimal(stop["price"]) > Decimal("266758") * Decimal("1.15")  # rests even if the stop triggers
    assert cancels.call_count == 2  # a cancel is attempted for the rejected order too
    report = (tmp_path / "probe.md").read_text()
    limit_row = next(line for line in report.splitlines() if line.startswith("| LIMIT |"))
    stop_row = next(line for line in report.splitlines() if line.startswith("| STOP_LIMIT |"))
    assert "| yes | NEW |" in limit_row and "| CANCELED |" in limit_row
    assert "| no |" in stop_row and "HTTP 422 invalid_type: stop orders disabled" in stop_row
    assert "nothing to cancel (not found)" in stop_row
    actions = [r["action"] for r in iter_family(tmp_path / "logs", "orders", strict=True)]
    assert actions[:2] == ["place", "response"] and "cancel_response" in actions and "error" in actions
    assert check_logs(tmp_path / "logs").ok


def test_check_account_probe_needs_the_typed_confirmation(check_account, tmp_path, monkeypatch) -> None:
    module, install = check_account
    install(wallex_sub_account_client_id=SUB_ID)  # routing is fine, so only the prompt can stop the probe
    monkeypatch.setattr("builtins.input", lambda prompt="": "yes")  # not the word PROBE
    with respx.mock(base_url=API, assert_all_called=False) as router:
        _mock_read_only_api(router)
        router.post("/mcp").respond(503)
        posted = router.post("/v1/account/orders").respond(201, json={})
        module.main(["--out", str(tmp_path / "probe.md"), "--probe-order", "--log-dir", str(tmp_path / "logs")])
    assert not posted.called
    assert "Aborted at the confirmation prompt; no order was sent." in (tmp_path / "probe.md").read_text()


class NoSleep:
    """Replaces the REST client's backoff sleep in the probe tests."""

    def __init__(self) -> None:
        self.delays: list[float] = []

    async def __call__(self, delay: float) -> None:
        self.delays.append(delay)


def test_check_account_probe_refuses_when_routing_warned(check_account, tmp_path, monkeypatch) -> None:
    module, _ = check_account  # no WALLEX_SUB_ACCOUNT_CLIENT_ID, while /sub-accounts lists one
    monkeypatch.setattr(module, "PROBE_RECHECK_DELAYS_S", (0.0,))
    not_found = {"success": False, "message": "not found"}
    with respx.mock(base_url=API, assert_all_called=False) as router:
        _mock_read_only_api(router)
        router.post("/mcp").respond(503)
        posted = router.post("/v1/account/orders").respond(201, json={})
        router.get(url__regex=r"/v1/account/orders/\w+$").respond(404, json=not_found)
        router.get("/v1/account/openOrders").respond(200, json={"result": [], "success": True})
        router.delete(url__regex=r"/v1/account/orders/\w+$").respond(404, json=not_found)
        args = ["--out", str(tmp_path / "probe.md"), "--probe-order", "--yes", "--log-dir", str(tmp_path / "logs")]
        assert module.main(args) == 1
        assert not posted.called
        assert "order probe refused: the routing checks warned" in (tmp_path / "probe.md").read_text()

        module.main([*args, "--probe-main-wallet"])  # the explicit override probes anyway
        assert posted.call_count == 2


def test_check_account_probe_cancel_retries_and_finds_a_late_order(check_account, tmp_path, monkeypatch) -> None:
    module, install = check_account
    install(wallex_sub_account_client_id=SUB_ID)
    monkeypatch.setattr(module, "PROBE_CANCEL_RETRY_DELAYS_S", (0.0, 0.0, 0.0))
    monkeypatch.setattr(module, "PROBE_RECHECK_DELAYS_S", (0.0, 0.0, 0.0))
    monkeypatch.setattr(module, "WallexREST", functools.partial(module.WallexREST, sleep=NoSleep()))
    bodies: dict[str, dict[str, Any]] = {}
    lookups: dict[str, int] = {}
    deletes: dict[str, int] = {}

    def place(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        bodies[body["client_id"]] = body
        if body["type"] == "LIMIT":
            return httpx.Response(201, json=_order(body, "NEW"))
        raise httpx.ReadTimeout("timed out")  # the STOP_LIMIT: state unknown, indexed late

    def lookup(request: httpx.Request) -> httpx.Response:
        cid = request.url.path.rsplit("/", 1)[-1]
        lookups[cid] = lookups.get(cid, 0) + 1
        late = bodies[cid]["type"] == "STOP_LIMIT" and lookups[cid] <= 2  # 404 until the 3rd lookup
        if late:
            return httpx.Response(404, json={"success": False, "message": "not found"})
        return httpx.Response(200, json=_order(bodies[cid], "NEW"))

    def cancel(request: httpx.Request) -> httpx.Response:
        cid = request.url.path.rsplit("/", 1)[-1]
        deletes[cid] = deletes.get(cid, 0) + 1
        if bodies[cid]["type"] == "LIMIT" and deletes[cid] == 1:
            return httpx.Response(503)  # one outage on the cancel: retried, not given up
        if bodies[cid]["type"] == "STOP_LIMIT" and lookups[cid] <= 2:
            return httpx.Response(404, json={"success": False, "message": "not found"})
        return httpx.Response(200, json=_order(bodies[cid], "CANCELED"))

    with respx.mock(base_url=API, assert_all_called=False) as router:
        _mock_read_only_api(router)
        router.post("/mcp").respond(503)
        router.post("/v1/account/orders").mock(side_effect=place)
        router.get(url__regex=r"/v1/account/orders/\w+$").mock(side_effect=lookup)
        router.get("/v1/account/openOrders").respond(200, json={"result": {"USDTTMN": []}, "success": True})
        router.delete(url__regex=r"/v1/account/orders/\w+$").mock(side_effect=cancel)
        module.main(["--out", str(tmp_path / "probe.md"), "--probe-order", "--yes", "--log-dir", str(tmp_path / "logs")])

    report = (tmp_path / "probe.md").read_text()
    limit_row = next(line for line in report.splitlines() if line.startswith("| LIMIT |"))
    stop_row = next(line for line in report.splitlines() if line.startswith("| STOP_LIMIT |"))
    assert "| CANCELED |" in limit_row  # cancelled on the retry after the 503
    assert "| unknown |" in stop_row and "found late, cancelled: CANCELED" in stop_row
    assert "COULD NOT BE CANCELLED" not in report


def test_check_account_probe_aborts_on_a_crossed_book(check_account, tmp_path) -> None:
    module, install = check_account
    install(wallex_sub_account_client_id=SUB_ID)
    crossed = {"result": {"bid": [{"price": "266800", "quantity": "1"}], "ask": [{"price": "266700", "quantity": "1"}]}}
    with respx.mock(base_url=API, assert_all_called=False) as router:
        _mock_read_only_api(router)
        router.get("/v1/depth", params={"symbol": "USDTTMN"}).respond(200, json=crossed)  # replaces the route
        router.post("/mcp").respond(503)
        posted = router.post("/v1/account/orders").respond(201, json={})
        module.main(["--out", str(tmp_path / "probe.md"), "--probe-order", "--yes", "--log-dir", str(tmp_path / "logs")])
    assert not posted.called
    assert "book is crossed or mis-parsed" in (tmp_path / "probe.md").read_text()


def test_equity_and_balance_helpers(check_account) -> None:
    module, _ = check_account
    from decimal import Decimal

    from wta.models import Balance

    balances = {
        "TMN": Balance(asset="TMN", free=Decimal("1000"), locked=Decimal("500")),
        "USDT": Balance(asset="USDT", free=Decimal("2")),
        "SOL": Balance(asset="SOL", free=Decimal("0.5")),
        "XYZ": Balance(asset="XYZ", free=Decimal("3")),
    }
    bids = {"USDTTMN": Decimal("266720"), "SOLUSDT": Decimal("100")}
    equity, unpriced = module.equity_tmn(balances, bids)
    assert equity == Decimal("1500") + 2 * Decimal("266720") + Decimal("0.5") * 100 * Decimal("266720")
    assert unpriced == ["XYZ"]
    assert module.compare_balances(balances, dict(balances)) == []
    assert module.compare_balances(balances, {"TMN": balances["TMN"]})[0] == "SOL: REST 0.5 vs MCP 0"
    assert module.find_client_ids({"result": [{"clientId": "a"}, {"nested": {"client_id": "b"}}, {"clientId": "a"}]}) == ["a", "b"]

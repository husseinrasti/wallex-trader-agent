"""Account and API probe: run this first with the challenge key (PRD-01 §10).

    python scripts/check_account.py                  # read-only checks
    python scripts/check_account.py --probe-order    # also place and cancel two far-from-market orders

The report goes to ``docs/REPORTS/api_probe.md`` (``--out``) and to stdout. Sections, in order:

1. fee rates for the watchlist (``/v1/account/fee``);
2. balances: raw payload (truncated, secrets masked) + parsed, and the equity in TMN at best bids;
3. MCP ``initialize`` / ``tools/list`` and the read-only credit sub-account tools, REST
   ``/sub-accounts``, and loud warnings when order routing looks wrong (no
   ``WALLEX_SUB_ACCOUNT_CLIENT_ID`` while credit sub-accounts exist, or MCP and REST balances differ);
4. market rules for the watchlist (tick/step decimals, minQty, minNotional, market orders, banned);
5. only with ``--probe-order`` and a typed ``PROBE`` confirmation (or ``--yes``): a LIMIT BUY
   USDTTMN at 90 % of the best bid, then a STOP_LIMIT whose stop is 15 % away from the market and
   whose limit price still rests in the book if the stop triggers at once. Prices come from a book
   fetched after the confirmation. Each order is fetched and a cancel is always attempted (with its
   own retries); after "not found" the script looks again for a few seconds in case the order is
   indexed late. The probe refuses to run when the routing checks warned, unless
   ``--probe-main-wallet`` is given.

Every section survives an unreachable API: it says what failed and the script moves on. The API
key is never written (only "set"/"not set") and every secret is scrubbed from the report.
Exit code: 0 when every section ran cleanly without warnings, 1 otherwise.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import time
from collections.abc import Awaitable, Callable, Iterable
from decimal import Decimal
from pathlib import Path
from typing import Any

from wta.ai.mcp_client import (
    READ_ONLY_ALLOWLIST,
    MCPClient,
    MCPError,
    is_state_changing,
    tool_names,
    tool_payload,
)
from wta.config import ROOT, Config, Settings, load_config, load_settings, mask_secrets
from wta.exchange.base import OrderRejected, WallexAPIError
from wta.exchange.wallex_rest import WallexREST, json_safe, parse_balances
from wta.logging_.decision_log import OrdersLog
from wta.models import Balance, BookSnapshot, MarketInfo, Order, OrderRequest, OrderType, Side
from wta.utils import gitinfo
from wta.utils.decimalutil import quantize_down, quantize_up
from wta.utils.ids import make_client_id
from wta.utils.timeutil import SystemClock, iso

log = logging.getLogger("check_account")

DEFAULT_OUT = ROOT / "docs" / "REPORTS" / "api_probe.md"
RAW_MAX_CHARS = 3000
MCP_SUB_ACCOUNTS_TOOL = "wallex_credit_active_sub_accounts_list"
MCP_SUB_BALANCES_TOOL = "wallex_credit_sub_account_balances_details"
# This script may also read the credit sub-account tools; the analyst's allowlist may not.
ACCOUNT_CHECK_ALLOWLIST: frozenset[str] = READ_ONLY_ALLOWLIST | {MCP_SUB_ACCOUNTS_TOOL, MCP_SUB_BALANCES_TOOL}
PROBE_SYMBOL = "USDTTMN"
PROBE_LIMIT_FACTOR = Decimal("0.9")  # LIMIT BUY at 90 % of the best bid: never fills
PROBE_FAR_BELOW = Decimal("0.85")  # 15 % under the best bid
PROBE_FAR_ABOVE = Decimal("1.15")  # 15 % over the best ask
PROBE_CANCEL_RETRY_DELAYS_S: tuple[float, ...] = (1.0, 2.0, 4.0)  # whatever --retries says
PROBE_RECHECK_DELAYS_S: tuple[float, ...] = (1.0, 2.0, 3.0)  # look again after a "not found" cancel


# ---------------------------------------------------------------- pure helpers


def _one_line(exc: BaseException) -> str:
    text = getattr(exc, "message", None) or str(exc) or type(exc).__name__
    return str(text).splitlines()[0][:300]


def _num(value: Decimal | None) -> str:
    if value is None:
        return "-"
    return format(value.normalize(), "f") if value else "0"


def _positive(value: object) -> bool:
    try:
        return Decimal(str(value)) > 0
    except ArithmeticError:  # InvalidOperation for None, "", text
        return False


def _pct(value: Decimal) -> str:
    return f"{value * 100:.3f}%"


def md_table(headers: list[str], rows: Iterable[Iterable[object]]) -> list[str]:
    """A Markdown table (pipes in cells are escaped)."""

    def cell(v: object) -> str:
        return str(v).replace("|", "\\|").replace("\n", " ")

    out = ["| " + " | ".join(headers) + " |", "|" + "---|" * len(headers)]
    out.extend("| " + " | ".join(cell(v) for v in row) + " |" for row in rows)
    return out


def equity_tmn(balances: dict[str, Balance], bids: dict[str, Decimal]) -> tuple[Decimal, list[str]]:
    """Total value in TMN at best bids (X via XTMN, else XUSDT x USDTTMN). Returns (equity, unpriced)."""
    total = Decimal(0)
    unpriced: list[str] = []
    for asset, bal in sorted(balances.items()):
        qty = bal.total
        if not qty:
            continue
        if asset == "TMN":
            total += qty
        elif f"{asset}TMN" in bids:
            total += qty * bids[f"{asset}TMN"]
        elif f"{asset}USDT" in bids and "USDTTMN" in bids:
            total += qty * bids[f"{asset}USDT"] * bids["USDTTMN"]
        else:
            unpriced.append(asset)
    return total, unpriced


def compare_balances(rest: dict[str, Balance], mcp: dict[str, Balance]) -> list[str]:
    """Assets whose totals differ between the two views (``"BTC: REST 0.1 vs MCP 0"``)."""
    diffs = []
    for asset in sorted(set(rest) | set(mcp)):
        a = rest[asset].total if asset in rest else Decimal(0)
        b = mcp[asset].total if asset in mcp else Decimal(0)
        if abs(a - b) > Decimal("1e-8") * max(Decimal(1), abs(a)):
            diffs.append(f"{asset}: REST {_num(a)} vs MCP {_num(b)}")
    return diffs


def find_client_ids(payload: Any) -> list[str]:
    """Every ``clientId`` / ``client_id`` value anywhere in a decoded JSON payload."""
    found: list[str] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if str(key).lower() in ("clientid", "client_id") and isinstance(value, str) and value:
                    found.append(value)
                else:
                    walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(payload)
    return list(dict.fromkeys(found))


def probe_qty(market: MarketInfo, price: Decimal) -> Decimal:
    """Smallest valid quantity at ``price``: minNotional / price rounded UP to the step, plus one step."""
    qty = quantize_up(market.min_notional / price, market.step_decimals) + market.step
    return max(qty, quantize_up(market.min_qty, market.step_decimals))


# ---------------------------------------------------------------- report


class Report:
    """Markdown lines plus the warnings and problems that decide the exit code."""

    def __init__(self) -> None:
        self.lines: list[str] = []
        self.warnings: list[str] = []
        self.problems: list[str] = []

    def heading(self, title: str) -> None:
        self.lines += ["", f"## {title}", ""]

    def line(self, text: str = "") -> None:
        self.lines.append(text)

    def table(self, headers: list[str], rows: Iterable[Iterable[object]]) -> None:
        self.lines += [*md_table(headers, rows), ""]

    def code(self, text: str) -> None:
        self.lines += ["```json", text, "```", ""]

    def problem(self, text: str) -> None:
        self.problems.append(text)
        self.lines += [f"**{text}**", ""]

    def warn(self, text: str) -> None:
        """A loud warning: in the report and on stderr at once."""
        self.warnings.append(text)
        self.lines += ["", f"> **WARNING: {text}**", ""]
        print(f"\n!!!!! WARNING: {text} !!!!!\n", file=sys.stderr, flush=True)

    def text(self) -> str:
        return "\n".join(self.lines).strip() + "\n"


# ---------------------------------------------------------------- the check


class AccountCheck:
    def __init__(self, cfg: Config, settings: Settings, args: argparse.Namespace) -> None:
        self.cfg = cfg
        self.settings = settings
        self.args = args
        self.key = settings.wallex_api_key.get_secret_value()
        self.clock = SystemClock()
        self.report = Report()
        self.watchlist = list(dict.fromkeys(s.upper() for s in cfg.markets.watchlist))
        self.orders_log: OrdersLog | None = None
        if args.probe_order:
            self.orders_log = OrdersLog(Path(args.log_dir), self.clock, settings.secret_values())
        self.rest = WallexREST(
            settings.wallex_base_url,
            self.key,
            rps=cfg.data.rate_limit_rps,
            timeout=args.timeout,
            max_retries=args.retries,
            sub_account_client_id=settings.wallex_sub_account_client_id,
            on_order_event=self._order_event if self.orders_log else None,
        )
        self.rest_down: str | None = None  # set on the first timeout/transport error
        self.markets: dict[str, MarketInfo] | None = None
        self.balances: dict[str, Balance] | None = None
        self.books: dict[str, BookSnapshot] | None = None
        self.mcp_client_ids: list[str] = []
        self.mcp_balances: Any = None
        self.rest_client_ids: list[str] = []
        self.routing_bad = False  # a routing warning was raised: the order probe refuses to run

    def _order_event(self, action: str, client_id: str, symbol: str, data: dict[str, Any]) -> None:
        assert self.orders_log is not None
        self.orders_log.log(action, client_id, symbol, data)

    # ------------------------------------------------------------ driver

    async def run(self) -> Report:
        try:
            await self._header()
            await self._section("1. Fee rates (/v1/account/fee)", self.fees)
            await self._section("2. Balances", self.balances_section)
            await self._section("3. MCP and sub-account routing", self.routing_section)
            await self._section("4. Market rules for the watchlist (/v1/markets)", self.markets_section)
            await self._section("5. Order-type probe (--probe-order)", self.probe_section)
            self._summary()
        finally:
            await self.rest.close()
            if self.orders_log is not None:
                self.orders_log.close()
        return self.report

    async def _section(self, title: str, job: Callable[[], Awaitable[None]]) -> None:
        """Run one section; any failure is reported in it and the next section still runs."""
        self.report.heading(title)
        print(f"[check_account] {title} ...", file=sys.stderr, flush=True)
        try:
            await job()
        except WallexAPIError as exc:
            if exc.status is None:
                self.rest_down = self.rest_down or _one_line(exc)
            self.report.problem(f"REST API error: {_one_line(exc)}")
        except MCPError as exc:
            self.report.problem(f"MCP error: {_one_line(exc)}")
        except Exception as exc:  # a bug in one section must not hide the others
            log.exception("section %s failed", title)
            self.report.problem(f"unexpected error: {type(exc).__name__}: {_one_line(exc)}")

    def _rest_ready(self, needs_key: bool = True) -> bool:
        if needs_key and not self.key:
            self.report.problem("skipped: WALLEX_API_KEY is not set")
            return False
        if self.rest_down:
            self.report.problem(f"skipped: REST API unreachable ({self.rest_down})")
            return False
        return True

    async def _header(self) -> None:
        git = gitinfo.get()
        s = self.settings
        sub = s.wallex_sub_account_client_id
        r = self.report
        r.line("# Wallex API probe")
        r.line()
        r.line(f"- generated: {iso(self.clock.now())} (UTC)")
        r.line(f"- git commit: {git['commit']}{' (dirty)' if git['dirty'] else ''}")
        r.line(f"- config hash: {self.cfg.config_hash}")
        r.line(f"- REST base URL: {s.wallex_base_url}")
        r.line(f"- MCP URL: {s.wallex_mcp_url} (auth header `{s.wallex_mcp_auth_header}`)")
        r.line(f"- API key: {'set (never printed)' if self.key else 'NOT SET'}")
        r.line(f"- sub-account-client-id: {sub if sub else 'not set'}")
        r.line(f"- order probe: {'requested' if self.args.probe_order else 'not requested (read-only run)'}")
        started = time.monotonic()
        try:
            self.markets = await self.rest.get_markets()
            ms = (time.monotonic() - started) * 1000
            r.line(f"- REST reachable: yes ({len(self.markets)} markets from /v1/markets in {ms:.0f} ms)")
        except WallexAPIError as exc:
            if exc.retryable:
                self.rest_down = _one_line(exc)
            r.line(f"- REST reachable: **NO** ({_one_line(exc)})")

    def _raw(self, payload: Any) -> str:
        text = json.dumps(json_safe(payload), indent=1, ensure_ascii=False)
        text = mask_secrets(text, self.settings.secret_values())
        if len(text) > RAW_MAX_CHARS:
            text = text[:RAW_MAX_CHARS] + f"\n... (truncated; {len(text)} chars in total)"
        return text

    async def _best_bids(self) -> dict[str, Decimal]:
        """Best bids for the watchlist and every balance asset; ``/v2/depth/all`` then per symbol."""
        if self.books is None:
            books: dict[str, BookSnapshot] = {}
            try:
                books = await self.rest.get_all_depth(levels=1)
            except WallexAPIError as exc:
                if exc.status is None:
                    raise
            wanted = set(self.watchlist) | {"USDTTMN"}
            for asset in self.balances or {}:
                wanted |= {f"{asset}TMN", f"{asset}USDT"}
            for sym in sorted(wanted - set(books)):
                if self.markets is not None and sym not in self.markets:
                    continue
                try:
                    books[sym] = await self.rest.get_depth(sym, levels=1)
                except WallexAPIError as exc:
                    if exc.status is None:
                        raise
            self.books = books
        return {s: b.best_bid for s, b in self.books.items() if b.best_bid}

    # ------------------------------------------------------------ 1. fees

    async def fees(self) -> None:
        if not self._rest_ready():
            return
        fees = await self.rest.get_fees()
        rows = []
        for sym in self.watchlist:
            if sym in fees:
                maker, taker = fees[sym]
                rows.append([sym, _pct(maker), _pct(taker)])
            else:
                rows.append([sym, "not returned", "not returned"])
        self.report.table(["symbol", "maker", "taker"], rows)
        self.report.line(f"{len(fees)} symbols in the fee answer.")

    # ------------------------------------------------------------ 2. balances

    async def balances_section(self) -> None:
        if not self._rest_ready():
            return
        paths = ["/v1/account/balances"]
        if self.settings.wallex_sub_account_client_id:
            paths.append("/v1/account/balances-detail")
        parsed: dict[str, dict[str, Balance]] = {}
        for path in paths:
            try:
                raw = await self.rest.request("GET", path)
            except WallexAPIError as exc:
                if exc.status is None:
                    raise
                self.report.problem(f"{path}: {_one_line(exc)}")
                continue
            parsed[path] = parse_balances(raw)
            self.report.line(f"### `{path}`: raw (truncated, secrets masked)")
            self.report.line()
            self.report.code(self._raw(raw))
            self.report.line(f"### `{path}`: parsed (non-zero)")
            self.report.line()
            rows = [[b.asset, _num(b.free), _num(b.locked), _num(b.total)] for b in parsed[path].values() if b.total]
            self.report.table(["asset", "free", "locked", "total"], rows or [["(none)", "", "", ""]])
        # the same preference as WallexREST.get_balances: the sub-account view when configured and
        # answered (even if empty); the main wallet only when there is no detail answer
        detail = "/v1/account/balances-detail"
        self.balances = parsed[detail] if detail in parsed else parsed.get("/v1/account/balances")
        if self.balances is None:
            return
        bids = await self._best_bids()
        equity, unpriced = equity_tmn(self.balances, bids)
        initial = self.cfg.engine.initial_equity_tmn
        change = (equity / initial - 1) if initial else Decimal(0)
        self.report.line(
            f"**Equity estimate: {equity:,.0f} TMN** at best bids "
            f"({_pct(change)} vs the configured initial {initial:,.0f} TMN)."
        )
        if unpriced:
            self.report.line(f"Not priced (no book): {', '.join(unpriced)}.")

    # ------------------------------------------------------------ 3. MCP + routing

    async def routing_section(self) -> None:
        await self._mcp_checks()
        await self._rest_sub_accounts()
        self._routing_warnings()

    async def _mcp_checks(self) -> None:
        r = self.report
        r.line("### MCP")
        r.line()
        mcp = MCPClient(
            self.settings.wallex_mcp_url,
            self.key,
            self.settings.wallex_mcp_auth_header,
            timeout=self.args.timeout,
            allowlist=ACCOUNT_CHECK_ALLOWLIST,
        )
        try:
            await mcp.initialize()
            info = mcp.server_info
            r.line(
                f"- initialize: ok (server {info.get('name', '?')} {info.get('version', '')}, protocol "
                f"{mcp.protocol_version}, session id {'returned' if mcp.session_id else 'none'})"
            )
            names = tool_names(await mcp.list_tools())
            risky = [n for n in names if is_state_changing(n)]
            r.line(f"- tools/list: {len(names)} tools ({len(risky)} state-changing, never called)")
            r.line()
            r.code(json.dumps(names, indent=1))
            for tool in (MCP_SUB_ACCOUNTS_TOOL, MCP_SUB_BALANCES_TOOL):
                await self._mcp_call(mcp, names, tool)
        except MCPError as exc:
            r.problem(f"MCP: {_one_line(exc)}")
        finally:
            await mcp.close()

    def _tool_args(self, mcp: MCPClient, tool: str) -> tuple[dict[str, Any], list[str]]:
        """Arguments for a credit tool: any property naming a client/sub-account id gets ours."""
        schema = (mcp.tools.get(tool) or {}).get("inputSchema") or {}
        client_id = self.settings.wallex_sub_account_client_id or next(iter(self.mcp_client_ids), "")
        args: dict[str, Any] = {}
        for prop in schema.get("properties") or {}:
            name = str(prop).lower().replace("_", "")
            if client_id and ("client" in name or "subaccount" in name):
                args[prop] = client_id
        missing = [str(p) for p in schema.get("required") or [] if p not in args]
        return args, missing

    async def _mcp_call(self, mcp: MCPClient, names: list[str], tool: str) -> None:
        r = self.report
        if names and tool not in names:
            r.line(f"- {tool}: not offered by this server")
            return
        args, missing = self._tool_args(mcp, tool)
        if missing:
            r.problem(f"{tool}: skipped, needs arguments {', '.join(missing)} (no sub-account clientId known)")
            return
        result = await mcp.call_tool(tool, args)
        payload = tool_payload(result)
        status = "tool error" if result.get("isError") else "ok"
        r.line(f"- {tool}({', '.join(args) or 'no arguments'}): {status}")
        r.line()
        r.code(self._raw(payload))
        if result.get("isError"):
            r.problem(f"{tool} returned an error")
        elif tool == MCP_SUB_ACCOUNTS_TOOL:
            self.mcp_client_ids = find_client_ids(payload)
        else:
            self.mcp_balances = payload

    async def _rest_sub_accounts(self) -> None:
        r = self.report
        r.line("### REST `/sub-accounts`")
        r.line()
        if not self._rest_ready():
            return
        try:
            subs = await self.rest.get_sub_accounts()
        except WallexAPIError as exc:
            if exc.status is None:
                raise
            r.problem(f"/sub-accounts: {_one_line(exc)}")
            return
        rows = [[s.get("title", ""), s.get("clientId", ""), s.get("type", ""), s.get("isActive", ""),
                 s.get("isSuspend", "")] for s in subs]
        r.table(["title", "clientId", "type", "isActive", "isSuspend"], rows or [["(none)", "", "", "", ""]])
        self.rest_client_ids = find_client_ids(subs)

    def _routing_warnings(self) -> None:
        r = self.report
        r.line("### Routing checks")
        r.line()
        configured = self.settings.wallex_sub_account_client_id
        known = list(dict.fromkeys(self.mcp_client_ids + self.rest_client_ids))
        if known and not configured:
            self.routing_bad = True
            r.warn(
                "WALLEX_SUB_ACCOUNT_CLIENT_ID IS EMPTY BUT CREDIT SUB-ACCOUNTS EXIST "
                f"({', '.join(known)}). Orders and balances would use the MAIN wallet, not the challenge "
                "account. Set WALLEX_SUB_ACCOUNT_CLIENT_ID in .env and run this check again."
            )
        elif configured and known and configured not in known:
            self.routing_bad = True
            r.warn(f"WALLEX_SUB_ACCOUNT_CLIENT_ID ({configured}) is not among the listed sub-accounts ({', '.join(known)}).")
        elif configured:
            r.line(f"- sub-account-client-id is set{' and listed' if known else ''}.")
        else:
            r.line("- no credit sub-accounts listed and no sub-account-client-id configured.")
        if self.mcp_balances is None:
            r.line("- MCP vs REST balances: not compared (MCP balances unavailable).")
            return
        mcp_balances = parse_balances(self.mcp_balances)
        if not mcp_balances:
            r.line("- MCP vs REST balances: the MCP balances could not be parsed; compare the raw payloads by hand.")
        elif self.balances is None:
            r.line("- MCP vs REST balances: not compared (REST balances unavailable).")
        else:
            diffs = compare_balances(self.balances, mcp_balances)
            if diffs:
                self.routing_bad = True
                r.warn(
                    "MCP SUB-ACCOUNT BALANCES DIFFER FROM REST BALANCES (" + "; ".join(diffs) + "). "
                    "The API key may route orders to a different account than the challenge account."
                )
            else:
                r.line("- MCP and REST balances match.")

    # ------------------------------------------------------------ 4. markets

    async def markets_section(self) -> None:
        r = self.report
        if self.markets is None:
            r.problem("market rules unavailable (/v1/markets failed, see the header); banned flags come from the config")
        rows = []
        for sym in self.watchlist:
            banned = "BANNED" if self.cfg.markets.is_banned(sym) else "no"
            m = (self.markets or {}).get(sym)
            if m is None:
                rows.append([sym, "-", "-", "-", "-", "-", banned])
            else:
                rows.append([
                    sym, m.tick_decimals, m.step_decimals, _num(m.min_qty), _num(m.min_notional),
                    "yes" if m.market_orders_enabled else "NO", banned,
                ])
        r.table(["symbol", "tick dec", "step dec", "minQty", "minNotional", "isMarketTypeEnable", "banned"], rows)
        banned = [s for s in self.watchlist if self.cfg.markets.is_banned(s)]
        if banned:
            r.warn(f"BANNED SYMBOLS IN THE WATCHLIST: {', '.join(banned)}")
        if self.markets is not None:
            missing = [s for s in self.watchlist if s not in self.markets]
            if missing:
                r.problem(f"watchlist symbols not listed on the exchange: {', '.join(missing)}")

    # ------------------------------------------------------------ 5. probe

    def _confirmed(self) -> bool:
        if self.args.yes:
            return True
        print(
            "\nThe order probe places two REAL orders on USDTTMN far from the market (a LIMIT BUY at 90% "
            "of the best bid and a STOP_LIMIT with its stop and limit 15% away) and cancels them right after.",
            file=sys.stderr,
        )
        try:
            answer = input("Type PROBE to continue: ")
        except EOFError:
            answer = ""
        return answer.strip() == "PROBE"

    async def probe_section(self) -> None:
        r = self.report
        if not self.args.probe_order:
            r.line("Not run. Pass `--probe-order` to place and cancel two far-from-market USDTTMN orders.")
            return
        if self.routing_bad and not self.args.probe_main_wallet:
            r.problem(
                "order probe refused: the routing checks warned that orders may not reach the challenge "
                "account. Fix the routing first, or pass --probe-main-wallet to probe anyway."
            )
            return
        if not self._rest_ready():
            return
        if not self._confirmed():
            r.line("Aborted at the confirmation prompt; no order was sent.")
            return
        market = (self.markets or {}).get(PROBE_SYMBOL)
        if market is None:
            r.problem(f"{PROBE_SYMBOL} market rules unavailable; probe skipped")
            return
        # a fresh book: the cached one is from section 2, before the MCP checks and the prompt
        book = await self.rest.get_depth(PROBE_SYMBOL, levels=1)
        if book.best_bid is None or book.best_ask is None:
            r.problem(f"no {PROBE_SYMBOL} order book; probe skipped")
            return
        if book.best_bid >= book.best_ask:
            r.problem(f"{PROBE_SYMBOL} book is crossed or mis-parsed (bid {book.best_bid} >= ask {book.best_ask}); probe skipped")
            return
        results = [await self._probe(self._limit_request(market, book))]
        results.append(await self._probe(self._stop_limit_request(market, book)))
        r.table(
            ["type", "side", "qty", "price", "stop", "accepted", "status", "stop_Price", "error", "cancel", "client_id"],
            [[x[k] for k in ("type", "side", "qty", "price", "stop", "accepted", "status", "stop_price",
                             "error", "cancel", "client_id")] for x in results],
        )

    def _request(self, kind: OrderType, side: Side, qty: Decimal, price: Decimal, stop: Decimal | None) -> OrderRequest:
        now = self.clock.now()
        decision_id = f"probe_{int(now)}_{kind}_{side}"
        return OrderRequest(
            client_id=make_client_id("PROBE", PROBE_SYMBOL, now, decision_id),
            symbol=PROBE_SYMBOL,
            side=side,
            type=kind,
            qty=qty,
            price=price,
            stop_price=stop,
            strategy="PROBE",
            purpose="probe",
            decision_id=decision_id,
        )

    def _limit_request(self, market: MarketInfo, book: BookSnapshot) -> OrderRequest:
        assert book.best_bid is not None
        price = quantize_down(book.best_bid * PROBE_LIMIT_FACTOR, market.tick_decimals)
        return self._request("LIMIT", "BUY", probe_qty(market, price), price, None)

    def _stop_limit_request(self, market: MarketInfo, book: BookSnapshot) -> OrderRequest:
        """A STOP_LIMIT that rests whatever its trigger does.

        SELL when the free USDT covers it: stop 15 % under the best bid, limit 15 % over the best
        ask. Otherwise BUY: stop 15 % over the best ask, limit 15 % under the best bid. If the stop
        triggered at once, the order would sit in the book far from the market instead of filling.
        Rounding always moves prices further away from the market.
        """
        assert book.best_bid is not None and book.best_ask is not None
        far_below = quantize_down(book.best_bid * PROBE_FAR_BELOW, market.tick_decimals)
        far_above = quantize_up(book.best_ask * PROBE_FAR_ABOVE, market.tick_decimals)
        usdt = (self.balances or {}).get("USDT")
        qty = probe_qty(market, far_above)
        if usdt is not None and usdt.free >= qty:
            return self._request("STOP_LIMIT", "SELL", qty, far_above, far_below)
        return self._request("STOP_LIMIT", "BUY", probe_qty(market, far_below), far_below, far_above)

    async def _probe(self, req: OrderRequest) -> dict[str, str]:
        """Place, fetch and ALWAYS try to cancel one order; returns a result row."""
        row = {
            "type": req.type, "side": req.side, "qty": _num(req.qty), "price": _num(req.price),
            "stop": _num(req.stop_price), "accepted": "no", "status": "-",
            "stop_price": "-" if req.stop_price is None else "?", "error": "", "cancel": "-",
            "client_id": req.client_id,
        }
        print(f"[check_account] probe: {req.type} {req.side} {req.qty} @ {req.price} ({req.client_id})", file=sys.stderr)
        rejected = False  # True only when the exchange definitely refused the first send
        try:
            order = await self.rest.place_order(req)
        except OrderRejected as exc:
            rejected = True
            row["error"] = f"HTTP {exc.status} {exc.code or ''}: {exc.message}".replace("  ", " ")
            if req.stop_price is not None:
                row["stop_price"] = "rejected (see error)"
        except WallexAPIError as exc:
            row["accepted"] = "unknown"
            row["error"] = f"state unknown: {exc.message}"
        else:
            row["accepted"], row["status"] = "yes", order.status
            try:
                fetched = await self.rest.get_order(req.client_id)
            except WallexAPIError as exc:
                fetched = None
                row["error"] = f"fetch failed: {exc.message}"
            raw = (fetched or order).raw
            if fetched is not None:
                row["status"] = fetched.status
            if req.stop_price is not None:
                echoed = raw.get("stopPrice")
                row["stop_price"] = f"accepted (stopPrice={echoed})" if _positive(echoed) else "not echoed; verify"
        finally:
            row["cancel"] = await self._cancel_probe(req.client_id, look_again=not rejected)
        return row

    async def _cancel_probe(self, client_id: str, look_again: bool) -> str:
        """Cancel a probe order. After "not found" (and unless the send was refused outright) look
        again for a few seconds: the exchange may index the order late. Never raises."""
        try:
            cancelled = await self._cancel_with_retries(client_id)
            if cancelled is not None:
                return cancelled.status
            if not look_again:
                return "nothing to cancel (not found)"
            late = await self._find_late_order(client_id)
            if late is None:
                return "nothing to cancel (not found)"
            if late.is_terminal:
                return f"found late, already {late.status}"
            cancelled = await self._cancel_with_retries(client_id)
            if cancelled is None:
                raise WallexAPIError(404, "not_found", "the order was listed but the cancel did not find it")
            return f"found late, cancelled: {cancelled.status}"
        except Exception as exc:
            self.report.warn(
                f"PROBE ORDER {client_id} COULD NOT BE CANCELLED ({_one_line(exc)}). "
                "Check the open orders of the account and cancel it."
            )
            return f"FAILED: {_one_line(exc)}"

    async def _cancel_with_retries(self, client_id: str) -> Order | None:
        """``cancel_order`` retried after outages (timeout, 5xx, 429), whatever ``--retries`` says."""
        for delay in (*PROBE_CANCEL_RETRY_DELAYS_S, None):
            try:
                return await self.rest.cancel_order(client_id)
            except WallexAPIError as exc:
                if not exc.retryable or delay is None:
                    raise
                print(f"[check_account] cancel {client_id} failed ({_one_line(exc)}); retrying", file=sys.stderr)
                await asyncio.sleep(delay)
        return None  # not reached: the last attempt returns or raises

    async def _find_late_order(self, client_id: str) -> Order | None:
        """``get_order`` and the open orders, polled a few times; None if the order never shows up."""
        for delay in PROBE_RECHECK_DELAYS_S:
            await asyncio.sleep(delay)
            order = await self.rest.get_order(client_id)
            if order is None:
                listed = await self.rest.get_open_orders(PROBE_SYMBOL)
                order = next((o for o in listed if o.client_id == client_id), None)
            if order is not None:
                return order
        return None

    # ------------------------------------------------------------ summary

    def _summary(self) -> None:
        r = self.report
        r.heading("Summary")
        r.line(f"- warnings: {len(r.warnings)}")
        for w in list(r.warnings):
            r.line(f"  - {w}")
        r.line(f"- problems: {len(r.problems)}")
        for p in list(r.problems):
            r.line(f"  - {p}")
        if not r.warnings and not r.problems:
            r.line("- all checks passed")


# ---------------------------------------------------------------- entry point


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Probe the Wallex account and API (read-only unless --probe-order).")
    p.add_argument("--out", type=Path, default=DEFAULT_OUT, help=f"report file (default {DEFAULT_OUT.relative_to(ROOT)})")
    p.add_argument("--timeout", type=float, default=10, help="HTTP timeout per request in seconds (default 10)")
    p.add_argument("--retries", type=int, default=0, help="REST retries per request (default 0: fail fast)")
    p.add_argument("--probe-order", action="store_true", help="place and cancel two far-from-market test orders")
    p.add_argument("--yes", action="store_true", help="skip the PROBE confirmation prompt")
    p.add_argument(
        "--probe-main-wallet",
        action="store_true",
        help="run --probe-order even when the routing checks warned (orders may use the main wallet)",
    )
    p.add_argument("--log-dir", default="logs", help="where probe order traffic is logged (orders.jsonl)")
    p.add_argument("--set", action="append", default=[], metavar="KEY=VALUE", help="config override")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    cfg = load_config(args.set)
    settings = load_settings()
    report = asyncio.run(AccountCheck(cfg, settings, args).run())
    text = mask_secrets(report.text(), settings.secret_values())
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")
    print(text)
    print(f"[check_account] report written to {out}", file=sys.stderr)
    return 0 if not report.warnings and not report.problems else 1


if __name__ == "__main__":
    raise SystemExit(main())

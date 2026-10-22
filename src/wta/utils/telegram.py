"""Optional Telegram alerts and operational inbound commands (PRD-06 §2).

Outbound: ``TelegramNotifier``
    ``await notifier.send(text, kind)`` posts a plain-text message to one chat. At most
    ``max_msgs_per_10min`` messages leave per *rolling* 10 minutes. Anything over that waits in a
    digest that goes out as ONE message as soon as the window has room again: ``flush_digest()``
    (call it from the heartbeat), which ``send`` also tries first so the order of alerts is kept.
    Telegram's own flood control (HTTP 429 ``retry_after``) and network failures (with backoff)
    are handled the same way: the alert joins the digest instead of being lost. ``send`` never
    raises: failures are logged with the token masked and counted in ``stats()``. Every text is
    trimmed to Telegram's 4096-character limit. ``send_nowait`` schedules a send on the running
    loop so a slow Telegram never delays an engine tick (``drain()`` awaits those sends).

    The ``fmt_*`` helpers build the alert texts PRD-06 lists: start/stop/restart, every fill
    (strategy, symbol, side, notional, PnL when the fill closes a position), risk pause/kill/resume,
    flatten phases, LLM failures in a row, stale data, API error bursts and the hourly equity line.

Inbound (optional): ``TelegramCommands``
    Long-polls ``getUpdates`` with an advancing ``offset``: every update is confirmed once it has
    been seen, even when it is ignored, so nothing is processed twice. Only messages from the
    configured ``chat_id`` are accepted; any other chat is ignored, counted and gets no reply.
    A command older than ``max_age_s`` (e.g. a backlog sent while the agent was down) is not
    executed. Supported: ``/status``, ``/pause``, ``/resume``, ``/kill`` (``/help`` and ``/start``
    are answered locally). Each accepted command is handed to ``on_command(command, args)``; the
    text it returns is sent back as the reply, and the engine logs the action as an event with
    ``source: "telegram"``.

    THESE ARE OPERATIONAL CONTROLS ONLY, NOT TRADING DECISIONS. No Telegram command can place,
    modify or cancel a specific order, or name a symbol, side, price or quantity: there is no
    /buy, /sell, /order or /cancel, and any other command is answered with "not supported"
    without ever reaching the engine (``on_command`` is only ever called with one of the four
    names above). The contract with the engine is:

    * ``/status`` only reads state;
    * ``/pause`` only stops NEW entries (the same effect as a risk pause);
    * ``/resume`` only lifts a pause that ``/pause`` set: it cannot override a risk-rule pause and
      cannot reset the kill-switch (that stays manual, ``wta reset-kill``);
    * ``/kill`` trips the same kill-switch as the ``KILL`` file. What happens next is the
      engine's own deterministic kill procedure (PRD-02 §6): it cancels its entry orders, its
      FlattenManager sells every non-TMN balance to TMN, then it only monitors. The operator
      chooses none of those orders.

    Every order the agent sends is still produced by a strategy or by flatten and approved by the
    RiskManager, so no manual trading is possible through Telegram.

One-shot CLI: ``send_alert_cli(settings, text)``
    The synchronous helper behind ``wta alert "text"`` (``scripts/run_forever.sh`` calls it when
    the agent keeps crashing). It does nothing when ``TELEGRAM_BOT_TOKEN`` / ``TELEGRAM_CHAT_ID``
    are not configured, and never raises.

Secrets: the bot token is part of every Bot API URL. It is kept in a private attribute, masked
out of every message this module logs or stores, never put into an exception, and a logging
filter masks it in the request lines httpx logs at INFO level.
"""

from __future__ import annotations

import asyncio
import logging
from collections import Counter, deque
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import asdict, dataclass, field
from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation
from typing import Any, Literal
from urllib.parse import quote

import httpx

from wta.utils.timeutil import Clock, iso

log = logging.getLogger(__name__)

API_BASE = "https://api.telegram.org"
TELEGRAM_MAX_CHARS = 4096  # sendMessage limit, counted in UTF-16 code units
WINDOW_S = 600.0  # the rolling rate-limit window: 10 minutes
TRUNCATED = "\n...[truncated]"
MASK = "***"

SendStatus = Literal["sent", "queued", "failed", "disabled", "deduped"]

SUPPORTED_COMMANDS: tuple[str, ...] = ("status", "pause", "resume", "kill")
LOCAL_COMMANDS: tuple[str, ...] = ("help", "start")
HELP_TEXT = (
    "Operational controls (no trading through Telegram):\n"
    "/status - equity, drawdown, positions, pause/kill state\n"
    "/pause [reason] - stop new entries\n"
    "/resume [reason] - lift a /pause\n"
    "/kill [reason] - kill-switch: the engine flattens everything to TMN, then monitors only\n"
    "No command can place, modify or cancel a specific order: manual trading is not possible here."
)
NO_TRADING_TEXT = (
    "/{cmd} is not supported. Telegram commands are operational controls only "
    "(/status, /pause, /resume, /kill): manual trading is not possible through Telegram."
)

# transient failures go to the digest and back off; permanent ones (400 bad chat, 401 bad token) don't
_BACKOFF_START_S = 30.0
_BACKOFF_MAX_S = 600.0
_DIGEST_ITEM_CHARS = 300  # each queued alert keeps at most this much text
_DIGEST_LINE_CHARS = 200  # ... and shows at most this much of its first line in the digest


# ---------------------------------------------------------------- secret masking


class _TokenScrubFilter(logging.Filter):
    """Masks registered bot tokens in log records (httpx logs every request URL at INFO)."""

    def __init__(self) -> None:
        super().__init__()
        self.tokens: set[str] = set()

    def filter(self, record: logging.LogRecord) -> bool:
        if not self.tokens:
            return True
        try:
            msg = record.getMessage()
        except Exception:  # noqa: BLE001 - a malformed record is not ours to fix
            return True
        masked = _mask(msg, self.tokens)
        if masked != msg:
            record.msg = masked
            record.args = None
        return True


_SCRUB = _TokenScrubFilter()
# loggers that may print a request URL; a logger filter only sees records created by that logger
_SCRUBBED_LOGGERS = ("httpx", "httpcore", "httpcore.http11", "httpcore.http2", "httpcore.connection")


def _register_token(token: str) -> None:
    if not token:
        return
    _SCRUB.tokens.add(token)
    for name in _SCRUBBED_LOGGERS:
        logger = logging.getLogger(name)
        if _SCRUB not in logger.filters:
            logger.addFilter(_SCRUB)


def _mask(text: str, tokens: Iterable[str]) -> str:
    for token in tokens:
        if token:
            text = text.replace(token, MASK).replace(quote(token, safe=""), MASK)
    return text


def _secret(value: Any) -> str:
    """A plain string from a ``str`` or a pydantic ``SecretStr`` (None -> '')."""
    if value is None:
        return ""
    getter = getattr(value, "get_secret_value", None)
    return str(getter() if callable(getter) else value).strip()


# ---------------------------------------------------------------- text helpers


def _utf16_len(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def trim_text(text: str, limit: int = TELEGRAM_MAX_CHARS) -> str:
    """``text`` cut to ``limit`` UTF-16 code units (Telegram's unit), marked as truncated.

    Empty text becomes a placeholder: Telegram rejects empty messages.
    """
    if not text or not text.strip():
        return "(empty message)"
    if _utf16_len(text) <= limit:
        return text
    budget = limit - _utf16_len(TRUNCATED)
    out: list[str] = []
    used = 0
    for ch in text:
        width = 2 if ord(ch) > 0xFFFF else 1
        if used + width > budget:
            break
        out.append(ch)
        used += width
    return "".join(out) + TRUNCATED


def _dec(x: Any) -> Decimal | None:
    if x is None or isinstance(x, bool):
        return None
    try:
        d = x if isinstance(x, Decimal) else Decimal(str(x).replace(",", "").strip())
    except (InvalidOperation, ValueError, TypeError):
        return None
    return d if d.is_finite() else None


def _places(asset: str) -> int:
    asset = asset.upper()
    if asset in ("TMN", "IRT", ""):
        return 0
    if asset in ("USDT", "USDC"):
        return 2
    return 8


def fmt_amount(x: Any, asset: str = "TMN") -> str:
    """``12345.6, "TMN"`` -> ``"12,346 TMN"``; ``"12.345", "USDT"`` -> ``"12.35 USDT"``; bad -> ``"n/a"``."""
    d = _dec(x)
    if d is None:
        return "n/a"
    places = _places(asset)
    q = d.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_EVEN)
    return f"{q:,.{places}f} {asset}".strip()


def fmt_pct(frac: Any, signed: bool = True) -> str:
    """A fraction as a percentage: ``0.0123`` -> ``"+1.23%"`` (``signed=False`` -> ``"1.23%"``)."""
    d = _dec(frac)
    if d is None:
        return "n/a"
    q = (d * 100).quantize(Decimal("0.01"), rounding=ROUND_HALF_EVEN)
    if q == 0:
        q = abs(q)  # no "-0.00%"
    return f"{q:+.2f}%" if signed else f"{q:.2f}%"


def _natural(d: Decimal) -> str:
    """Every significant digit, thousands separated: ``Decimal("82845.00")`` -> ``"82,845"``."""
    return f"{d.normalize():,f}"


def _quote_of(symbol: str) -> str:
    s = symbol.upper()
    for q in ("USDT", "USDC", "TMN"):
        if s.endswith(q) and len(s) > len(q):
            return q
    return ""


def _kv(details: Mapping[str, Any] | None, limit: int = 8) -> str:
    if not details:
        return ""
    items = sorted(details.items())[:limit]
    return ", ".join(f"{k}={v}" for k, v in items)


# ---------------------------------------------------------------- alert formatters


def fmt_start_stop(
    event: str,
    mode: str,
    *,
    git_commit: str = "",
    config_hash: str = "",
    reason: str = "",
    equity: Any = None,
) -> str:
    """Start / stop / restart of the agent: ``"STARTED wta [shadow] commit abc1234 config ..."``."""
    word = {"start": "STARTED", "stop": "STOPPED", "restart": "RESTARTED"}.get(event.lower(), event.upper())
    parts = [f"{word} wta [{mode}]"]
    if git_commit:
        parts.append(f"commit {git_commit[:12]}")
    if config_hash:
        parts.append(f"config {config_hash}")
    if equity is not None:
        parts.append(f"equity {fmt_amount(equity, 'TMN')}")
    text = " ".join(parts)
    return f"{text}\nreason: {reason}" if reason else text


def fmt_fill(
    strategy: str,
    symbol: str,
    side: str,
    notional: Any,
    *,
    pnl: Any = None,
    pnl_pct: Any = None,
    pnl_asset: str = "TMN",
    qty: Any = None,
    price: Any = None,
    quote: str | None = None,
    is_maker: bool | None = None,
) -> str:
    """One fill: ``"FILL S1 BUY BTCUSDT 12.43 USDT (0.00015 @ 82,845.00, maker)"``.

    ``pnl`` (realized, in ``pnl_asset``) is shown only when the fill closes a position.
    """
    q = (quote or _quote_of(symbol)).upper()
    text = f"FILL {strategy} {side.upper()} {symbol.upper()} {fmt_amount(notional, q)}"
    extra: list[str] = []
    qd, pd = _dec(qty), _dec(price)
    if qd is not None and pd is not None:
        extra.append(f"{_natural(qd)} @ {_natural(pd)}")
    if is_maker is not None:
        extra.append("maker" if is_maker else "taker")
    if extra:
        text += f" ({', '.join(extra)})"
    if pnl is not None:
        d = _dec(pnl)
        sign = "+" if d is not None and d > 0 else ""
        closed = f"closed: PnL {sign}{fmt_amount(pnl, pnl_asset)}"
        if pnl_pct is not None:
            closed += f" ({fmt_pct(pnl_pct)})"
        text += "\n" + closed
    return text


def fmt_risk(action: str, reason: str = "", *, source: str = "", details: Mapping[str, Any] | None = None) -> str:
    """Risk state changes: pause / resume / kill / reset_kill (any other action is upper-cased)."""
    a = action.lower()
    head = {
        "pause": "RISK PAUSE: no new entries",
        "resume": "RISK RESUME: entries allowed again",
        "kill": "RISK KILL-SWITCH: canceling entries, flattening everything to TMN, then monitoring only",
        "reset_kill": "RISK KILL-SWITCH RESET: trading may start again",
    }.get(a, f"RISK {action.upper()}")
    lines = [head]
    if reason:
        lines.append(f"reason: {reason}")
    if source:
        lines.append(f"source: {source}")
    kv = _kv(details)
    if kv:
        lines.append(kv)
    return "\n".join(lines)


_FLATTEN_PHASES: dict[str, tuple[str, str]] = {
    "flatten_start": ("phase 1 (T-2h)", "no new exposure; selling everything to TMN with maker-first limits"),
    "flatten_final": ("phase 2 (T-30min)", "only flatten orders; leftovers sold with MARKET orders"),
    "challenge_end": ("phase 3 (end)", "every open order canceled; no orders from now on"),
    "kill": ("kill-switch", "selling every non-TMN balance to TMN, then monitoring only"),
    "manual": ("manual", "flatten-now requested by the operator"),
    "complete": ("complete", "100% TMN (dust only)"),
}
_PHASE_NAMES = {1: "flatten_start", 2: "flatten_final", 3: "challenge_end"}


def fmt_flatten(phase: int | str, detail: str = "") -> str:
    """A flatten phase: ``1``/``"flatten_start"``, ``2``/``"flatten_final"``, ``3``/``"challenge_end"``,
    ``"kill"``, ``"manual"``, ``"complete"``."""
    key = _PHASE_NAMES.get(phase, "") if isinstance(phase, int) else str(phase).lower()
    label, what = _FLATTEN_PHASES.get(key, (f"phase {phase}", ""))
    text = f"FLATTEN {label}" + (f": {what}" if what else "")
    return f"{text}\n{detail}" if detail else text


def fmt_llm_failures(n: int, last_error: str = "") -> str:
    """``n`` LLM calls failed in a row (the engine keeps the last valid plan or runs rules-only)."""
    text = f"LLM: {n} failures in a row; keeping the last valid plan (rules-only once it is stale)"
    return f"{text}\nlast error: {last_error[:300]}" if last_error else text


def fmt_stale(symbols: Iterable[str], age: Any, max_listed: int = 8) -> str:
    """Market data older than the alert threshold: ``"DATA STALE: no update for 135 s: BTCUSDT, ETHUSDT"``."""
    syms = sorted({s.upper() for s in symbols})
    shown = ", ".join(syms[:max_listed]) if syms else "(all markets)"
    if len(syms) > max_listed:
        shown += f" (+{len(syms) - max_listed} more)"
    d = _dec(age)
    age_txt = f"{int(d)} s" if d is not None else "n/a"
    return f"DATA STALE: no update for {age_txt}: {shown}"


def fmt_api_burst(n: int, window_s: float = 60) -> str:
    """An API error burst: ``"API ERRORS: 12 errors in the last 60 s"``."""
    return f"API ERRORS: {n} errors in the last {int(window_s)} s"


def fmt_equity_hourly(equity: Any, ret: Any, dd: Any, *, peak: Any = None, daily_pnl: Any = None) -> str:
    """Hourly equity line. ``ret``, ``dd`` and ``daily_pnl`` are fractions (``0.0123`` = 1.23%)."""
    parts = [f"EQUITY {fmt_amount(equity, 'TMN')}", f"return {fmt_pct(ret)}", f"drawdown {fmt_pct(dd, signed=False)}"]
    if daily_pnl is not None:
        parts.append(f"today {fmt_pct(daily_pnl)}")
    if peak is not None:
        parts.append(f"peak {fmt_amount(peak, 'TMN')}")
    return " | ".join(parts)


# ---------------------------------------------------------------- Bot API plumbing


@dataclass(frozen=True)
class _ApiResult:
    ok: bool
    result: Any = None
    error: str = ""
    status: int | None = None
    retry_after: float | None = None

    @property
    def transient(self) -> bool:
        """Worth retrying later: transport errors, 429 and 5xx (not 400 bad chat / 401 bad token)."""
        return self.status is None or self.status == 429 or self.status >= 500


def _parse_response(resp: httpx.Response, method: str, tokens: Iterable[str]) -> _ApiResult:
    try:
        body = resp.json()
    except ValueError:
        body = None
    if resp.status_code == 200 and isinstance(body, dict) and body.get("ok") is True:
        return _ApiResult(True, result=body.get("result"), status=200)
    desc = ""
    retry_after = None
    if isinstance(body, dict):
        desc = str(body.get("description") or "")
        params = body.get("parameters")
        if isinstance(params, dict) and _dec(params.get("retry_after")) is not None:
            retry_after = float(params["retry_after"])
    else:
        desc = resp.text[:200]
    error = _mask(f"{method}: HTTP {resp.status_code}: {desc}".rstrip(": "), tokens)
    return _ApiResult(False, error=error, status=resp.status_code, retry_after=retry_after)


def _exc_text(method: str, exc: BaseException, tokens: Iterable[str]) -> str:
    return _mask(f"{method}: {type(exc).__name__}: {exc}", tokens)


class _BotApi:
    """Shared Bot API access. The token lives in ``_token`` only and is masked everywhere else."""

    def __init__(
        self,
        token: Any,
        chat_id: Any,
        transport: httpx.AsyncBaseTransport | None,
        api_base: str,
    ) -> None:
        self._token = _secret(token)
        self.chat_id = str(chat_id or "").strip()
        self._transport = transport
        self._api_base = api_base.rstrip("/")
        _register_token(self._token)

    @property
    def configured(self) -> bool:
        return bool(self._token and self.chat_id)

    def _mask(self, text: str) -> str:
        return _mask(text, [self._token])

    async def _call(self, method: str, payload: dict[str, Any], timeout: float) -> _ApiResult:
        url = f"{self._api_base}/bot{self._token}/{method}"
        try:
            async with httpx.AsyncClient(timeout=timeout, transport=self._transport) as client:
                resp = await client.post(url, json=payload)
        except Exception as exc:  # noqa: BLE001 - never raise; the message is masked
            return _ApiResult(False, error=_exc_text(method, exc, [self._token]))
        return _parse_response(resp, method, [self._token])

    def __repr__(self) -> str:
        return f"{type(self).__name__}(chat_id={self.chat_id!r}, token={'***' if self._token else '<empty>'})"


# ---------------------------------------------------------------- outbound alerts


@dataclass
class TelegramStats:
    sent: int = 0  # messages delivered (a digest counts once)
    failed: int = 0  # delivery attempts that failed (HTTP error, 429, network)
    queued: int = 0  # alerts that went into the digest
    digests_sent: int = 0
    digest_dropped: int = 0  # queued alerts dropped: digest full (oldest first) or a permanent error
    deduped: int = 0
    disabled: int = 0  # send() calls while disabled / not configured
    last_error: str = ""

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class _Queued:
    ts: float
    kind: str
    text: str


@dataclass
class _Digest:
    items: list[_Queued] = field(default_factory=list)
    dropped: int = 0


def build_digest(items: list[_Queued], dropped: int, limit_per_10min: int) -> str:
    """ONE message summarising the alerts the rate limit held back, oldest first.

    Header with the count, time span and per-kind counts; then one line per alert (first line,
    shortened) while it fits into Telegram's limit; then ``"... and N more"``.
    """
    n = len(items)
    span = f"{iso(items[0].ts)[11:19]}-{iso(items[-1].ts)[11:19]} UTC" if items else ""
    title = f"DIGEST: {n} alert{'s' if n != 1 else ''} held back by the rate limit ({limit_per_10min} msgs / 10 min)"
    head = [f"{title}, {span}" if span else title]
    counts = Counter(i.kind for i in items)
    if counts:
        head.append(", ".join(f"{k} x{c}" for k, c in sorted(counts.items(), key=lambda kc: (-kc[1], kc[0]))))
    if dropped:
        head.append(f"({dropped} older alert{'s' if dropped != 1 else ''} dropped: digest full)")
    text = "\n".join(head)
    reserve = 60  # room for the "... and N more" line
    for idx, item in enumerate(items):
        first = item.text.strip().splitlines()[0] if item.text.strip() else ""
        if len(first) > _DIGEST_LINE_CHARS:
            first = first[: _DIGEST_LINE_CHARS - 3] + "..."
        line = f"\n{iso(item.ts)[11:19]} [{item.kind}] {first}"
        if _utf16_len(text) + _utf16_len(line) > TELEGRAM_MAX_CHARS - reserve:
            text += f"\n... and {n - idx} more (see logs/events.jsonl)"
            break
        text += line
    return text


class TelegramNotifier(_BotApi):
    """Rate-limited Telegram alerts with a digest for the overflow. See the module docstring."""

    def __init__(
        self,
        token: Any,
        chat_id: Any,
        clock: Clock,
        enabled: bool = True,
        max_msgs_per_10min: int = 20,
        transport: httpx.AsyncBaseTransport | None = None,
        *,
        prefix: str = "",
        timeout: float = 10.0,
        dedupe_s: float = 300.0,
        max_digest_items: int = 500,
        api_base: str = API_BASE,
    ) -> None:
        super().__init__(token, chat_id, transport, api_base)
        self.clock = clock
        self.enabled = bool(enabled and self.configured)
        self.max_msgs = max(1, int(max_msgs_per_10min))
        self.prefix = prefix
        self.timeout = timeout
        self.dedupe_s = dedupe_s
        self.max_digest_items = max(1, int(max_digest_items))
        self.counters = TelegramStats()
        self._window: deque[float] = deque()  # timestamps of delivery attempts in the last 10 min
        self._digest = _Digest()
        self._blocked_until = 0.0  # Telegram's retry_after, or our backoff after a network failure
        self._backoff_s = _BACKOFF_START_S
        self._last_key_ts: dict[str, float] = {}
        self._lock = asyncio.Lock()
        self._tasks: set[asyncio.Task[Any]] = set()

    @classmethod
    def from_settings(
        cls,
        settings: Any,
        alerts_cfg: Any,
        clock: Clock,
        *,
        prefix: str = "",
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> TelegramNotifier:
        """From ``Settings`` (TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID) and ``Config.alerts``."""
        return cls(
            settings.telegram_bot_token,
            settings.telegram_chat_id,
            clock,
            enabled=bool(getattr(alerts_cfg, "telegram_enabled", False)),
            max_msgs_per_10min=int(getattr(alerts_cfg, "max_msgs_per_10min", 20)),
            transport=transport,
            prefix=prefix,
        )

    # ------------------------------------------------------------ public API

    async def send(self, text: str, kind: str = "info", *, dedupe_key: str | None = None) -> SendStatus:
        """Send now if the rolling window allows, else queue for the digest. Never raises.

        Returns ``"sent"``, ``"queued"`` (digest), ``"failed"`` (permanent error, dropped),
        ``"disabled"`` or ``"deduped"`` (same ``dedupe_key`` within ``dedupe_s``).
        """
        if not self.enabled:
            self.counters.disabled += 1
            return "disabled"
        try:
            async with self._lock:
                now = self.clock.now()
                if dedupe_key is not None and self._is_duplicate(dedupe_key, now):
                    self.counters.deduped += 1
                    return "deduped"
                await self._flush_locked(now, force=False)
                if self._digest.items or not self._has_slot(now):
                    self._enqueue(now, kind, text)
                    return "queued"
                res = await self._deliver(self._compose(text), now)
                if res.ok:
                    return "sent"
                if res.transient:
                    self._enqueue(now, kind, text)
                    return "queued"
                return "failed"
        except Exception as exc:  # noqa: BLE001 - alerts must never break the caller
            self._record_failure(_exc_text("send", exc, [self._token]))
            return "failed"

    async def flush_digest(self, force: bool = False) -> bool:
        """Send the queued alerts as ONE message if the window allows. True if it was delivered.

        ``force=True`` ignores the rate limit and any backoff (one message, e.g. at shutdown).
        """
        if not self.enabled or not self._digest.items:
            return False
        try:
            async with self._lock:
                return await self._flush_locked(self.clock.now(), force=force)
        except Exception as exc:  # noqa: BLE001
            self._record_failure(_exc_text("flush_digest", exc, [self._token]))
            return False

    def send_nowait(self, text: str, kind: str = "info", *, dedupe_key: str | None = None) -> None:
        """Schedule ``send`` on the running loop and return at once (fire-and-forget)."""
        if not self.enabled:
            self.counters.disabled += 1
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            self._record_failure("send_nowait: no running event loop; alert dropped")
            return
        task = loop.create_task(self.send(text, kind, dedupe_key=dedupe_key))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def drain(self) -> None:
        """Wait for every ``send_nowait`` still in flight."""
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)

    @property
    def digest_pending(self) -> int:
        return len(self._digest.items)

    def stats(self) -> dict[str, Any]:
        """Counters for the heartbeat ``extra`` (no secrets)."""
        out = self.counters.as_dict()
        out.update(
            enabled=self.enabled,
            digest_pending=self.digest_pending,
            window_used=len(self._window),
            window_max=self.max_msgs,
        )
        return out

    # ------------------------------------------------------------ internals

    def _compose(self, text: str) -> str:
        body = str(text)
        return trim_text(f"{self.prefix} {body}" if self.prefix else body)

    def _prune(self, now: float) -> None:
        while self._window and self._window[0] <= now - WINDOW_S:
            self._window.popleft()

    def _has_slot(self, now: float) -> bool:
        self._prune(now)
        return now >= self._blocked_until and len(self._window) < self.max_msgs

    def _is_duplicate(self, key: str, now: float) -> bool:
        last = self._last_key_ts.get(key)
        if last is not None and now - last < self.dedupe_s:
            return True
        self._last_key_ts[key] = now
        if len(self._last_key_ts) > 1000:
            self._last_key_ts = {k: t for k, t in self._last_key_ts.items() if now - t < self.dedupe_s}
        return False

    def _enqueue(self, now: float, kind: str, text: str) -> None:
        body = str(text)
        if len(body) > _DIGEST_ITEM_CHARS:
            body = body[: _DIGEST_ITEM_CHARS - 3] + "..."
        self._digest.items.append(_Queued(now, str(kind), body))
        self.counters.queued += 1
        self._cap_digest()

    def _cap_digest(self) -> None:
        over = len(self._digest.items) - self.max_digest_items
        if over > 0:
            del self._digest.items[:over]
            self._digest.dropped += over
            self.counters.digest_dropped += over

    async def _flush_locked(self, now: float, force: bool) -> bool:
        if not self._digest.items:
            return False
        if not force and not self._has_slot(now):
            return False
        pending, self._digest = self._digest, _Digest()
        text = self._compose(build_digest(pending.items, pending.dropped, self.max_msgs))
        res = await self._deliver(text, now)
        if res.ok:
            self.counters.digests_sent += 1
            return True
        # put them back in front of anything queued meanwhile; a permanent error drops them
        if res.transient:
            self._digest.items[:0] = pending.items
            self._digest.dropped += pending.dropped
            self._cap_digest()
        else:
            self.counters.digest_dropped += len(pending.items)
        return False

    async def _deliver(self, text: str, now: float) -> _ApiResult:
        self._window.append(now)  # every attempt counts against the window (reserved before awaiting)
        res = await self._call(
            "sendMessage",
            {"chat_id": self.chat_id, "text": text, "disable_web_page_preview": True},
            self.timeout,
        )
        if res.ok:
            self.counters.sent += 1
            self._backoff_s = _BACKOFF_START_S
            return res
        self._record_failure(res.error)
        if res.retry_after is not None:
            self._blocked_until = max(self._blocked_until, now + res.retry_after)
        elif res.transient:
            self._blocked_until = max(self._blocked_until, now + self._backoff_s)
            self._backoff_s = min(self._backoff_s * 2, _BACKOFF_MAX_S)
        return res

    def _record_failure(self, error: str) -> None:
        self.counters.failed += 1
        self.counters.last_error = self._mask(error)
        log.warning("telegram: %s", self.counters.last_error)


# ---------------------------------------------------------------- inbound commands


def parse_command(text: str) -> tuple[str, str, str] | None:
    """``"/pause@wta_bot  maintenance"`` -> ``("pause", "maintenance", "wta_bot")``.

    Returns ``(command, args, bot_mention)`` with the command lower-cased, or None when the text
    is not a command (does not start with ``/``).
    """
    s = (text or "").strip()
    if not s.startswith("/"):
        return None
    head, *rest = s.split(maxsplit=1)
    name, _, mention = head[1:].partition("@")
    return name.lower(), " ".join(rest[0].split()) if rest else "", mention


@dataclass(frozen=True)
class CommandResult:
    """What one accepted-chat command did (for tests and the engine's own bookkeeping)."""

    command: str
    args: str
    reply: str
    executed: bool  # True only when on_command ran
    reason: str = ""  # "", "stale", "unsupported", "help", "error"


@dataclass
class CommandStats:
    polls: int = 0
    poll_errors: int = 0
    updates: int = 0
    executed: int = 0
    ignored_chat: int = 0  # messages from any chat other than the configured one
    ignored_other: int = 0  # non-command text, edits, other update types
    stale: int = 0
    unsupported: int = 0
    handler_errors: int = 0
    reply_errors: int = 0
    last_error: str = ""

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


OnCommand = Callable[[str, str], Awaitable[str]]


class TelegramCommands(_BotApi):
    """Operational inbound commands over ``getUpdates`` long polling. See the module docstring.

    ``on_command(command, args)`` is called for ``status``/``pause``/``resume``/``kill`` only and
    returns the reply text. It must not place orders; the engine logs an event with
    ``source: "telegram"`` for each call.
    """

    def __init__(
        self,
        token: Any,
        chat_id: Any,
        on_command: OnCommand,
        clock: Clock,
        transport: httpx.AsyncBaseTransport | None = None,
        *,
        enabled: bool = True,
        poll_timeout_s: int = 25,
        poll_interval_s: float = 1.0,
        error_backoff_s: float = 1.0,
        max_age_s: float = 120.0,
        bot_username: str = "",
        prefix: str = "",
        api_base: str = API_BASE,
    ) -> None:
        super().__init__(token, chat_id, transport, api_base)
        self.on_command = on_command
        self.clock = clock
        self.enabled = bool(enabled and self.configured)
        self.poll_timeout_s = int(poll_timeout_s)
        self.poll_interval_s = poll_interval_s
        self.error_backoff_s = error_backoff_s  # doubles per failed poll, up to 60 s
        self.max_age_s = max_age_s
        self.bot_username = bot_username.lstrip("@").lower()
        self.prefix = prefix
        self.offset = 0  # next update_id to fetch; everything below it is confirmed
        self.counters = CommandStats()
        self._retry_after: float | None = None

    @classmethod
    def from_settings(
        cls,
        settings: Any,
        alerts_cfg: Any,
        on_command: OnCommand,
        clock: Clock,
        *,
        prefix: str = "",
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> TelegramCommands:
        return cls(
            settings.telegram_bot_token,
            settings.telegram_chat_id,
            on_command,
            clock,
            transport,
            enabled=bool(getattr(alerts_cfg, "telegram_enabled", False)),
            prefix=prefix,
        )

    async def poll_once(self) -> list[CommandResult]:
        """One ``getUpdates`` round: handle every new update, advance ``offset``. Never raises."""
        if not self.enabled:
            return []
        self.counters.polls += 1
        res = await self._call(
            "getUpdates",
            {"offset": self.offset, "timeout": self.poll_timeout_s, "allowed_updates": ["message"]},
            self.poll_timeout_s + 10,
        )
        if not res.ok:
            self.counters.poll_errors += 1
            self._record_error(res.error)
            self._retry_after = res.retry_after
            return []
        self._retry_after = None
        updates = res.result if isinstance(res.result, list) else []
        out: list[CommandResult] = []
        for update in sorted((u for u in updates if isinstance(u, dict)), key=_update_id):
            uid = _update_id(update)
            if uid < 0 or uid < self.offset:
                continue  # malformed, or already handled (redelivery)
            self.offset = uid + 1  # confirmed on the next call, whatever happens below
            self.counters.updates += 1
            try:
                result = await self._handle(update)
            except Exception as exc:  # noqa: BLE001 - one bad update must not stop the poller
                self.counters.handler_errors += 1
                self._record_error(_exc_text("update", exc, [self._token]))
                continue
            if result is not None:
                out.append(result)
        return out

    async def run(self, stop: asyncio.Event | None = None) -> None:
        """Poll until ``stop`` is set (or the task is canceled), backing off on errors."""
        if not self.enabled:
            return
        stop = stop or asyncio.Event()
        backoff = self.error_backoff_s
        while not stop.is_set():
            errors_before = self.counters.poll_errors
            await self.poll_once()
            if self.counters.poll_errors > errors_before:
                delay = self._retry_after if self._retry_after is not None else backoff
                backoff = min(backoff * 2, 60.0)
            else:
                delay = self.poll_interval_s
                backoff = self.error_backoff_s
            if delay > 0:
                try:
                    await asyncio.wait_for(stop.wait(), timeout=delay)
                except TimeoutError:
                    pass
            else:
                await asyncio.sleep(0)

    # ------------------------------------------------------------ internals

    async def _handle(self, update: dict[str, Any]) -> CommandResult | None:
        msg = update.get("message")
        if not isinstance(msg, dict):
            self.counters.ignored_other += 1  # edited_message, channel_post, callbacks, ...
            return None
        chat = msg.get("chat") if isinstance(msg.get("chat"), dict) else {}
        chat_id = str(chat.get("id", "")).strip()
        if chat_id != self.chat_id:
            self.counters.ignored_chat += 1
            log.warning("telegram: ignored a message from chat %s (not the configured chat)", chat_id or "?")
            return None
        text = msg.get("text")
        parsed = parse_command(text) if isinstance(text, str) else None
        if parsed is None:
            self.counters.ignored_other += 1
            return None
        command, args, mention = parsed
        if mention and self.bot_username and mention.lower() != self.bot_username:
            self.counters.ignored_other += 1  # addressed to another bot in the same group
            return None
        sent_at = _dec(msg.get("date"))
        age = self.clock.now() - float(sent_at) if sent_at is not None else 0.0
        if age > self.max_age_s:
            self.counters.stale += 1
            reply = (
                f"/{command} ignored: it was sent {int(age)} s ago (limit {int(self.max_age_s)} s). "
                "Send it again if it still applies."
            )
            result = CommandResult(command, args, reply, executed=False, reason="stale")
        elif command in LOCAL_COMMANDS:
            result = CommandResult(command, args, HELP_TEXT, executed=False, reason="help")
        elif command in SUPPORTED_COMMANDS:
            try:
                reply = await self.on_command(command, args)
                result = CommandResult(command, args, str(reply) if reply else f"/{command}: ok", executed=True)
                self.counters.executed += 1
            except Exception as exc:  # noqa: BLE001 - reply with the failure, keep polling
                self.counters.handler_errors += 1
                self._record_error(_exc_text(f"/{command}", exc, [self._token]))
                result = CommandResult(
                    command, args, f"/{command} failed: {type(exc).__name__}", executed=True, reason="error"
                )
        else:
            self.counters.unsupported += 1
            result = CommandResult(
                command, args, NO_TRADING_TEXT.format(cmd=command or "?"), executed=False, reason="unsupported"
            )
        await self._reply(result.reply)
        return result

    async def _reply(self, text: str) -> None:
        body = trim_text(f"{self.prefix} {text}" if self.prefix else text)
        res = await self._call("sendMessage", {"chat_id": self.chat_id, "text": body}, 10.0)
        if not res.ok:
            self.counters.reply_errors += 1
            self._record_error(res.error)

    def stats(self) -> dict[str, Any]:
        """Counters for the heartbeat ``extra`` (no secrets)."""
        return {**self.counters.as_dict(), "enabled": self.enabled, "offset": self.offset}

    def _record_error(self, error: str) -> None:
        self.counters.last_error = self._mask(error)
        log.warning("telegram: %s", self.counters.last_error)


def _update_id(update: dict[str, Any]) -> int:
    uid = update.get("update_id")
    return uid if isinstance(uid, int) and not isinstance(uid, bool) else -1


# ---------------------------------------------------------------- one-shot CLI helper


def send_alert_cli(
    settings: Any,
    text: str,
    *,
    transport: httpx.BaseTransport | None = None,
    timeout: float = 10.0,
    prefix: str = "",
    api_base: str = API_BASE,
) -> bool:
    """Synchronous one-shot alert for ``wta alert "text"``. True if Telegram accepted it.

    A no-op (False, no network) when ``TELEGRAM_BOT_TOKEN`` or ``TELEGRAM_CHAT_ID`` is empty.
    Never raises; errors are logged with the token masked.
    """
    token = _secret(getattr(settings, "telegram_bot_token", None))
    chat_id = str(getattr(settings, "telegram_chat_id", "") or "").strip()
    if not token or not chat_id:
        log.info("telegram: not configured; alert not sent")
        return False
    _register_token(token)
    body = trim_text(f"{prefix} {text}" if prefix else str(text))
    url = f"{api_base.rstrip('/')}/bot{token}/sendMessage"
    try:
        with httpx.Client(timeout=timeout, transport=transport) as client:
            resp = client.post(url, json={"chat_id": chat_id, "text": body, "disable_web_page_preview": True})
        res = _parse_response(resp, "sendMessage", [token])
    except Exception as exc:  # noqa: BLE001 - a failed alert must not fail the caller
        res = _ApiResult(False, error=_exc_text("sendMessage", exc, [token]))
    if not res.ok:
        log.warning("telegram: alert not sent: %s", _mask(res.error, [token]))
    return res.ok


__all__ = [
    "API_BASE",
    "HELP_TEXT",
    "SUPPORTED_COMMANDS",
    "TELEGRAM_MAX_CHARS",
    "TRUNCATED",
    "CommandResult",
    "CommandStats",
    "SendStatus",
    "TelegramCommands",
    "TelegramNotifier",
    "TelegramStats",
    "build_digest",
    "fmt_amount",
    "fmt_api_burst",
    "fmt_equity_hourly",
    "fmt_fill",
    "fmt_flatten",
    "fmt_llm_failures",
    "fmt_pct",
    "fmt_risk",
    "fmt_stale",
    "fmt_start_stop",
    "parse_command",
    "send_alert_cli",
    "trim_text",
]

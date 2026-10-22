"""Telegram alerts and operational commands (PRD-06 §2).

Every Bot API call is mocked with respx (or an ``httpx.MockTransport``); nothing reaches
api.telegram.org. The fake bot token has the real ``<digits>:<35 chars>`` shape so the masking is
tested against a realistic value, and it is assembled at runtime so this file never contains a
complete token-like string.
"""

from __future__ import annotations

import asyncio
import json
import logging
from decimal import Decimal
from typing import Any

import httpx
import pytest
import respx

from wta.config import AlertsCfg, Settings
from wta.models import EventRecord
from wta.utils.telegram import (
    HELP_TEXT,
    TELEGRAM_MAX_CHARS,
    TRUNCATED,
    TelegramCommands,
    TelegramNotifier,
    fmt_amount,
    fmt_api_burst,
    fmt_equity_hourly,
    fmt_fill,
    fmt_flatten,
    fmt_llm_failures,
    fmt_pct,
    fmt_risk,
    fmt_stale,
    fmt_start_stop,
    parse_command,
    send_alert_cli,
    trim_text,
)
from wta.utils.timeutil import SimClock, iso

from .conftest import T0

TOKEN = "1234567890" + ":" + "AAF" + "t" * 32
CHAT = "424242"
API = "https://api.telegram.org"
SEND_URL = f"{API}/bot{TOKEN}/sendMessage"
UPDATES_URL = f"{API}/bot{TOKEN}/getUpdates"


# ---------------------------------------------------------------- helpers


def ok(result: Any = None) -> httpx.Response:
    return httpx.Response(200, json={"ok": True, "result": result if result is not None else {"message_id": 1}})


def tg_error(status: int, description: str, retry_after: int | None = None) -> httpx.Response:
    body: dict[str, Any] = {"ok": False, "error_code": status, "description": description}
    if retry_after is not None:
        body["parameters"] = {"retry_after": retry_after}
    return httpx.Response(status, json=body)


def payloads(route: respx.Route) -> list[dict[str, Any]]:
    return [json.loads(call.request.content) for call in route.calls]


def texts(route: respx.Route) -> list[str]:
    return [p["text"] for p in payloads(route)]


def utf16_len(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


@pytest.fixture
def mock() -> Any:
    with respx.mock(assert_all_called=False) as router:
        yield router


@pytest.fixture
def clock() -> SimClock:
    return SimClock(T0)


def settings(token: str = TOKEN, chat: str = CHAT) -> Settings:
    return Settings(_env_file=None, telegram_bot_token=token, telegram_chat_id=chat)


# ---------------------------------------------------------------- notifier: sending + rate limit


async def test_send_posts_plain_text_to_the_configured_chat(mock: Any, clock: SimClock) -> None:
    route = mock.post(SEND_URL).mock(return_value=ok())
    n = TelegramNotifier(TOKEN, CHAT, clock, prefix="[shadow]")
    assert await n.send("STARTED wta", kind="start") == "sent"
    (payload,) = payloads(route)
    assert payload == {"chat_id": CHAT, "text": "[shadow] STARTED wta", "disable_web_page_preview": True}
    assert "parse_mode" not in payload  # plain text: no Markdown escaping surprises
    assert n.stats()["sent"] == 1


async def test_rate_limit_queues_overflow_and_flushes_it_as_one_digest(mock: Any, clock: SimClock) -> None:
    route = mock.post(SEND_URL).mock(return_value=ok())
    n = TelegramNotifier(TOKEN, CHAT, clock, max_msgs_per_10min=3)
    statuses = []
    for i in range(5):
        statuses.append(await n.send(f"alert {i}", kind="fill"))
        clock.advance(1)
    assert statuses == ["sent", "sent", "sent", "queued", "queued"]
    assert route.call_count == 3
    assert n.digest_pending == 2

    assert await n.flush_digest() is False  # the window is still full: nothing is sent
    assert route.call_count == 3

    clock.set(T0 + 600)  # the first message (T0) leaves the rolling window
    assert await n.flush_digest() is True
    assert route.call_count == 4
    digest = texts(route)[-1]
    assert digest.startswith("DIGEST: 2 alerts held back by the rate limit (3 msgs / 10 min)")
    assert "fill x2" in digest
    assert "[fill] alert 3" in digest and "[fill] alert 4" in digest
    assert "alert 0" not in digest  # already delivered on its own
    assert n.digest_pending == 0
    stats = n.stats()
    assert (stats["sent"], stats["queued"], stats["digests_sent"]) == (4, 2, 1)
    # the digest used the free slot: the window is full again
    assert await n.send("next") == "queued"
    assert await n.flush_digest() is False


async def test_window_is_rolling_not_a_fixed_bucket(mock: Any, clock: SimClock) -> None:
    route = mock.post(SEND_URL).mock(return_value=ok())
    n = TelegramNotifier(TOKEN, CHAT, clock, max_msgs_per_10min=2)
    assert await n.send("A") == "sent"  # T0
    clock.set(T0 + 300)
    assert await n.send("B") == "sent"
    clock.set(T0 + 400)
    assert await n.send("C") == "queued"
    clock.set(T0 + 601)  # A expired, B still inside the window
    assert await n.flush_digest() is True
    clock.set(T0 + 700)  # window (T0+100, T0+700] holds B and the digest: full
    assert await n.send("D") == "queued"
    assert route.call_count == 3
    clock.set(T0 + 901)  # B expired
    assert await n.flush_digest() is True
    assert route.call_count == 4


async def test_pending_digest_goes_out_before_a_new_alert(mock: Any, clock: SimClock) -> None:
    route = mock.post(SEND_URL).mock(return_value=ok())
    n = TelegramNotifier(TOKEN, CHAT, clock, max_msgs_per_10min=2)
    await n.send("A")
    clock.advance(1)
    await n.send("B")
    clock.advance(1)
    assert await n.send("C", kind="risk") == "queued"
    clock.set(T0 + 601)  # A and B both expired: two free slots
    assert await n.send("D") == "sent"
    sent = texts(route)
    assert sent[:2] == ["A", "B"]
    assert sent[2].startswith("DIGEST: 1 alert held back") and "[risk] C" in sent[2]
    assert sent[3] == "D"  # after the digest: the order of alerts is kept


async def test_digest_fits_telegram_limit_and_reports_dropped(mock: Any, clock: SimClock) -> None:
    route = mock.post(SEND_URL).mock(return_value=ok())
    n = TelegramNotifier(TOKEN, CHAT, clock, max_msgs_per_10min=1, max_digest_items=50)
    assert await n.send("first") == "sent"
    for i in range(80):
        clock.advance(1)
        kind = "risk" if i % 10 == 0 else "fill"
        assert await n.send(f"alert-{i:02d} " + "y" * 1000, kind=kind) == "queued"
    assert n.digest_pending == 50
    assert n.stats()["digest_dropped"] == 30
    clock.set(T0 + 700)
    assert await n.flush_digest() is True
    digest = texts(route)[-1]
    assert utf16_len(digest) <= TELEGRAM_MAX_CHARS
    assert digest.startswith("DIGEST: 50 alerts held back")
    assert "(30 older alerts dropped: digest full)" in digest
    assert "alert-29" not in digest and "alert-30" in digest  # the oldest were dropped
    assert "... and " in digest and "more (see logs/events.jsonl)" in digest
    assert "fill x45, risk x5" in digest


async def test_long_texts_are_trimmed_to_4096(mock: Any, clock: SimClock) -> None:
    route = mock.post(SEND_URL).mock(return_value=ok())
    n = TelegramNotifier(TOKEN, CHAT, clock)
    await n.send("x" * 5000)
    await n.send("\U0001f600" * 3000)  # astral characters count twice in Telegram's UTF-16 limit
    first, second = texts(route)
    assert len(first) == TELEGRAM_MAX_CHARS and first.endswith(TRUNCATED)
    assert utf16_len(second) <= TELEGRAM_MAX_CHARS and second.endswith(TRUNCATED)
    assert trim_text("short") == "short"
    assert trim_text("   ") == "(empty message)"


async def test_flood_control_429_queues_and_waits_retry_after(mock: Any, clock: SimClock) -> None:
    route = mock.post(SEND_URL).mock(
        side_effect=[tg_error(429, "Too Many Requests: retry after 30", retry_after=30), ok(), ok()]
    )
    n = TelegramNotifier(TOKEN, CHAT, clock)
    assert await n.send("A") == "queued"  # not lost: it waits in the digest
    clock.advance(10)
    assert await n.send("B") == "queued"
    assert route.call_count == 1  # no request while Telegram asked us to wait
    assert await n.flush_digest() is False
    clock.set(T0 + 31)
    assert await n.flush_digest() is True
    digest = texts(route)[-1]
    assert "A" in digest and "B" in digest and digest.startswith("DIGEST: 2 alerts")
    stats = n.stats()
    assert stats["failed"] == 1 and "HTTP 429" in stats["last_error"]


async def test_network_error_never_raises_backs_off_and_keeps_the_alert(mock: Any, clock: SimClock) -> None:
    route = mock.post(SEND_URL).mock(side_effect=[httpx.ConnectError(f"cannot connect to {SEND_URL}"), ok()])
    n = TelegramNotifier(TOKEN, CHAT, clock)
    assert await n.send("A", kind="risk") == "queued"
    clock.advance(5)
    assert await n.send("B") == "queued"  # backing off: no second request
    assert route.call_count == 1
    clock.set(T0 + 31)
    assert await n.flush_digest() is True
    assert "[risk] A" in texts(route)[-1]
    assert n.stats()["failed"] == 1


async def test_permanent_error_drops_the_alert(mock: Any, clock: SimClock) -> None:
    route = mock.post(SEND_URL).mock(return_value=tg_error(400, "Bad Request: chat not found"))
    n = TelegramNotifier(TOKEN, CHAT, clock)
    assert await n.send("A") == "failed"
    assert n.digest_pending == 0  # retrying a bad chat id would never help
    assert await n.send("B") == "failed"  # no backoff for permanent errors
    assert route.call_count == 2
    assert n.stats()["last_error"] == "sendMessage: HTTP 400: Bad Request: chat not found"


async def test_force_flush_ignores_the_window(mock: Any, clock: SimClock) -> None:
    route = mock.post(SEND_URL).mock(return_value=ok())
    n = TelegramNotifier(TOKEN, CHAT, clock, max_msgs_per_10min=1)
    await n.send("A")
    assert await n.send("STOPPED wta", kind="stop") == "queued"
    assert await n.flush_digest() is False
    assert await n.flush_digest(force=True) is True  # e.g. at shutdown
    assert "[stop] STOPPED wta" in texts(route)[-1]


async def test_dedupe_key_suppresses_repeats_within_dedupe_s(mock: Any, clock: SimClock) -> None:
    route = mock.post(SEND_URL).mock(return_value=ok())
    n = TelegramNotifier(TOKEN, CHAT, clock, dedupe_s=300)
    stale = fmt_stale(["BTCUSDT"], 130)
    assert await n.send(stale, "stale", dedupe_key="stale") == "sent"
    clock.advance(10)
    assert await n.send(stale, "stale", dedupe_key="stale") == "deduped"
    assert await n.send("other", "info") == "sent"  # no key: never deduped
    clock.advance(300)
    assert await n.send(stale, "stale", dedupe_key="stale") == "sent"
    assert route.call_count == 3 and n.stats()["deduped"] == 1


async def test_send_nowait_schedules_in_order_and_drain_waits(mock: Any, clock: SimClock) -> None:
    route = mock.post(SEND_URL).mock(return_value=ok())
    n = TelegramNotifier(TOKEN, CHAT, clock)
    for i in range(3):
        n.send_nowait(f"fill {i}", "fill")
    assert route.call_count == 0  # nothing awaited yet
    await n.drain()
    assert texts(route) == ["fill 0", "fill 1", "fill 2"]


def test_send_nowait_without_a_loop_does_not_raise(clock: SimClock) -> None:
    n = TelegramNotifier(TOKEN, CHAT, clock)
    n.send_nowait("x")
    assert n.stats()["failed"] == 1


async def test_injected_transport_is_used(clock: SimClock) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return ok()

    n = TelegramNotifier(TOKEN, CHAT, clock, transport=httpx.MockTransport(handler))
    assert await n.send("via transport") == "sent"
    assert len(seen) == 1 and seen[0].url.path.endswith("/sendMessage")


async def test_disabled_or_unconfigured_is_a_no_op(mock: Any, clock: SimClock) -> None:
    route = mock.post(url__regex=r"https://api\.telegram\.org/.*").mock(return_value=ok())
    for n in (
        TelegramNotifier(TOKEN, CHAT, clock, enabled=False),
        TelegramNotifier("", CHAT, clock),
        TelegramNotifier(TOKEN, "", clock),
    ):
        assert n.enabled is False
        assert await n.send("x") == "disabled"
        n.send_nowait("y")
        await n.drain()
        assert await n.flush_digest(force=True) is False
        assert n.stats()["disabled"] == 2
    assert route.call_count == 0


def test_from_settings_uses_alerts_config(clock: SimClock) -> None:
    n = TelegramNotifier.from_settings(
        settings(), AlertsCfg(telegram_enabled=True, max_msgs_per_10min=7), clock, prefix="[live]"
    )
    assert n.enabled and n.max_msgs == 7 and n.chat_id == CHAT and n.prefix == "[live]"
    assert TelegramNotifier.from_settings(settings(), AlertsCfg(), clock).enabled is False  # off by default
    assert TelegramNotifier.from_settings(settings(token=""), AlertsCfg(telegram_enabled=True), clock).enabled is False


# ---------------------------------------------------------------- the token never leaks


async def test_token_never_in_logs_errors_stats_or_repr(
    mock: Any, clock: SimClock, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    for name in ("httpx", "httpcore", "wta.utils.telegram"):
        caplog.set_level(logging.DEBUG, logger=name)
    mock.post(SEND_URL).mock(
        side_effect=[
            ok(),  # httpx logs "HTTP Request: POST <url with the token>" at INFO
            tg_error(500, f"Internal error for /bot{TOKEN}/sendMessage"),  # echoed back by a proxy
            httpx.ConnectError(f"cannot connect to {SEND_URL}"),
        ]
    )
    n = TelegramNotifier(TOKEN, CHAT, clock, max_msgs_per_10min=10)
    assert await n.send("one") == "sent"
    assert await n.send("two") == "queued"
    clock.advance(60)  # past the backoff
    assert await n.flush_digest() is False

    mock.post(UPDATES_URL).mock(side_effect=httpx.ReadTimeout(f"timed out reading {UPDATES_URL}"))
    cmds = TelegramCommands(TOKEN, CHAT, _never_called, clock)
    assert await cmds.poll_once() == []

    url_lines = [r.getMessage() for r in caplog.records if r.name == "httpx"]
    assert url_lines and all("/bot***/" in line for line in url_lines)  # the filter really ran
    everything = "\n".join(
        [caplog.text, str(n.stats()), str(cmds.stats()), repr(n), repr(cmds), *(r.getMessage() for r in caplog.records)]
    )
    assert TOKEN not in everything
    assert TOKEN.split(":")[1] not in everything  # not even the secret half
    assert "ConnectError" in n.stats()["last_error"] and "***" in n.stats()["last_error"]
    assert "ReadTimeout" in cmds.stats()["last_error"]


# ---------------------------------------------------------------- inbound commands


def update(
    uid: int, text: str, *, chat: str = CHAT, date: float = T0, kind: str = "message"
) -> dict[str, Any]:
    return {
        "update_id": uid,
        kind: {
            "message_id": 1000 + uid,
            "date": int(date),
            "chat": {"id": int(chat), "type": "private"},
            "from": {"id": int(chat), "is_bot": False, "first_name": "op"},
            "text": text,
        },
    }


def updates(*items: dict[str, Any]) -> httpx.Response:
    return ok(list(items))


async def _never_called(command: str, args: str) -> str:  # pragma: no cover - asserted by tests
    raise AssertionError(f"on_command must not be called ({command} {args})")


class FakeEngine:
    """Stands in for the engine side: records the event it would log with source=telegram."""

    def __init__(self, clock: SimClock) -> None:
        self.clock = clock
        self.calls: list[tuple[str, str]] = []
        self.events: list[EventRecord] = []
        self.paused = False

    async def on_command(self, command: str, args: str) -> str:
        self.calls.append((command, args))
        self.events.append(
            EventRecord(ts=iso(self.clock.now()), kind=command, reason=args, data={"source": "telegram"})
        )
        if command == "pause":
            self.paused = True
            return "paused: no new entries"
        if command == "resume":
            self.paused = False
            return "resumed"
        if command == "kill":
            return "kill-switch tripped: flattening to TMN"
        return "equity 5,000,000 TMN | drawdown 0.00% | paused no"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("/status", ("status", "", "")),
        ("  /PAUSE   maintenance   window ", ("pause", "maintenance window", "")),
        ("/kill@wta_bot drawdown looks wrong", ("kill", "drawdown looks wrong", "wta_bot")),
        ("/resume\nall good", ("resume", "all good", "")),
        ("/buy 1 BTC", ("buy", "1 BTC", "")),
        ("/", ("", "", "")),
        ("status", None),
        ("", None),
    ],
)
def test_parse_command(text: str, expected: tuple[str, str, str] | None) -> None:
    assert parse_command(text) == expected


async def test_supported_commands_reach_the_engine_and_get_a_reply(mock: Any, clock: SimClock) -> None:
    mock.post(UPDATES_URL).mock(
        return_value=updates(
            update(1, "/status"),
            update(2, "/pause maintenance"),
            update(3, "/resume"),
            update(4, "/kill drawdown"),
        )
    )
    send = mock.post(SEND_URL).mock(return_value=ok())
    engine = FakeEngine(clock)
    cmds = TelegramCommands(TOKEN, CHAT, engine.on_command, clock, prefix="[live]")
    results = await cmds.poll_once()
    assert engine.calls == [("status", ""), ("pause", "maintenance"), ("resume", ""), ("kill", "drawdown")]
    assert [r.executed for r in results] == [True] * 4
    assert texts(send) == [
        "[live] equity 5,000,000 TMN | drawdown 0.00% | paused no",
        "[live] paused: no new entries",
        "[live] resumed",
        "[live] kill-switch tripped: flattening to TMN",
    ]
    assert all(p["chat_id"] == CHAT for p in payloads(send))
    # one event per command, each tagged source=telegram (the engine side writes them)
    assert [(e.kind, e.data["source"]) for e in engine.events] == [
        ("status", "telegram"),
        ("pause", "telegram"),
        ("resume", "telegram"),
        ("kill", "telegram"),
    ]
    assert cmds.stats()["executed"] == 4


async def test_trading_commands_are_refused_without_reaching_the_engine(mock: Any, clock: SimClock) -> None:
    mock.post(UPDATES_URL).mock(
        return_value=updates(
            update(1, "/buy BTCUSDT 0.001"),
            update(2, "/sell ETHTMN all"),
            update(3, "/order LIMIT BUY BTCTMN"),
            update(4, "/cancel S1_BTCUSDT_1"),
            update(5, "/flatten"),
        )
    )
    send = mock.post(SEND_URL).mock(return_value=ok())
    cmds = TelegramCommands(TOKEN, CHAT, _never_called, clock)
    results = await cmds.poll_once()
    assert [r.reason for r in results] == ["unsupported"] * 5
    assert not any(r.executed for r in results)
    replies = texts(send)
    assert len(replies) == 5
    assert all("manual trading is not possible through Telegram" in r for r in replies)
    assert replies[0].startswith("/buy is not supported")
    assert cmds.stats()["unsupported"] == 5


async def test_only_the_configured_chat_is_accepted(
    mock: Any, clock: SimClock, caplog: pytest.LogCaptureFixture
) -> None:
    mock.post(UPDATES_URL).mock(
        return_value=updates(
            update(1, "/kill", chat="999"),
            update(2, "/pause", chat="-100555"),
            update(3, "/status"),
        )
    )
    send = mock.post(SEND_URL).mock(return_value=ok())
    engine = FakeEngine(clock)
    cmds = TelegramCommands(TOKEN, CHAT, engine.on_command, clock)
    results = await cmds.poll_once()
    assert engine.calls == [("status", "")]  # strangers never reach the engine
    assert [r.command for r in results] == ["status"]
    assert send.call_count == 1  # and get no reply
    stats = cmds.stats()
    assert stats["ignored_chat"] == 2 and stats["executed"] == 1
    assert cmds.offset == 4  # ignored updates are confirmed too
    assert "ignored a message from chat 999" in caplog.text


async def test_get_updates_offset_advances_and_redeliveries_are_skipped(mock: Any, clock: SimClock) -> None:
    route = mock.post(UPDATES_URL).mock(
        side_effect=[
            updates(update(10, "/status"), update(11, "/pause first")),
            updates(update(11, "/pause first"), update(12, "hello"), update(13, "x", kind="edited_message")),
            updates(update(14, "/resume")),
            updates(),
        ]
    )
    mock.post(SEND_URL).mock(return_value=ok())
    engine = FakeEngine(clock)
    cmds = TelegramCommands(TOKEN, CHAT, engine.on_command, clock, poll_timeout_s=25)
    for _ in range(4):
        await cmds.poll_once()
    sent = payloads(route)
    assert [p["offset"] for p in sent] == [0, 12, 14, 15]
    assert all(p["timeout"] == 25 and p["allowed_updates"] == ["message"] for p in sent)
    assert engine.calls == [("status", ""), ("pause", "first"), ("resume", "")]  # 11 handled once
    stats = cmds.stats()
    assert stats["updates"] == 5 and stats["ignored_other"] == 2  # plain text + an edit
    assert stats["offset"] == 15


async def test_stale_commands_are_not_executed(mock: Any, clock: SimClock) -> None:
    mock.post(UPDATES_URL).mock(
        return_value=updates(update(1, "/kill", date=T0 - 3600), update(2, "/status", date=T0 - 30))
    )
    send = mock.post(SEND_URL).mock(return_value=ok())
    engine = FakeEngine(clock)
    cmds = TelegramCommands(TOKEN, CHAT, engine.on_command, clock, max_age_s=120)
    results = await cmds.poll_once()
    assert engine.calls == [("status", "")]  # a /kill from an hour ago (agent was down) is not run
    assert results[0].reason == "stale" and not results[0].executed
    assert texts(send)[0].startswith("/kill ignored: it was sent 3600 s ago")
    assert cmds.stats()["stale"] == 1


async def test_help_is_answered_locally(mock: Any, clock: SimClock) -> None:
    mock.post(UPDATES_URL).mock(return_value=updates(update(1, "/help"), update(2, "/start")))
    send = mock.post(SEND_URL).mock(return_value=ok())
    cmds = TelegramCommands(TOKEN, CHAT, _never_called, clock)
    results = await cmds.poll_once()
    assert [r.reason for r in results] == ["help", "help"]
    assert texts(send) == [HELP_TEXT, HELP_TEXT]
    assert "manual trading is not possible" in HELP_TEXT


async def test_mentions_of_another_bot_are_ignored(mock: Any, clock: SimClock) -> None:
    mock.post(UPDATES_URL).mock(return_value=updates(update(1, "/kill@other_bot"), update(2, "/status@WTA_bot")))
    mock.post(SEND_URL).mock(return_value=ok())
    engine = FakeEngine(clock)
    cmds = TelegramCommands(TOKEN, CHAT, engine.on_command, clock, bot_username="@wta_bot")
    await cmds.poll_once()
    assert engine.calls == [("status", "")]


async def test_handler_errors_are_replied_not_raised(mock: Any, clock: SimClock) -> None:
    async def broken(command: str, args: str) -> str:
        raise RuntimeError(f"engine exploded near {TOKEN}")

    async def silent(command: str, args: str) -> str:
        return ""

    mock.post(UPDATES_URL).mock(return_value=updates(update(1, "/pause")))
    send = mock.post(SEND_URL).mock(return_value=ok())
    cmds = TelegramCommands(TOKEN, CHAT, broken, clock)
    (result,) = await cmds.poll_once()
    assert result.reason == "error" and texts(send) == ["/pause failed: RuntimeError"]
    assert cmds.stats()["handler_errors"] == 1
    assert TOKEN not in cmds.stats()["last_error"]

    cmds2 = TelegramCommands(TOKEN, CHAT, silent, clock)
    (result2,) = await cmds2.poll_once()
    assert result2.reply == "/pause: ok"


async def test_poll_and_reply_errors_never_raise(mock: Any, clock: SimClock) -> None:
    mock.post(UPDATES_URL).mock(
        side_effect=[
            tg_error(409, "Conflict: terminated by other getUpdates request"),
            httpx.ConnectError("offline"),
            updates(update(1, "/status")),
        ]
    )
    mock.post(SEND_URL).mock(return_value=tg_error(500, "Internal Server Error"))
    engine = FakeEngine(clock)
    cmds = TelegramCommands(TOKEN, CHAT, engine.on_command, clock)
    assert await cmds.poll_once() == []
    assert cmds.offset == 0 and "HTTP 409" in cmds.stats()["last_error"]
    assert await cmds.poll_once() == []
    (result,) = await cmds.poll_once()  # the reply fails, the command still ran
    assert result.executed and engine.calls == [("status", "")]
    stats = cmds.stats()
    assert (stats["poll_errors"], stats["reply_errors"], stats["offset"]) == (2, 1, 2)


async def test_run_polls_until_stopped(mock: Any, clock: SimClock) -> None:
    stop = asyncio.Event()
    engine = FakeEngine(clock)

    async def on_command(command: str, args: str) -> str:
        reply = await engine.on_command(command, args)
        stop.set()
        return reply

    route = mock.post(UPDATES_URL).mock(side_effect=[updates(), tg_error(502, "Bad Gateway"), updates(update(5, "/pause"))])
    mock.post(SEND_URL).mock(return_value=ok())
    cmds = TelegramCommands(TOKEN, CHAT, on_command, clock, poll_interval_s=0, error_backoff_s=0.01)
    cmds_task = asyncio.create_task(cmds.run(stop))
    await asyncio.wait_for(cmds_task, timeout=5)  # one short backoff after the 502
    assert route.call_count == 3
    assert engine.calls == [("pause", "")]
    assert cmds.offset == 6


async def test_disabled_commands_do_nothing(mock: Any, clock: SimClock) -> None:
    route = mock.post(url__regex=r"https://api\.telegram\.org/.*").mock(return_value=updates())
    for cmds in (
        TelegramCommands(TOKEN, CHAT, _never_called, clock, enabled=False),
        TelegramCommands("", CHAT, _never_called, clock),
        TelegramCommands.from_settings(settings(), AlertsCfg(), _never_called, clock),
    ):
        assert cmds.enabled is False
        assert await cmds.poll_once() == []
        await asyncio.wait_for(cmds.run(), timeout=1)  # returns at once
    assert route.call_count == 0
    assert TelegramCommands.from_settings(
        settings(), AlertsCfg(telegram_enabled=True), _never_called, clock
    ).enabled


# ---------------------------------------------------------------- wta alert (sync one-shot)


def test_send_alert_cli_is_a_no_op_without_config(mock: Any) -> None:
    route = mock.post(url__regex=r"https://api\.telegram\.org/.*").mock(return_value=ok())
    assert send_alert_cli(settings(token="", chat=""), "agent restarted 20 times") is False
    assert send_alert_cli(settings(chat=""), "x") is False
    assert send_alert_cli(settings(token=""), "x") is False
    assert route.call_count == 0


def test_send_alert_cli_sends_and_never_raises(mock: Any, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    caplog.set_level(logging.DEBUG, logger="httpx")
    route = mock.post(SEND_URL).mock(
        side_effect=[ok(), tg_error(401, "Unauthorized"), httpx.ConnectError(f"no route to {SEND_URL}")]
    )
    assert send_alert_cli(settings(), "run_forever: 20 restarts in 1 h, giving up", prefix="[wta]") is True
    assert payloads(route)[0] == {
        "chat_id": CHAT,
        "text": "[wta] run_forever: 20 restarts in 1 h, giving up",
        "disable_web_page_preview": True,
    }
    assert send_alert_cli(settings(), "x") is False
    assert send_alert_cli(settings(), "y" * 10_000) is False  # trimmed, then the network fails
    assert len(payloads(route)[2]["text"]) == TELEGRAM_MAX_CHARS
    assert TOKEN not in caplog.text
    assert "HTTP 401: Unauthorized" in caplog.text and "ConnectError" in caplog.text


def test_send_alert_cli_accepts_a_plain_namespace(mock: Any) -> None:
    class Plain:
        telegram_bot_token = TOKEN
        telegram_chat_id = int(CHAT)

    route = mock.post(SEND_URL).mock(return_value=ok())
    assert send_alert_cli(Plain(), "hi") is True
    assert payloads(route)[0]["chat_id"] == CHAT


# ---------------------------------------------------------------- formatters


def test_fmt_start_stop() -> None:
    text = fmt_start_stop(
        "restart", "live", git_commit="0123456789abcdef", config_hash="1a2b3c4d5e6f", reason="watchdog", equity=5_000_000
    )
    assert text == "RESTARTED wta [live] commit 0123456789ab config 1a2b3c4d5e6f equity 5,000,000 TMN\nreason: watchdog"
    assert fmt_start_stop("stop", "shadow") == "STOPPED wta [shadow]"


def test_fmt_fill_entry_and_closing() -> None:
    entry = fmt_fill("S1", "BTCUSDT", "buy", Decimal("12.4321"), qty=Decimal("0.000150"), price=Decimal("82845.10"), is_maker=True)
    assert entry == "FILL S1 BUY BTCUSDT 12.43 USDT (0.00015 @ 82,845.1, maker)"
    closing = fmt_fill("S3", "ETHTMN", "SELL", Decimal("1234567"), pnl=Decimal("12345"), pnl_pct=Decimal("0.0025"))
    assert closing == "FILL S3 SELL ETHTMN 1,234,567 TMN\nclosed: PnL +12,345 TMN (+0.25%)"
    loss = fmt_fill("S1", "SOLTMN", "SELL", 60000, pnl=-250, is_maker=False)
    assert loss == "FILL S1 SELL SOLTMN 60,000 TMN (taker)\nclosed: PnL -250 TMN"
    assert "PnL" not in entry


def test_fmt_risk_flatten_and_monitoring_lines() -> None:
    assert fmt_risk("kill", "drawdown 5.10% >= 5.00%", source="drawdown", details={"drawdown_pct": "0.051"}) == (
        "RISK KILL-SWITCH: canceling entries, flattening everything to TMN, then monitoring only\n"
        "reason: drawdown 5.10% >= 5.00%\nsource: drawdown\ndrawdown_pct=0.051"
    )
    assert fmt_risk("pause", "telegram /pause", source="telegram").startswith("RISK PAUSE: no new entries")
    assert fmt_risk("resume").startswith("RISK RESUME")
    assert fmt_flatten(1).startswith("FLATTEN phase 1 (T-2h): no new exposure")
    assert fmt_flatten("flatten_final", "2 assets left") == (
        "FLATTEN phase 2 (T-30min): only flatten orders; leftovers sold with MARKET orders\n2 assets left"
    )
    assert fmt_flatten(3).startswith("FLATTEN phase 3 (end)")
    assert fmt_flatten("kill").startswith("FLATTEN kill-switch")
    assert fmt_llm_failures(3, "timeout") == (
        "LLM: 3 failures in a row; keeping the last valid plan (rules-only once it is stale)\nlast error: timeout"
    )
    assert fmt_stale(["ethusdt", "BTCUSDT"], 135.7) == "DATA STALE: no update for 135 s: BTCUSDT, ETHUSDT"
    assert fmt_stale([f"C{i}USDT" for i in range(10)], 200).endswith("(+2 more)")
    assert fmt_api_burst(12) == "API ERRORS: 12 errors in the last 60 s"
    assert fmt_equity_hourly(Decimal("5012345.6"), Decimal("0.0024691"), Decimal("0.004"), daily_pnl=Decimal("-0.001")) == (
        "EQUITY 5,012,346 TMN | return +0.25% | drawdown 0.40% | today -0.10%"
    )


def test_number_helpers_are_robust() -> None:
    assert fmt_amount(None) == "n/a" and fmt_amount("garbage") == "n/a" and fmt_amount(float("nan")) == "n/a"
    assert fmt_amount("1,234.5", "USDT") == "1,234.50 USDT"
    assert fmt_pct(Decimal("-0.00001")) == "+0.00%"  # no "-0.00%"
    assert fmt_pct(None) == "n/a"

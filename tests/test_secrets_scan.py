"""Secrets scan: unit tests with planted fake keys, plus the real guard over logs/, deliverables/
and docs/REPORTS/ (where scripts/check_account.py writes raw account payloads).

Fake keys are assembled at runtime so this file itself never contains a complete key-like string.
The real guard never prints or asserts on secret values: findings name the file, line and pattern.
"""

from __future__ import annotations

import bz2
import gzip
import os
import zipfile
from pathlib import Path

import pytest
from pydantic import SecretStr, ValidationError

from wta.config import ROOT, Settings
from wta.logging_.checker import scan_for_secrets

OPENROUTER = "sk" + "-or-v1-" + "0123456789abcdef" * 4
OPENAI = "sk" + "-proj-" + "Ab3dEf6hIj9kLm2nOp5qRs8tUv1wXy4z"
GOOGLE = "AI" + "za" + "SyA1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q"
TELEGRAM = "1234567890" + ":" + "AAH" + "x" * 32
WALLEX = "wlx-test-" + "9f8e7d6c5b4a39281706"
WALLEX_SHAPE = "48213" + "|" + "Q7wErTy2UiOp4AsDf6GhJk8LzXc0VbNm1QaZsWxEdCrFvT"  # the "<id>|<token>" form
GOOGLE_AQ = "AQ" + "." + "Ab8RN6Kq-1w2E3r4T5y6U7i8O9p0_aSdFgHjKlZxCvBnM"


def plant(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def labels(findings: list[str]) -> list[str]:
    return [f.rsplit(": ", 1)[1] for f in findings]


# ---------------------------------------------------------------- unit tests


def test_literal_secret_found_recursively_and_never_echoed(tmp_path: Path) -> None:
    f = plant(tmp_path / "logs" / "llm_cache" / "ab" / "cd.json", f'{{"msg": "x"}}\n{{"key": "{WALLEX}"}}\n')
    plant(tmp_path / "logs" / "clean.jsonl", '{"ok": true}\n')
    findings = scan_for_secrets([tmp_path / "logs"], {"wallex_api_key": WALLEX})
    assert findings == [f"{f}:2: configured secret wallex_api_key"]
    assert all(WALLEX not in x for x in findings)
    # a plain list labels findings by position
    assert labels(scan_for_secrets([tmp_path], ["unused-value-123", WALLEX])) == ["configured secret #2"]


def test_json_escaped_secret_is_found(tmp_path: Path) -> None:
    secret = 'pa"ss\\w0rd-xyz'
    plant(tmp_path / "x.jsonl", '{"v": "pa\\"ss\\\\w0rd-xyz"}\n')
    assert labels(scan_for_secrets([tmp_path], [secret])) == ["configured secret #1"]


@pytest.mark.parametrize(
    ("text", "label"),
    [
        (f"Authorization: Bearer {OPENROUTER}", "OpenRouter key (sk-or-v1-...)"),
        (f'{{"api_key": "{OPENAI}"}}', "OpenAI-style key (sk-...)"),
        (f"url=https://x/?key={GOOGLE}", "Google API key (AIza...)"),
        (f"https://api.telegram.org/bot{TELEGRAM}/sendMessage", "Telegram bot token"),
        (f"WALLEX_API_KEY={WALLEX_SHAPE}", "Wallex API key (id|token)"),
        (f'{{"headers": {{"X-API-Key": "{WALLEX_SHAPE}"}}}}', "Wallex API key (id|token)"),
        (f"GEMINI_API_KEY={GOOGLE_AQ}", "Google API key (AQ.)"),
        (f"https://generativelanguage.googleapis.com/v1beta/models?key={GOOGLE_AQ}", "Google API key (AQ.)"),
        ("X-API-Key: " + "abcdef0123456789abcd", "X-API-Key header value"),
        ('{"x-api-key": "' + "ABCDEF0123456789.abcd" + '"}', "X-API-Key header value"),
    ],
)
def test_key_patterns(tmp_path: Path, text: str, label: str) -> None:
    f = plant(tmp_path / "out.log", f"first line\n{text}\n")
    findings = scan_for_secrets([tmp_path], [])
    assert findings == [f"{f}:2: {label}"]
    assert all(text not in x for x in findings)


def test_harmless_text_is_not_flagged(tmp_path: Path) -> None:
    plant(
        tmp_path / "decisions-20261022.jsonl",
        "\n".join(
            [
                '{"ts": "2026-10-22T10:00:00.000Z", "client_id": "S1_BTCUSDT_1729591234567_A1B2"}',
                "risk-management-keeps-the-drawdown-small",
                "X-API-Key: ***",
                "X-API-Key: <key>",
                '{"wallex_api_key": "<set>", "note": "sk-short"}',
                "ratio 1729591234:12",
                "| 12 | ETHUSDT | 0.25% | maker |",
                "S1_BTCTMN|" + "a" * 40,
                "AQ.short-value",
            ]
        ),
    )
    assert scan_for_secrets([tmp_path], ["", "abc"]) == []  # empty/too-short secrets are ignored


def test_symlinked_and_missing_files_are_skipped(tmp_path: Path) -> None:
    root = tmp_path / "deliverables"
    root.mkdir()
    outside = plant(tmp_path / "outside" / "secret.txt", WALLEX)
    os.symlink(outside, root / "link.txt")
    os.symlink(outside.parent, root / "linkdir")
    assert scan_for_secrets([root, tmp_path / "missing"], [WALLEX]) == []
    assert scan_for_secrets([], [WALLEX]) == []


def test_binary_and_large_files_are_scanned_not_skipped(tmp_path: Path) -> None:
    blob = tmp_path / "bin" / "blob.dat"
    blob.parent.mkdir()
    blob.write_bytes(b"\x00\x01" * 5000 + b"\n" + WALLEX.encode() + b"\x00")
    big = plant(tmp_path / "decisions-20261022.jsonl", "x" * 5000 + "\n" + WALLEX + "\n")
    assert scan_for_secrets([blob], [WALLEX]) == [f"{blob}:2: configured secret #1"]  # NUL bytes do not hide it
    assert labels(scan_for_secrets([big], [WALLEX])) == ["configured secret #1"]  # no size cap by default
    # a cap, when set, fails closed: the file is reported, not silently passed
    assert scan_for_secrets([big], [WALLEX], max_bytes=1000) == [f"{big}: not scanned (larger than 1000 bytes)"]


def test_compressed_files_are_decompressed_or_reported(tmp_path: Path) -> None:
    with zipfile.ZipFile(tmp_path / "export.zip", "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("logs/clean.jsonl", '{"ok": true}\n')
        zf.writestr("logs/orders.jsonl", f'{{"ok": true}}\n{{"key": "{WALLEX}"}}\n')
    with gzip.open(tmp_path / "health.jsonl.gz", "wt") as fh:
        fh.write(f"{WALLEX}\n")
    (tmp_path / "events.jsonl.bz2").write_bytes(bz2.compress(b"clean\n"))
    (tmp_path / "archive.7z").write_bytes(b"7z\xbc\xaf\x27\x1c" + b"\x00" * 32)
    (tmp_path / "broken.zip").write_bytes(b"PK\x03\x04 not really a zip")

    findings = scan_for_secrets([tmp_path], {"wallex_api_key": WALLEX})
    assert findings == [
        f"{tmp_path / 'archive.7z'}: not scanned (compressed format)",
        f"{tmp_path / 'broken.zip'}: not scanned (BadZipFile)",
        f"{tmp_path / 'export.zip'}!logs/orders.jsonl:2: configured secret wallex_api_key",
        f"{tmp_path / 'health.jsonl.gz'}:1: configured secret wallex_api_key",
    ]
    assert all(WALLEX not in f for f in findings)


def test_the_scan_covers_every_secret_and_the_sub_account_id(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("OPENROUTER_API_KEY", "GEMINI_API_KEY", "WALLEX_API_KEY", "TELEGRAM_BOT_TOKEN",
                 "WALLEX_SUB_ACCOUNT_CLIENT_ID"):
        monkeypatch.delenv(name, raising=False)
    sub_id = "challenge-sub-" + "7c41d2e9"
    settings = Settings(_env_file=None, wallex_api_key=WALLEX, telegram_bot_token=TELEGRAM,
                        wallex_sub_account_client_id=sub_id)
    values = settings.scan_values()
    secret_names = {n for n in type(settings).model_fields if isinstance(getattr(settings, n), SecretStr)}
    assert set(values) == secret_names | {"wallex_sub_account_client_id"}
    assert values["wallex_sub_account_client_id"] == sub_id and values["openrouter_api_key"] == ""
    f = plant(tmp_path / "api_probe.md", f"- sub-account-client-id: {sub_id}\n")
    assert scan_for_secrets([tmp_path], values) == [f"{f}:1: configured secret wallex_sub_account_client_id"]
    from scripts.make_deliverables import secret_fields
    from wta.cli import _secret_fields

    assert _secret_fields(settings) == values == secret_fields(settings)


# ---------------------------------------------------------------- the real guard


def _scan_repo_outputs() -> list[str]:
    """Scan logs/ (incl. llm_cache), deliverables/ and docs/REPORTS/ for this machine's real secrets
    and key patterns.

    Kept in a helper so secret values never sit in the test's own frame (tracebacks show locals).
    """
    try:
        settings = Settings()  # reads .env if present
    except ValidationError as exc:
        fields = sorted({".".join(str(p) for p in e["loc"]) for e in exc.errors()})
        pytest.fail(f"Settings() failed to load; check these .env fields: {fields}", pytrace=False)
    secrets = {name: value for name, value in settings.scan_values().items() if value}
    return scan_for_secrets([ROOT / "logs", ROOT / "deliverables", ROOT / "docs" / "REPORTS"], secrets)


def test_no_secrets_in_logs_deliverables_or_reports() -> None:
    findings = _scan_repo_outputs()
    if findings:
        pytest.fail("secret material found (file:line: pattern):\n" + "\n".join(findings), pytrace=False)

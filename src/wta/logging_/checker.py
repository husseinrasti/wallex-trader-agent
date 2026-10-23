"""Log validation (``python -m wta logs check``) and the secrets scan.

* ``check_logs`` validates every line of the decisions, health, events and orders families against
  their pydantic models and checks that each outcome update refers to a recorded decision.
  Missing or empty files are fine: a fresh checkout validates as ok.
* ``scan_for_secrets`` looks for configured secret values and key-like patterns in files and
  directories (logs/, logs/llm_cache/, deliverables/, docs/REPORTS/). Findings name the file, line
  and pattern, never the secret itself; a file it cannot search is a finding too (fail closed).

Error messages are built from field names and pydantic's messages only, never from the input
values, so a bad line that happens to contain a key cannot leak it into the report.
"""

from __future__ import annotations

import bz2
import gzip
import io
import lzma
import os
import re
import zipfile
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TypedDict

import orjson
from pydantic import BaseModel, ValidationError

from wta.logging_.jsonl import MIN_SECRET_LEN, family_files, secret_forms
from wta.models import DecisionRecord, EventRecord, HealthRecord, OrderLogRecord, OutcomeUpdate
from wta.utils.timeutil import parse_iso

MAX_ERRORS_PER_FILE = 50
ZIP_MAGIC = b"PK\x03\x04"
STREAM_OPENERS: tuple[tuple[bytes, Callable[..., Any]], ...] = (
    (b"\x1f\x8b", gzip.open),
    (b"BZh", bz2.open),
    (b"\xfd7zXZ\x00", lzma.open),
)
UNSUPPORTED_MAGIC: tuple[bytes, ...] = (b"7z\xbc\xaf\x27\x1c", b"\x28\xb5\x2f\xfd")  # 7z, zstd


class FileCheck(TypedDict):
    lines: int  # non-blank lines
    errors: list[str]  # "line N: ..." messages, at most MAX_ERRORS_PER_FILE (+ a "... more" line)


@dataclass
class CheckReport:
    ok: bool = True
    files: dict[str, FileCheck] = field(default_factory=dict)  # file name (relative to log_dir) -> result

    def summary(self) -> str:
        """Human-readable report for the CLI."""
        if not self.files:
            return "no log files found (ok)"
        out = []
        for name, res in self.files.items():
            status = "ok" if not res["errors"] else f"{len(res['errors'])} error(s)"
            out.append(f"{name}: {res['lines']} lines, {status}")
            out.extend(f"  {e}" for e in res["errors"])
        out.append("OK" if self.ok else "FAILED")
        return "\n".join(out)


def _validate_decision(obj: dict[str, Any]) -> BaseModel:
    if obj.get("kind") == "outcome":
        return OutcomeUpdate.model_validate(obj)
    return DecisionRecord.model_validate(obj)


# family base name -> validator for one parsed line
FAMILIES: dict[str, Callable[[dict[str, Any]], BaseModel]] = {
    "decisions": _validate_decision,
    "health": HealthRecord.model_validate,
    "events": EventRecord.model_validate,
    "orders": OrderLogRecord.model_validate,
}


def _describe(exc: ValidationError) -> str:
    """Field paths and messages only (``str(exc)`` would echo the input values)."""
    parts = [f"{'.'.join(str(p) for p in e['loc']) or '<root>'}: {e['msg']}" for e in exc.errors()[:5]]
    more = exc.error_count() - len(parts)
    return "; ".join(parts) + (f"; +{more} more" if more > 0 else "")


def _is_iso(ts: object) -> bool:
    try:
        return isinstance(ts, str) and parse_iso(ts) is not None
    except ValueError:
        return False


def _check_file(
    path: Path, validate: Callable[[dict[str, Any]], BaseModel]
) -> tuple[FileCheck, list[BaseModel], list[tuple[int, OutcomeUpdate]]]:
    """Validate one file. Returns its result, the valid records and (line, update) for outcomes."""
    errors: list[str] = []
    valid: list[BaseModel] = []
    outcomes: list[tuple[int, OutcomeUpdate]] = []
    lines = 0
    with path.open("rb") as fh:
        for lineno, raw in enumerate(fh, start=1):
            if not raw.strip():
                continue
            lines += 1
            try:
                obj = orjson.loads(raw)
            except orjson.JSONDecodeError:
                errors.append(f"line {lineno}: not valid JSON")
                continue
            if not isinstance(obj, dict):
                errors.append(f"line {lineno}: not a JSON object")
                continue
            try:
                rec = validate(obj)
            except ValidationError as exc:
                errors.append(f"line {lineno}: {_describe(exc)}")
                continue
            if not _is_iso(getattr(rec, "ts", None)):
                errors.append(f"line {lineno}: ts is not an ISO-8601 timestamp")
                continue
            valid.append(rec)
            if isinstance(rec, OutcomeUpdate):
                outcomes.append((lineno, rec))
    return {"lines": lines, "errors": errors}, valid, outcomes


def _cap(errors: list[str]) -> list[str]:
    if len(errors) <= MAX_ERRORS_PER_FILE:
        return errors
    return errors[:MAX_ERRORS_PER_FILE] + [f"... {len(errors) - MAX_ERRORS_PER_FILE} more errors"]


def check_logs(log_dir: Path) -> CheckReport:
    """Validate every log family in ``log_dir``; ``ok`` is False if any line fails."""
    log_dir = Path(log_dir)
    report = CheckReport()
    decision_ids: set[str] = set()
    outcome_refs: list[tuple[str, int, str]] = []  # (file name, line, decision_id)
    for base, validate in FAMILIES.items():
        for path in family_files(log_dir, base):
            result, valid, outcomes = _check_file(path, validate)
            report.files[path.name] = result
            if base == "decisions":
                decision_ids.update(r.decision_id for r in valid if isinstance(r, DecisionRecord))
                outcome_refs.extend((path.name, n, upd.decision_id) for n, upd in outcomes)
    for name, lineno, decision_id in outcome_refs:
        if decision_id not in decision_ids:
            report.files[name]["errors"].append(
                f"line {lineno}: outcome references unknown decision_id {decision_id!r}"
            )
    for result in report.files.values():
        result["errors"] = _cap(result["errors"])
    report.ok = all(not r["errors"] for r in report.files.values())
    return report


# ---------------------------------------------------------------- secrets scan


# (label, pattern). Labels go into findings; the matched text never does.
KEY_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("OpenRouter key (sk-or-v1-...)", re.compile(r"(?<![A-Za-z0-9])sk-or-v1-[A-Za-z0-9]{20,}")),
    ("OpenAI-style key (sk-...)", re.compile(r"(?<![A-Za-z0-9])sk-(?!or-v1-)[A-Za-z0-9_-]{20,}")),
    ("Google API key (AIza...)", re.compile(r"AIza[0-9A-Za-z_-]{35}")),
    ("Google API key (AQ.)", re.compile(r"(?<![A-Za-z0-9])AQ\.[A-Za-z0-9_-]{40,}")),
    # Wallex trading keys are "<numeric id>|<40+ alphanumerics>" (PRD-06 §7 scans for "\|[A-Za-z0-9]{30,}")
    ("Wallex API key (id|token)", re.compile(r"(?<![A-Za-z0-9|])[0-9]{1,10}\|[A-Za-z0-9]{30,}")),
    ("Telegram bot token", re.compile(r"(?<![0-9])[0-9]{8,10}:[A-Za-z0-9_-]{35}(?![A-Za-z0-9_-])")),
    ("X-API-Key header value", re.compile(r"(?i)x-api-key[\"']?\s*[:=]\s*[\"']?[A-Za-z0-9_.-]{16,}")),
]


def _needles(secrets: Sequence[str] | Mapping[str, str]) -> list[tuple[str, str]]:
    """(label, text) pairs to search for literally; a mapping labels findings by secret name."""
    if isinstance(secrets, Mapping):
        named = list(secrets.items())
    else:
        named = [(f"#{i + 1}", s) for i, s in enumerate(secrets)]
    out: list[tuple[str, str]] = []
    for name, value in named:
        if value and len(value) >= MIN_SECRET_LEN:
            out.extend((f"configured secret {name}", form) for form in secret_forms([value]))
    return out


def _iter_files(paths: Sequence[Path]) -> Iterator[Path]:
    """Regular files under ``paths`` (recursively), symlinks skipped, missing paths ignored."""
    for root in paths:
        root = Path(root)
        if root.is_symlink():
            continue
        if root.is_file():
            yield root
        elif root.is_dir():
            for dirpath, dirnames, filenames in os.walk(root):
                dirnames.sort()
                for name in sorted(filenames):
                    p = Path(dirpath) / name
                    if p.is_file() and not p.is_symlink():
                        yield p


def _scan_lines(lines: Iterable[str], where: str, needles: list[tuple[str, str]]) -> list[str]:
    findings: list[str] = []
    for lineno, line in enumerate(lines, start=1):
        labels = {label for label, text in needles if text in line}
        labels.update(label for label, pat in KEY_PATTERNS if pat.search(line))
        findings.extend(f"{where}:{lineno}: {label}" for label in sorted(labels))
    return findings


def _scan_file(path: Path, needles: list[tuple[str, str]], max_bytes: int | None) -> list[str]:
    """Findings for one file. A file that cannot be searched is itself a finding (fail closed)."""
    try:
        if max_bytes is not None and path.stat().st_size > max_bytes:
            return [f"{path}: not scanned (larger than {max_bytes} bytes)"]
        with path.open("rb") as fh:
            head = fh.read(8)
        if head.startswith(ZIP_MAGIC):  # .zip, and .docx/.xlsx/.pptx
            findings: list[str] = []
            with zipfile.ZipFile(path) as zf:
                for info in zf.infolist():
                    if not info.is_dir():
                        with zf.open(info) as raw:
                            text = io.TextIOWrapper(raw, encoding="utf-8", errors="replace")
                            findings += _scan_lines(text, f"{path}!{info.filename}", needles)
            return findings
        opener = next((op for magic, op in STREAM_OPENERS if head.startswith(magic)), None)
        if opener is not None:  # gzip / bzip2 / xz: scan the decompressed text
            with opener(path, "rt", encoding="utf-8", errors="replace") as fh:
                return _scan_lines(fh, str(path), needles)
        if head.startswith(UNSUPPORTED_MAGIC):
            return [f"{path}: not scanned (compressed format)"]
        # text, or binary searched as text: a literal key inside a binary file is still found
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            return _scan_lines(fh, str(path), needles)
    except (OSError, EOFError, RuntimeError, zipfile.BadZipFile, lzma.LZMAError) as exc:
        return [f"{path}: not scanned ({type(exc).__name__})"]


def scan_for_secrets(
    paths: Sequence[Path],
    secrets: Sequence[str] | Mapping[str, str],
    max_bytes: int | None = None,
) -> list[str]:
    """Findings like ``"logs/x.jsonl:12: OpenRouter key (sk-or-v1-...)"``; empty list = clean.

    Checks each configured secret literally (also in its JSON-escaped form) and the patterns in
    ``KEY_PATTERNS``. Directories are scanned recursively, symlinks are skipped. Binary files are
    searched as text, zip/gzip/bzip2/xz files are decompressed first. A file that cannot be
    searched (unreadable, a 7z/zstd archive, or larger than ``max_bytes`` when that is set) is
    reported as a finding, so the scan fails closed instead of passing it.
    """
    needles = _needles(secrets)
    findings: list[str] = []
    for path in _iter_files(paths):
        findings += _scan_file(path, needles, max_bytes)
    return findings

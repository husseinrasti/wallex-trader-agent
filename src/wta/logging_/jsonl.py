"""Append-only, daily-rotated JSONL files with secret scrubbing (PRD-01 §8).

Layout for a family called ``decisions``::

    logs/decisions-20261022.jsonl     one file per UTC day (from the injected Clock)
    logs/decisions-20261023.jsonl
    logs/decisions.jsonl -> decisions-20261023.jsonl   relative symlink to the current day

Every line is ``canonical_json`` (sorted keys, Decimals as strings), so the same record always
serialises to the same bytes (replay compares lines). Before a line reaches the disk, every
configured secret value in the serialised text is replaced by ``***``.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterable, Iterator, Mapping
from pathlib import Path
from typing import IO, Any

import orjson
from pydantic import BaseModel

from wta.config import canonical_json
from wta.utils.timeutil import Clock, utc_day

SCRUB = "***"
MIN_SECRET_LEN = 4  # shorter values would mangle ordinary text (same rule as config.mask_secrets)


def secret_forms(secrets: Iterable[str]) -> list[str]:
    """The strings to look for: each secret as-is and as it appears inside a JSON string.

    Longest first, so a secret that contains another one is replaced whole.
    """
    forms: set[str] = set()
    for s in secrets:
        if s and len(s) >= MIN_SECRET_LEN:
            forms.add(s)
            forms.add(orjson.dumps(s).decode("utf-8")[1:-1])  # e.g. a quote becomes \"
    return sorted(forms, key=len, reverse=True)


class JsonlWriter:
    """Writes one canonical JSON object per line to ``<base>-YYYYMMDD.jsonl`` (or ``<base>.jsonl``).

    * Append-only: files are opened in ``"a"`` mode and never truncated or rewritten.
    * Line-buffered and flushed after every write; ``fsync=True`` also forces it to the disk.
    * ``scrubbed`` counts how many secret occurrences were replaced by ``***``.
    * ``symlink_ok`` turns False if the platform refuses symlinks (logging continues without it).
    """

    def __init__(
        self,
        log_dir: Path,
        base: str,
        clock: Clock,
        rotate_daily: bool = True,
        secrets: list[str] | None = None,
        fsync: bool = False,
    ) -> None:
        self.log_dir = Path(log_dir)
        self.base = base
        self.clock = clock
        self.rotate_daily = rotate_daily
        self.fsync = fsync
        self.scrubbed = 0
        self.symlink_ok = True
        self._forms = secret_forms(secrets or [])
        self._fh: IO[str] | None = None
        self._path: Path | None = None

    # ------------------------------------------------------------ paths

    @property
    def link_path(self) -> Path:
        """``<base>.jsonl``: the current-day symlink (or the only file when not rotating)."""
        return self.log_dir / f"{self.base}.jsonl"

    def path_for(self, ts: float) -> Path:
        if not self.rotate_daily:
            return self.link_path
        return self.log_dir / f"{self.base}-{utc_day(ts)}.jsonl"

    @property
    def path(self) -> Path:
        """File the next write goes to."""
        return self.path_for(self.clock.now())

    # ------------------------------------------------------------ writing

    def scrub(self, text: str) -> str:
        """Replace every configured secret in ``text`` by ``***`` and count the replacements."""
        for form in self._forms:
            hits = text.count(form)
            if hits:
                self.scrubbed += hits
                text = text.replace(form, SCRUB)
        return text

    def write(self, record: BaseModel | Mapping[str, Any]) -> str:
        """Serialise, scrub and append one record; returns the line written (without newline)."""
        obj = record.model_dump(mode="json") if isinstance(record, BaseModel) else dict(record)
        line = self.scrub(canonical_json(obj).decode("utf-8"))
        fh = self._open_for(self.path_for(self.clock.now()))
        fh.write(line + "\n")
        fh.flush()
        if self.fsync:
            os.fsync(fh.fileno())
        return line

    def close(self) -> None:
        if self._fh is not None:
            self._fh.close()
        self._fh = None
        self._path = None

    def __enter__(self) -> JsonlWriter:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _open_for(self, path: Path) -> IO[str]:
        if self._fh is not None and self._path == path:
            return self._fh
        self.close()
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self._fh = path.open("a", encoding="utf-8", buffering=1)
        self._path = path
        if self.rotate_daily:
            self._point_link_to(path)
        return self._fh

    def _point_link_to(self, target: Path) -> None:
        """Atomically re-point ``<base>.jsonl`` at ``target`` (relative link, same directory)."""
        link = self.link_path
        if link.exists() and not link.is_symlink():
            return  # a real file (e.g. a copied deliverable or an old non-rotated log): never clobber
        tmp = self.log_dir / f".{self.base}.jsonl.tmp"
        try:
            if tmp.is_symlink() or tmp.exists():
                tmp.unlink()
            os.symlink(target.name, tmp)
            os.replace(tmp, link)
        except (OSError, NotImplementedError):
            self.symlink_ok = False


# ---------------------------------------------------------------- reading


def family_files(log_dir: Path, base: str) -> list[Path]:
    """All files of a family, oldest first.

    A plain ``<base>.jsonl`` that is a regular file (not our symlink) comes first: it is either an
    old non-rotated log or a copied deliverable. Then ``<base>-YYYYMMDD.jsonl`` in date order.
    """
    log_dir = Path(log_dir)
    if not log_dir.is_dir():
        return []
    day_re = re.compile(rf"{re.escape(base)}-\d{{8}}\.jsonl")
    dated = sorted(
        p for p in log_dir.iterdir() if day_re.fullmatch(p.name) and p.is_file() and not p.is_symlink()
    )
    plain = log_dir / f"{base}.jsonl"
    head = [plain] if plain.is_file() and not plain.is_symlink() else []
    return head + dated


def iter_file(path: Path, strict: bool = False) -> Iterator[dict[str, Any]]:
    """JSON objects of one file, skipping blank lines.

    Undecodable or non-object lines (e.g. a line cut short by a crash) are skipped, or raise
    ``ValueError`` naming the file and line when ``strict``. ``python -m wta logs check`` reports them.
    Money fields are strings in our logs; numbers come back as int/float exactly as written.
    """
    with Path(path).open("rb") as fh:
        for lineno, raw in enumerate(fh, start=1):
            if not raw.strip():
                continue
            try:
                obj = orjson.loads(raw)
            except orjson.JSONDecodeError:
                obj = None
            if isinstance(obj, dict):
                yield obj
            elif strict:
                raise ValueError(f"{path}:{lineno}: not a JSON object")


def iter_family(log_dir: Path, base: str, strict: bool = False) -> Iterator[dict[str, Any]]:
    """Every record of a family (see ``family_files``), oldest first, blank lines skipped."""
    for path in family_files(log_dir, base):
        yield from iter_file(path, strict=strict)

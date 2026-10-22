"""Decision-by-decision comparison of a replay with the recorded run (PRD-05 §6).

``compare_decisions(reference, replayed)`` merges each side's ``decisions`` family with
``merge_decisions`` (outcome lines joined to their decision), aligns the decisions and compares the
deterministic fields. The result, ``ReplayReport``, gives the judge-facing line
``"N/N decisions reproduced"`` and renders ``docs/REPORTS/replay_<YYYYMMDD>.md``.

Alignment. By ``decision_id`` first (ids are ``d_<tick ms>_<strategy>_<symbol>_<seq>``, so equal
ids mean the same tick and the same intent). Decisions left over on both sides are then paired in
log order within the same ``(kind, strategy, symbol)``; such a pair also reports its differing
``decision_id``. What is still left is ``missing`` (recorded, not replayed) or ``extra`` (replayed,
never recorded).

Compared fields (``FIELDS``, the PRD-05 list): ``kind``, ``strategy``, ``symbol``, the intent's
``side``, ``purpose``, ``order_type``, ``qty``, ``notional``, ``price``, ``stop_price`` and
``reason``; the risk decision's ``approved``, ``adjusted_qty`` (the quantity after risk),
``veto_reason`` and every check (``code``, ``passed``, ``detail``, keyed by code; a repeated code
such as the second ``MIN_NOTIONAL`` is ``MIN_NOTIONAL#2``); the order's ``accepted``, request
``qty``/``price``/``stop_price`` and answer ``status``; the plan's ``prompt_hash``; the outcome's
``status``, ``done_reason`` and ``exit_reason``; and ``notes``. Numbers are compared as Decimals
(``"0.000030"`` equals ``"0.00003"``); everything else exactly. A field missing on one side counts
as ``None``.

``strict=True`` compares every field of the merged records instead, except ``IGNORE``.

``IGNORE`` (never compared, in either mode):
  * time stamps and durations: any key ``ts``, ``*_ts``, ``latency_ms`` and ``hold_s`` (line
    times, ``intent.ts``, ``order.response.created_ts/updated_ts``, ``outcome.done_ts/closed_ts``,
    plan times): a replay runs on the simulated clock, PRD-05 excludes them;
  * ``mode`` (the replay writes its own mode) and ``plan_snapshot.cached`` (the replay is served by
    the LLM cache, so its plans say ``cached: true``);
  * ``git_commit`` and ``config_hash``: reported as provenance (equal or not), not as mismatches.

Exchange divergences (``ReplayExchange.all_divergences()``: requests the recorded run never made,
recorded placements never replayed) can be passed as ``divergences``; any of them makes the report
not ``ok``.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import orjson

from wta.logging_.decision_log import MergedDecisions, merge_decisions
from wta.logging_.jsonl import iter_family

FIELDS: tuple[str, ...] = (
    "kind",
    "strategy",
    "symbol",
    "intent.side",
    "intent.purpose",
    "intent.order_type",
    "intent.qty",
    "intent.notional",
    "intent.price",
    "intent.stop_price",
    "intent.reason",
    "risk.approved",
    "risk.adjusted_qty",
    "risk.veto_reason",
    "risk.checks",
    "order.accepted",
    "order.request.qty",
    "order.request.price",
    "order.request.stop_price",
    "order.response.status",
    "plan.prompt_hash",
    "outcome.status",
    "outcome.done_reason",
    "outcome.exit_reason",
    "notes",
)

IGNORE: frozenset[str] = frozenset({"mode", "git_commit", "config_hash", "plan_snapshot.cached"})
IGNORE_KEYS: frozenset[str] = frozenset({"ts", "latency_ms", "hold_s"})  # leaf key names, at any depth
IGNORE_SUFFIX = "_ts"  # leaf keys ending like this, at any depth

PROVENANCE_FIELDS = ("git_commit", "config_hash", "mode")
_ALIGNMENT_PATHS = frozenset({"decision_id", "intent.id"})  # strict mode: the alignment reports these
_NUMBER = re.compile(r"-?(\d+(\.\d*)?|\.\d+)([eE][-+]?\d+)?")
_MAX_CELL = 300

Records = Iterable[Mapping[str, Any]] | Mapping[str, Mapping[str, Any]] | str | Path


@dataclass(frozen=True)
class Mismatch:
    """One differing field of one aligned decision."""

    decision_id: str
    field: str
    reference: Any
    replayed: Any

    def to_dict(self) -> dict[str, Any]:
        return {
            "decision_id": self.decision_id,
            "field": self.field,
            "reference": self.reference,
            "replayed": self.replayed,
        }


@dataclass
class ReplayReport:
    """Result of ``compare_decisions``."""

    n_reference: int
    n_replayed: int
    n_matched: int
    mismatches: list[Mismatch] = field(default_factory=list)
    missing: list[dict[str, Any]] = field(default_factory=list)  # recorded decisions not replayed
    extra: list[dict[str, Any]] = field(default_factory=list)  # replayed decisions never recorded
    aligned_by_order: list[tuple[str, str]] = field(default_factory=list)  # (reference id, replayed id)
    provenance: dict[str, dict[str, Any]] = field(default_factory=dict)
    divergences: list[dict[str, Any]] = field(default_factory=list)  # from ReplayExchange
    orphans: dict[str, int] = field(default_factory=dict)  # outcome lines without a decision, per side
    strict: bool = False
    fields: tuple[str, ...] = FIELDS

    @property
    def ok(self) -> bool:
        return (
            self.n_matched == self.n_reference == self.n_replayed
            and not self.mismatches
            and not self.missing
            and not self.extra
            and not self.divergences
        )

    @property
    def summary(self) -> str:
        return f"{self.n_matched}/{self.n_reference} decisions reproduced"

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "summary": self.summary,
            "n_reference": self.n_reference,
            "n_replayed": self.n_replayed,
            "n_matched": self.n_matched,
            "mismatches": [m.to_dict() for m in self.mismatches],
            "missing": list(self.missing),
            "extra": list(self.extra),
            "aligned_by_order": [list(p) for p in self.aligned_by_order],
            "provenance": self.provenance,
            "divergences": list(self.divergences),
            "orphans": dict(self.orphans),
            "strict": self.strict,
            "fields": "all except IGNORE" if self.strict else list(self.fields),
            "ignore": sorted(IGNORE) + [f"*.{k}" for k in sorted(IGNORE_KEYS)] + [f"*{IGNORE_SUFFIX}"],
        }

    def to_markdown(self, date: str | None = None, title: str | None = None) -> str:
        """The report for ``docs/REPORTS/replay_<date>.md`` (deterministic: no wall clock)."""
        head = title or (f"Replay report {date}" if date else "Replay report")
        verdict = "PASS" if self.ok else "FAIL"
        lines = [
            f"# {head}",
            "",
            f"**{self.summary}** ({verdict})",
            "",
            "| | reference | replayed |",
            "|---|---|---|",
            f"| decisions | {self.n_reference} | {self.n_replayed} |",
            f"| reproduced (all compared fields equal) | {self.n_matched} | |",
            f"| field mismatches | {len(self.mismatches)} | |",
            f"| missing / extra | {len(self.missing)} | {len(self.extra)} |",
            f"| aligned by order (id differs) | {len(self.aligned_by_order)} | |",
            f"| exchange divergences | {len(self.divergences)} | |",
            _row(
                "outcome lines without a decision",
                self.orphans.get("reference", 0),
                self.orphans.get("replayed", 0),
            ),
            "",
            "## Provenance",
            "",
            "| field | reference | replayed | equal |",
            "|---|---|---|---|",
        ]
        for name in PROVENANCE_FIELDS:
            p = self.provenance.get(name, {})
            ref = ", ".join(p.get("reference", [])) or "-"
            rep = ", ".join(p.get("replayed", [])) or "-"
            lines.append(f"| `{name}` | {_cell(ref)} | {_cell(rep)} | {'yes' if p.get('equal') else 'no'} |")
        lines += ["", "## Mismatches", ""]
        if self.mismatches:
            lines += ["| decision_id | field | reference | replayed |", "|---|---|---|---|"]
            for m in self.mismatches:
                lines.append(
                    _row(
                        f"`{m.decision_id}`",
                        f"`{m.field}`",
                        _cell(_show(m.reference)),
                        _cell(_show(m.replayed)),
                    )
                )
        else:
            lines.append("None.")
        for name, rows in (
            ("Missing (recorded, not replayed)", self.missing),
            ("Extra (replayed, never recorded)", self.extra),
        ):
            lines += ["", f"## {name}", ""]
            if rows:
                lines += ["| decision_id | kind | strategy | symbol |", "|---|---|---|---|"]
                for r in rows:
                    lines.append(
                        _row(f"`{r.get('decision_id')}`", r.get("kind"), r.get("strategy"), r.get("symbol"))
                    )
            else:
                lines.append("None.")
        lines += ["", "## Exchange divergences", ""]
        if self.divergences:
            lines += ["| kind | client_id | decision_id | message |", "|---|---|---|---|"]
            for d in self.divergences:
                cid, did = d.get("client_id"), d.get("decision_id") or "-"
                lines.append(_row(d.get("kind"), f"`{cid}`", f"`{did}`", _cell(str(d.get("message", "")))))
        else:
            lines.append("None: every exchange answer came from the record.")
        lines += ["", "## What is compared", ""]
        if self.strict:
            lines.append(
                "Every field of the merged decision records (outcomes joined), except the ignored ones."
            )
        else:
            lines.append("Fields: " + ", ".join(f"`{f}`" for f in self.fields) + ".")
        lines += [
            "Numbers are compared as Decimals; risk checks by code (`code`, `passed`, `detail`).",
            "Ignored: "
            + ", ".join(f"`{f}`" for f in sorted(IGNORE))
            + ", and any key "
            + ", ".join(f"`{k}`" for k in sorted(IGNORE_KEYS))
            + f" or `*{IGNORE_SUFFIX}` (time stamps and durations; provenance is reported above).",
            "",
        ]
        return "\n".join(lines)

    def write_markdown(self, path: str | Path, date: str | None = None) -> Path:
        out = Path(path)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(self.to_markdown(date=date), encoding="utf-8")
        return out


def report_path(reports_dir: str | Path, date: str) -> Path:
    """``<reports_dir>/replay_<date>.md`` (``date`` as ``YYYYMMDD``, like the other reports)."""
    return Path(reports_dir) / f"replay_{date}.md"


# ---------------------------------------------------------------- comparison


def compare_decisions(
    reference: Records,
    replayed: Records,
    *,
    strict: bool = False,
    fields: Iterable[str] = FIELDS,
    divergences: Iterable[Mapping[str, Any]] | None = None,
) -> ReplayReport:
    """Compare the replayed decisions with the recorded ones (see the module docstring).

    ``reference`` / ``replayed``: a log directory (its ``decisions`` family), raw records (e.g.
    ``iter_family(dir, "decisions")``) or an already merged ``decision_id -> record`` mapping.
    """
    ref = _merged(reference)
    rep = _merged(replayed)
    wanted = tuple(fields)
    report = ReplayReport(
        n_reference=len(ref),
        n_replayed=len(rep),
        n_matched=0,
        strict=strict,
        fields=wanted,
        divergences=[dict(d) for d in (divergences or [])],
        orphans={"reference": len(getattr(ref, "orphans", [])), "replayed": len(getattr(rep, "orphans", []))},
    )
    pairs: list[tuple[str, str]] = [(d, d) for d in ref if d in rep]
    left_ref = [d for d in ref if d not in rep]
    left_rep = [d for d in rep if d not in ref]
    by_key: dict[tuple[str, str, str], list[str]] = {}
    for d in left_rep:
        by_key.setdefault(_key(rep[d]), []).append(d)
    for d in left_ref:
        bucket = by_key.get(_key(ref[d]))
        if bucket:
            other = bucket.pop(0)
            pairs.append((d, other))
            report.aligned_by_order.append((d, other))
        else:
            report.missing.append(_brief(d, ref[d]))
    paired_rep = {b for _, b in pairs}
    report.extra = [_brief(d, rep[d]) for d in rep if d not in paired_rep]
    order = {d: i for i, d in enumerate(ref)}
    pairs.sort(key=lambda p: order[p[0]])
    for a, b in pairs:
        rows: list[Mismatch] = []
        if a != b:
            rows.append(Mismatch(a, "decision_id", a, b))
        rows += _diff(a, ref[a], rep[b], strict, wanted)
        report.mismatches += rows
        report.n_matched += int(not rows)
    report.provenance = {name: _provenance(ref, rep, name) for name in PROVENANCE_FIELDS}
    return report


def _merged(records: Records) -> Mapping[str, Mapping[str, Any]]:
    if isinstance(records, str | Path):
        return merge_decisions(iter_family(Path(records), "decisions"))
    if isinstance(records, MergedDecisions):
        return records
    if isinstance(records, Mapping):
        return records  # already merged: decision_id -> record
    return merge_decisions(records)


def _key(rec: Mapping[str, Any]) -> tuple[str, str, str]:
    return (str(rec.get("kind")), str(rec.get("strategy")), str(rec.get("symbol")))


def _brief(decision_id: str, rec: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "decision_id": decision_id,
        "kind": rec.get("kind"),
        "strategy": rec.get("strategy"),
        "symbol": rec.get("symbol"),
    }


def _provenance(
    ref: Mapping[str, Mapping[str, Any]], rep: Mapping[str, Mapping[str, Any]], name: str
) -> dict[str, Any]:
    a = sorted({str(r.get(name)) for r in ref.values() if r.get(name) is not None})
    b = sorted({str(r.get(name)) for r in rep.values() if r.get(name) is not None})
    return {"reference": a, "replayed": b, "equal": a == b}


def _get(rec: Mapping[str, Any], path: str) -> Any:
    if path.startswith("plan."):  # the plan in force: plan_snapshot
        path = "plan_snapshot." + path[len("plan.") :]
    value: Any = rec
    for part in path.split("."):
        if not isinstance(value, Mapping):
            return None
        value = value.get(part)
    return value


def _diff(
    decision_id: str, a: Mapping[str, Any], b: Mapping[str, Any], strict: bool, wanted: tuple[str, ...]
) -> list[Mismatch]:
    if strict:
        fa, fb = _flatten(a), _flatten(b)
        paths = sorted((set(fa) | set(fb)) - _ALIGNMENT_PATHS)
        return [
            Mismatch(decision_id, p, fa.get(p), fb.get(p)) for p in paths if not _same(fa.get(p), fb.get(p))
        ]
    rows: list[Mismatch] = []
    for path in wanted:
        if path == "risk.checks":
            rows += _diff_checks(decision_id, _get(a, path), _get(b, path))
            continue
        va, vb = _get(a, path), _get(b, path)
        if not _same(va, vb):
            rows.append(Mismatch(decision_id, path, va, vb))
    return rows


def _keyed_checks(checks: Any) -> dict[str, Mapping[str, Any]]:
    out: dict[str, Mapping[str, Any]] = {}
    seen: dict[str, int] = {}
    for c in checks if isinstance(checks, list) else []:
        if not isinstance(c, Mapping):
            continue
        code = str(c.get("code"))
        seen[code] = seen.get(code, 0) + 1
        out[code if seen[code] == 1 else f"{code}#{seen[code]}"] = c
    return out


def _diff_checks(decision_id: str, a: Any, b: Any) -> list[Mismatch]:
    ka, kb = _keyed_checks(a), _keyed_checks(b)
    rows: list[Mismatch] = []
    for key in list(ka) + [k for k in kb if k not in ka]:
        ca, cb = ka.get(key), kb.get(key)
        if ca is None or cb is None:
            rows.append(Mismatch(decision_id, f"risk.checks[{key}]", _check_tuple(ca), _check_tuple(cb)))
            continue
        for attr in ("passed", "detail"):
            if not _same(ca.get(attr), cb.get(attr)):
                rows.append(Mismatch(decision_id, f"risk.checks[{key}].{attr}", ca.get(attr), cb.get(attr)))
    if not rows and list(ka) != list(kb):
        rows.append(Mismatch(decision_id, "risk.checks.order", list(ka), list(kb)))
    return rows


def _check_tuple(c: Mapping[str, Any] | None) -> list[Any] | None:
    return None if c is None else [c.get("code"), c.get("passed"), c.get("detail")]


def _ignored(path: str) -> bool:
    if path in IGNORE:
        return True
    leaf = path.rsplit(".", 1)[-1]
    leaf = leaf.split("[", 1)[0]
    return leaf in IGNORE_KEYS or leaf.endswith(IGNORE_SUFFIX)


def _flatten(rec: Mapping[str, Any]) -> dict[str, Any]:
    """Dotted leaf paths (lists by index, ``risk.checks`` by code) minus the ignored ones."""
    out: dict[str, Any] = {}

    def walk(value: Any, path: str) -> None:
        if path and _ignored(path):
            return
        if path == "risk.checks" and isinstance(value, list):
            for key, c in _keyed_checks(value).items():
                for attr in ("passed", "detail"):
                    out[f"risk.checks[{key}].{attr}"] = c.get(attr)
            return
        if isinstance(value, Mapping):
            if not value and path:
                out[path] = {}
            for k in sorted(value):
                walk(value[k], f"{path}.{k}" if path else str(k))
            return
        if isinstance(value, list):
            if not value:
                out[path] = []
            for i, v in enumerate(value):
                walk(v, f"{path}[{i}]")
            return
        out[path] = value

    walk(rec, "")
    return out


def _as_number(value: Any) -> Decimal | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, str) and _NUMBER.fullmatch(value):
        try:
            return Decimal(value)
        except InvalidOperation:  # pragma: no cover - the pattern only admits valid numbers
            return None
    return None


def _same(a: Any, b: Any) -> bool:
    na, nb = _as_number(a), _as_number(b)
    if na is not None and nb is not None:
        return na == nb
    if isinstance(a, bool) or isinstance(b, bool):
        return type(a) is type(b) and a == b
    return bool(a == b)


def _show(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, str):
        return value
    return orjson.dumps(value, option=orjson.OPT_SORT_KEYS).decode("utf-8")


def _row(*cells: Any) -> str:
    return "| " + " | ".join(str(c) for c in cells) + " |"


def _cell(text: str) -> str:
    text = text.replace("\r", " ").replace("\n", " ").replace("|", "\\|")
    if len(text) > _MAX_CELL:
        text = text[: _MAX_CELL - 1] + "…"
    return text

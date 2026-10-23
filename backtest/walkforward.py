"""Walk-forward parameter selection and the overfitting guard (PRD-05 §3.4).

``wta backtest ... --param-grid strategies.s1_crossbook.margin_min_pct=0.0008,0.0012,0.0015,0.0025``:

1. The grid: at most ``MAX_PARAMS`` (2) parameters with at most ``MAX_VALUES`` (8) values each
   (``parse_param_grid``; every key must exist in the config and every value must validate). The
   pre-grid default (the value in ``configs/*.yaml`` + ``--set``) is always a candidate too, so the
   table shows it next to the grid values.
2. The split: the window's **data** time (the backtest's tick times, ``runner.plan_steps``: gaps in
   recorded books do not count) is cut 70 / 30: in-sample = the first 70 % of the ticks,
   out-of-sample = the rest. Each half is its own backtest (a fresh paper account); the OOS run
   preloads the candles before its start as indicator warm-up, like any run — no look-ahead.
   Every run (both halves of every candidate and the final run) ends under the SAME rule: no
   end-of-window flatten, holdings valued at the bid (``flatten_at_end=False``), unless
   ``flatten_at_end`` is given, which then applies to all of them. The runner's automatic rule
   (flatten in windows >= 8 h) would flatten the 70 % half and not the 30 % one, costing only the
   in-sample half its last 2 h and the sell-to-TMN fees, which biases the guard towards accepting.
3. The choice: the candidate with the highest **in-sample** return (``return_pct``); a tie goes to
   the lower in-sample max drawdown, then to the default, then to grid order.
4. The guard (``accept_rule``): the chosen value is ACCEPTED only if its out-of-sample return **per
   day of data** does not degrade by more than 50 % against its in-sample return per day —
   ``(IS/d_IS - OOS/d_OOS) / |IS/d_IS| <= 0.5`` with ``d`` = the half's data ticks x the tick step —
   and is not negative; a grid value whose in-sample return is not positive is never adopted
   (``docs/STRATEGY.md`` §10). Otherwise the pre-grid default is kept. A return that cannot be
   computed (insufficient data) rejects. (Per day, because the halves
   differ in length: comparing the 70 % and 30 % totals would reject a perfectly steady strategy,
   whose OOS total is only 3/7 of its IS total.)
5. The final run: the whole window with the final parameters into ``out_dir`` (``report.md`` etc.),
   with a "Walk-forward" section (the table and the decision) inserted into ``report.md``,
   ``walkforward`` in ``summary.json``, and ``out_dir/walkforward.md`` + ``walkforward.json``. The
   per-candidate runs live in ``out_dir/walkforward/<nn>_<value>/{is,oos}``.

With ``ai=on`` every candidate run calls the analyst (capped per run); the LLM cache is shared
(``out_dir/llm_cache``), so identical prompts are paid once.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import math
import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from backtest.report import SYNTHETIC_BANNER
from backtest.runner import (
    LLM_CACHE,
    SYNTHETIC_TAG,
    BacktestResult,
    arun_backtest,
    insert_section,
    parse_strategies,
    parse_ts,
    plan_steps,
    update_summary,
)
from wta.config import Config, apply_changes, get_path
from wta.utils.timeutil import iso

MAX_PARAMS = 2
MAX_VALUES = 8
IS_FRACTION = 0.7
MAX_DEGRADATION = 0.5
TIE_EPS = 1e-12
WALKFORWARD_MD = "walkforward.md"
WALKFORWARD_JSON = "walkforward.json"
RUNS_DIR = "walkforward"

Runner = Callable[..., Awaitable[BacktestResult]]


# ---------------------------------------------------------------- grid


def parse_param_grid(specs: str | Sequence[str] | None, cfg: Config | None = None) -> dict[str, list[Any]]:
    """``["a.b=1,2", "c.d=x,y"]`` -> ``{"a.b": [1, 2], "c.d": ["x", "y"]}`` (values YAML-typed, order kept).

    Raises ``ValueError`` for more than 2 parameters, more than 8 values, duplicates, an unknown key
    (when ``cfg`` is given) or a value the config refuses."""
    if specs is None:
        return {}
    items = [specs] if isinstance(specs, str) else list(specs)
    grid: dict[str, list[Any]] = {}
    for spec in items:
        if "=" not in spec:
            raise ValueError(f"--param-grid must be key=v1,v2,..., got {spec!r}")
        key, raw = spec.split("=", 1)
        key = key.strip()
        if not key:
            raise ValueError(f"--param-grid {spec!r}: empty key")
        if key in grid:
            raise ValueError(f"--param-grid: {key} given twice")
        values = [yaml.safe_load(v.strip()) for v in raw.split(",") if v.strip()]
        if not values:
            raise ValueError(f"--param-grid {key}: no values")
        if len(values) > MAX_VALUES:
            raise ValueError(f"--param-grid {key}: {len(values)} values (at most {MAX_VALUES}, PRD-05 §3.4)")
        if len({json.dumps(v, sort_keys=True, default=str) for v in values}) != len(values):
            raise ValueError(f"--param-grid {key}: duplicate values")
        grid[key] = values
    if len(grid) > MAX_PARAMS:
        raise ValueError(f"--param-grid: {len(grid)} parameters (at most {MAX_PARAMS} at a time, PRD-05 §3.4)")
    if cfg is not None:
        tree = cfg.model_dump(mode="json")
        for key, values in grid.items():
            try:
                get_path(tree, key)
            except (KeyError, TypeError):
                raise ValueError(f"--param-grid: unknown config key {key}") from None
            for v in values:
                try:
                    apply_changes(cfg, {key: v})
                except ValueError as exc:
                    raise ValueError(f"--param-grid {key}={v!r}: invalid ({str(exc).splitlines()[0][:200]})") from None
    return grid


def default_params(cfg: Config, keys: Sequence[str]) -> dict[str, Any]:
    tree = cfg.model_dump(mode="json")
    return {k: get_path(tree, k) for k in keys}


def combos(grid: Mapping[str, Sequence[Any]]) -> list[dict[str, Any]]:
    keys = list(grid)
    return [dict(zip(keys, vals, strict=True)) for vals in itertools.product(*(grid[k] for k in keys))]


def _same(a: Mapping[str, Any], b: Mapping[str, Any]) -> bool:
    """Equal parameter sets (``0.0015`` and ``"0.0015"`` count as equal: config values are dumped as text)."""
    def norm(v: Any) -> str:
        try:
            return repr(float(v))
        except (TypeError, ValueError):
            return json.dumps(v, sort_keys=True, default=str)

    return set(a) == set(b) and all(norm(a[k]) == norm(b[k]) for k in a)


def label(params: Mapping[str, Any]) -> str:
    return ", ".join(f"{k.rsplit('.', 1)[-1]}={v}" for k, v in params.items())


def _slug(i: int, params: Mapping[str, Any]) -> str:
    text = "__".join(f"{k.rsplit('.', 1)[-1]}={v}" for k, v in params.items())
    return f"{i:02d}_" + re.sub(r"[^A-Za-z0-9_.=-]+", "-", text)[:80]


# ---------------------------------------------------------------- rule


def accept_rule(is_ret: float | None, oos_ret: float | None, max_degradation: float = MAX_DEGRADATION, *,
                is_days: float | None = None, oos_days: float | None = None) -> tuple[bool, str]:
    """PRD-05 §3.4: accept only when the in-sample return is positive, the out-of-sample return is not
    negative and does not degrade > 50 % vs in-sample. Returns ``(accepted, why)``.

    With ``is_days`` and ``oos_days`` (> 0, the halves' data time) the returns are compared **per day**
    (``ret / days``), so halves of different length compare fairly; without them, as given."""
    if is_ret is None or oos_ret is None or not (math.isfinite(is_ret) and math.isfinite(oos_ret)):
        return False, "a return is missing (insufficient data): keep the default"
    per_day = bool(is_days and oos_days and is_days > 0 and oos_days > 0)
    if per_day:
        assert is_days is not None and oos_days is not None
        is_ret, oos_ret = is_ret / is_days, oos_ret / oos_days
    what = "return per day" if per_day else "return"
    if is_ret <= 0:  # docs/STRATEGY.md §10: a non-positive in-sample result is never adopted
        return False, f"in-sample {what} {is_ret:+.4%} is not positive: a non-positive in-sample result is never adopted"
    if oos_ret < 0:
        return False, f"out-of-sample {what} {oos_ret:+.4%} is negative while in-sample is {is_ret:+.4%}"
    degradation = (is_ret - oos_ret) / abs(is_ret)
    if degradation > max_degradation:
        return False, (f"out-of-sample {what} {oos_ret:+.4%} degrades {degradation:.0%} vs in-sample "
                       f"{is_ret:+.4%} (> {max_degradation:.0%})")
    return True, (f"out-of-sample {what} {oos_ret:+.4%} vs in-sample {is_ret:+.4%}: degradation "
                  f"{max(degradation, 0.0):.0%} (<= {max_degradation:.0%})")


@dataclass
class Point:
    """One candidate: its parameters and its in-sample / out-of-sample results."""

    params: dict[str, Any]
    is_default: bool
    index: int
    is_result: BacktestResult | None = None
    oos_result: BacktestResult | None = None
    is_days: float | None = None  # data time of each half (ticks x step), for the per-day guard
    oos_days: float | None = None

    @property
    def label(self) -> str:
        return label(self.params) + (" (default)" if self.is_default else "")

    def _m(self, which: str, key: str) -> Any:
        res = self.is_result if which == "is" else self.oos_result
        return None if res is None else getattr(res.metrics, key)

    @property
    def is_return(self) -> float | None:
        return self._m("is", "return_pct")

    @property
    def is_drawdown(self) -> float | None:
        return self._m("is", "max_drawdown_pct")

    @property
    def oos_return(self) -> float | None:
        return self._m("oos", "return_pct")

    @property
    def oos_drawdown(self) -> float | None:
        return self._m("oos", "max_drawdown_pct")

    @staticmethod
    def _per_day(ret: float | None, days: float | None) -> float | None:
        return ret / days if ret is not None and days else None

    @property
    def is_per_day(self) -> float | None:
        return self._per_day(self.is_return, self.is_days)

    @property
    def oos_per_day(self) -> float | None:
        return self._per_day(self.oos_return, self.oos_days)

    def row(self) -> dict[str, Any]:
        return {
            "params": dict(self.params),
            "default": self.is_default,
            "is_return_pct": self.is_return,
            "is_max_drawdown_pct": self.is_drawdown,
            "is_trades": self._m("is", "n_trades"),
            "is_orders": self._m("is", "n_orders"),
            "oos_return_pct": self.oos_return,
            "oos_max_drawdown_pct": self.oos_drawdown,
            "oos_trades": self._m("oos", "n_trades"),
            "oos_orders": self._m("oos", "n_orders"),
            "is_days": self.is_days,
            "oos_days": self.oos_days,
            "is_return_per_day": self.is_per_day,
            "oos_return_per_day": self.oos_per_day,
        }


def choose(points: Sequence[Point]) -> Point:
    """Highest in-sample return; tie -> lower in-sample drawdown -> the default -> grid order.
    A candidate without an in-sample return ranks last."""
    def key(p: Point) -> tuple[Any, ...]:
        ret = p.is_return
        dd = p.is_drawdown
        return (
            ret is None,
            -round(ret, 12) if ret is not None else 0.0,
            round(dd, 12) if dd is not None else math.inf,
            not p.is_default,
            p.index,
        )

    if not points:
        raise ValueError("no candidates")
    return min(points, key=key)


# ---------------------------------------------------------------- run


@dataclass
class WalkForwardResult:
    grid: dict[str, list[Any]]
    default: dict[str, Any]
    split_ts: float | None
    is_window: tuple[float, float]
    oos_window: tuple[float, float]
    points: list[Point]
    chosen: Point
    accepted: bool
    reason: str
    final_params: dict[str, Any]
    final: BacktestResult | None
    paths: dict[str, Path] = field(default_factory=dict)
    synthetic: bool = False
    flatten_at_end: bool = False  # the end-of-window rule every run of the walk-forward used

    def to_dict(self) -> dict[str, Any]:
        return {
            "grid": self.grid,
            "default": self.default,
            "split": iso(self.split_ts) if self.split_ts is not None else None,
            "in_sample": [iso(self.is_window[0]), iso(self.is_window[1])],
            "out_of_sample": [iso(self.oos_window[0]), iso(self.oos_window[1])],
            "rule": f"returns per day of data: accept only if IS > 0, OOS >= 0 and (IS - OOS) / IS <= "
                    f"{MAX_DEGRADATION:.0%}; else keep the pre-grid default",
            "chosen": dict(self.chosen.params),
            "chosen_is_default": self.chosen.is_default,
            "accepted": self.accepted,
            "reason": self.reason,
            "final_params": dict(self.final_params),
            "rows": [p.row() for p in self.points],
            "synthetic": self.synthetic,
            "flatten_at_end": self.flatten_at_end,
        }


def split_window(steps: Sequence[float], from_ts: float, to_ts: float, fraction: float = IS_FRACTION) -> tuple[
        tuple[float, float], tuple[float, float], float | None]:
    """In-sample / out-of-sample windows: the first ``fraction`` of the data ticks, then the rest
    (by time when there are fewer than two ticks)."""
    if not 0 < fraction < 1:
        raise ValueError("the in-sample fraction must be in (0, 1)")
    if len(steps) >= 2:
        k = min(max(1, math.floor(len(steps) * fraction)), len(steps) - 1)
        return (from_ts, steps[k - 1]), (steps[k], to_ts), steps[k]
    cut = from_ts + (to_ts - from_ts) * fraction
    return (from_ts, cut), (cut, to_ts), cut


def run_walkforward(cfg: Config, **kwargs: Any) -> WalkForwardResult:
    """Synchronous ``arun_walkforward``."""
    return asyncio.run(arun_walkforward(cfg, **kwargs))


async def arun_walkforward(
    cfg: Config,
    *,
    grid: Mapping[str, Sequence[Any]],
    driver: str,
    from_ts: float | str,
    to_ts: float | str,
    out_dir: str | Path,
    strategies: Sequence[str] | str | None = None,
    market_db: str | Path | None = None,
    step_s: float | None = None,
    fraction: float = IS_FRACTION,
    final_run: bool = True,
    runner: Runner | None = None,
    **backtest_kwargs: Any,
) -> WalkForwardResult:
    """The walk-forward of the module docstring. ``runner`` replaces ``arun_backtest`` (tests)."""
    run = runner or arun_backtest
    extra_limits = list(backtest_kwargs.pop("limitations", ()) or ())
    final_title = backtest_kwargs.pop("title", None)
    if not grid:
        raise ValueError("an empty parameter grid")
    if len(grid) > MAX_PARAMS or any(len(v) > MAX_VALUES for v in grid.values()):
        raise ValueError(f"at most {MAX_PARAMS} parameters with at most {MAX_VALUES} values each")
    a, b = parse_ts(from_ts, "from"), parse_ts(to_ts, "to")
    strats = parse_strategies(strategies, driver)
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    plan = plan_steps(cfg, driver=driver, from_ts=a, to_ts=b, strategies=strats, market_db=market_db, step_s=step_s)
    is_win, oos_win, split = split_window(plan.steps, a, b, fraction)
    if len(plan.steps) >= 2 and split is not None:
        n_is = sum(1 for t in plan.steps if t < split)
        is_days = n_is * plan.step_s / 86400.0
        oos_days = (len(plan.steps) - n_is) * plan.step_s / 86400.0
    else:
        is_days, oos_days = (is_win[1] - is_win[0]) / 86400.0, (oos_win[1] - oos_win[0]) / 86400.0
    keys = list(grid)
    default = default_params(cfg, keys)
    candidates = combos(grid)
    points: list[Point] = []
    if not any(_same(c, default) for c in candidates):
        points.append(Point(dict(default), True, 0, is_days=is_days, oos_days=oos_days))
    for c in candidates:
        points.append(Point(dict(c), _same(c, default), len(points), is_days=is_days, oos_days=oos_days))
    if backtest_kwargs.get("ai", "off") != "off":
        backtest_kwargs.setdefault("llm_cache", out / LLM_CACHE)
    if backtest_kwargs.get("flatten_at_end") is None:  # one end rule for every run (module docstring, 2.)
        backtest_kwargs["flatten_at_end"] = False
    flatten = bool(backtest_kwargs["flatten_at_end"])
    common = {"driver": driver, "strategies": strats, "market_db": market_db, "step_s": step_s, **backtest_kwargs}
    for p in points:
        pcfg = apply_changes(cfg, p.params)
        base = out / RUNS_DIR / _slug(p.index, p.params)
        p.is_result = await run(pcfg, from_ts=is_win[0], to_ts=is_win[1], out_dir=base / "is",
                                title=f"Walk-forward in-sample: {p.label}", limitations=extra_limits, **common)
        p.oos_result = await run(pcfg, from_ts=oos_win[0], to_ts=oos_win[1], out_dir=base / "oos",
                                 title=f"Walk-forward out-of-sample: {p.label}", limitations=extra_limits, **common)
    chosen = choose(points)
    if chosen.is_default:
        accepted, reason = True, "the pre-grid default has the best in-sample return: it stays"
        final_params = dict(default)
    else:
        accepted, reason = accept_rule(chosen.is_return, chosen.oos_return, is_days=chosen.is_days,
                                       oos_days=chosen.oos_days)
        final_params = dict(chosen.params) if accepted else dict(default)
        if not accepted:
            reason += "; the pre-grid default is kept"
    result = WalkForwardResult(
        grid={k: list(v) for k, v in grid.items()}, default=default, split_ts=split, is_window=is_win,
        oos_window=oos_win, points=points, chosen=chosen, accepted=accepted, reason=reason,
        final_params=final_params, final=None, synthetic=plan.synthetic is not None, flatten_at_end=flatten,
    )
    lines = markdown(result)
    md = out / WALKFORWARD_MD
    md.write_text("\n".join(lines) + "\n", encoding="utf-8")
    js = out / WALKFORWARD_JSON
    js.write_text(json.dumps(result.to_dict(), sort_keys=True, indent=2, default=str) + "\n", encoding="utf-8")
    result.paths.update(markdown=md, json=js)
    if final_run:
        fcfg = apply_changes(cfg, final_params)
        note = (f"Parameters from the walk-forward grid ({WALKFORWARD_MD}): {label(final_params)} "
                f"({'grid value accepted' if accepted and not chosen.is_default else 'pre-grid default'}).")
        final = await run(fcfg, from_ts=a, to_ts=b, out_dir=out, limitations=[*extra_limits, note],
                          title=final_title, **common)
        result.final = final
        report = final.paths.get("report")
        if report is not None and Path(report).is_file():
            insert_section(Path(report), ["## Walk-forward (parameter grid)", "", *lines[1:]])
            summary = final.paths.get("summary")
            if summary is not None:
                update_summary(Path(summary), "walkforward", result.to_dict())
    return result


def _pct(v: float | None) -> str:
    return "n/a" if v is None else f"{v * 100:+.3f}%"


def _dd(v: float | None) -> str:
    return "n/a" if v is None else f"{v * 100:.3f}%"


def markdown(result: WalkForwardResult) -> list[str]:
    """``walkforward.md`` (also inserted, without its title, into the final ``report.md``)."""
    keys = ", ".join(f"`{k}`" for k in result.grid)
    lines = [f"# Walk-forward: {keys}", ""]
    if result.synthetic:
        lines += [f"> **{SYNTHETIC_BANNER}.** The grid was evaluated on generated data ({SYNTHETIC_TAG}).", ""]
    lines += [
        f"- In-sample (first {IS_FRACTION:.0%} of the data ticks): {iso(result.is_window[0])} → {iso(result.is_window[1])}",
        f"- Out-of-sample (the rest): {iso(result.oos_window[0])} → {iso(result.oos_window[1])}",
        f"- Choice: highest in-sample return (tie: lower in-sample drawdown). Guard: accept only if the "
        f"out-of-sample return **per day of data** does not degrade more than {MAX_DEGRADATION:.0%} vs in-sample "
        "and is not negative, and only if the in-sample return is positive; otherwise keep the pre-grid default. "
        "Each half is a fresh paper account.",
        "- End of every run (both halves of each candidate and the final run): "
        + ("the deadline flatten in its last 2 h (no new entries there), then everything sold to TMN."
           if result.flatten_at_end else
           "no end-of-window flatten; holdings at the end are valued at the bid (unrealized), the same rule "
           "for the in-sample and the out-of-sample half."),
    ]
    p0 = result.points[0] if result.points else None
    if p0 is not None and p0.is_days and p0.oos_days:
        lines.append(f"- Data time: in-sample {p0.is_days * 24:,.2f} h, out-of-sample {p0.oos_days * 24:,.2f} h.")
    lines += [
        "",
        "| candidate | IS return | IS / day | IS max DD | IS trades | OOS return | OOS / day | OOS max DD | OOS trades | |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---|",
    ]
    for p in result.points:
        r = p.row()
        mark = "chosen" if p is result.chosen else ""
        lines.append(
            f"| {p.label} | {_pct(r['is_return_pct'])} | {_pct(r['is_return_per_day'])} | "
            f"{_dd(r['is_max_drawdown_pct'])} | {r['is_trades']} | {_pct(r['oos_return_pct'])} | "
            f"{_pct(r['oos_return_per_day'])} | {_dd(r['oos_max_drawdown_pct'])} | {r['oos_trades']} | {mark} |"
        )
    verdict = "ACCEPTED" if result.accepted and not result.chosen.is_default else (
        "DEFAULT KEPT" if result.chosen.is_default else "REJECTED")
    lines += [
        "",
        f"**Decision: {verdict}.** Chosen in-sample: {label(result.chosen.params)}. {result.reason}. "
        f"Final parameters: {label(result.final_params)}.",
        "",
        "_Returns are fractions of the starting equity of each half (shown ×100). Few trades in a half make "
        "this comparison weak; read the trade counts first._",
    ]
    return lines


__all__ = [
    "MAX_PARAMS",
    "MAX_VALUES",
    "Point",
    "WalkForwardResult",
    "accept_rule",
    "arun_walkforward",
    "choose",
    "combos",
    "default_params",
    "markdown",
    "parse_param_grid",
    "run_walkforward",
    "split_window",
]

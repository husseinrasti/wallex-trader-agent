"""Replay check for judges (PRD-05 §6; ``make replay``): re-run a recorded run, compare every decision.

    python scripts/replay_check.py                         # newest live run under logs/, else newest shadow run
    python scripts/replay_check.py --decisions logs/decisions.jsonl     # the run whose decisions these are
    python scripts/replay_check.py --log-dir logs/shadow_synth_4h --out /tmp/replay1 --json
    python scripts/replay_check.py --bundle deliverables/replay/shadow_synth_4h   # make replay-deliverable

The engine is rebuilt as the recorded run had it (config hash checked; runtime overlay as recorded;
AI as recorded with the strict LLM cache) and driven over the recorded inputs: the ticks, books and
candles the engine stored in its state DB, the recorded exchange answers (``ReplayExchange``) and the
cached analyst plans. Every internet connection attempt is refused and counted during the replay.
The decision log it writes is compared with the recorded one (``wta.replay.runner``), the result is
printed and written to ``docs/REPORTS/replay_<YYYYMMDD>.md`` (the UTC day of the last replayed tick).

``--bundle DIR`` replays a committed replay bundle instead of a run under ``logs/`` (``logs/`` and
``data/`` are not in git): ``DIR/SHA256SUMS`` is checked against ``logs.tar.xz`` and ``state.sqlite.xz``
first, both are unpacked into a temporary directory, the same replay runs on them, and the temporary
directory is removed afterwards (also on failure). With ``--bundle`` no report is written unless
``--reports-dir`` is given, so the repo's ``docs/REPORTS`` stay as committed; the replay output is
removed too unless ``--out`` is given.

Exit codes: 0 = every decision reproduced on the PRD-05 fields AND in the strict pass (every field,
time stamps, fees, fills and outcome PnL included), every plan re-derived identically, zero network
calls; 1 = a mismatch, a missing or extra decision, a plan re-derived differently, an exchange
divergence, a replayed crash the recorded run did not have, a determinism failure (LLM cache miss),
a partial recording (no stop event, fewer ticks in the state DB than the stop event counts, records
after the last committed tick; ``--allow-partial`` replays the committed prefix instead) or a network
call; 2 = nothing to replay or compare, or bad arguments (no recorded run, no state DB, output
directory not empty, a recorded window without any decision, a bundle that is incomplete, does not match
its SHA256SUMS or holds an unsafe archive member).
"""

from __future__ import annotations

import argparse
import json
import logging
import shutil
import sys
import tempfile
from pathlib import Path

from wta.config import ROOT, Config, load_config, parse_overrides
from wta.replay.runner import BundleError, ReplayInputError, extract_replay_bundle, pick_run_dir, run_replay


def _config(sets: list[str], overlays: list[str]) -> Config:
    if overlays:
        from wta.cli import load_cli_config  # the CLI's own merge of --overlay files and --set

        return load_cli_config(sets, overlays)
    return load_config(parse_overrides(sets))


def _log_dir(args: argparse.Namespace) -> tuple[Path | None, str]:
    """(recorded log dir, why it was chosen)."""
    if args.log_dir:
        return Path(args.log_dir), "given with --log-dir"
    if args.decisions:
        path = Path(args.decisions)
        if path.exists():  # a decisions file (or its current-day symlink): its directory is the run
            return path.parent, f"directory of {path}"
        reason = f"{path} does not exist (no live run recorded there)"
    else:
        reason = "no --log-dir / --decisions given"
    root = Path(args.logs_root)
    if not root.is_absolute() and not root.exists():
        root = ROOT / root
    found = pick_run_dir(root)
    if found is None:
        return None, f"{reason}; no engine run under {root}"
    kind = "newest live run" if found["mode"] == "live" else f"no live run; newest {found['mode']} run"
    return Path(found["path"]), f"{reason}; {kind} under {root}"


def _bundle_dir(value: str) -> Path:
    path = Path(value)
    if not path.is_absolute() and not path.exists() and (ROOT / path).exists():
        path = ROOT / path
    return path


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="replay_check.py", description=__doc__.split("\n\n")[0])
    ap.add_argument("--bundle", default=None, metavar="DIR",
                    help="a replay bundle (logs.tar.xz, state.sqlite.xz, SHA256SUMS): checksums verified, unpacked "
                         "into a temporary directory, replayed, removed")
    ap.add_argument("--decisions", default=None, help="a recorded decisions file; its directory is the run")
    ap.add_argument("--log-dir", default=None, help="the recorded run's log directory")
    ap.add_argument("--logs-root", default="logs", help="where to look for runs when none is given (default logs)")
    ap.add_argument("--state-db", default=None, help="the recorded engine state DB (default: from its start event)")
    ap.add_argument("--segment", type=int, default=None,
                    help="process segment of the log dir, 1-based (default: the latest fresh start)")
    ap.add_argument("--out", default=None, help="new or empty directory for the replay's logs + state DB "
                                                 "(default: a temporary directory)")
    ap.add_argument("--reports-dir", default=None,
                    help="where replay_<date>.md goes ('' = nowhere; default docs/REPORTS, with --bundle nowhere)")
    ap.add_argument("--set", action="append", default=[], metavar="KEY=VALUE",
                    help="the recorded run's config overrides, if its config cannot be recovered (repeatable)")
    ap.add_argument("--overlay", action="append", default=[], metavar="FILE", help="the recorded run's overlays")
    ap.add_argument("--lenient", action="store_true",
                    help="keep going after an LLM cache miss / unmatched config (still exits 1 on a mismatch)")
    ap.add_argument("--allow-partial", action="store_true",
                    help="replay the committed prefix of a run that is still being written, crashed or whose "
                         "state DB copy is incomplete (a PARTIAL replay; without it that is a failure)")
    ap.add_argument("--no-index", action="store_true", help="do not regenerate docs/REPORTS/README.md")
    ap.add_argument("--json", action="store_true", help="print the full result as JSON")
    ap.add_argument("-v", "--verbose", action="store_true", help="engine log lines on stderr")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO if args.verbose else logging.ERROR, stream=sys.stderr,
                        format="%(levelname)s %(name)s: %(message)s")

    if args.bundle is None:
        return _check(args)
    if args.log_dir or args.decisions or args.state_db:
        print("replay: --bundle replays the bundle's own run: drop --log-dir / --decisions / --state-db",
              file=sys.stderr)
        return 2
    bundle = _bundle_dir(args.bundle)
    work = Path(tempfile.mkdtemp(prefix="wta-bundle-"))
    try:
        try:
            log_dir, state_db = extract_replay_bundle(bundle, work / "in")
        except BundleError as exc:
            print(f"replay: bundle {args.bundle}: {exc}", file=sys.stderr)
            return 2
        print(f"bundle:   {args.bundle}: SHA256SUMS verified (logs.tar.xz, state.sqlite.xz); "
              f"unpacked into a temporary directory")
        return _check(args, log_dir=log_dir, state_db=state_db, out=Path(args.out) if args.out else work / "out",
                      why=f"from bundle {args.bundle}", keep_out=bool(args.out))
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _check(args: argparse.Namespace, *, log_dir: Path | None = None, state_db: Path | None = None,
           out: Path | None = None, why: str = "", keep_out: bool = True) -> int:
    """The replay of one recorded run: a run found from the arguments, or ``log_dir`` + ``state_db``
    (an unpacked bundle; ``keep_out=False``: its output directory is removed by the caller)."""
    bundled = log_dir is not None
    if log_dir is None:
        log_dir, why = _log_dir(args)
    if log_dir is None:
        print(f"replay: nothing to replay: {why}", file=sys.stderr)
        return 2
    try:
        cfg = _config(args.set, args.overlay)
    except (ValueError, OSError) as exc:
        print(f"replay: bad --set/--overlay: {exc}", file=sys.stderr)
        return 2
    if out is None:
        out = Path(args.out) if args.out else Path(tempfile.mkdtemp(prefix="wta-replay-"))
    if args.reports_dir is None:
        reports = None if bundled else "docs/REPORTS"
    else:
        reports = args.reports_dir or None
    shown = f"{log_dir.name} (unpacked, {why})" if bundled else f"{log_dir} ({why})"
    print(f"replay: {shown}")
    try:
        result = run_replay(cfg, log_dir=log_dir, state_db=state_db or args.state_db, out_dir=out,
                            reports_dir=reports, strict=not args.lenient, segment=args.segment,
                            allow_partial=args.allow_partial)
    except ReplayInputError as exc:
        print(f"replay: cannot replay {log_dir}: {exc}", file=sys.stderr)
        return 2
    if reports and not args.no_index:
        try:
            sys.path.insert(0, str(ROOT))
            from backtest.report import update_index

            update_index(reports)
        except Exception as exc:  # noqa: BLE001 - the index is a convenience, the verdict stands
            print(f"replay: reports index not updated: {exc}", file=sys.stderr)

    code = 0 if result.ok else (2 if result.n_reference == 0 else 1)
    if args.json:
        print(json.dumps(result.to_dict(), indent=2, sort_keys=True, default=str))
        return code
    r = result.run
    rec, win, net, ai = r["recorded"], r["window"], r["network"], r["ai"]
    label = " — SYNTHETIC, not evidence of performance" if rec["synthetic"] else ""
    print(f"recorded: mode {rec['mode']}, feed {rec['feed']}{label}; segment {rec['segment']} of {rec['segments']}"
          + ("; PARTIAL (committed prefix only, --allow-partial)" if rec.get("partial") and args.allow_partial
             else ""))
    conf = r["config"]
    same = "= recorded" if conf["equal"] else f"DIFFERS from recorded {conf['recorded_hash']}"
    print(f"window:   {win['first']} -> {win['last']} ({win['hours']:.2f} h, {win['ticks']} ticks); "
          f"plans {ai['plans_reproduced']}/{ai['plans_recorded']} re-derived identically; "
          f"config {conf['replay_hash']} ({same})")
    print(f"network:  {net['socket_attempts']} socket attempts, {net['provider_calls']} LLM provider calls; "
          f"LLM cache {ai['cache_hits']} hits / {ai['cache_misses']} misses")
    print(f"exchange: {', '.join(f'{k} {v}' for k, v in sorted(r['exchange']['answers'].items())) or 'no calls'}; "
          f"divergences {len(result.divergences)}")
    for e in result.errors:
        print(f"ERROR:    {e}")
    for m in result.mismatches[:10]:
        print(f"mismatch: {m.decision_id} {m.field}: recorded {m.reference!r} replayed {m.replayed!r}")
    if len(result.mismatches) > 10:
        print(f"mismatch: ... {len(result.mismatches) - 10} more (see the report)")
    for d in result.missing[:5]:
        print(f"missing:  {d['decision_id']} ({d['kind']} {d['strategy']} {d['symbol']})")
    for d in result.extra[:5]:
        print(f"extra:    {d['decision_id']} ({d['kind']} {d['strategy']} {d['symbol']})")
    print(f"strict:   {result.strict_check.get('summary')} (every field, time stamps included)")
    end = r.get("end_state") or {}
    if end.get("compared"):
        rows = end.get("rows") or []
        print(f"end:      {sum(1 for x in rows if x['equal'])}/{len(rows)} final-heartbeat values equal "
              "(equity, fees, plan freshness, S1 tick counters; informational)")
    print(f"result:   {result.summary} -> {'PASS' if result.ok else 'FAIL'}")
    if keep_out:
        print(f"report:   {result.report_file or out / 'replay_report.md'}  (replay output: {out})")
    else:
        print(f"report:   {result.report_file or 'not written (--bundle without --reports-dir)'}  "
              "(replay output removed with the temporary directory; --out DIR keeps it)")
    return code


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env bash
# Demo helper for the ~5-minute video. The shot list and the narration are in docs/VIDEO_SHOTLIST.md.
#
# Each step prints its command, waits for Enter, then runs it.
#
#   bash scripts/demo.sh                 every scene in order (0 = preparation, before recording)
#   bash scripts/demo.sh --list          the scenes and their steps; runs nothing
#   bash scripts/demo.sh --scene 3       one scene; repeat the flag for several (--scene 0 --scene 3)
#   bash scripts/demo.sh --yes           do not wait for Enter; live views print one frame (rehearsal, tests)
#   bash scripts/demo.sh --dry-run       print the commands only
#   bash scripts/demo.sh --status        the background runs started by scene 0
#   bash scripts/demo.sh --stop          stop those background runs
#
# Options:
#   --workdir DIR   every file the demo writes goes here (default: ${TMPDIR:-/tmp}/wta-demo); outside the repo
#   --feed synthetic|live  the dashboard run's market data: synthetic (default, offline) or live = the REAL
#                   Wallex feed (needs api.wallex.ir, e.g. VPN off). Still shadow mode: paper fills, never live
#                   trading. The kill-switch drill always uses the synthetic feed (it needs the synthetic shock).
#   --ai on|off|offline  on (default): the analyst as configured. That is the real LLM when an OpenRouter (or Gemini)
#                   key is set, else the offline rules provider (labelled "not an LLM").
#                   off: rules only (--set ai.enabled=false). offline: the labelled offline rules provider even
#                   with keys (a network that reaches Wallex but not OpenRouter). The drill always runs with AI off.
#   --no-open       do not open the report PNGs in an image viewer (their paths are printed)
#   --fresh         delete the work dir before starting
#
# Safety:
#   * Never live. WTA_LIVE=0 is exported, and any step whose command mentions live trading is refused.
#   * Offline. Every engine run uses --feed synthetic (labelled SYNTHETIC on every screen). No API key is needed.
#     The analyst calls OpenRouter only if a key is configured; use --ai off to make no network call at all.
#   * The repo is only read. Runs write their logs, state DBs, LLM cache and reviewer overlays into the work
#     dir, never into logs/, data/, configs/runtime/ or docs/. The replay writes no report into docs/REPORTS.
#     (Reading a state DB in WAL mode touches only SQLite's -shm index file next to it.)
#   * Telegram alerts are switched off for every run (--set alerts.telegram_enabled=false).
#
# Exit code: 0, unless the arguments are wrong (2). A failed step prints its exit code; the demo goes on.

set -u -o pipefail

ORIG_PWD=$PWD
ROOT=$(cd "$(dirname "$0")/.." && pwd)
cd "$ROOT" || exit 2

export WTA_LIVE=0
PY="$ROOT/.venv/bin/python"
[ -x "$PY" ] || PY=$(command -v python3 || echo python3)

# The recorded 4-h shadow run (synthetic feed, real LLM). logs/ and data/ are not in git; on a fresh clone the
# run's copy in deliverables/shadow_synth_4h (decision log, events, heartbeats, LLM cache; no state DB) is used.
RUN_LOGS=${DEMO_RUN_LOGS:-logs/shadow_synth_4h}
if [ -z "${DEMO_RUN_LOGS:-}" ] && [ ! -d "$RUN_LOGS" ] && [ -d deliverables/shadow_synth_4h ]; then
    RUN_LOGS=deliverables/shadow_synth_4h
fi
RUN_STATE_TEMPLATE='data/state_shadow4h_{mode}.sqlite'
[ -n "${DEMO_RUN_STATE_TEMPLATE:-}" ] && RUN_STATE_TEMPLATE=$DEMO_RUN_STATE_TEMPLATE
RUN_STATE=${RUN_STATE_TEMPLATE//\{mode\}/shadow_synthetic}
RUN_SUMMARY=docs/REPORTS/shadow_20261023_synth4h/summary.json
RUN_REPORT_DIR=docs/REPORTS/shadow_20261023_synth4h
BUNDLE=${DEMO_REPLAY_BUNDLE:-deliverables/replay/shadow_synth_4h}

W=${TMPDIR:-/tmp}
W="${W%/}/wta-demo"
AI=on
FEED=${DEMO_FEED:-synthetic}  # the dashboard run's market data; the kill-switch drill is always synthetic
YES=0
DRY_RUN=0
OPEN=1
FRESH=0
ACTION=run
SCENES=()
DASH_SECONDS=${DEMO_DASH_SECONDS:-1500}
SHOCK_AT=${DEMO_SHOCK_AT:-60}
DRILL_SECONDS=${DEMO_DRILL_SECONDS:-200}

usage() { awk 'NR > 1 && /^#/ { sub(/^# ?/, ""); print; next } NR > 1 { exit }' "$0"; }

while [ $# -gt 0 ]; do
    case $1 in
        --list) ACTION=list ;;
        --status) ACTION=status ;;
        --stop) ACTION=stop ;;
        --scene)
            [ $# -ge 2 ] || { echo "demo.sh: --scene needs a number (0-7)" >&2; exit 2; }
            case $2 in [0-7]) SCENES+=("$2") ;; *) echo "demo.sh: unknown scene '$2' (0-7)" >&2; exit 2 ;; esac
            shift ;;
        --scene=*)
            v=${1#--scene=}
            case $v in [0-7]) SCENES+=("$v") ;; *) echo "demo.sh: unknown scene '$v' (0-7)" >&2; exit 2 ;; esac ;;
        --yes|-y) YES=1 ;;
        --dry-run|-n) DRY_RUN=1 ;;
        --no-open) OPEN=0 ;;
        --fresh) FRESH=1 ;;
        --ai)
            [ $# -ge 2 ] || { echo "demo.sh: --ai needs on, off or offline" >&2; exit 2; }
            case $2 in on|off|offline) AI=$2 ;; *) echo "demo.sh: --ai on|off|offline" >&2; exit 2 ;; esac
            shift ;;
        --feed)
            [ $# -ge 2 ] || { echo "demo.sh: --feed needs synthetic or live" >&2; exit 2; }
            case $2 in synthetic|live) FEED=$2 ;; *) echo "demo.sh: --feed synthetic|live" >&2; exit 2 ;; esac
            shift ;;
        --workdir)
            [ $# -ge 2 ] || { echo "demo.sh: --workdir needs a directory" >&2; exit 2; }
            W=${2%/}; shift ;;
        -h|--help) usage; exit 0 ;;
        *) echo "demo.sh: unknown argument '$1' (see --help)" >&2; exit 2 ;;
    esac
    shift
done

case $W in /*) ;; *) W="$ORIG_PWD/$W" ;; esac
case "$W/" in
    "$ROOT"/*)
        echo "demo.sh: --workdir must be outside the repo (the demo never writes into it): $W" >&2
        exit 2 ;;
esac
export DEMO="$W"

# ------------------------------------------------------------------ terminal

if [ -t 1 ] && [ -z "${NO_COLOR:-}" ]; then
    C_SCENE=$'\033[1;35m'; C_STEP=$'\033[1;36m'; C_CMD=$'\033[1;32m'; C_NOTE=$'\033[2m'
    C_WARN=$'\033[1;33m'; C_ERR=$'\033[1;31m'; C_OFF=$'\033[0m'
else
    C_SCENE=""; C_STEP=""; C_CMD=""; C_NOTE=""; C_WARN=""; C_ERR=""; C_OFF=""
fi
if [ -n "${DEMO_WIDTH:-}" ]; then
    COLS=$DEMO_WIDTH
elif [ -t 1 ]; then
    COLS=$(tput cols 2>/dev/null || echo 200)
else
    COLS=200
fi
export DEMO_COLS=$COLS

if [ "$ACTION" = run ] && [ "$YES" = 0 ] && [ "$DRY_RUN" = 0 ]; then
    if ! { : < /dev/tty; } 2>/dev/null; then
        echo "demo.sh: no terminal to read Enter from; continuing as with --yes" >&2
        YES=1
    fi
fi

note() { printf '%s  %s%s\n' "$C_NOTE" "$*" "$C_OFF"; }
warn() { printf '%s  %s%s\n' "$C_WARN" "$*" "$C_OFF"; }

# ------------------------------------------------------------------ showing and running commands

# One word as it should be typed. Words with the work dir use double quotes so that $DEMO expands.
quote_word() {
    local w=$1 d
    case $w in
        *@@DEMO@@*)
            d=$(printf '%s' "$w" | sed -e 's/[\\"`$]/\\&/g' -e 's/@@DEMO@@/$DEMO/g')
            case $d in
                *[!A-Za-z0-9_./:=,@%+\$-]*) printf '"%s"' "$d" ;;
                *) printf '%s' "$d" ;;
            esac ;;
        '') printf "''" ;;
        *[!A-Za-z0-9_./:=,@%+-]*) printf "'%s'" "$(printf '%s' "$w" | sed "s/'/'\\\\''/g")" ;;
        *) printf '%s' "$w" ;;
    esac
}

show_cmd() {
    local out="" w
    for w in "$@"; do
        [ "$w" = "$PY" ] && w=python
        w=${w//"$W"/@@DEMO@@}
        w=${w//"$ROOT/"/}
        out="$out $(quote_word "$w")"
    done
    printf '%s' "${out# }"
}

is_live_cmd() {
    local prev="" w
    for w in "$@"; do
        case $w in
            *--i-understand-live*|*flatten-live*|*--mode=live*|*WTA_LIVE=1*|*"--mode live"*|*"make live"*) return 0 ;;
        esac
        case "$prev $w" in "--mode live"|"make live") return 0 ;; esac
        prev=$w
    done
    return 1
}

scene_title() {   # scene_title <n> <title> <timing>
    printf '\n%s━━━ Scene %s · %s  (%s)%s\n' "$C_SCENE" "$1" "$2" "$3" "$C_OFF"
}

# step <id> <title> <shown command, or - to show the command itself> <command...>
step() {
    local id=$1 title=$2 shown=$3 ans rc
    shift 3
    [ "$shown" = "-" ] && shown=$(show_cmd "$@")
    printf '\n%s[%s] %s%s\n' "$C_STEP" "$id" "$title" "$C_OFF"
    printf '%s$ %s%s\n' "$C_CMD" "$shown" "$C_OFF"
    if is_live_cmd "$shown" "$@"; then
        printf '%s  refused: this demo never runs live mode%s\n' "$C_ERR" "$C_OFF"
        return 1
    fi
    [ "$DRY_RUN" = 1 ] && return 0
    if [ "$YES" = 0 ]; then
        ans=""
        rc=0
        read -r -p "  [Enter] run   s skip   q quit > " ans < /dev/tty || rc=$?
        if [ "$rc" -gt 128 ]; then echo; exit 130; fi
        case $ans in
            s|S) note "skipped"; return 0 ;;
            q|Q) exit 0 ;;
        esac
    fi
    "$@"
    rc=$?
    [ "$rc" = 0 ] || warn "(exit $rc)"
    return 0
}

# Ctrl-C ends the foreground program (the dashboard) or a wait and returns to the demo; at a prompt it quits.
INTERRUPTED=0
trap 'INTERRUPTED=1; printf "\n"' INT

# ------------------------------------------------------------------ background runs (scene 0)

JOBS="dash drill replay"

job_dir() { printf '%s/jobs' "$W"; }
job_pid() { cat "$(job_dir)/$1.pid" 2>/dev/null; }
job_running() {
    local pid
    pid=$(job_pid "$1") || return 1
    [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null && [ ! -f "$(job_dir)/$1.exit" ]
}
job_state() {
    local d
    d=$(job_dir)
    if [ -f "$d/$1.exit" ]; then
        printf 'finished, exit %s' "$(cat "$d/$1.exit")"
    elif job_running "$1"; then
        printf 'running (pid %s, since %s)' "$(job_pid "$1")" "$(cat "$d/$1.started" 2>/dev/null)"
    elif [ -f "$d/$1.pid" ]; then
        printf 'stopped'
    else
        printf 'not started'
    fi
}

# job_start <name> <shown command> <command...>: runs in the background in its own process group, so that
# Ctrl-C in the terminal (leaving the dashboard) does not reach it, and closing the terminal does not stop it.
job_start() {
    local name=$1 shown=$2 d
    shift 2
    d=$(job_dir)
    mkdir -p "$d"
    if job_running "$name"; then
        note "$name is already running (pid $(job_pid "$name")); output: \$DEMO/jobs/$name.out"
        return 0
    fi
    rm -f "$d/$name.exit"
    printf '%s\n' "$shown" > "$d/$name.cmd"
    date -u +%H:%M:%SZ > "$d/$name.started"
    set -m
    (
        trap '' HUP
        "$@" > "$d/$name.out" 2>&1 < /dev/null
        echo $? > "$d/$name.exit"
    ) > /dev/null 2>&1 < /dev/null &
    echo $! > "$d/$name.pid"
    set +m
    note "started $name in the background (pid $!); output: \$DEMO/jobs/$name.out"
}

fresh_dir() {   # fresh_dir <name>: a new run starts from an empty dir; the previous one is kept as <name>.prev
    [ -d "$W/$1" ] || return 0
    rm -rf "$W/$1.prev"
    mv "$W/$1" "$W/$1.prev"
    note "kept the previous $1 run as \$DEMO/$1.prev"
}

job_wait() {   # job_wait <name> <timeout s>
    local name=$1 limit=$2 waited=0
    job_running "$name" || return 0
    printf '  waiting for %s (up to %s s; Ctrl-C stops waiting) ' "$name" "$limit"
    INTERRUPTED=0
    while job_running "$name" && [ "$waited" -lt "$limit" ] && [ "$INTERRUPTED" = 0 ]; do
        sleep 5
        waited=$((waited + 5))
        printf '.'
    done
    printf ' %s\n' "$(job_state "$name")"
}

job_stop() {
    local pid
    pid=$(job_pid "$1") || return 0
    if job_running "$1"; then
        kill -TERM -- "-$pid" 2>/dev/null || { pkill -TERM -P "$pid" 2>/dev/null; kill -TERM "$pid" 2>/dev/null; }
        wait "$pid" 2>/dev/null   # a job of this shell: no "Terminated" message
        # the engine finishes its tick and writes its stop event; wait for it (up to 20 s) so that a new run
        # on the same state DB does not hit the one-engine-per-account lock
        local i=0
        while kill -0 -- "-$pid" 2>/dev/null && [ "$i" -lt 40 ]; do sleep 0.5; i=$((i + 1)); done
        note "stopped $1 (process group $pid)"
    fi
}

print_status() {
    local j
    printf 'work dir %s\n' "$W"
    for j in $JOBS; do printf '  %-7s %s\n' "$j" "$(job_state "$j")"; done
}

# ------------------------------------------------------------------ the runs

# The dashboard run: shadow, synthetic feed, S1+S2, analyst every 60 s (production: 300 s).
dash_cmd() {
    local cmd=("$PY" -m wta run --mode shadow --feed "$FEED" --seconds "$DASH_SECONDS"
        --log-dir "$W/dash/logs"
        --set "data.sqlite_path=$W/dash/market.sqlite"
        --set "data.state_sqlite_path=$W/dash/state_{mode}.sqlite"
        --set "ai.cache_dir=$W/dash/llm_cache"
        --set engine.slow_loop_interval_s=60 --set engine.heartbeat_interval_s=20
        --set alerts.telegram_enabled=false
        --runtime-dir "$W/dash/runtime")
    [ "$AI" = off ] && cmd+=(--set ai.enabled=false)
    [ "$AI" = offline ] && cmd+=(--ai offline)
    "${cmd[@]}"
}
dash_shown() {
    local extra=""
    [ "$AI" = off ] && extra=" --set ai.enabled=false"
    [ "$AI" = offline ] && extra=" --ai offline"
    printf '%s%s' "$(show_cmd "$PY" -m wta run --mode shadow --feed "$FEED" --seconds "$DASH_SECONDS" \
        --log-dir "$W/dash/logs" --set "data.sqlite_path=$W/dash/market.sqlite" \
        --set "data.state_sqlite_path=$W/dash/state_{mode}.sqlite" --set "ai.cache_dir=$W/dash/llm_cache" \
        --set engine.slow_loop_interval_s=60 --set engine.heartbeat_interval_s=20 \
        --set alerts.telegram_enabled=false --runtime-dir "$W/dash/runtime")" "$extra"
}
DASH_VIEW=(--log-dir "$W/dash/logs" --mode shadow --feed "$FEED"
    --set "data.sqlite_path=$W/dash/market.sqlite" --set "data.state_sqlite_path=$W/dash/state_{mode}.sqlite")

# The kill-switch drill: strategies and AI off, one injected BTCUSDT entry at 5 s, a -8 % synthetic shock
# at SHOCK_AT s, an injected USDT buy at 120 s that the RiskManager must veto while killed.
DRILL_ARGS=(-m wta run --mode shadow --feed synthetic --seconds "$DRILL_SECONDS"
    --inject-intents "$W/drill/inject.jsonl"
    --paper-balances '{"TMN": 2000000, "USDT": 12}'
    --synthetic-shock-at "$SHOCK_AT" --synthetic-shock-pct -0.08
    --log-dir "$W/drill/logs"
    --set "data.sqlite_path=$W/drill/market.sqlite"
    --set "data.state_sqlite_path=$W/drill/state_{mode}.sqlite"
    --set ai.enabled=false --set strategies.s1_crossbook.enabled=false --set strategies.s2_usdtalloc.enabled=false
    --set alerts.telegram_enabled=false
    --runtime-dir "$W/drill/runtime")
DRILL_VIEW=(--log-dir "$W/drill/logs" --mode shadow --feed synthetic
    --set "data.sqlite_path=$W/drill/market.sqlite" --set "data.state_sqlite_path=$W/drill/state_{mode}.sqlite")

drill_prepare() {
    mkdir -p "$W/drill"
    cat > "$W/drill/inject.jsonl" <<'JSONL'
# kill-switch drill (scripts/demo.sh): one entry before the synthetic shock, one entry-like order after the kill
{"at_s": 5, "strategy": "S3", "symbol": "BTCUSDT", "side": "BUY", "order_type": "LIMIT", "qty": "0.00004", "stop_price": "81600", "purpose": "entry", "taker": true, "expected_edge_pct": "0.008", "reason": "demo drill: injected BTCUSDT entry before the synthetic shock"}
{"at_s": 120, "strategy": "S2", "symbol": "USDTTMN", "side": "BUY", "order_type": "LIMIT", "qty": "2", "purpose": "rebalance", "taker": true, "reason": "demo drill: injected USDT buy after the kill; the RiskManager must veto it"}
JSONL
}
drill_was_reset() { grep -qs '"kind":"reset_kill"' "$W"/drill/logs/events*.jsonl; }

# The replay: the committed bundle when `make replay-deliverable` exists, else the recorded run on this machine.
replay_mode() {
    if [ -f "$BUNDLE/SHA256SUMS" ] && make -n replay-deliverable > /dev/null 2>&1; then
        echo bundle
    elif [ -d "$RUN_LOGS" ] && [ -f "$RUN_STATE" ]; then
        echo local
    else
        echo none
    fi
}
replay_cmd() {
    case $(replay_mode) in
        bundle) make replay-deliverable ;;
        local) rm -rf "$W/replay/out"; mkdir -p "$W/replay"
               "$PY" scripts/replay_check.py --log-dir "$RUN_LOGS" --state-db "$RUN_STATE" \
                   --out "$W/replay/out" --reports-dir '' --no-index ;;
        *) echo "no replay input on this machine: no $BUNDLE (make replay-deliverable) and no $RUN_LOGS + $RUN_STATE"
           echo "the committed proof is docs/REPORTS/replay_20261023.md:"
           sed -n '1,3p' docs/REPORTS/replay_20261023.md
           return 2 ;;
    esac
}
replay_shown() {
    case $(replay_mode) in
        bundle) printf 'make replay-deliverable' ;;
        local) show_cmd "$PY" scripts/replay_check.py --log-dir "$RUN_LOGS" --state-db "$RUN_STATE" \
                   --out "$W/replay/out" --reports-dir '' --no-index ;;
        *) printf '(no replay input on this machine: neither %s nor %s)' "$BUNDLE" "$RUN_STATE" ;;
    esac
}

# ------------------------------------------------------------------ helper (python, written into the work dir)

HELPER="$W/demo_helper.py"
H() { "$PY" "$HELPER" "$@"; }

write_helper() {
    mkdir -p "$W"
    cat > "$HELPER" <<'PYHELPER'
"""Read-only helpers for scripts/demo.sh: field walks, tables and slides on a terminal of DEMO_COLS columns."""
import json
import os
import sys
from pathlib import Path

try:
    from rich.console import Console
    from rich.markdown import Markdown
    from rich.panel import Panel
    from rich.table import Table
    from rich.text import Text
except ImportError:  # plain output without rich
    Console = None

COLS = int(os.environ.get("DEMO_COLS") or 200)
CON = Console(width=COLS, highlight=False) if Console else None


def out(obj=""):
    if CON:
        CON.print(obj)
    else:
        print(obj)


def cut(s, n):
    s = " ".join(str(s).split())
    return s if len(s) <= n else s[: max(n - 1, 0)] + "…"


def short(path):
    """A path as the demo shows it: the work dir as $DEMO."""
    demo = os.environ.get("DEMO", "")
    text = str(path)
    return "$DEMO" + text[len(demo):] if demo and text.startswith(demo) else text


def decision_files(path):
    p = Path(path)
    if p.is_file():
        return [p]
    files = sorted(p.glob("decisions-*.jsonl"))
    if not files and (p / "decisions.jsonl").is_file():
        files = [p / "decisions.jsonl"]
    return files


def records(path):
    for f in decision_files(path):
        with f.open() as fh:
            for n, line in enumerate(fh, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    yield f, n, json.loads(line)
                except json.JSONDecodeError:
                    continue


def failed_codes(r):
    return [c.get("code") for c in ((r.get("risk") or {}).get("checks") or []) if not c.get("passed")]


def select(path, sel):
    if sel.startswith("line:"):
        want = int(sel.split(":", 1)[1])
        for f, n, r in records(path):
            if n == want:
                return f, n, r
        return None
    if sel == "plan":
        last = None
        for f, n, r in records(path):
            if r.get("kind") == "plan":
                last = (f, n, r)
        return last
    for f, n, r in records(path):
        kind = r.get("kind")
        if sel == "first-order" and kind == "order" and r.get("strategy") != "AI":
            return f, n, r
        if sel.startswith("veto") and kind == "veto":
            code = sel.split(":", 1)[1] if ":" in sel else None
            if code is None or code in failed_codes(r):
                return f, n, r
    return None


def grid():
    t = Table.grid(padding=(0, 2))
    t.add_column(style="bold cyan", no_wrap=True)
    t.add_column(overflow="fold")
    return t


def cmd_decision(path, sel):
    found = select(path, sel)
    if not found:
        print(f"no decision record matching '{sel}' in {short(path)}")
        return 1
    f, n, r = found
    outcome = None
    for f2, n2, r2 in records(path):
        if r2.get("kind") == "outcome" and r2.get("decision_id") == r.get("decision_id"):
            outcome = (f2, n2, r2)
    if not CON:
        print(json.dumps(r, indent=1))
        return 0
    w = COLS - 22
    t = grid()

    def add(k, v, style=None):
        t.add_row(k, Text(str(v), style=style or ""))

    add("record", f"{short(f)} line {n}")
    add("decision_id", r.get("decision_id"))
    add("ts · mode", f"{r.get('ts')}   mode {r.get('mode')}")
    add("provenance", f"git_commit {str(r.get('git_commit'))[:12]}   config_hash {r.get('config_hash')}")
    add("kind", f"{r.get('kind')}   strategy {r.get('strategy')}   symbol {r.get('symbol')}",
        "bold red" if r.get("kind") == "veto" else None)
    feats = r.get("features") or {}
    if feats:
        syms = sorted((feats.get("symbols") or {}).keys())
        add("features", f"the inputs at this tick: {len(syms)} symbol snapshot(s) {', '.join(syms[:6])}"
                        f"{' …' if len(syms) > 6 else ''}; keys {', '.join(sorted(feats))[:w]}")
    it = r.get("intent") or {}
    if it:
        stop = f"   stop {it.get('stop_price')}" if it.get("stop_price") else ""
        price = it.get("price") if it.get("price") is not None else "the book (priced at placement)"
        add("intent", f"{it.get('side')} {it.get('qty')} {it.get('symbol')} {it.get('order_type')} @ {price}"
                      f"   purpose {it.get('purpose')}   taker {it.get('taker')}{stop}")
        add("intent.reason", it.get("reason"))
        f2 = it.get("features") or {}
        keys = [k for k in ("equity_tmn", "usdt_share", "target_share", "gap_pp", "regime", "regime_source",
                            "plan_fresh", "initial_allocation", "edge_pct", "cost_pct", "injected") if k in f2]
        if keys:
            add("intent.features", "   ".join(f"{k} {f2[k]}" for k in keys) + f"   (+{len(f2) - len(keys)} more)")
    ps = r.get("plan_snapshot")
    if ps:
        add("plan_snapshot", f"model {ps.get('model')}   prompt_hash {str(ps.get('prompt_hash'))[:12]}   enabled "
                             f"{ps.get('enabled_strategies')}   size x{ps.get('size_multiplier')}   USDT target "
                             f"{ps.get('usdt_target_share')}   S1 margin {ps.get('s1_margin_min_pct')}")
    else:
        add("plan_snapshot", "none: no fresh AI plan was in force at this tick, so the rules ran alone")
    rk = r.get("risk") or {}
    if rk:
        checks = rk.get("checks") or []
        bad = [c for c in checks if not c.get("passed")]
        add("risk", f"approved {rk.get('approved')}   adjusted_qty {rk.get('adjusted_qty')}   "
                    f"{len(checks)} checks, {len(bad)} failed", "bold red" if bad else "bold green")
        if rk.get("veto_reason"):
            add("risk.veto_reason", rk.get("veto_reason"), "red")
        for c in checks:
            ok = c.get("passed")
            mark = "✓" if ok else "✗"
            t.add_row("", Text(f"{mark} {c.get('code', ''):<22} " + cut(c.get("detail", ""), w - 25),
                               style="" if ok else "bold red"))
    od = r.get("order")
    if od:
        rq, rs = od.get("request") or {}, od.get("response") or {}
        add("order", f"client_id {rq.get('client_id')}   {rq.get('type')} {rq.get('side')} {rq.get('qty')} @ "
                     f"{rq.get('price')}   accepted {od.get('accepted')}   status {rs.get('status')}")
    elif r.get("kind") == "veto":
        add("order", "none: a veto sends nothing to the exchange")
    if outcome:
        f2, n2, o = outcome
        oc = o.get("outcome") or {}
        bits = [f"{k} {oc[k]}" for k in ("status", "done_reason", "filled_qty", "avg_price", "fee_paid", "fee_asset",
                                         "reprices", "exit_reason", "pnl_tmn", "pnl_pct", "hold_s") if oc.get(k) is not None]
        add("outcome", f"{f2.name} line {n2}, joined by decision_id: " + "   ".join(bits))
    elif r.get("kind") != "veto":
        add("outcome", "not written yet (the order is still working)")
    if r.get("notes"):
        add("notes", r.get("notes"))
    out(t)
    return 0


def cmd_plan(path):
    found = select(path, "plan")
    if not found:
        print(f"no plan record in {path}")
        return 1
    f, n, r = found
    ps = r.get("plan_snapshot") or {}
    if not CON:
        print(json.dumps(ps, indent=1))
        return 0
    t = grid()
    t.add_row("record", Text(f"{short(f)} line {n}   {r.get('ts')}"))
    t.add_row("model", Text(f"{ps.get('model')}   prompt_hash {str(ps.get('prompt_hash'))[:16]}   "
                            f"latency {ps.get('latency_ms')} ms   tools {ps.get('tools_used')} via {ps.get('tools_source')}"))
    t.add_row("envelope", Text(f"enabled {ps.get('enabled_strategies')}   size x{ps.get('size_multiplier')}   "
                               f"USDT target {ps.get('usdt_target_share')}   S1 margin >= {ps.get('s1_margin_min_pct')}   "
                               f"clamped {ps.get('clamped') or 'nothing'}"))
    t.add_row("risk_flags", Text(", ".join(ps.get("risk_flags") or []) or "none"))
    for sym, g in sorted((ps.get("regimes") or {}).items()):
        t.add_row(f"regime {sym}", Text(cut(f"{g.get('regime')} ({g.get('confidence')}): {g.get('evidence')}", COLS - 24)))
    t.add_row("notes", Text(str(ps.get("notes")), style="bold"))
    out(t)
    return 0


def cmd_events(path, *kinds):
    p = Path(path)
    files = sorted(p.glob("events-*.jsonl")) or ([p / "events.jsonl"] if (p / "events.jsonl").is_file() else [])
    rows = []
    for f in files:
        for line in f.read_text().splitlines():
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not kinds or e.get("kind") in kinds:
                rows.append(e)
    if not rows:
        print(f"no events in {short(path)}")
        return 1
    for e in rows:
        line = f"{str(e.get('ts'))[11:23]}  {e.get('kind'):<20} " + cut(e.get("reason"), COLS - 40)
        style = "bold red" if e.get("kind") in ("kill", "flatten") else ("bold green" if "reset" in str(e.get("kind")) else "")
        out(Text(line, style=style) if CON else line)
    return 0


def section(path, prefix):
    lines = Path(path).read_text().splitlines()
    fence, start, level = False, None, 0
    for i, line in enumerate(lines):
        if line.startswith("```"):
            fence = not fence
        if fence or not line.startswith("#"):
            continue
        lv = len(line) - len(line.lstrip("#"))
        if start is None and line.lstrip("#").strip().startswith(prefix):
            start, level = i, lv
        elif start is not None and lv <= level:
            return lines[start:i]
    return lines[start:] if start is not None else []


def cmd_md(path, prefix, mode="all", max_lines="0"):
    sel = section(path, prefix)
    if not sel:
        print(f"section '{prefix}' not found in {path}")
        return 1
    if mode == "tables":
        kept, prev_table = [sel[0]], False
        for x in sel[1:]:
            is_table = x.startswith("|")
            if is_table or x.startswith("**"):
                if not (is_table and prev_table):
                    kept.append("")
                kept.append(x)
                prev_table = is_table
        sel = kept
    if int(max_lines) > 0:
        sel = sel[: int(max_lines)]
    text = "\n".join(sel)
    out(Markdown(text) if CON else text)
    out(Text(f"source: {path}", style="dim") if CON else f"source: {path}")
    return 0


def cmd_diagram(*paths):
    for p in paths:
        if not Path(p).is_file():
            continue
        block, fence = [], False
        for line in Path(p).read_text().splitlines():
            if line.startswith("```"):
                if fence and any("SLOW LOOP" in x for x in block):
                    out(Text("\n".join(block)) if CON else "\n".join(block))
                    out(Text(f"source: {p}", style="dim") if CON else f"source: {p}")
                    return 0
                fence, block = not fence, []
                continue
            if fence:
                block.append(line)
    print("architecture diagram not found")
    return 1


SLIDES = {
    "problem": (
        "The problem: Wallex algorithmic trading challenge",
        [
            ("Capital", "5,000,000 TMN on a challenge sub-account (about 18.7 USDT)"),
            ("Time", "3 days (72 h), no manual intervention"),
            ("Score", "net account value in TMN at the official end; coins left over do not count; "
                      "a negative return scores 0 on the 90-point profit item"),
            ("Fees", "TMN pairs 0.25 % maker / 0.30 % taker; USDT pairs 0.10 % / 0.13 %. "
                     "Round trip: TMN pair 0.50-0.60 %, USDT pair 0.20-0.26 %"),
            ("Rules", "no manual trades, no self-trading or wash trading, no banned markets"),
            ("", ""),
            ("Thesis", "don't lose, trade regularly, be auditable"),
            ("S1 CrossBook", "a coin trades against TMN and against USDT; when one book lags by more than "
                             "both taker fees + 0.15 %, buy the cheap book, sell the dear one"),
            ("S2 USDTAlloc", "hold 25-70 % of equity in USDT by dollar regime: the dollar bet, made explicit"),
            ("S3 Momentum", "off by default (no evidence yet)"),
            ("FLATTEN", "everything back to TMN from T-2h; market orders for leftovers at T-30min"),
            ("Layers", "AI plans (5 min) · rules trade (2 s) · risk guards (23 checks, kill-switch, flatten)"),
        ],
    ),
    "wrap": (
        "Wrap-up",
        [
            ("Built", "one engine for backtest, shadow, live and replay; 23 risk checks on every intent; "
                      "an LLM that plans but never trades"),
            ("Proven", "risk rules fire as specified; plans are bounded, logged and cached; "
                       "the 4-h shadow run replays 40/40 decisions with 0 network calls"),
            ("Not proven", "that the strategies make money: the real Wallex data is ~20 min of order books; "
                           "longer runs used the SYNTHETIC feed"),
            ("Live", "not started yet; only the operator starts it, from a live-YYYYMMDD-HHMM tag"),
            ("Read", "README -> docs/STRATEGY.md -> docs/RISK.md -> deliverables/REPORT.md"),
        ],
    ),
}


def cmd_slide(name):
    title, rows = SLIDES[name]
    if not CON:
        print(title)
        for k, v in rows:
            print(f"  {k:<14} {v}")
        return 0
    t = grid()
    for k, v in rows:
        t.add_row(k, Text(v, style="bold" if k in ("Thesis", "Proven", "Not proven") else ""))
    out(Panel(t, title=title, width=min(COLS, 150), padding=(1, 2)))
    return 0


def cmd_provenance(path):
    rows = {(r.get("git_commit"), r.get("config_hash"), r.get("mode")) for _, _, r in records(path)
            if r.get("kind") != "outcome"}
    for c, h, m in sorted(rows, key=str):
        print(f"git_commit {c}   config_hash {h}   mode {m}")
    return 0 if rows else 1


def cmd_commit(summary):
    print(json.loads(Path(summary).read_text())["meta"]["git_commit"])
    return 0


def cmd_final(summary):
    d = json.loads(Path(summary).read_text())
    m, meta = d["metrics"], d["meta"]
    ret = (m["final_equity_tmn"] / m["initial_equity_tmn"] - 1) * 100
    lines = [
        f"4-h shadow run ({'SYNTHETIC feed' if d.get('synthetic') else meta.get('feed')}, real LLM): "
        f"{m['initial_equity_tmn']:,.0f} -> {m['final_equity_tmn']:,.0f} TMN ({ret:+.2f} %), "
        f"max drawdown {m['max_drawdown_pct'] * 100:.2f} %, {m['n_trades']} trades, {m['n_orders']} orders, "
        f"fees {m['fees_paid_tmn']:,.0f} TMN",
        "  a shadow run has no deadline, so it ends holding the S2 USDT share; the deadline flatten runs only "
        "when CHALLENGE_END_UTC is set",
        "live: no live run yet, so there is no final TMN balance to show. It comes from the operator's live run "
        "(final_state event, docs/REPORTS live report, deliverables/TAGS.md).",
    ]
    for x in lines:
        print(x)
    return 0


def main(argv):
    cmds = {"decision": cmd_decision, "plan": cmd_plan, "events": cmd_events, "md": cmd_md,
            "diagram": cmd_diagram, "slide": cmd_slide, "provenance": cmd_provenance, "commit": cmd_commit,
            "final": cmd_final}
    if not argv or argv[0] not in cmds:
        print(f"usage: demo_helper.py {{{','.join(cmds)}}} ...")
        return 2
    return cmds[argv[0]](*argv[1:])


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
PYHELPER
}

# ------------------------------------------------------------------ small actions used by the steps

open_images() {
    local f
    for f in "$@"; do
        if [ ! -f "$f" ]; then warn "missing: $f"; continue; fi
        if [ "$OPEN" = 1 ] && command -v open > /dev/null 2>&1 && [ "$(uname -s)" = Darwin ]; then
            open "$f"
        elif [ "$OPEN" = 1 ] && command -v xdg-open > /dev/null 2>&1; then
            xdg-open "$f" > /dev/null 2>&1 &
        fi
        printf '  image: %s\n' "$f"
    done
}

# "set" or "not-set", as the engine sees it: the environment first (even when empty), then .env. Never prints a value.
key_state() {
    local k=$1 isset="" val=""
    eval "isset=\${$k+x}"
    if [ -n "$isset" ]; then
        eval "val=\${$k}"
        [ -n "$val" ] && echo set || echo not-set
    elif grep -Eqs "^$k=[\"']?[^[:space:]#\"']" .env; then
        echo set
    else
        echo not-set
    fi
}

preflight() {
    local keys="" k
    printf '  repo         %s\n' "$ROOT"
    printf '  python       %s (%s)\n' "$("$PY" --version 2>&1)" "$PY"
    printf '  commit       %s   %s   tree %s\n' "$(git rev-parse --short HEAD 2>/dev/null)" \
        "$(git describe --tags --always 2>/dev/null)" \
        "$([ -z "$(git status --porcelain 2>/dev/null)" ] && echo clean || echo dirty)"
    for k in OPENROUTER_API_KEY GEMINI_API_KEY WALLEX_API_KEY; do
        keys="$keys $k:$(key_state "$k")"
    done
    printf '  keys        %s   (values are never printed; Wallex is not used: synthetic feed)\n' "$keys"
    if [ "$AI" = off ]; then
        printf '  AI           off: rules only\n'
    elif [[ $keys == *OPENROUTER_API_KEY:set* ]]; then
        printf '  AI           the real LLM through OpenRouter (model from OPENROUTER_MODEL_FAST; Gemini as fallback)\n'
    elif [[ $keys == *GEMINI_API_KEY:set* ]]; then
        printf '  AI           the real LLM through Gemini only; its key may be quota-limited (HTTP 429), then the rules run alone\n'
    else
        printf '  AI           no key: the offline rules provider, labelled "not an LLM" in every plan\n'
    fi
    printf '  4-h run      logs %s: %s   state DB %s: %s\n' "$RUN_LOGS" "$([ -d "$RUN_LOGS" ] && echo present || echo absent)" \
        "$RUN_STATE" "$([ -f "$RUN_STATE" ] && echo present || echo absent)"
    printf '  replay       %s\n' "$(replay_mode) ($(replay_shown))"
    printf '  terminal     %s columns (the dashboard wants >= 200; set DEMO_WIDTH to force)\n' "$COLS"
    printf '  work dir     export DEMO=%s\n' "$W"
    printf '  live mode    never (WTA_LIVE=0 for every command)\n'
    if [ "$COLS" -lt 200 ]; then warn "terminal is narrower than 200 columns: enlarge it or lower the font size"; fi
}

tui_dash() {
    if ! job_running dash && [ ! -d "$W/dash/logs" ]; then
        warn "the dashboard run is not there: run scene 0 first (bash scripts/demo.sh --scene 0)"
        return 1
    fi
    job_running dash || warn "the dashboard run has ended ($(job_state dash)); the screen shows its last state"
    if [ "$YES" = 1 ]; then
        "$PY" -m wta tui "${DASH_VIEW[@]}" --width "$COLS" --once
    else
        "$PY" -m wta tui "${DASH_VIEW[@]}" --width "$COLS"
    fi
}

tui_recorded() {
    if [ ! -d "$RUN_LOGS" ]; then
        warn "$RUN_LOGS is not on this machine (logs/ is not in git); showing the dashboard run instead"
        "$PY" -m wta tui "${DASH_VIEW[@]}" --width "$COLS" --once
        return
    fi
    "$PY" -m wta tui --log-dir "$RUN_LOGS" --set "data.state_sqlite_path=$RUN_STATE_TEMPLATE" --width "$COLS" --once
}

plan_recorded() {
    if [ -d "$RUN_LOGS" ]; then H plan "$RUN_LOGS"; else H plan "$W/dash/logs"; fi
}

walk_recorded() {
    if [ -d "$RUN_LOGS" ]; then H decision "$RUN_LOGS" first-order; else H decision "$W/dash/logs" first-order; fi
}

drill_run() {
    if drill_was_reset && ! job_running drill; then
        note "the last drill was already reset: running a fresh one ($DRILL_SECONDS s)"
        fresh_dir drill
        rm -f "$(job_dir)"/drill.*
    fi
    if job_running drill; then
        job_wait drill $((DRILL_SECONDS + 120))
    elif [ ! -f "$(job_dir)/drill.exit" ]; then
        note "the drill was not started in scene 0: running it now in the foreground ($DRILL_SECONDS s)"
        fresh_dir drill
        drill_prepare
        mkdir -p "$(job_dir)"
        date -u +%H:%M:%SZ > "$(job_dir)/drill.started"
        "$PY" "${DRILL_ARGS[@]}" 2>&1 | tee "$(job_dir)/drill.out" | grep -E "kill|flatten|stop" | cut -c1-"$COLS"
        echo "${PIPESTATUS[0]}" > "$(job_dir)/drill.exit"
    fi
    note "started at $(cat "$(job_dir)/drill.started" 2>/dev/null || echo 'just now'); its events:"
    H events "$W/drill/logs"
}

drill_veto_and_frame() {
    H decision "$W/drill/logs" veto:KILLED
    echo
    "$PY" -m wta tui "${DRILL_VIEW[@]}" --width "$COLS" --once | head -n 8
}

drill_reset() {
    "$PY" -m wta reset-kill --reason "demo drill: synthetic -8% shock reviewed; the account is flat in TMN" "${DRILL_VIEW[@]}"
    H events "$W/drill/logs" reset_kill kill
}

replay_show() {
    if job_running replay; then
        job_wait replay 300
    elif [ ! -f "$(job_dir)/replay.exit" ]; then
        note "the replay was not started in scene 0: running it now (about 2 minutes)"
        mkdir -p "$(job_dir)"
        date -u +%H:%M:%SZ > "$(job_dir)/replay.started"
        replay_shown > "$(job_dir)/replay.cmd"
        replay_cmd > "$(job_dir)/replay.out" 2>&1
        echo $? > "$(job_dir)/replay.exit"
    fi
    note "started at $(cat "$(job_dir)/replay.started" 2>/dev/null); exit $(cat "$(job_dir)/replay.exit" 2>/dev/null); output:"
    grep -vE '^report: ' "$(job_dir)/replay.out" | tail -n 14 | cut -c1-"$COLS"
}

tag_link() {
    local c
    c=$(H commit "$RUN_SUMMARY") || return 1
    if [ -d "$RUN_LOGS" ]; then
        printf '%s$ (distinct git_commit, config_hash, mode in %s)%s\n' "$C_CMD" "$RUN_LOGS/decisions-*.jsonl" "$C_OFF"
        H provenance "$RUN_LOGS"
    else
        printf '  git_commit of the 4-h run (from %s): %s\n' "$RUN_SUMMARY" "$c"
    fi
    printf '%s$ git describe --tags %s%s\n' "$C_CMD" "${c:0:7}" "$C_OFF"; git describe --tags "$c"
    printf '%s$ git tag --contains %s%s\n' "$C_CMD" "${c:0:7}" "$C_OFF"; git tag --contains "$c"
    printf '%s$ git tag -l%s\n' "$C_CMD" "$C_OFF"; git tag -l
    printf '%s$ git tag -l "live-*"%s\n' "$C_CMD" "$C_OFF"
    if [ -z "$(git tag -l 'live-*')" ]; then
        echo "(none yet: the operator tags live-YYYYMMDD-HHMM before each live start; see deliverables/TAGS.md)"
    else
        git tag -l 'live-*'
    fi
}

# ------------------------------------------------------------------ scenes

shown_h() { show_cmd "$PY" "$HELPER" "$@"; }

evidence_survey() {
    open_images docs/REPORTS/survey_20261022_edges.png
    H md docs/REPORTS/survey_20261022.md "Recommendation"
}
evidence_backtest() {
    open_images docs/REPORTS/bt_candles_30d/equity.png
    H md docs/REPORTS/ablation_20261023.md "1." tables
    H md docs/REPORTS/ablation_20261023.md "2." tables
}
evidence_shadow() {
    open_images "$RUN_REPORT_DIR/equity.png" "$RUN_REPORT_DIR/drawdown.png"
    H md "$RUN_REPORT_DIR/report.md" "Metrics" all 24
}
evidence_live() {
    ls docs/REPORTS | grep -i '^live' || echo "(none: no live run yet; live trading is started only by the operator)"
}
stop_all() {
    local j
    for j in $JOBS; do job_stop "$j"; done
    print_status
}

scene0() {
    scene_title 0 "Preparation (before recording)" "not recorded; about 4 min of background work"
    step 0.1 "Preflight: python, commit, keys (set or not), recorded run, terminal width" "(checks)" preflight
    step 0.2 "Start the dashboard run in the background (shadow, synthetic feed, S1+S2, $DASH_SECONDS s)" \
        "$(dash_shown) &" eval 'job_running dash || fresh_dir dash; job_start dash "$(dash_shown)" dash_cmd'
    step 0.3 "Start the kill-switch drill in the background (shock at $SHOCK_AT s, ends after $DRILL_SECONDS s)" \
        "$(show_cmd "$PY" "${DRILL_ARGS[@]}") &" \
        eval 'job_running drill || { fresh_dir drill; drill_prepare; }; job_start drill "$(show_cmd "$PY" "${DRILL_ARGS[@]}")" "$PY" "${DRILL_ARGS[@]}"'
    step 0.4 "Start the replay of the 4-h shadow run in the background (about 2 min)" \
        "$(replay_shown) &" job_start replay "$(replay_shown)" replay_cmd
    step 0.5 "Background runs (start recording once the drill and the replay have finished)" \
        "bash scripts/demo.sh --status" print_status
}

scene1() {
    scene_title 1 "Problem & approach" "0:00-0:40"
    step 1.1 "Slide: constraints, fees, TMN valuation, thesis, three layers" "$(shown_h slide problem)" H slide problem
}

scene2() {
    scene_title 2 "Architecture" "0:40-1:40"
    step 2.1 "Diagram: AI plans, rules trade, risk guards" \
        "$(shown_h diagram docs/ARCHITECTURE.md docs/00-OVERVIEW.md)" H diagram docs/ARCHITECTURE.md docs/00-OVERVIEW.md
    step 2.2 "One CLI, one engine: run (shadow or live), backtest, replay" - "$PY" -m wta --help
}

scene3() {
    scene_title 3 "Live dashboard" "1:40-3:00"
    step 3.1 "Dashboard on the running synthetic shadow (Ctrl-C returns to the demo)" \
        "$(show_cmd "$PY" -m wta tui "${DASH_VIEW[@]}" --width "$COLS")" tui_dash
    step 3.2 "Dashboard frame of the recorded 4-h run (plan notes to read aloud)" \
        "$(show_cmd "$PY" -m wta tui --log-dir "$RUN_LOGS" --set "data.state_sqlite_path=$RUN_STATE_TEMPLATE" --width "$COLS" --once)" \
        tui_recorded
    step 3.3 "The last AI plan of that run: envelope, regimes with evidence, notes" \
        "$(shown_h plan "$RUN_LOGS")" plan_recorded
    step 3.4 "One decisions.jsonl line, field by field (the first S2 order, joined with its outcome line)" \
        "$(shown_h decision "$RUN_LOGS" first-order)" walk_recorded
}

scene4() {
    scene_title 4 "Risk in action" "3:00-3:40"
    step 4.1 "A veto record: FEE_BUDGET in the 30-day candle backtest (SYNTHETIC data)" \
        "$(shown_h decision docs/REPORTS/bt_candles_30d veto:FEE_BUDGET)" H decision docs/REPORTS/bt_candles_30d veto:FEE_BUDGET
    step 4.2 "Kill-switch drill: -8 % shock -> drawdown kill -> flatten to TMN" \
        "$(show_cmd "$PY" "${DRILL_ARGS[@]}")" drill_run
    step 4.3 "The order the RiskManager vetoed while killed, and the dashboard header" \
        "$(shown_h decision "$W/drill/logs" veto:KILLED); $(show_cmd "$PY" -m wta tui "${DRILL_VIEW[@]}" --width "$COLS" --once) | head -n 8" \
        drill_veto_and_frame
    step 4.4 "Manual reset (the only way out of a kill)" \
        "$(show_cmd "$PY" -m wta reset-kill --reason "demo drill: synthetic -8% shock reviewed; the account is flat in TMN" "${DRILL_VIEW[@]}")" \
        drill_reset
}

scene5() {
    scene_title 5 "Evidence" "3:40-4:30"
    step 5.1 "What the evidence is and is not: results in one table" \
        "$(shown_h md docs/REPORTS/final_report_summary.md "Results in one table")" \
        H md docs/REPORTS/final_report_summary.md "Results in one table"
    step 5.2 "Survey on the real recorded books (0.33 h aligned): edges chart and recommendation" \
        "open docs/REPORTS/survey_20261022_edges.png; $(shown_h md docs/REPORTS/survey_20261022.md Recommendation)" \
        evidence_survey
    step 5.3 "Backtest equity (30-day candles, SYNTHETIC) and the AI ablation tables" \
        "open docs/REPORTS/bt_candles_30d/equity.png; $(shown_h md docs/REPORTS/ablation_20261023.md 1. tables); $(shown_h md docs/REPORTS/ablation_20261023.md 2. tables)" \
        evidence_backtest
    step 5.4 "Shadow report of the 4-h run (SYNTHETIC feed, real LLM): equity, drawdown, metrics" \
        "open $RUN_REPORT_DIR/equity.png $RUN_REPORT_DIR/drawdown.png; $(shown_h md "$RUN_REPORT_DIR/report.md" Metrics all 24)" \
        evidence_shadow
    step 5.5 "Walk-forward guard on S1's margin (SYNTHETIC): the best in-sample value was rejected" \
        "$(shown_h md docs/REPORTS/walkforward_s1_margin/walkforward.md Walk-forward tables)" \
        H md docs/REPORTS/walkforward_s1_margin/walkforward.md "Walk-forward" tables
    step 5.6 "Live equity curve" "ls docs/REPORTS | grep -i '^live'" evidence_live
}

scene6() {
    scene_title 6 "Reproducibility" "4:30-4:50"
    local shown
    shown=$(cat "$(job_dir)/replay.cmd" 2>/dev/null) || shown=$(replay_shown)
    step 6.1 "Replay of the 4-h shadow run: every decision re-derived offline" "$shown" replay_show
    step 6.2 "Git tag <-> the commit in every decision record" "git describe --tags <git_commit>; git tag --contains <git_commit>" tag_link
    step 6.3 "Final balance" "$(shown_h final "$RUN_SUMMARY")" H final "$RUN_SUMMARY"
}

scene7() {
    scene_title 7 "Wrap-up" "4:50-5:00"
    step 7.1 "Closing card" "$(shown_h slide wrap)" H slide wrap
    step 7.2 "Stop the background runs" "bash scripts/demo.sh --stop" stop_all
}

list_scenes() {
    cat <<'LIST'
Scene 0  Preparation (before recording)            ~4 min of background work
  0.1 preflight: python, commit, keys set or not (never their values), recorded run, terminal width
  0.2 background: dashboard run (shadow, synthetic feed, S1+S2, analyst every 60 s, 1500 s)
  0.3 background: kill-switch drill (-8 % synthetic shock at 60 s, 200 s)
  0.4 background: replay of the 4-h shadow run (make replay-deliverable, else scripts/replay_check.py; ~2 min)
  0.5 status of the background runs
Scene 1  Problem & approach                        0:00-0:40
  1.1 slide: capital, 3 days, TMN valuation, fees, thesis, S1/S2/S3/FLATTEN, three layers
Scene 2  Architecture                              0:40-1:40
  2.1 diagram from docs/ARCHITECTURE.md
  2.2 python -m wta --help (one engine: run, backtest, replay)
Scene 3  Live dashboard                            1:40-3:00
  3.1 python -m wta tui on the running synthetic shadow
  3.2 python -m wta tui --once on the recorded 4-h run (plan notes)
  3.3 the last AI plan of the 4-h run, field by field
  3.4 one decisions.jsonl line, field by field, with its outcome line
Scene 4  Risk in action                            3:00-3:40
  4.1 a veto record (FEE_BUDGET, 30-day candle backtest, synthetic)
  4.2 kill-switch drill: shock -> kill -> flatten (events)
  4.3 the veto while killed (KILLED) + dashboard header
  4.4 python -m wta reset-kill
Scene 5  Evidence                                  3:40-4:30
  5.1 results in one table (final_report_summary.md)
  5.2 survey edges chart + recommendation
  5.3 30-day backtest equity + AI ablation tables
  5.4 shadow report PNGs + metrics
  5.5 walk-forward table
  5.6 live equity curve (none yet)
Scene 6  Reproducibility                           4:30-4:50
  6.1 replay result: "40/40 decisions reproduced"
  6.2 git tag <-> decision log commit
  6.3 final balance (4-h shadow run; live: none yet)
Scene 7  Wrap-up                                   4:50-5:00
  7.1 closing card
  7.2 stop the background runs
LIST
}

# ------------------------------------------------------------------ main

case $ACTION in
    list) list_scenes; exit 0 ;;
    status) print_status; exit 0 ;;
    stop) stop_all; exit 0 ;;
esac

if [ "$FRESH" = 1 ] && [ "$DRY_RUN" = 0 ]; then
    for j in $JOBS; do job_stop "$j"; done
    rm -rf "$W"
fi
[ "$DRY_RUN" = 1 ] || write_helper

[ ${#SCENES[@]} -gt 0 ] || SCENES=(0 1 2 3 4 5 6 7)
for s in "${SCENES[@]}"; do
    "scene$s"
done
printf '\n%sdone.%s Background runs: bash scripts/demo.sh --status · stop them: bash scripts/demo.sh --stop\n' "$C_STEP" "$C_OFF"
exit 0

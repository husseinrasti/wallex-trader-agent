#!/usr/bin/env bash
# Keep a wta command alive (bare-metal alternative to docker compose, PRD-06 §1).
#
#   scripts/run_forever.sh record --symbols-from configs/markets.yaml
#   scripts/run_forever.sh run --mode shadow --log-dir logs/shadow
#
# Restarts the command after it exits, with a 10 s back-off; more than MAX_RESTARTS_PER_HOUR
# restarts within an hour stops the loop (and sends a Telegram alert when configured), because a
# crash loop must not hammer the exchange. Each restart is logged to logs/run_forever.log; the
# engine itself writes an events "start" record with reason "restart" when it finds prior state.
# The alert gets the wrapped command's --set / --overlay arguments, so a Telegram enabled with
# `--set alerts.telegram_enabled=true` reaches `wta alert` too.
#
# SIGTERM / SIGINT: forwarded to the child; the loop then waits until the child has really exited
# (the engine finishes its running tick and drains its alerts) before it logs "stopped" and exits,
# so a follow-up `make flatten` or restart never races the still-running engine.
#
# Watchdog (PRD-06 §2): a child that is alive but silent is restarted too. For `run` the heartbeat is
# the newest <--log-dir>/health-*.jsonl (default logs/), for `record` the newest logs/recorder-*.jsonl;
# both are written every 60 s. When the newest one is older than WATCHDOG_S (default 180 s), checked
# every WATCHDOG_POLL_S (15 s) once WATCHDOG_GRACE_S (300 s: start-up with an unreachable API takes
# about 2.5 min) have passed since the child started, the watchdog logs "watchdog: no heartbeat",
# sends SIGTERM, then SIGKILL after WATCHDOG_KILL_S (30 s), and alerts. The loop restarts the child
# like after a crash (back-off and the hourly cap apply). WATCHDOG_S=0 turns it off. A heartbeat
# file older than the child's start never counts.
set -u
cd "$(dirname "$0")/.."
PY="${PY:-.venv/bin/python}"
BACKOFF_S="${BACKOFF_S:-10}"
MAX_RESTARTS_PER_HOUR="${MAX_RESTARTS_PER_HOUR:-20}"
LOG="${RUN_FOREVER_LOG:-logs/run_forever.log}"
WATCHDOG_S="${WATCHDOG_S:-180}"
WATCHDOG_POLL_S="${WATCHDOG_POLL_S:-15}"
WATCHDOG_GRACE_S="${WATCHDOG_GRACE_S:-300}"
WATCHDOG_KILL_S="${WATCHDOG_KILL_S:-30}"
mkdir -p "$(dirname "$LOG")"

restarts=()
stop=0
child=""
dog=""
trap 'stop=1; [ -n "${child:-}" ] && kill -TERM "$child" 2>/dev/null' INT TERM

# the config arguments of the wrapped command (--set K=V, --overlay FILE, also the --opt=value form)
cfg_args=()
want=""
for arg in "$@"; do
  if [ -n "$want" ]; then
    cfg_args+=("$want" "$arg")
    want=""
    continue
  fi
  case "$arg" in
    --set|--overlay) want="$arg" ;;
    --set=*|--overlay=*) cfg_args+=("$arg") ;;
  esac
done

alert() {
  echo "$(date -u +%FT%TZ) $*" >>"$LOG"
  "$PY" -m wta alert ${cfg_args[@]+"${cfg_args[@]}"} -- "$*" >/dev/null 2>&1 || true
}

cmd_text="$*"
# the heartbeat files of the wrapped command (empty: no watchdog for this command)
log_dir="logs"
want=""
for arg in "$@"; do
  if [ -n "$want" ]; then log_dir="$arg"; want=""; continue; fi
  case "$arg" in
    --log-dir) want=1 ;;
    --log-dir=*) log_dir="${arg#--log-dir=}" ;;
  esac
done
case "${1:-}" in
  run) beat_glob="$log_dir/health-*.jsonl" ;;
  record) beat_glob="logs/recorder-*.jsonl" ;;
  *) beat_glob="" ;;
esac

mtime() { stat -c %Y "$1" 2>/dev/null || stat -f %m "$1" 2>/dev/null; }

newest_beat() {  # newest heartbeat mtime (epoch s), 0 when there is none
  local best=0 f m
  for f in $beat_glob; do
    [ -e "$f" ] || continue
    m=$(mtime "$f") || continue
    [ -n "$m" ] && [ "$m" -gt "$best" ] && best=$m
  done
  echo "$best"
}

watchdog() {  # $1 = the child's pid, $2 = its start time (epoch s)
  local pid=$1 started=$2 now last age
  while kill -0 "$pid" 2>/dev/null; do
    sleep "$WATCHDOG_POLL_S"
    now=$(date +%s)
    [ $((now - started)) -ge "$WATCHDOG_GRACE_S" ] || continue
    last=$(newest_beat)
    [ "$last" -ge "$started" ] || last=$started
    age=$((now - last))
    [ "$age" -gt "$WATCHDOG_S" ] || continue
    kill -0 "$pid" 2>/dev/null || return 0
    echo "$(date -u +%FT%TZ) watchdog: no heartbeat in $beat_glob for ${age}s (limit ${WATCHDOG_S}s); SIGTERM to $pid" >>"$LOG"
    # the alert runs on its own: the loop may end this watchdog as soon as the child has exited
    alert "run_forever: watchdog restarts wta $cmd_text (no heartbeat for ${age}s)" &
    kill -TERM "$pid" 2>/dev/null
    local waited=0
    while kill -0 "$pid" 2>/dev/null && [ "$waited" -lt "$WATCHDOG_KILL_S" ]; do
      sleep 1
      waited=$((waited + 1))
    done
    if kill -0 "$pid" 2>/dev/null; then
      echo "$(date -u +%FT%TZ) watchdog: $pid still running ${WATCHDOG_KILL_S}s after SIGTERM; SIGKILL" >>"$LOG"
      kill -KILL "$pid" 2>/dev/null
    fi
    return 0
  done
}

while [ "$stop" -eq 0 ]; do
  echo "$(date -u +%FT%TZ) starting: wta $*" >>"$LOG"
  "$PY" -m wta "$@" &
  child=$!
  dog=""
  if [ -n "$beat_glob" ] && [ "$WATCHDOG_S" -gt 0 ]; then
    watchdog "$child" "$(date +%s)" &
    dog=$!
  fi
  wait "$child"
  code=$?
  if [ "$stop" -eq 1 ]; then
    # a trapped signal interrupts `wait` at once: wait for the child itself to finish
    while kill -0 "$child" 2>/dev/null; do
      wait "$child"
      code=$?
    done
    [ -n "$dog" ] && kill "$dog" 2>/dev/null
    child=""
    break
  fi
  if [ -n "$dog" ]; then
    kill "$dog" 2>/dev/null
    wait "$dog" 2>/dev/null
  fi
  child=""
  now=$(date +%s)
  kept=()
  for t in "${restarts[@]:-}"; do
    [ -n "$t" ] && [ $((now - t)) -lt 3600 ] && kept+=("$t")
  done
  restarts=("${kept[@]:-}" "$now")
  n=0; for t in "${restarts[@]}"; do [ -n "$t" ] && n=$((n + 1)); done
  if [ "$n" -gt "$MAX_RESTARTS_PER_HOUR" ]; then
    alert "run_forever: wta $* restarted $n times in 1 h (last exit $code) - giving up"
    exit 1
  fi
  echo "$(date -u +%FT%TZ) exited with $code; restart $n/$MAX_RESTARTS_PER_HOUR in ${BACKOFF_S}s" >>"$LOG"
  sleep "$BACKOFF_S"
done
echo "$(date -u +%FT%TZ) stopped" >>"$LOG"

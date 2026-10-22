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
set -u
cd "$(dirname "$0")/.."
PY="${PY:-.venv/bin/python}"
BACKOFF_S="${BACKOFF_S:-10}"
MAX_RESTARTS_PER_HOUR="${MAX_RESTARTS_PER_HOUR:-20}"
LOG="${RUN_FOREVER_LOG:-logs/run_forever.log}"
mkdir -p "$(dirname "$LOG")"

restarts=()
stop=0
child=""
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

while [ "$stop" -eq 0 ]; do
  echo "$(date -u +%FT%TZ) starting: wta $*" >>"$LOG"
  "$PY" -m wta "$@" &
  child=$!
  wait "$child"
  code=$?
  if [ "$stop" -eq 1 ]; then
    # a trapped signal interrupts `wait` at once: wait for the child itself to finish
    while kill -0 "$child" 2>/dev/null; do
      wait "$child"
      code=$?
    done
    child=""
    break
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

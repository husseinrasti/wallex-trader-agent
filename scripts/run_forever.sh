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
set -u
cd "$(dirname "$0")/.."
PY="${PY:-.venv/bin/python}"
BACKOFF_S="${BACKOFF_S:-10}"
MAX_RESTARTS_PER_HOUR="${MAX_RESTARTS_PER_HOUR:-20}"
LOG="${RUN_FOREVER_LOG:-logs/run_forever.log}"
mkdir -p "$(dirname "$LOG")"

restarts=()
stop=0
trap 'stop=1; [ -n "${child:-}" ] && kill -TERM "$child" 2>/dev/null' INT TERM

alert() {
  echo "$(date -u +%FT%TZ) $*" >>"$LOG"
  "$PY" -m wta alert "$*" >/dev/null 2>&1 || true
}

while [ "$stop" -eq 0 ]; do
  echo "$(date -u +%FT%TZ) starting: wta $*" >>"$LOG"
  "$PY" -m wta "$@" &
  child=$!
  wait "$child"
  code=$?
  child=""
  [ "$stop" -eq 1 ] && break
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

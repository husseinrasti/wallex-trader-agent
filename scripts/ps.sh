#!/usr/bin/env bash
# What is running and is data flowing? (make ps)
#
#   1. our processes (wta record, wta run, record_market.py)
#   2. PID files in run/*.pid, alive or dead
#   3. the last line of the newest logs/recorder-*.jsonl and logs/health-*.jsonl
#   4. row counts per table and books per symbol in the last 10 minutes (data/wta.sqlite, read-only)
#
# Read-only, and never fails because a file is missing: every step reports "none" instead.

set -u
cd "$(dirname "$0")/.." || exit 0

DB="${WTA_DB:-data/wta.sqlite}"
PY=".venv/bin/python"
[ -x "$PY" ] || PY="$(command -v python3 || true)"

echo "== processes"
for pattern in "wta record" "wta run" "record_market.py"; do
    if ! pgrep -fl "$pattern" 2>/dev/null; then
        echo "  (no process matching '$pattern')"
    fi
done

echo
echo "== pid files (run/*.pid)"
found=0
for pidfile in run/*.pid; do
    [ -e "$pidfile" ] || continue
    found=1
    pid="$(tr -cd '0-9' < "$pidfile")"
    if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
        echo "  $pidfile: $pid alive"
    else
        echo "  $pidfile: ${pid:-?} DEAD"
    fi
done
[ "$found" -eq 1 ] || echo "  (none)"

newest() {  # newest file matching a glob, or nothing
    ls -t $1 2>/dev/null | head -n 1
}

echo
for family in recorder health; do
    file="$(newest "logs/${family}-*.jsonl")"
    echo "== last ${family} line"
    if [ -n "$file" ]; then
        echo "  ($file)"
        tail -n 1 "$file" | cut -c 1-2000
    else
        echo "  (no logs/${family}-*.jsonl)"
    fi
    echo
done

echo "== database ($DB)"
if [ ! -f "$DB" ]; then
    echo "  (no database yet)"
elif [ -z "$PY" ]; then
    echo "  (no python found to read it)"
else
    "$PY" - "$DB" <<'PY' || echo "  (could not read the database)"
import sqlite3
import sys
import time
from pathlib import Path

uri = Path(sys.argv[1]).resolve().as_uri() + "?mode=ro"  # read-only: never blocks the writer
conn = sqlite3.connect(uri, uri=True, timeout=5)
tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
for table in tables:
    if table == "sqlite_sequence":
        continue
    count = conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
    print(f"  {table:<10} {count:>12,}")
if "books" in tables:
    since = time.time() - 600
    rows = conn.execute(
        "SELECT symbol, COUNT(*), MAX(ts) FROM books WHERE ts >= ? GROUP BY symbol ORDER BY symbol", (since,)
    ).fetchall()
    print("  books in the last 10 minutes:" if rows else "  books in the last 10 minutes: none")
    for symbol, n, last in rows:
        print(f"    {symbol:<12} {n:>7,}  last {time.time() - last:5.1f}s ago")
PY
fi
exit 0

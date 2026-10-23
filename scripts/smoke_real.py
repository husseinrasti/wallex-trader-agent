"""One-command smoke test of every REAL Wallex path, read-only (``make smoke-real``).

Run it on the machine and network that will trade (for example with the VPN off). It never places an
order: it calls the public market-data endpoints, the read-only account endpoints (when
``WALLEX_API_KEY`` is set), the WebSocket feed, the LLM once (one analyst dry-run), and a short paper
shadow run on the real feed in its own scratch account. Every step has a timeout; the summary table
says PASS / WARN / FAIL with the reason, and everything is saved under ``logs/smoke_<UTC time>/``
(gitignored: it may contain balances). Exit code: 0 when no step FAILed.

    python scripts/smoke_real.py                 # all steps (~5 minutes)
    python scripts/smoke_real.py --quick         # skip the shadow run (~1 minute)
    python scripts/smoke_real.py --shadow-seconds 300
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

PY = str(ROOT / ".venv" / "bin" / "python") if (ROOT / ".venv" / "bin" / "python").exists() else sys.executable


@dataclass
class Step:
    name: str
    status: str  # PASS / WARN / FAIL / SKIP
    detail: str
    seconds: float


def run(cmd: list[str], out: Path, timeout: float, env: dict[str, str] | None = None) -> tuple[int | None, str]:
    """Run ``cmd`` from the repo root, save stdout+stderr to ``out``; returns (exit code or None on timeout, text)."""
    t0 = time.monotonic()
    try:
        proc = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=timeout,
                              env={**os.environ, "WTA_LIVE": "0", **(env or {})})
        text = proc.stdout + proc.stderr
        code: int | None = proc.returncode
    except subprocess.TimeoutExpired as exc:
        text = (exc.stdout or "") + (exc.stderr or "") if isinstance(exc.stdout, str) else ""
        code = None
    out.write_text(f"$ {' '.join(cmd)}\n# exit={code} after {time.monotonic() - t0:.1f}s\n\n{text}")
    return code, text


def tail(text: str, n: int = 2) -> str:
    lines = [ln.strip() for ln in text.strip().splitlines() if ln.strip()]
    return " | ".join(lines[-n:])[:300]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--quick", action="store_true", help="skip the shadow run")
    ap.add_argument("--shadow-seconds", type=float, default=180, help="length of the real-feed shadow run (default 180)")
    ap.add_argument("--timeout", type=float, default=10, help="HTTP timeout for one-shot calls (default 10)")
    args = ap.parse_args(argv)

    from wta.config import load_settings  # noqa: E402 - after sys.path

    settings = load_settings()
    has_key = bool(settings.wallex_api_key.get_secret_value())
    has_llm = bool(settings.openrouter_api_key.get_secret_value() or settings.gemini_api_key.get_secret_value())
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    out = ROOT / "logs" / f"smoke_{stamp}"
    out.mkdir(parents=True, exist_ok=True)
    steps: list[Step] = []

    def record(name: str, status: str, detail: str, t0: float) -> None:
        steps.append(Step(name, status, detail, time.monotonic() - t0))
        mark = {"PASS": "OK  ", "WARN": "WARN", "FAIL": "FAIL", "SKIP": "skip"}[status]
        print(f"[{mark}] {name}: {detail}", flush=True)

    print(f"smoke test of the real Wallex paths -> {out.relative_to(ROOT)}  (nothing is traded)\n", flush=True)

    # 1. reachability ---------------------------------------------------------------------------
    t0 = time.monotonic()
    import httpx  # noqa: E402

    reachable = False
    try:
        r = httpx.get(f"{settings.wallex_base_url.rstrip('/')}/v1/markets", timeout=args.timeout)
        reachable = r.status_code == 200
        record("1 api.wallex.ir reachable", "PASS" if reachable else "FAIL", f"HTTP {r.status_code}", t0)
    except httpx.HTTPError as exc:
        record("1 api.wallex.ir reachable", "FAIL",
               f"{type(exc).__name__} (VPN on? Wallex drops foreign IPs during TLS)", t0)
    if not reachable:
        print("\napi.wallex.ir does not answer from this network: turn the VPN off (or use an Iranian IP) and rerun.")
        return summarize(steps, out)

    # 2. markets ------------------------------------------------------------------------------------
    t0 = time.monotonic()
    code, text = run([PY, "-m", "wta", "markets", "--timeout", str(args.timeout)], out / "02_markets.txt", 60)
    record("2 wta markets (precision, ban list)", "PASS" if code == 0 else "FAIL",
           "watchlist parsed, none banned" if code == 0 else f"exit {code}: {tail(text)}", t0)

    # 3. account (read-only) --------------------------------------------------------------------
    t0 = time.monotonic()
    if not has_key:
        record("3 check_account (read-only)", "SKIP", "WALLEX_API_KEY not set in .env", t0)
    else:
        code, text = run([PY, "scripts/check_account.py", "--timeout", str(args.timeout)], out / "03_check_account.txt", 180)
        warn = "WARN" in text.upper() or "warning" in text.lower()
        status = "PASS" if code == 0 else ("WARN" if code is not None and code != 2 else "FAIL")
        if status == "PASS" and warn:
            status = "WARN"
        record("3 check_account (read-only)", status,
               "report: logs/api_probe.md (balances, fees, sub-accounts; not committed)"
               + (f" — exit {code}: {tail(text)}" if status != "PASS" else ""), t0)

    # 4. WebSocket + REST feed ------------------------------------------------------------------
    t0 = time.monotonic()
    code, text = run([PY, "-m", "wta", "feed", "BTCUSDT", "--seconds", "20"], out / "04_feed_ws.txt", 60)
    ws_lines = sum(1 for ln in text.splitlines() if " ws " in f" {ln} ")
    rest_lines = sum(1 for ln in text.splitlines() if " rest " in f" {ln} ")
    if code == 0 and ws_lines:
        record("4 feed BTCUSDT (WebSocket)", "PASS", f"{ws_lines} ws lines, {rest_lines} rest lines", t0)
    elif code == 0 and rest_lines:
        record("4 feed BTCUSDT (WebSocket)", "WARN", f"no ws data, REST fallback works ({rest_lines} lines)", t0)
    else:
        record("4 feed BTCUSDT (WebSocket)", "FAIL", f"exit {code}: {tail(text)}", t0)

    # 5. REST only ------------------------------------------------------------------------------
    t0 = time.monotonic()
    code, text = run([PY, "-m", "wta", "feed", "BTCUSDT", "--no-ws", "--seconds", "8"], out / "05_feed_rest.txt", 40)
    rest_lines = sum(1 for ln in text.splitlines() if " rest " in f" {ln} ")
    record("5 feed BTCUSDT (REST only)", "PASS" if code == 0 and rest_lines else "FAIL",
           f"{rest_lines} rest lines" if code == 0 and rest_lines else f"exit {code}: {tail(text)}", t0)

    # 6. LLM from this network --------------------------------------------------------------------
    t0 = time.monotonic()
    ai_flag = "on"
    if not has_llm:
        record("6 LLM analyst (one dry-run plan)", "SKIP", "no OpenRouter/Gemini key: the offline rules provider runs", t0)
        ai_flag = "offline"
    else:
        code, text = run([PY, "-m", "wta", "plan", "--feed", "live", "--warmup", "20",
                          "--log-dir", str(out / "plan_logs")], out / "06_plan.txt", 240)
        ai_source = ""
        try:
            ai_source = json.loads(text[text.index("{"): text.rindex("}") + 1]).get("ai_source", "")
        except (ValueError, AttributeError):
            pass
        if code == 0 and ai_source == "llm":
            record("6 LLM analyst (one dry-run plan)", "PASS", "OpenRouter answers from this network", t0)
        else:
            ai_flag = "offline"
            record("6 LLM analyst (one dry-run plan)", "WARN",
                   "no LLM plan from this network (OpenRouter is often blocked without VPN): run with "
                   "--ai offline (labelled rules provider) or --ai off", t0)

    # 7. paper shadow on the real feed -----------------------------------------------------------
    t0 = time.monotonic()
    if args.quick:
        record("7 shadow run on the real feed (paper)", "SKIP", "--quick", t0)
    else:
        logs = out / "shadow_logs"
        state = out / "state_{mode}.sqlite"
        cmd = [PY, "-m", "wta", "run", "--mode", "shadow", "--seconds", str(args.shadow_seconds),
               "--log-dir", str(logs), "--set", f"data.state_sqlite_path={state}", "--ai", ai_flag,
               "--set", "engine.heartbeat_interval_s=20", "--runtime-dir", str(out / "runtime")]
        code, text = run(cmd, out / "07_shadow.txt", args.shadow_seconds + 240)
        hb = last_json(logs, "health")
        decisions = list(iter_jsonl(logs, "decisions"))
        extra = (hb or {}).get("extra", {}) if isinstance((hb or {}).get("extra"), dict) else {}
        fresh = extra.get("fresh_books") or extra.get("books_fresh")
        plans = sum(1 for d in decisions if d.get("kind") == "plan")
        orders = sum(1 for d in decisions if d.get("kind") == "order")
        ok = code == 0 and hb is not None and hb.get("rest_ok")
        detail = (f"rest_ok={hb.get('rest_ok') if hb else None} ws_ok={hb.get('ws_ok') if hb else None} "
                  f"fresh_books={fresh} plans={plans} paper_orders={orders} ai={ai_flag}")
        record("7 shadow run on the real feed (paper)", "PASS" if ok else "FAIL",
               detail if ok else f"exit {code}: {detail} {tail(text)}", t0)
        t1 = time.monotonic()
        code, text = run([PY, "-m", "wta", "logs", "check", "--dir", str(logs), "--secrets"], out / "08_logs_check.txt", 120)
        record("8 logs check + secrets scan", "PASS" if code == 0 else "FAIL", tail(text, 1), t1)
        print(f"\n   dashboard of that run:  {PY} -m wta tui --log-dir {logs} --once")

    return summarize(steps, out)


def iter_jsonl(log_dir: Path, base: str):
    for path in sorted(log_dir.glob(f"{base}-*.jsonl")):
        for line in path.read_text().splitlines():
            if line.strip():
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    continue


def last_json(log_dir: Path, base: str) -> dict | None:
    rows = list(iter_jsonl(log_dir, base))
    return rows[-1] if rows else None


def summarize(steps: list[Step], out: Path) -> int:
    lines = ["| step | result | detail | s |", "|---|---|---|---:|"]
    lines += [f"| {s.name} | {s.status} | {s.detail.replace('|', '/')} | {s.seconds:.0f} |" for s in steps]
    failed = [s for s in steps if s.status == "FAIL"]
    verdict = "FAIL" if failed else ("WARN" if any(s.status == "WARN" for s in steps) else "PASS")
    text = f"# Real-API smoke test — {verdict}\n\n" + "\n".join(lines) + "\n\nOutputs: one file per step in this folder.\n"
    (out / "SUMMARY.md").write_text(text)
    print(f"\nresult: {verdict}  ({len(failed)} failed) — summary: {(out / 'SUMMARY.md').relative_to(ROOT)}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())

PYTHON ?= python3
VENV   ?= .venv
PY     := $(VENV)/bin/python
FROM   ?= 2026-09-22
TO     ?= 2026-10-22
BOOKS_FROM ?= 2026-10-22T10:00Z
BOOKS_TO   ?= 2026-10-23T10:00Z
AI     ?= off
REPORT_SOURCE ?= logs/shadow
REPORT_OUT    ?= docs/REPORTS/shadow_report
REPLAY_ARGS   ?= --decisions logs/decisions.jsonl --reports-dir logs --no-index
REPLAY_REPORT_ARGS ?= --decisions logs/decisions.jsonl
DELIVER_RUN   ?= logs/shadow_synth_4h

.PHONY: smoke-real smoke-real-quick install test lint record survey shadow shadow-synth live backtest backtest-books backtest-synth backtest-cache market-db replay replay-report replay-deliverable deliverables final-report report export flatten flatten-live status tui ps docker

install:
	$(PYTHON) -m venv $(VENV) && $(VENV)/bin/pip install -U pip && $(VENV)/bin/pip install -e ".[dev]"

test: lint         # pyproject addopts already has -q; a second -q would hide the 'N passed' summary line
	$(PY) -m pytest

lint:
	$(VENV)/bin/ruff check src tests backtest scripts

record:
	$(PY) -m wta record --symbols-from configs/markets.yaml

survey:
	$(PY) scripts/survey.py --since 24h

shadow:
	$(PY) -m wta run --mode shadow --log-dir logs/shadow

shadow-synth:       # offline exercise on the synthetic feed (never evidence of performance)
	$(PY) -m wta run --mode shadow --feed synthetic --log-dir logs/shadow_synth --set data.sqlite_path=data/shadow_synth.sqlite

live:
	WTA_LIVE=1 $(PY) -m wta run --mode live --i-understand-live

backtest:           # 30-day candle backtest of REAL candles (S2, S3 with allow_rules_only as in the shipped candle reports; AI=off|on) over data/wta.sqlite (backfill first: wta backfill --days 30); never touches the SYNTHETIC bt_candles_30d report; state DB (~130 MB) under data/
	$(PY) -m wta backtest --driver candles --from $(FROM) --to $(TO) --ai $(AI) --strategies S2,S3 --set strategies.s3_momentum.allow_rules_only=true --out docs/REPORTS/bt_candles_30d_real --state-db data/backtest_state/bt_candles_30d_real.sqlite

backtest-books:     # book-driven backtest (S1, S2) over the recorded order books; the report states the hours really covered
	$(PY) -m wta backtest --driver books --from $(BOOKS_FROM) --to $(BOOKS_TO) --ai $(AI) --strategies S1,S2 --out docs/REPORTS/bt_books --state-db data/backtest_state/bt_books.sqlite

backtest-synth:     # SYNTHETIC data (never evidence of performance): exercises the backtest path offline; output under the gitignored logs/
	$(PY) -m wta backtest --driver books --synthetic-db data/synthetic_books.sqlite --synthetic-hours 6 --strategies S1,S2 --out logs/synthetic_bt_books --state-db data/backtest_state/synthetic_bt_books.sqlite

backtest-cache:     # the shipped AI ablation's ai=on run (real LLM, SYNTHETIC books) again, from its committed LLM cache: no key, no provider call, a miss is exit 1
	$(PY) -m wta backtest --driver books --synthetic-db data/synth_books_24h.sqlite --synthetic-hours 24 --from 2026-10-01T12:00Z --to 2026-10-01T18:00Z --ai cache --strategies S1,S2 --set ai.cache_dir=docs/REPORTS/bt_books_synth24h_6h_ai_on/llm_cache --out logs/bt_cache_synth24h_6h --state-db data/backtest_state/bt_cache_synth24h_6h.sqlite

market-db:          # unpack the committed REAL Wallex market data (deliverables/market) into data/wta.sqlite: SHA256SUMS checked, never overwrites
	$(PY) scripts/market_db.py

replay:             # re-run the newest recorded run under logs/, compare every decision (exit 1 on mismatch); report in logs/replay_<day>.md, repo files untouched
	$(PY) scripts/replay_check.py $(REPLAY_ARGS)

replay-report:      # the same, but (re)writes docs/REPORTS/replay_<day>.md and the reports index (for regenerating the committed proof)
	$(PY) scripts/replay_check.py $(REPLAY_REPORT_ARGS)

replay-deliverable: # the same replay from the committed bundle (no logs/ or data/ needed): SHA256SUMS checked, unpacked to a temp dir, replayed, removed
	$(PY) scripts/replay_check.py --bundle deliverables/replay/shadow_synth_4h

deliverables:       # copy a recorded run into deliverables/<name>/ + its replay bundle, secrets-scanned (needs logs/ and data/)
	$(PY) scripts/make_deliverables.py --run-dir $(DELIVER_RUN) --bundle --replay

final-report:       # rebuild docs/REPORTS/README.md and deliverables/REPORT.md from the reports (+ its deliverables index)
	$(PY) scripts/assemble_report.py

report:             # shadow/live run report from its logs + state DB (add --compare logs for paper-vs-live)
	$(PY) -m wta report --source $(REPORT_SOURCE) --out $(REPORT_OUT)

export:             # market tables to Parquet (CSV when pyarrow is missing)
	$(PY) -m wta export --parquet data/export

flatten:            # paper by default (uses the shadow paper state)
	$(PY) scripts/flatten_now.py

flatten-live:       # LIVE account: operator only, needs WTA_LIVE=1 in the environment
	WTA_LIVE=1 $(PY) scripts/flatten_now.py --i-understand-live

status:
	$(PY) -m wta status

tui:
	$(PY) -m wta tui

ps:
	bash scripts/ps.sh

docker:
	docker build -t wallex-trader-agent .

smoke-real:         # READ-ONLY check of every real Wallex path + a 3-min paper shadow on the real feed (VPN off)
	$(PY) scripts/smoke_real.py

smoke-real-quick:   # the same without the shadow run (~1 min)
	$(PY) scripts/smoke_real.py --quick

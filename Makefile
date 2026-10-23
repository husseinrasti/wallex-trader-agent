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
REPLAY_ARGS   ?= --decisions logs/decisions.jsonl
DELIVER_RUN   ?= logs/shadow_synth_4h

.PHONY: install test lint record survey shadow shadow-synth live backtest backtest-books backtest-synth replay replay-deliverable deliverables report export flatten flatten-live status tui ps docker

install:
	$(PYTHON) -m venv $(VENV) && $(VENV)/bin/pip install -U pip && $(VENV)/bin/pip install -e ".[dev]"

test: lint
	$(PY) -m pytest -q

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

backtest:           # 30-day candle backtest (S2, S3; ai off) over data/wta.sqlite (backfill first: wta backfill --days 30); state DB (~130 MB) under data/
	$(PY) -m wta backtest --driver candles --from $(FROM) --to $(TO) --ai $(AI) --strategies S2,S3 --out docs/REPORTS/bt_candles_30d --state-db data/backtest_state/bt_candles_30d.sqlite

backtest-books:     # book-driven backtest (S1, S2) over the recorded order books; the report states the hours really covered
	$(PY) -m wta backtest --driver books --from $(BOOKS_FROM) --to $(BOOKS_TO) --ai $(AI) --strategies S1,S2 --out docs/REPORTS/bt_books --state-db data/backtest_state/bt_books.sqlite

backtest-synth:     # SYNTHETIC data (never evidence of performance): exercises the backtest path offline
	$(PY) -m wta backtest --driver books --synthetic-db data/synthetic_books.sqlite --synthetic-hours 6 --strategies S1,S2 --out docs/REPORTS/synthetic_bt_books --state-db data/backtest_state/synthetic_bt_books.sqlite

replay:             # judges' one-liner: re-run the recorded run, compare every decision (exit 1 on mismatch)
	$(PY) scripts/replay_check.py $(REPLAY_ARGS)

replay-deliverable: # the same replay from the committed bundle (no logs/ or data/ needed): SHA256SUMS checked, unpacked to a temp dir, replayed, removed
	$(PY) scripts/replay_check.py --bundle deliverables/replay/shadow_synth_4h

deliverables:       # copy a recorded run into deliverables/<name>/ + its replay bundle, secrets-scanned (needs logs/ and data/)
	$(PY) scripts/make_deliverables.py --run-dir $(DELIVER_RUN) --bundle --replay

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

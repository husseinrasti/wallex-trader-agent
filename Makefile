PYTHON ?= python3
VENV   ?= .venv
PY     := $(VENV)/bin/python
FROM   ?= 2026-09-22
TO     ?= 2026-10-22

.PHONY: install test lint record survey shadow live backtest replay report flatten status tui ps docker

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
	$(PY) -m wta run --mode shadow

live:
	WTA_LIVE=1 $(PY) -m wta run --mode live --i-understand-live

backtest:
	$(PY) -m wta backtest --from $(FROM) --to $(TO)

replay:
	$(PY) scripts/replay_check.py --decisions logs/decisions.jsonl

report:
	$(PY) -m wta report --out docs/REPORTS

flatten:
	$(PY) scripts/flatten_now.py --i-understand-live

status:
	$(PY) -m wta status

tui:
	$(PY) -m wta tui

ps:
	bash scripts/ps.sh

docker:
	docker build -t wallex-trader-agent .

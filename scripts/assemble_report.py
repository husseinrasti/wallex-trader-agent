"""Rebuild ``docs/REPORTS/README.md`` and ``deliverables/REPORT.md`` from the reports in ``docs/REPORTS``.

    python scripts/assemble_report.py      # = make final-report

1. ``backtest.report.update_index("docs/REPORTS")``: the reports index, with each report's data label;
2. ``backtest.report.assemble_final_report``: the final report, one section per entry of ``SECTIONS``
   (the latest report matching each pattern wins; a missing section says so); the live section is
   filled from ``docs/REPORTS/live_*`` once a live run's report exists
   (``python -m wta report --source logs --out docs/REPORTS/live_<date>``);
3. ``scripts/make_deliverables.py --index-only``: the "Deliverables index" block at the top of
   REPORT.md, rewritten from what is in ``deliverables/``; every relative link must resolve.

Nothing is computed here: the numbers come from the reports. Exit code 0, or 1 when a link of
REPORT.md does not resolve.
"""

from __future__ import annotations

import sys

from backtest.report import FinalSection as F
from backtest.report import assemble_final_report, update_index
from wta.config import ROOT

REPORTS = ROOT / "docs" / "REPORTS"
FINAL = ROOT / "deliverables" / "REPORT.md"
TITLE = "Wallex Trader Agent — final report (Phase 5: backtests, shadow, replay)"
LIVE_TEXT = (
    "**No live run yet** (live trading is started only by the operator). No live order has been placed. "
    "A live run needs `WTA_LIVE=1` in the process environment and a clean, committed tree (`docs/DECISIONS.md`); "
    "the steps are in `docs/GO_LIVE_CHECKLIST.md`. Its report will be "
    "`python -m wta report --source logs --out docs/REPORTS/live_<date>`, which this section then embeds."
)
SECTIONS = [
    F("summary", "Read first: what this evidence is and is not", ("final_report_summary.md",)),
    F("survey", "Market survey (real recorded books)", ("survey_20261022.md",)),
    F("bt_candles_30d", "Backtest: 1-min candles, 30 days, S2+S3 (SYNTHETIC)", ("bt_candles_30d/report.md",)),
    F("bt_candles_real", "Backtest: 1-min candles, all recorded real candles (2.25 h), S2+S3",
      ("bt_candles_recorded_ai_off/report.md",)),
    F("bt_books_real_off", "Backtest: all recorded real order books (0.32 h), S1+S2, ai=off",
      ("bt_books_recorded_ai_off/report.md",)),
    F("bt_books_real_on", "Backtest: all recorded real order books (0.32 h), S1+S2, ai=on (real LLM)",
      ("bt_books_recorded_ai_on/report.md",)),
    F("bt_books_24h", "Backtest: 24 h order books, S1+S2, ai=off (SYNTHETIC)", ("bt_books_synth24h_ai_off/report.md",)),
    F("bt_books_6h_on", "Backtest: 6 h order books, S1+S2, ai=on with the real LLM (SYNTHETIC)",
      ("bt_books_synth24h_6h_ai_on/report.md",)),
    F("ablation", "AI ablation (ai=on − ai=off)", ("ablation_20261023.md",)),
    F("wf", "Walk-forward: S1 margin grid, S1+S2 (SYNTHETIC)", ("walkforward_s1_margin/walkforward.md",)),
    F("wf_s1only", "Walk-forward: S1 margin grid, S1 only (SYNTHETIC)", ("walkforward_s1_margin_s1only/walkforward.md",)),
    F("shadow", "Shadow (paper) run: 4 h, S1+S2, AI on (SYNTHETIC feed)", ("shadow_20261023_synth4h/report.md",)),
    F("live", "Live challenge run", ("live_*/report.md", "live_*.md"), status_if_missing="no live run yet",
      text_if_missing=LIVE_TEXT),
    F("replay", "Replay proof (the 4-h shadow run)", ("replay_20261023.md",)),
]


def main(argv: list[str] | None = None) -> int:
    print(f"index:  {update_index(REPORTS)}")
    print(f"report: {assemble_final_report(REPORTS, FINAL, SECTIONS, title=TITLE)}")
    sys.path.insert(0, str(ROOT))
    from scripts.make_deliverables import main as deliverables_main

    return deliverables_main(["--index-only"])


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

"""Replay (PRD-05 §6): prove the delivered code reproduces the delivered decisions.

* ``wta.exchange.replay.ReplayExchange`` answers the engine with the recorded exchange answers.
* ``wta.replay.compare.compare_decisions`` compares the replayed decision log with the recorded one
  and builds the ``ReplayReport`` (``"N/N decisions reproduced"``, ``docs/REPORTS/replay_<date>.md``).
"""

from wta.replay.compare import (
    FIELDS,
    IGNORE,
    Mismatch,
    ReplayReport,
    compare_decisions,
    report_path,
)

__all__ = ["FIELDS", "IGNORE", "Mismatch", "ReplayReport", "compare_decisions", "report_path"]

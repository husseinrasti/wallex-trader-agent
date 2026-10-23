# Walk-forward: `strategies.s1_crossbook.margin_min_pct`

> **SYNTHETIC DATA — not evidence of performance.** The grid was evaluated on generated data (SYNTHETIC — not evidence of performance).

- In-sample (first 70% of the data ticks): 2026-10-01T00:00:00.000Z → 2026-10-01T16:47:58.000Z
- Out-of-sample (the rest): 2026-10-01T16:48:00.000Z → 2026-10-02T00:00:00.000Z
- Choice: highest in-sample return (tie: lower in-sample drawdown). Guard: accept only if the out-of-sample return **per day of data** does not degrade more than 50% vs in-sample and is not negative, and only if the in-sample return is positive; otherwise keep the pre-grid default. Each half is a fresh paper account.
- End of every run (both halves of each candidate and the final run): no end-of-window flatten; holdings at the end are valued at the bid (unrealized), the same rule for the in-sample and the out-of-sample half.
- Data time: in-sample 16.80 h, out-of-sample 7.20 h.

| candidate | IS return | IS / day | IS max DD | IS trades | OOS return | OOS / day | OOS max DD | OOS trades | |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---|
| margin_min_pct=0.0008 | +0.011% | +0.016% | 0.218% | 1 | +0.000% | +0.000% | 0.000% | 0 | chosen |
| margin_min_pct=0.0012 | +0.000% | +0.000% | 0.000% | 0 | +0.000% | +0.000% | 0.000% | 0 |  |
| margin_min_pct=0.0015 (default) | +0.000% | +0.000% | 0.000% | 0 | +0.000% | +0.000% | 0.000% | 0 |  |
| margin_min_pct=0.0025 | +0.000% | +0.000% | 0.000% | 0 | +0.000% | +0.000% | 0.000% | 0 |  |

**Decision: REJECTED.** Chosen in-sample: margin_min_pct=0.0008. out-of-sample return per day +0.0000% degrades 100% vs in-sample +0.0163% (> 50%); the pre-grid default is kept. Final parameters: margin_min_pct=0.0015.

_Returns are fractions of the starting equity of each half (shown ×100). Few trades in a half make this comparison weak; read the trade counts first._

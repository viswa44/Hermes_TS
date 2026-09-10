# B06 QA-001 validation report -- FAIL

Completed: 2026-09-08T09:21:00+05:30

## Scope and safety review

The live collector invokes only the read-only OpenAlgo expiry, quotes,
option-chain, and option-Greeks data paths. Its source imports neither order,
account, position, nor WebSocket APIs. Raw provider OI is inserted into the
append-only `option_snapshot.oi` field. `option_greeks_snapshot` has no `oi`
column, so raw OI is not duplicated into calculated metrics.

## Regression evidence

- `PYTHONPATH=/Users/viswatej/Desktop/openalgo .venv/bin/python -m pytest -q
  tests/test_b04_option_metrics.py` -- **7 passed**.
- `PYTHONPATH=/Users/viswatej/Desktop/openalgo .venv/bin/python -m pytest -q
  tests` -- **31 passed**.
- The focused tests cover `oi`, `open_interest`, and absent OI remaining NULL.

## Live PostgreSQL evidence

At 09:20:10 through 09:20:30 IST, five consecutive clock boundaries each
contained two `VALID` raw ATM rows (CE and PE), non-NULL raw provider OI, and
separately joined derived IV. The active LaunchAgent collector was running
with its five-second interval.

However, the independent continuity query over the active session found 68
persisted cycles from 09:15:10 to 09:20:55 IST and one non-five-second gap:

| Previous cycle IST | Next cycle IST | Observed gap |
| --- | --- | ---: |
| 09:15:10 | 09:15:25 | 15 seconds |

This is two missed five-second collection intervals. Therefore the required
fresh CE/PE OI persistence every five seconds has not been demonstrated for
the current live run.

## Verdict

**FAIL** -- no QA fixes made. Return B06 to BUILD-001 to investigate and
regression-protect the startup/session continuity gap, then submit a new
BUILD handoff for an independent QA retry.

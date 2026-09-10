# Q04 QA-001 report — PASS

Verified: 2026-09-07T08:21:51Z

## Scope and safety checks

- Static inspection found the B04 runner calls only OpenAlgo `expiry`,
  `quotes`, `optionchain`, and `optiongreeks` data endpoints.
- No order, position, account, or WebSocket invocation is present in the B04
  collector or storage path.
- The 5- and 10-second cadence is explicitly constrained; the five-second
  runner disables retries, keeping its two Greek calculations per cycle within
  the 30/minute endpoint limit.

## Test evidence

- Focused B04 tests: 6 / 6 passed.
- Full available Hermes suite: 22 / 22 passed independently.
- The writer performs transaction-scoped append-only `INSERT`s. Its primary
  keys prevent normal overwrite, and calculated rows have a versioned identity.

## Live PostgreSQL evidence

Checked capture window: 13:50:55–13:51:50 IST.

- `market_snapshot`: 12 rows across 12 timestamps.
- `option_snapshot`: 24 rows, exactly two ATM CE/PE raw quotes per timestamp.
- `option_greeks_snapshot`: 24 rows, exactly two complete calculated records
  per timestamp.
- Every capture timestamp was five-second aligned; no missed interval occurred.
- All 60 B04 rows had `ingestion_time >= timestamp_ist`.
- No raw LTP was missing or non-positive.
- `option_snapshot` has no IV/delta/gamma/theta/vega values, while the separate
  calculated table has IV, delta, gamma, theta, vega, rho, and
  `OPENALGO_BLACK76` provenance. This confirms raw/derived separation.

## Verdict

No blocking defect found. This PASS covers the bounded live ATM CE/PE collector
only; multi-strike collection and long-duration operational monitoring remain
outside B04.

PASS

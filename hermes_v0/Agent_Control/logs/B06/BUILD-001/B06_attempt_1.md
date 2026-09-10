# B06 BUILD-001 work log — NIFTY option OI capture and PostgreSQL verification

Started: 2026-09-07T09:11:25Z

## Approved scope

Verify and regression-protect raw NIFTY ATM CE/PE OI capture in the approved
read-only B04 collector. Raw OI belongs in `option_snapshot.oi`; it must not
be copied into the separately-derived IV/Greeks table. Support both OpenAlgo
payload spellings, preserve absent OI as NULL, and provide a safe operator
query. No order, position, account, or WebSocket endpoint is permitted.

## Implementation

- The live B04 adapter already normalizes the OpenAlgo option-chain `oi` or
  `open_interest` field into `OptionSnapshot.oi`.
- The B04 PostgreSQL writer already inserts that raw field into
  `option_snapshot.oi` on every five-second CE/PE collection cycle.
- Added regression coverage for both provider field names, absent-OI-to-NULL
  handling, and the writer's OI insert position. No schema migration or
  duplicate derived-data column was needed.

## Live PostgreSQL evidence

At 14:51:50 IST, the active LaunchAgent wrote the current ATM pair:

| Option | Raw LTP | Raw OI | IV |
| --- | ---: | ---: | ---: |
| NIFTY 23750 CE | 108.60 | 8,338,590 | 13.34 |
| NIFTY 23750 PE | 37.05 | 18,469,295 | 13.35 |

At 14:55:05 IST, a later verification recorded CE OI `8,375,575` and PE OI
`19,227,195`, with both rows `VALID` and their matching IV/Greeks present.
The ten-minute continuity query found 120 cycles with exactly two CE/PE raw
option rows and two non-NULL OI values in every cycle; zero cycles had missing
OI.

## Operator query

```zsh
/Applications/Postgres.app/Contents/Versions/14/bin/psql -d hermes -P pager=off -c "
WITH latest AS (SELECT max(timestamp_ist) AS ts FROM option_snapshot)
SELECT to_char(o.timestamp_ist AT TIME ZONE 'Asia/Kolkata','YYYY-MM-DD HH24:MI:SS') AS recorded_ist,
       o.symbol AS underlying, o.strike, o.option_type, o.ltp AS raw_ltp, o.oi AS raw_oi,
       g.implied_volatility AS iv, g.delta, g.gamma, g.theta, g.vega, g.rho, o.data_status
FROM option_snapshot o
LEFT JOIN option_greeks_snapshot g
  ON g.timestamp_ist = o.timestamp_ist
 AND g.underlying_symbol = o.symbol
 AND g.strike = o.strike
 AND g.option_type = o.option_type
 AND g.expiry_date = o.expiry_date
WHERE o.timestamp_ist = (SELECT ts FROM latest)
ORDER BY o.option_type;"
```

## Tests run

- `PYTHONPATH=/Users/viswatej/Desktop/openalgo .venv/bin/python -m pytest -q
  tests/test_b04_option_metrics.py` — **7 passed**.
- `PYTHONPATH=/Users/viswatej/Desktop/openalgo .venv/bin/python -m pytest -q
  tests` — **31 passed**.

No database schema change was required: `option_snapshot.oi` already exists
and is the correct immutable raw-observation home for provider OI. The derived
`option_greeks_snapshot` intentionally remains free of duplicated raw OI.

Result: HANDOFF_READY

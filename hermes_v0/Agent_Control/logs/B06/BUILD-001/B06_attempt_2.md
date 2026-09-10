# B06 BUILD-001 rework report -- HANDOFF_READY

Completed: 2026-09-08T09:31:45+05:30

## Defect fixed

Q06 correctly found that the collector cancelled an in-flight, valid raw ATM
OI cycle too early. The whole-cycle cutoff was smaller than the five-second
cadence, and then remained too tight for a provider response already in
flight at the boundary.

`collector/live_option_metrics.py` now gives a started, read-only cycle a
strictly bounded 0.5-second completion grace (`interval_seconds + 0.5`). It
does not add retries or call any additional endpoint. The scheduler continues
to calculate subsequent absolute five-second boundaries.

## Files modified

- `hermes_v0/collector/live_option_metrics.py`
- `hermes_v0/tests/test_b04_option_metrics.py`
- this handoff report

## Regression tests

- Focused B04/B06 suite: **8 passed**.
- Full Hermes V0 suite: **32 passed**.
- Added a regression assertion that a five-second raw-OI cycle receives the
  bounded 5.5-second completion window.

## Fresh live evidence

After one controlled restart of the existing read-only LaunchAgent, PostgreSQL
recorded 23 consecutive cycles from 09:29:40 through 09:31:30 IST. Each cycle
had exactly the ATM CE and PE rows, both with non-NULL provider OI. The
continuity query found zero non-five-second gaps. The active collector
continued to use only `/api/v1/quotes`, `/api/v1/optionchain`, and
`/api/v1/optiongreeks`; it did not invoke an order, position, account, or
WebSocket endpoint.

## Operator query

```sql
WITH latest AS (SELECT max(timestamp_ist) AS ts FROM option_snapshot)
SELECT o.timestamp_ist, o.option_type, o.oi AS raw_oi,
       g.implied_volatility, g.delta, g.gamma, g.theta, g.vega, g.rho
FROM option_snapshot o
LEFT JOIN option_greeks_snapshot g
  ON g.timestamp_ist = o.timestamp_ist
 AND g.underlying_symbol = o.symbol
 AND g.strike = o.strike
 AND g.option_type = o.option_type
 AND g.expiry_date = o.expiry_date
WHERE o.timestamp_ist = (SELECT ts FROM latest)
ORDER BY o.option_type;
```

No unresolved B06 defects were observed in the post-fix validation window.
Return to QA-001 for independent Q06 attempt 2.

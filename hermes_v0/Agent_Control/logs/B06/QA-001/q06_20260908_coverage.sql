-- Read-only evidence for the entire 2026-09-08 IST session.
BEGIN READ ONLY;

WITH expected AS (
    SELECT generate_series(
        TIMESTAMPTZ '2026-09-08 09:15:00+05:30',
        TIMESTAMPTZ '2026-09-08 15:29:55+05:30',
        INTERVAL '5 seconds'
    ) AS ts
), actual AS (
    SELECT timestamp_ist AS ts, count(*) AS rows,
           count(DISTINCT option_type) AS sides, count(oi) AS oi_rows
    FROM option_snapshot
    WHERE symbol = 'NIFTY'
      AND timestamp_ist >= TIMESTAMPTZ '2026-09-08 09:15:00+05:30'
      AND timestamp_ist < TIMESTAMPTZ '2026-09-08 15:30:00+05:30'
    GROUP BY timestamp_ist
)
SELECT count(*) AS expected_cycles, count(a.ts) AS observed_cycles,
       count(*) FILTER (WHERE a.rows = 2 AND a.sides = 2 AND a.oi_rows = 2)
           AS complete_oi_cycles,
       count(*) FILTER (WHERE a.ts IS NULL) AS missing_cycles,
       round(100.0 * count(a.ts) / count(*), 2) AS coverage_pct
FROM expected e LEFT JOIN actual a USING (ts);

-- Fixed post-rework boundary; this does not discard subsequent failures.
WITH cycles AS (
    SELECT DISTINCT timestamp_ist AS ts
    FROM option_snapshot
    WHERE symbol = 'NIFTY'
      AND timestamp_ist >= TIMESTAMPTZ '2026-09-08 09:29:40+05:30'
      AND timestamp_ist < TIMESTAMPTZ '2026-09-08 15:30:00+05:30'
), gaps AS (
    SELECT ts, lag(ts) OVER (ORDER BY ts) AS previous_ts FROM cycles
)
SELECT previous_ts AT TIME ZONE 'Asia/Kolkata' AS previous_ist,
       ts AT TIME ZONE 'Asia/Kolkata' AS next_ist,
       extract(epoch FROM ts - previous_ts) AS gap_seconds
FROM gaps WHERE ts - previous_ts > INTERVAL '5 seconds' ORDER BY ts;

-- Raw OI joined with separately derived IV/Greeks; no duplicated OI column.
SELECT o.timestamp_ist AT TIME ZONE 'Asia/Kolkata' AS recorded_ist,
       o.symbol, o.strike, o.expiry_date, o.option_type, o.ltp, o.oi,
       g.implied_volatility, g.delta, g.gamma, g.theta, g.vega, g.rho,
       o.data_status AS raw_status, g.data_status AS derived_status
FROM option_snapshot o
LEFT JOIN option_greeks_snapshot g
  ON g.timestamp_ist = o.timestamp_ist AND g.underlying_symbol = o.symbol
 AND g.strike = o.strike AND g.expiry_date = o.expiry_date
 AND g.option_type = o.option_type
WHERE o.symbol = 'NIFTY'
  AND o.timestamp_ist = TIMESTAMPTZ '2026-09-08 10:26:15+05:30'
ORDER BY o.option_type;

COMMIT;

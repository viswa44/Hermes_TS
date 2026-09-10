# B05 BUILD-001 work log — weekday option-metrics automation

Started: 2026-09-07T08:34:44Z

## Approved scope

Install and verify a macOS per-user LaunchAgent that starts the approved
read-only NIFTY ATM CE/PE IV/LTP/Greeks PostgreSQL collector Monday through
Friday at 09:15 IST. The existing collector owns market-close shutdown at
15:30. Store the OpenAlgo API key only in the logged-in user's Keychain; never
place it in source, a shell history, a plist, or logs. Retain a checkable local
runtime status. No order, position, account, or WebSocket endpoint is allowed.

## Implementation

- Added `automation/run_b04_option_metrics.zsh`, the sole LaunchAgent entry
  point. It reads the OpenAlgo API key only from the logged-in user's macOS
  Keychain (`com.openalgo.hermes-v0.option-metrics`), sets the bounded NIFTY
  collector configuration, and executes only
  `hermes_v0.collector.live_option_metrics --interval-seconds 5`.
- Added `automation/com.openalgo.hermes-v0-option-metrics.plist` and installed
  it at `/Users/viswatej/Library/LaunchAgents/` under the same label. Its five
  calendar entries are Weekday 1–5 at 09:15; macOS defines 0 and 7 as Sunday,
  so this is Monday through Friday. `KeepAlive` is false, and the collector's
  existing market-hours guard stops the session at 15:30 IST.
- Added `tests/test_b05_automation.py`, which verifies the weekday schedule,
  Keychain retrieval, local PostgreSQL configuration, the bounded five-second
  runner, and absence of order/position invocations in the launcher.

## Credential and safety verification

- The active OpenAlgo API key was entered through a macOS secure dialog and
  stored in the user's Keychain. It was verified with the read-only expiry
  endpoint. No key was written to source, plist, report, shell history, or
  logs.
- The launcher has no order, position, account, or WebSocket call. It starts
  the approved B04 REST-only collector, which is bounded to NIFTY ATM CE/PE
  observations and PostgreSQL writes.

## Verification evidence

- Installed plist: `plutil -lint` returned `OK`.
- Launcher: `zsh -n automation/run_b04_option_metrics.zsh` passed.
- Hermes test suite: `PYTHONPATH=/Users/viswatej/Desktop/openalgo
  .venv/bin/python -m pytest -q tests` returned `30 passed`.
- The installed LaunchAgent was bootstrapped in the current user's `gui/501`
  domain and directly started with `launchctl kickstart` for an environment
  verification. It reported `state = running`, process id `74343`, and an
  established TCP connection to local PostgreSQL.
- From 14:32:40 through 14:36:00 IST, the LaunchAgent appended 82 valid
  `option_greeks_snapshot` rows across 41 clock-aligned five-second cycles.
  The most recent row at that check was 14:36:00 IST; no non-VALID Greek rows
  occurred. This is an actual LaunchAgent/Keychain/OpenAlgo/PostgreSQL write,
  not a terminal-only collector run.

## Operator checks

Use the current user's LaunchAgent status and the most recent database rows:

```zsh
launchctl print "gui/$(id -u)/com.openalgo.hermes-v0-option-metrics" | head -30
/Applications/Postgres.app/Contents/Versions/14/bin/psql -d hermes -P pager=off -c "
SELECT to_char(timestamp_ist AT TIME ZONE 'Asia/Kolkata','YYYY-MM-DD HH24:MI:SS') AS recorded_ist,
       option_symbol, option_ltp, implied_volatility, delta, gamma, theta, vega, rho, data_status
FROM option_greeks_snapshot
ORDER BY timestamp_ist DESC, option_type LIMIT 10;"
```

The Mac must be powered on and the user logged in for a per-user LaunchAgent
to start at 09:15. If it wakes after that time, launchd starts a missed calendar
event once on wake. The schedule is Monday–Friday as requested; it does not
yet carry a separate NSE holiday calendar.

Result: HANDOFF_READY

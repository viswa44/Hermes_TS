# Operations dashboard

Open [http://127.0.0.1:8765](http://127.0.0.1:8765) on this Mac. The separate dashboard remains available on market holidays and refreshes its view every 30 seconds.

The view shows today's gateway decision, next expected session, upcoming official holidays, calendar freshness, collector/watchdog/cleaner startup checks, and historical export evidence. Select a date to inspect raw PostgreSQL counts, accepted and quarantined rows, collection gaps, both cleaned-table previews and the saved S3 publication link. The calendar banner always describes today; the session selector controls historical data separately.

Table 1 contains timestamps, spot, supplied IV and volume with identity/provenance. Table 2 contains raw OI, LTP and contract fields alongside calculated days to expiry and optional Greeks. Receipt times are displayed in IST with milliseconds; hovering retains the original stored timestamp. For the current PostgreSQL flow, IV and Greeks remain null because verified raw IV is unavailable. Cleaning PASS does not imply complete receipt coverage or verified provider timestamps.

## Run

From the workspace root, reuse `data_cleaning_agent/.venv` with the cleaner and market-calendar dependencies installed:

```bash
# Foreground development instance on a separate local port.
data_cleaning_agent/.venv/bin/python -m operations_dashboard.server --port 8766

# Local files only, without PostgreSQL queries.
data_cleaning_agent/.venv/bin/python -m operations_dashboard.server --port 8766 --no-database

# Install the normal dashboard and gateway schedules.
data_cleaning_agent/.venv/bin/python -m market_calendar.install_launchagent --install
```

The installed `com.openalgo.operations-dashboard` LaunchAgent serves port 8765 at login and restarts on failure. Logs are under ignored `operations_dashboard/runtime/`. It binds only to `127.0.0.1`; no remote/public endpoint is deployed.

## Evidence and access

GET `/api/status` reads cached calendar, local collector status and cleaning manifests. GET `/api/day?date=YYYY-MM-DD&limit=5` previews the first 1–10 rows of each matching local Parquet export. GET `/api/database?date=YYYY-MM-DD` performs bounded parameterized PostgreSQL SELECTs in a read-only transaction; results are cached for 30 seconds. `/health` checks the HTTP service only.

S3 publication and checksum labels use saved evidence from prior successful uploads and verification. This view does not contact AWS, Mistral or market-data providers. AWS links open the signed-in user's AWS console. PostgreSQL counts cover the full selected IST date; export counts and coverage describe the regular collection window. Evidence timestamps distinguish historical state from current activity.

The server exposes fixed routes and public fields, rejects nonlocal Host/Origin headers and write requests, refuses symlinked evidence paths, and keeps errors free of credentials. Static assets use no third-party services. Calendar refresh and job control are handled by their own modules; the dashboard reads their evidence.

```bash
data_cleaning_agent/.venv/bin/python -m pytest operations_dashboard/tests -q
```

Tests cover read-only SQL and date bounds, revision matching, preview limits, missing data, error redaction, local HTTP routes and access boundaries. See [gateway verification](../market_calendar/VERIFICATION.md) for the installed system and browser checks.

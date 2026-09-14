# Market calendar gateway

The gateway starts collection and cleaning only on a normal NSE F&O trading weekday with fresh official calendar evidence. It runs before credentials, provider connections, database work, Mistral or S3 publication. The calendar refresher and local dashboard stay available on holidays.

## Official evidence

`refresh.py` downloads and parses the [2026 annual NSE F&O circular, NSE/FAOP/71777](https://nsearchives.nseindia.com/content/circulars/FAOP71777.pdf) and reconciles it with the [current NSE trading-holiday feed](https://www.nseindia.com/api/holiday-master?type=trading), using its `FO` segment. Both sources are required: the live feed can add closures announced after the annual circular. The live 2026 feed includes 20 dates, including the January 15 election closure.

Downloaded sources are archived with SHA256 hashes under ignored `runtime/sources/`. Date/weekday, year, segment, complete annual coverage and source hashes are checked before an atomic cache replacement. Only official NSE HTTPS hosts are accepted, with certificate verification, bounded response sizes, timeouts and retries. The gateway itself reads local files and makes no network calls.

| Decision | Market job behavior |
| --- | --- |
| `OPEN` | May start within its configured time window |
| `CLOSED` | Skip weekend or known NSE holiday |
| `UNKNOWN` | Block missing, corrupt, unsupported-year or stale evidence |
| `OUTSIDE_SESSION` | Collector/watchdog wait for 09:15–15:30 IST |
| `WAITING_FOR_CLEANING` | Scheduled cleaning waits until 15:45 IST |

Cache freshness expires after 48 hours. Failed refreshes preserve the last valid cache without extending its expiry. A known closure remains blocked when stale; other stale weekdays become `UNKNOWN`. Future `next_open_date` values are forecasts, not permission to run without a fresh check on that day. The refresher discovers next year's official annual circular and requires a complete matching API year; it never reuses one year's holidays for another.

Weekend and Muhurat sessions require a separate explicit schedule and are not automatically enabled. The 8 November 2026 Diwali entry therefore does not start the normal weekday jobs. An annual/API disagreement blocks replacement and needs review.

## Install and operate

Run from `/Users/viswatej/Desktop/openalgo`. The existing Hermes environment runs collection; the cleaner environment runs calendar refresh and the dashboard. Gateway decisions require only Python's standard library.

```bash
uv pip install --python data_cleaning_agent/.venv/bin/python -r market_calendar/requirements.txt

# Fetch and validate official evidence now.
data_cleaning_agent/.venv/bin/python -m market_calendar.refresh

# Install the gateway into collector/watchdog, plus refresher and dashboard.
data_cleaning_agent/.venv/bin/python -m market_calendar.install_launchagent --install

# Install the cleaning schedule through the same gateway.
data_cleaning_agent/.venv/bin/python -m data_cleaning_agent.automation.install_launchagent --install

# Read current calendar state without starting market jobs.
data_cleaning_agent/.venv/bin/python -m market_calendar.gateway --json --status

# Inspect a date. Exit 0 = open, 10 = closed, 11 = unknown.
data_cleaning_agent/.venv/bin/python -m market_calendar.gateway --date 2026-09-14 --json

# Simulate a startup time without executing a command or changing runtime status.
data_cleaning_agent/.venv/bin/python -m market_calendar.run_guarded cleaner --check-only --at 2026-09-14T15:45:00+05:30
```

The installer updates only its named LaunchAgents and saves previous plists beside them as timestamped backups. A failed replacement restores the previous plist. No secrets are written into launch definitions. Reinstall after moving the workspace. `MARKET_CALENDAR_CACHE_DIR` optionally selects another cache for controlled deployments/tests; every component must share the same setting.

| LaunchAgent | Schedule in this Mac's Asia/Kolkata timezone |
| --- | --- |
| `com.openalgo.market-calendar` | Daily 06:00, every 6 hours, and login |
| `com.openalgo.hermes-v0-option-metrics` | Monday–Friday 09:15; collector stops by 15:30 |
| `com.openalgo.hermes-v0-watchdog` | Gateway check every 20 seconds; market checks only during an allowed session |
| `com.openalgo.data-cleaning-agent` | Monday–Friday 15:45; login/30-minute retries only after 15:45 on an open day |
| `com.openalgo.operations-dashboard` | Always available while this user is logged in; restart on failure |

The small gateway may run on a holiday and record a skip; downstream collection, watchdog checks and cleaning never start. The collector also rechecks calendar permission during its processing loop. Manual collector/replay and cleaning commands share the calendar boundary. Read-only status commands remain available. This does not shut down the independent OpenAlgo web application or PostgreSQL server.

The Mac must be awake with this user logged in for exact-time execution. macOS calendar triggers follow the machine timezone; Python guards always enforce IST. If the timezone is changed, update the macOS schedules accordingly. Login/wake can retry missed work on the next permitted day after 15:45. LaunchAgent registration and offline checks do not prove a future full market session.

## Lookup and verification

Open the [operations dashboard](http://127.0.0.1:8765). Local evidence is in `runtime/calendar-YYYY.json`, `refresh_status.json`, and `gate-collector.json`, `gate-watchdog.json`, `gate-cleaner.json`. Known holiday skips exit successfully in normal scheduled wrappers; an unknown calendar remains visibly blocked in these files. `refresh.out.log` and `refresh.err.log` record refresh service output.

```bash
data_cleaning_agent/.venv/bin/python -m pytest market_calendar/tests operations_dashboard/tests -q
```

Integration tests use isolated synthetic caches and reject unexpected network access. They verify holidays, absent/stale evidence, public entrypoints, loop guards, no early scheduled cleaning, and no bypass through historical replay. See [VERIFICATION.md](VERIFICATION.md) for installed-service and live official-source evidence.

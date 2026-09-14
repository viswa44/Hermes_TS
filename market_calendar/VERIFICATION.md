# Holiday gateway and operations dashboard — 2026-09-13

The gateway is installed in the collector, watchdog and cleaning LaunchAgents. The official calendar refresher and dashboard are also installed. These are macOS user services in the current GUI session, not a cloud deployment.

## Official-source verification

A real verified-TLS refresh completed at `2026-09-13T10:16:26Z` (15:46 IST). The annual [NSE/FAOP/71777 circular](https://nsearchives.nseindia.com/content/circulars/FAOP71777.pdf) and [current NSE trading-holiday API](https://www.nseindia.com/api/holiday-master?type=trading), segment `FO`, were downloaded, parsed, reconciled and archived. The cache contains 20 holiday dates for 2026 and expires 48 hours after this fetch unless refreshed again.

Monday, **2026-09-14**, is Ganesh Chaturthi. Read-only startup checks at Monday's 09:15 collector/watchdog window and 15:45 cleaning window all returned `CLOSED`, `allowed=false`. Tuesday, **2026-09-15**, returned `OPEN` at those windows with the currently fresh cache. These use injected future clocks only; no future session or upload has occurred. At actual startup the calendar is checked again.

The refresher returned `ANNUAL_CIRCULAR_NOT_AVAILABLE` for 2027. No 2027 calendar was fabricated; that year remains blocked until both official annual and current API evidence are available. Muhurat/weekend sessions do not automatically authorize ordinary weekday jobs.

Local source archives, hashes, cache and refresh result are under ignored `market_calendar/runtime/`. `startup_verification.json` contains the six read-only Monday/Tuesday decisions with their source and freshness timestamps.

## Installed services

`launchctl print` confirmed these five registered services, with their installed `ProgramArguments` inspected:

| Label | Installed command and schedule | Observed status |
| --- | --- | --- |
| `com.openalgo.hermes-v0-option-metrics` | `market_calendar.run_guarded collector`, M–F 09:15 | Triggered once on Sunday; gateway exited 0 without starting collection |
| `com.openalgo.hermes-v0-watchdog` | `market_calendar.run_guarded watchdog`, every 20 seconds | Sunday gate checks exit 0; downstream watchdog stays stopped |
| `com.openalgo.data-cleaning-agent` | `market_calendar.run_guarded cleaner`, M–F 15:45, login and 30-minute retry | Sunday gate exited 0; no cleaning or upload started |
| `com.openalgo.market-calendar` | `market_calendar.refresh`, daily 06:00, six-hour interval and login | Official refresh completed, exit 0 |
| `com.openalgo.operations-dashboard` | `operations_dashboard.server --port 8765`, login and restart on failure | Running; HTTP health returned 200 |

The Mac's timezone resolves to `Asia/Kolkata`. Previous LaunchAgent files were retained as timestamped backups. Fresh `gate-collector.json`, `gate-watchdog.json`, and `gate-cleaner.json` all record Sunday `CLOSED`, `allowed=false`. No raw data or S3 objects were changed by these gateway checks.

## Automated tests

```text
data_cleaning_agent/.venv/bin/python -m pytest market_calendar/tests operations_dashboard/tests -q
135 passed

data_cleaning_agent/.venv/bin/python -m pytest data_cleaning_agent/tests -q
239 passed, 1 warning

HERMES_RUN_DB_TESTS=0 PYTHONDONTWRITEBYTECODE=1 hermes_v0/.venv/bin/python -B -m pytest -p no:cacheprovider -q hermes_v0/tests --ignore=hermes_v0/tests/test_recovery_postgres.py
85 passed
```

Total: **459 passed**. The one warning is the existing NumPy timedelta deprecation in a deliberately altered timestamp test. `uv pip check` found all installed cleaner-environment packages compatible; `git diff --check` passed.

Gate integration tests isolate synthetic calendar archives and prohibit network access. They verify holiday/missing/stale decisions before credentials, locks, database/provider constructors, Mistral and upload; public manual/replay entrypoints; calendar checks inside collector processing; the 15:45 scheduled cutoff; source-day holiday filtering; and future-clock checks that cannot launch a job. Installer tests cover command wiring and restoration of a previous plist after a failed bootstrap.

## Dashboard verification

The dashboard is available at [http://127.0.0.1:8765](http://127.0.0.1:8765). Live HTTP requests returned 200 for health, status, date selection, Parquet previews and the read-only PostgreSQL summary. Friday's summary returned 8,948 option rows and 4,474 market rows; the two matching cleaned Parquet tables each contain 8,948 rows. Selecting Monday's holiday returned `CLOSED` and no export preview.

Chromium 130 browser checks passed at 1440×1100 and 390×844. They exercised both table tabs, live receipt counts, date selection, the holiday empty state, IST timestamp display and gateway badges. There were no JavaScript/console errors or horizontal page overflow. Screenshots were inspected and are saved locally as `operations_dashboard/runtime/dashboard-desktop.png` and `dashboard-mobile.png`; `browser_verification.json` records the checks. Browser tooling is a development-only dependency; this Mac's older OS required Playwright 1.48.

The dashboard labels S3 checksum evidence as historical and makes no AWS calls. It preserves Friday's 26 missing receipt slots, `PARTIAL`/unverified provider timestamps, and null IV/Greeks alongside the cleaning PASS. Prior PostgreSQL-to-S3 publication and downloaded checksum results remain documented in [the cleaning verification](../data_cleaning_agent/VERIFICATION.md).

These tests and installed-service checks establish implementation and current local operation. They do not establish a future full-session collection, live Mistral authentication, gap-free provider coverage or QA promotion. Exact-time runs require the Mac awake, this user logged in, fresh calendar evidence and the normal database/provider/AWS prerequisites.

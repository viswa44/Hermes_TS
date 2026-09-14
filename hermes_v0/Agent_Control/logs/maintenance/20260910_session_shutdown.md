# Weekday collection schedule and final-slot shutdown

- Task ID: `SESSION-SHUTDOWN-20260910`
- Agent ID: Codex workspace maintenance (implementation), with independent read-only peer review
- Start: 2026-09-10 19:32 IST
- End: 2026-09-10 19:43 IST
- Status: `LOCAL_VALIDATION_PASS`; next live session remains unverified
- Handoff: existing weekday LaunchAgent loads the updated collector at its next scheduled start. No B06/Q06 queue transition or promotion was performed.

## User requirement and dashboard explanation

Automatic Hermes fetching and persistence must run on weekdays from 09:15 to 15:30 IST. The screenshot shows pgAdmin's server/database activity dashboard. Its sessions, commits, returned rows, and cache reads include database browsing and monitoring; they do not independently prove new Hermes captures.

At inspection time it was Thursday evening, after market close. The collector was stopped, its LaunchAgent was not running and had exited with code 0, and the watchdog reported `MARKET_CLOSED`. The old collector PID 87274 no longer existed. A later connection inspection showed six idle pgAdmin connections plus this read-only inspection's psql connection.

## Schedule verified

- Installed collector LaunchAgent: weekdays 1 through 5 at 09:15; `KeepAlive=false`.
- macOS local time: IST. The installed launchd calendar uses the machine's local time.
- Runtime `session_time`: converts to `Asia/Kolkata`, allows Monday through Friday and 09:15 inclusive to 15:30 exclusive.
- Collector interval: five seconds. The final scheduled slot is 15:29:55.
- Watchdog exits its off-market check before provider or PostgreSQL access; ordinary off-market collector startup does likewise.
- Both repository plist files and launcher shell syntax validate. The schedule definitions were already correct.

## Live evidence for 2026-09-10

Read-only PostgreSQL and SQLite checks found:

| Measure | Result |
| --- | --- |
| First raw receipt | 09:15:10.516617 IST |
| Last raw receipt persisted in PostgreSQL | 15:29:50.343222 IST |
| Last raw PostgreSQL save timestamp | 15:29:51.506692 IST |
| Last derived PostgreSQL save timestamp | 15:29:53.117453 IST |
| Raw receipts persisted | 4,473 |
| Raw option rows persisted | 8,946 |
| Derived receipts persisted | 8,732 |
| Expected five-second session slots | 4,500 |
| Slots with a PostgreSQL raw receipt | 4,473 (99.4%) |
| Slots without a PostgreSQL raw receipt | 27 |
| Captures retained by the collector | 4,474 |
| Pending local raw observation | One, scheduled at 15:29:55 and received at 15:29:55.342217 IST |
| Receipt receive/save timestamps outside the session window | Zero for today's RAW and DERIVED receipts |

One of the 27 slots missing from PostgreSQL is buffered locally. The other 26 slots lack captures. SQLite records rejected captures and missed/skipped intervals; this maintenance does not fabricate replacement observations or claim complete coverage. The startup normally selects the next five-second boundary, so exact 09:15:00 coverage is not established.

## Defect and change

The scheduler ends immediately after its 15:29:55 tick because the next boundary is outside market hours. Previously, `RecoveryService.run()` immediately canceled the database worker and final Greeks job, then closed the writer. If the worker was sleeping or awaiting acknowledgment, the final durable raw capture remained pending in SQLite.

Shutdown now cancels and awaits the background database worker before reusing the writer. It flushes durable raw observations first, then lets the existing final Greeks task finish and drains any remaining records. This has a maximum five-second budget, further restricted to the time remaining before 15:30 with a 250 ms cancellation reserve. It starts no shutdown flush outside the weekday session. A deadline or database failure leaves unacknowledged records available for exact replay, and the status includes a `shutdown_flush` result.

The deadline is a best-effort asynchronous cancellation boundary, not proof of a hard server-side commit cutoff under every failure. Existing idempotent receipt handling protects ambiguous commits during replay. This change covers automatic session shutdown; explicit administrative replay/history modes retain their existing behavior.

## Validation

- New final-session regression fails against the original shutdown implementation and passes with the fix.
- Regression covers both a sleeping worker and an active worker interrupted after a simulated commit but before acknowledgment. Replay preserves payload bytes and does not duplicate the simulated committed observation.
- Local tests cover raw persistence before optional Greeks, weekday pre-open/after-close and weekend guards, deadline cancellation, and database-failure evidence preservation.
- Full local Hermes suite: **85 passed**, excluding the opt-in PostgreSQL integration test.
- Full-suite validation exposed an existing date-dependent B04 mock: its September 7 fixture selected a different expiry after September 9. The test now freezes the adapter clock to its fixture date. Production expiry selection was not changed.
- Independent read-only review found no material issues in the final code/test changes.
- `git diff --check`, both plist validations, and launcher shell syntax checks passed.
- At 19:42 IST, PostgreSQL still contained 4,473 raw receipts for today, and SQLite still retained the one pending raw observation. All live database access during maintenance was read-only; no provider calls, replay, collector start, or database migration was performed.
- Orchestrator state, queue, and registry were unchanged. This is local maintenance validation, not a Q06 PASS or full-session live approval.

## Artifacts and follow-up

- `hermes_v0/collector/recovery.py`
- `hermes_v0/tests/test_integrity_recovery.py`
- `hermes_v0/tests/test_b04_option_metrics.py`
- This report

Next scheduled start: Friday, 2026-09-11 at 09:15 IST, subject to the Mac/user session and existing OpenAlgo/Fyers/PostgreSQL prerequisites. The normal recovery worker can replay the preserved observation during that session while retaining its original capture timestamps. Verify `shutdown_flush`, pending-buffer counts, and PostgreSQL receipts after the next close before calling the live shutdown fix proven.

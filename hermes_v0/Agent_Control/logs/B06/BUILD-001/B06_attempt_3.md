# Controller handoff

Created: 2026-09-08T13:43:46+00:00

You are BUILD-001.

TASK: B06-FIX-03
Requirement: Verify and regression-protect the approved read-only B04 collector's raw NIFTY ATM CE/PE option-chain OI capture. Persist provider OI only in the append-only PostgreSQL option_snapshot.oi raw-observation field, supporting both oi and open_interest payload names and preserving an absent field as NULL rather than a fabricated zero. Do not duplicate raw OI into the derived Greeks table or call order, position, account, or WebSocket endpoints. Verify fresh live CE/PE OI rows every five seconds and provide the read-only PostgreSQL query that joins OI with the separately-derived IV/Greeks values.

Fix ONLY defects in /Users/viswatej/Desktop/openalgo/hermes_v0/Agent_Control/logs/B06/QA-001/Q06_attempt_2.md. Do not expand scope.

After work, run the B06 tests and report files modified, tests run, and unresolved defects.
Return exactly one terminal status: HANDOFF_READY or BLOCKED.

Save your final report to: /Users/viswatej/Desktop/openalgo/hermes_v0/Agent_Control/logs/B06/BUILD-001/B06_attempt_3.md

---

# B06 attempt 3 — integrity-first recovery BUILD report

task_id: B06-FIX-03
agent_id: BUILD-001
start_time: 2026-09-08T13:43:46+00:00 (controller handoff)
end_time: 2026-09-09T03:24:00+00:00
status: HANDOFF_READY
handoff: QA-001 / Q06 attempt 3; independent QA and market-session evidence required.

## Owner scope revision

The owner subsequently instructed: data loss is acceptable, wrong data is not;
impose this strictly and implement the discussed recovery observations. This
explicitly supersedes the narrow fix-only/zero-gap requirement in the preserved
controller handoff above. The revised requirement is recorded in queue.yaml,
DECISION.md ADR-006, and source_of_truth/INTEGRITY_AND_RECOVERY.md.

This BUILD does not approve itself or revise the historical Q06 FAIL verdict.

## Implemented behavior

- Strict adapter and immutable normalized observation contract: finite positive
  prices, exact expiry/strike/side identity, consistent OI aliases, integral
  lossless counts, crossed/stale/late response rejection. Missing or ambiguous
  zero OI remains NULL; no expiry guessing or synthetic gap filling.
- Unavailable source timestamps never become verified timestamps. Version-2
  rows are PARTIAL with APPLICATION_RECEIPT / UNVERIFIED_PROVIDER_TIME metadata.
  Scheduled, request-start, receipt and persistence clocks remain separate.
  hermes_verified_options intentionally excludes all currently unverified rows.
- Raw pair durably journaled before bounded optional Greeks work. Independent
  database retry/replay; SQLite WAL/FULL outbox retains exact bytes after ACK.
  Conflicting or invalid replay is quarantined, never an overwrite. Full buffer
  refuses new observations. Captured OI is never duplicated into Greeks.
- Greeks retain independent response clocks and requested-contract identity.
  Provider spot_price is retained as calculation_spot_ltp, not relabeled as a
  proven forward_price. Missing explicit forward_price stays NULL.
- Scheduler skips late wakeups; bounded source/database backoff reaches 60s.
  Durable gap windows and incidents are separate from observations. SQL coverage
  reconciliation detects downtime gaps that a dead process cannot log.
- Twenty-second watchdog reads actual PostgreSQL RAW receipt age, sampler
  heartbeat and OpenAlgo health. Local alerts, at most three collector restart
  attempts per IST day, single process lock, and session-only idle-sleep guard.
  No OpenAlgo/PostgreSQL restarts, trading APIs or external messaging.
- Historical exact-symbol five-second candles are operator-requested and stored
  only in hermes_history_backfill, never live tables or reconstructed Greeks.
- PostgreSQL UPDATE, DELETE and TRUNCATE blocked on five observation tables;
  new legacy-version inserts blocked. NOT VALID gates preserve old evidence
  without certifying it and enforce new inserts. Superuser bypass is outside
  these application-integrity safeguards.

## Artifacts changed in this BUILD

- collector/integrity.py; collector/adapters/strict_openalgo.py;
  collector/recovery.py; collector/scheduler.py; collector/live_option_metrics.py.
- storage/recovery_journal.py; storage/recovery_writer.py;
  storage/migrations/20260908_integrity_recovery.sql.
- automation/watchdog.py; automation/com.openalgo.hermes-v0-watchdog.plist;
  automation/run_b04_option_metrics.zsh; .gitignore.
- tests/test_integrity_recovery.py; tests/test_recovery_postgres.py;
  tests/test_b05_automation.py (supported CLI routing expectation).
- source_of_truth/INTEGRITY_AND_RECOVERY.md; source_of_truth/DECISION.md;
  Agent_Control/queue.yaml; this BUILD report and controller handoff artifacts.
- Installed per-user LaunchAgent:
  /Users/viswatej/Library/LaunchAgents/com.openalgo.hermes-v0-watchdog.plist.

Existing unrelated staged/untracked user work was preserved; no commit made.

## Tests and observed evidence

Run from hermes_v0:

```zsh
HERMES_RUN_DB_TESTS=1 PYTHONPATH=/Users/viswatej/Desktop/openalgo .venv/bin/python -m pytest -q tests
```

Final suite: 78 passed in 6.05 seconds on 2026-09-09, before the market session.

The PostgreSQL test uses a unique isolated schema and an outer rollback. No
synthetic observation is ever inserted into public tables. It exercised exact
replay, conflicting replay, NULL/zero/NaN/infinite price rejection, old-writer
version rejection, immutable writes including TRUNCATE, derived parent/clock
provenance, NULL forward-price preservation, and historical/live separation.

Behavioral tests cover malformed values and identities, OI alias conflicts,
ambiguous zeros, decimal count precision loss, stale quotes, late scheduler
wakes, calendar/expiry failure, corrupted outbox quarantine, full buffer,
database failure then replay, raw durability before blocked Greeks, gap-window
durability, watchdog restart limits/auth cases, and no pre/post-session I/O.

Additional checks: git diff --check for hermes_v0; zsh -n launcher; plutil -lint
repository and installed watchdog plists; installed plist equals source.

## Deployment evidence — 2026-09-09 08:49–08:53 IST

- Additive migration applied transactionally to local hermes PostgreSQL with
  ON_ERROR_STOP; repeated successfully to verify idempotence. Verified ten
  immutable/no-truncate triggers and three new-version gates installed.
- Public table counts before and after: market_snapshot 1758,
  option_snapshot 3482, option_greeks_snapshot 3482. No old observations changed
  or deleted. New receipt count zero; verified view count zero as expected.
- Watchdog bootstrapped under gui/501. Last inspected run count 9, last exit 0,
  interval 20 seconds; watchdog.json checked_at 2026-09-09T03:22:28.436928+00:00,
  MARKET_CLOSED, starts_today 0. Periodic jobs normally show not running between
  checks; this is not proof of live collection.
- Existing collector LaunchAgent manually kicked once before open: exit 0,
  MARKET_CLOSED, collection_started false. Its existing weekday 09:15 schedule
  still points to the now-updated Keychain-only launcher. --status reports
  NEVER_STARTED because no version-2 market capture has yet begun.
- Read-only unauthenticated OpenAlgo /health/status returned HTTP 200. This is
  reachability evidence only, not broker-login or quote-freshness proof.

## Remaining verification and limitations

LIVE_VALIDATION_REQUIRED. Neither the full 09:15–15:30 session, real broker auth
recovery, real collector kill/restart, nor a real source/database outage during
market capture has been demonstrated by this BUILD. No historical data was
fetched to fill September 8's gaps. Existing legacy evidence is not recertified.

The provider can still supply false values without revealing that fact. Hermes
rejects known-invalid inputs and does not claim unknown provider freshness is
verified. Local notification delivery depends on macOS permissions. Sleep,
power loss and offline sessions can leave gaps. Buffer retention is bounded at
256 MiB payload; archiving acknowledged evidence needs an explicit retention
decision and must not erase unacknowledged records.

QA must independently exercise the revised integrity contract and live data
path. QA must not fix code or call static tests live proof. The controller may
dispatch B07 only after a genuine Q06 PASS; B07 currently remains a placeholder
without substantive acceptance criteria. The controller creates handoffs; an
external agent runner must actually execute them. No background AI runner was
installed or implicitly claimed by this BUILD.

HANDOFF_READY

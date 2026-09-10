# Controller handoff

Created: 2026-09-08T04:02:06+00:00

You are QA-001.

TASK: Q06 attempt 2
Target: BUILD-001 / B06
Requirement: Verify and regression-protect the approved read-only B04 collector's raw NIFTY ATM CE/PE option-chain OI capture. Persist provider OI only in the append-only PostgreSQL option_snapshot.oi raw-observation field, supporting both oi and open_interest payload names and preserving an absent field as NULL rather than a fabricated zero. Do not duplicate raw OI into the derived Greeks table or call order, position, account, or WebSocket endpoints. Verify fresh live CE/PE OI rows every five seconds and provide the read-only PostgreSQL query that joins OI with the separately-derived IV/Greeks values.

Read the latest BUILD report: /Users/viswatej/Desktop/openalgo/hermes_v0/Agent_Control/logs/B06/BUILD-001/B06_attempt_2.md
Read previous QA defects when present: /Users/viswatej/Desktop/openalgo/hermes_v0/Agent_Control/logs/B06/QA-001/Q06_attempt_1.md

Retest all previously failed cases and critical B06 regression cases.
Do not fix anything. Do not begin another task.

Return exactly one terminal status: PASS, FAIL, or BLOCKED.
Include defects, tests run, observed results, and unresolved blockers.

Save your final report to: /Users/viswatej/Desktop/openalgo/hermes_v0/Agent_Control/logs/B06/QA-001/Q06_attempt_2.md

## Q06 attempt 2 final QA report

task_id: Q06 / B06
agent_id: QA-001
audit_date: 2026-09-08 IST (after market close)
start_time: 2026-09-08T19:08:46+05:30
end_time: 2026-09-08T19:13:34+05:30
status: FAIL
handoff: Return B06 to BUILD-001 attempt 3 through the orchestrator.

The owner's request to approve and promote was reviewed against the existing
QA gate. Today's recorded evidence fails the outstanding continuity criterion.
Approval and promotion are therefore not supported. No implementation or
OpenAlgo configuration was changed during QA.

### Expected versus observed

| Case | Expected | Observed | Finding |
| --- | --- | --- | --- |
| Existing regression suite | All checks succeed | 32 passed in 6.19 seconds | Existing checks succeed |
| Raw OI handling | Both field aliases supported; absent values remain NULL | Focused tests and adapter code cover these; 1,622 saved raw rows all have OI | OI mapping retained |
| Raw/derived boundary | Provider OI stays in option_snapshot; IV/Greeks are separate | Writer's append-only transaction inserts raw OI separately; join retrieves the two record types | Boundary retained |
| Post-rework five-second continuity | No missing five-second slots | 649 saved cycles, 17 internal gaps, 31 missed internal slots from 09:29:40 to 10:26:15 | FAIL |
| Deterministic 5.2-second collection | Preserve five-second cadence | Real scheduler yields 10:00:05 then 10:00:15, skipping 10:00:10 | FAIL |
| Deterministic 5.2-second persistence | Slow persistence does not hide cadence loss | Same skipped 10:00:10 boundary; writer is outside collection timeout | FAIL |

### Defects and evidence

1. The `interval_seconds + 0.5` collection allowance does not fix five-second
   continuity. `live_option_metrics.py:321` permits the current cycle to
   extend beyond the next boundary; the scheduler resumes after the awaited
   collection/write and calculates a boundary strictly after its current
   time (`scheduler.py:101`). A 5.2-second successful collection still skips
   the next boundary. Increasing the timeout alone cannot establish cadence.
   The existing added test only checks the numeric timeout with a fake
   scheduler, so it does not exercise this behavior.
2. Raw observations are still committed only after all adapter metric work
   returns (`live_option_metrics.py:332-340`). An outer timeout can discard
   already-received raw OI. The claimed early raw-persistence change was not
   implemented. The database write itself is outside the collection timeout.
3. Recorded live evidence after the final restart confirms continuing gaps,
   including 09:45:30 -> 09:45:45 (15 seconds), 09:48:15 -> 09:48:25
   (10 seconds), and 10:25:40 -> 10:26:05 (25 seconds). The prior 23-cycle
   window was real but did not demonstrate that the defect was resolved.

### Today's collection coverage

The audit queried the local `hermes` PostgreSQL database. The collection is
the NIFTY index plus its ATM CE/PE pair, not all 50 constituent equities.
The configured session is 09:15 inclusive to 15:30 exclusive, so 4,500
five-second slots end at 15:29:55.

- Saved: 811 index rows and 1,622 raw option rows across 811 cycles.
- Recorded range: 09:15:10 through 10:26:15 IST, with internal gaps.
- Coverage: 811 / 4,500 = 18.02%; 3,689 scheduled slots are absent.
- All 1,622 saved raw option rows are VALID and have non-NULL OI.
- Derived rows: 1,622, including 9 whose status is not VALID.
- The final raw CE/PE pair at 10:26:15 has OI 35,249,630 / 36,551,775;
  its separately derived IV/Greeks are MISSING.
- No raw/index/derived rows were saved after 10:26:15. The launcher log
  records TimeoutError at 10:26:30, subsequent ReadTimeout errors, and
  repeated ConnectError beginning at 10:32:35. These establish request
  failures; they do not establish why the upstream service became unreachable.
- LaunchAgent was not running when checked after market close. There is
  no completed 2026-09-08 daemon summary in its stdout log.

### Reproduction and artifacts

From `/Users/viswatej/Desktop/openalgo`:

```zsh
PYTHONPATH=/Users/viswatej/Desktop/openalgo hermes_v0/.venv/bin/python -m pytest -q hermes_v0/tests
PYTHONPATH=/Users/viswatej/Desktop/openalgo hermes_v0/.venv/bin/python hermes_v0/Agent_Control/logs/B06/QA-001/q06_attempt_2_cadence_repro.py
/Applications/Postgres.app/Contents/Versions/14/bin/psql -X -v ON_ERROR_STOP=1 -d hermes -P pager=off -f hermes_v0/Agent_Control/logs/B06/QA-001/q06_20260908_coverage.sql
```

The timing reproduction uses a virtual clock, existing fake adapter/writer,
and the real scheduler and runner. It performs no network or database I/O.
Its three cases observe missed_intervals = [0, 1, 1] and bounded verification
results = [true, false, false]. It reproduces scheduler behavior rather than
real HTTP timing; today's PostgreSQL/log evidence is recorded separately.

BUILD should address the demonstrated cadence/raw-observation-loss defects
and add behavioral regressions before a further QA handoff. The old gaps
must remain auditable; absent observations must not be fabricated or filled
with present-day timestamps. Live verification of a new fix requires an
open session (LIVE_VERIFICATION_REQUIRED). B07 remains pending.

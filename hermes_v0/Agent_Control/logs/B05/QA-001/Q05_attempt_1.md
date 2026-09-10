# Q05 QA-001 audit — B05 weekday option-metrics automation

Audited: 2026-09-07 (IST)  
Target: BUILD-001 / B05 attempt 1

## Scope and separation

Read the controller handoff and the BUILD report. This QA pass made no changes
to the launcher, plist, Keychain, database, or active collector. It covers the
requested per-user macOS automation only; it does not approve trading or any
unrelated future task.

## Acceptance evidence

1. **Weekday 09:15 start — PASS**
   - `plutil -lint automation/com.openalgo.hermes-v0-option-metrics.plist` returned
     `OK`.
   - The source plist exactly matches the installed
     `/Users/viswatej/Library/LaunchAgents/com.openalgo.hermes-v0-option-metrics.plist`.
   - Both contain precisely five `StartCalendarInterval` entries: Weekday 1, 2,
     3, 4, and 5, each at `09:15`; `KeepAlive` is false.
   - `launchctl print gui/501/com.openalgo.hermes-v0-option-metrics` shows all
     five registered calendar triggers and `state = running`.

2. **15:30 close boundary — PASS**
   - Independent scheduler check returned: Monday 09:15 `True`, Monday
     15:29:59 `True`, Monday 15:30:00 `False`, and Saturday 09:15 `False`.
   - The live runner consumes the scheduler tick generator, which exits before
     a boundary outside market hours; therefore the unbounded LaunchAgent run
     stops at the existing 15:30 IST close.

3. **Credential and launcher safety — PASS**
   - `zsh -n automation/run_b04_option_metrics.zsh` passed.
   - The launcher gets the credential only with macOS `security
     find-generic-password` for service
     `com.openalgo.hermes-v0.option-metrics`, supplies it to the child process,
     then unsets the shell variable. The source plist contains no credential.
   - The installed launchd environment exposes only `HOME`, `USER`, and
     `LOGNAME` (plus launchd metadata), not `OPENALGO_API_KEY`.
   - Static artifact checks found no literal OpenAlgo-key assignment in the
     B05 launcher/plist/tests/reports and no Authorization or x-api-key header
     in the B05 logs. No secret was retrieved or displayed during QA.

4. **Read-only endpoint scope — PASS**
   - Static review of the invoked collector/adapter found only OpenAlgo
     `/expiry`, `/quotes`, `/optionchain`, and `/optiongreeks` calls.
   - The launcher has no order, position, account, or WebSocket invocation.
   - The active log records successful `/quotes`, `/optionchain`, and two
     `/optiongreeks` calls per cycle; no prohibited endpoint was observed.

5. **Installed runtime and append-only PostgreSQL proof — PASS**
   - The loaded LaunchAgent reports PID `74343`, `runs = 1`, and `state =
     running`; the process is the approved
     `hermes_v0.collector.live_option_metrics --interval-seconds 5` module with
     parent PID 1.
   - That PID holds the single-collector lock and an established TCP connection
     to local PostgreSQL.
   - Independent two-point database check advanced from 636 rows at 14:38:55
     IST to 642 rows at 14:39:10 IST: three new five-second cycles, two CE/PE
     Greek rows each. The four most recent cycles all had exactly two option
     types and `VALID` status.
   - Continuity query for the active LaunchAgent period (14:32:40–14:39:45
     IST) found 86 cycles / 172 Greek rows, zero malformed cycles, and zero
     non-five-second gaps.

## Tests run

- `PYTHONPATH=/Users/viswatej/Desktop/openalgo .venv/bin/python -m pytest -q tests/test_b05_automation.py` — **2 passed**.
- `PYTHONPATH=/Users/viswatej/Desktop/openalgo .venv/bin/python -m pytest -q tests` — **30 passed**.

## Operational note

This is a per-user LaunchAgent: the Mac must be powered on and the user logged
in for the 09:15 trigger. The requested Monday–Friday calendar schedule does
not encode a separate NSE holiday calendar.

## Verdict

PASS

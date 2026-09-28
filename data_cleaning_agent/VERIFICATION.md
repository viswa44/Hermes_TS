# Enriched PostgreSQL → S3 rebuild — 2026-09-19

All nine available completed sessions were rebuilt and published as new immutable revisions. Direct S3 downloads verified every new Parquet file, CSV companion and quality report against the committed SHA-256 checksums and sizes. All published objects use AES256 encryption. Verification completed at **09:20:47 IST on 19 September 2026**.

| Trading date | Rows per table | Usable IV + Greeks | Missing calculations |
| --- | ---: | ---: | ---: |
| 2026-09-07 | 1,860 | 1,860 | 0 |
| 2026-09-08 | 1,622 | 1,613 | 9 |
| 2026-09-09 | 8,064 | 7,646 | 418 |
| 2026-09-10 | 8,948 | 8,732 | 216 |
| 2026-09-11 | 8,948 | 8,884 | 64 |
| 2026-09-15 | 4,986 | 4,978 | 8 |
| 2026-09-16 | 8,792 | 8,745 | 47 |
| 2026-09-17 | 8,860 | 8,804 | 56 |
| 2026-09-18 | 8,976 | 8,891 | 85 |
| **Total** | **61,056** | **60,153** | **903** |

All rows now have an unambiguous stored contract symbol (directly linked or from an explicitly labeled contract mapping). No RAW rows were rejected. Missing analytics comprise 894 observations with no matching Greek record and nine legacy failed calculations. They remain null with explicit reasons. Provider timestamps remain unknown; model assumptions and input-time limitations remain visible. This is cleaning/publication verification, not a claim of complete collection or approval of a Hermes BUILD/QA stage.

The user's sample retains observation ID `f4fe7f492df9c48c2d2a24f2e067ccde9a51b2642d068697e0153bd3e4c344d8`, RAW receipt **2026-09-18 09:15:05.902530 IST**, RAW LTP **112.65**, and RAW spot **23,344.95**. It now carries IV **0.1055 (10.55%)**, delta **0.5219**, gamma **0.001496**, theta **-12.4511**, vega **10.0545**, and rho **-0.013178**. Availability is independently preserved as **09:15:07.647454 IST**; calculation option price remains separately identified as **112.90**.

## Checks performed

- Full cleaner, market-calendar and dashboard suite, including the opt-in READ ONLY PostgreSQL query test: **443 passed**. One pre-existing pandas/NumPy timedelta deprecation warning remains.
- After adding efficient S3 HEAD checksum verification, focused S3 publication/reuse suite: **135 passed** (includes the new fallback/corruption checks).
- Updated dashboard coverage messaging: **22 dashboard tests passed**; live dashboard reports 8,891 of 8,976 rows with IV/Greeks for 18 September.
- Independent complete 18 September pilot: every original ID and RAW timestamp, price, OI, volume and provenance field unchanged; every CSV cell matched Parquet; all artifact hashes matched.
- Adversarial parent, contract, version, timing and receipt cases rejected analytics while retaining valid RAW observations. Failed legacy calculations remained missing.
- Live S3 verification: nine daily commits, nine manifests and **45 downloaded data/report objects**; row counts and analytics coverage reconciled with PostgreSQL.

Execution used the explicit maintenance entrypoint, with the actual Saturday clock and calendar checks for each source session:

```bash
DAILY_START_DATE=2026-09-07 data_cleaning_agent/.venv/bin/python -m data_cleaning_agent.daily --rebuild-completed --planner deterministic
```

Evidence: `runtime/enrichment_verification_2026-09-19.json` contains exact revision URIs, artifact hashes, counts and upload times. `runtime/progress.json` points to the new revisions. CSV/Parquet files are under each recorded `output/postgres/YYYY-MM-DD/<run-id>/`; older immutable revisions remain available. The normal weekday schedule loads the updated pipeline on its next invocation.

---

# Historical verification — 2026-09-12

The section below records the earlier export implementation before stored-analytics enrichment.

The weekday job is installed and the five existing PostgreSQL dates were cleaned and published to AWS. Original PostgreSQL row counts were unchanged after the exports.

Environment: project `.venv`, Python 3.13.11. Exact installed versions are recorded in `requirements.lock`. `uv pip check` found all 67 installed packages compatible.

```text
.venv/bin/python -m pytest tests -q
239 passed, 1 warning in 10.46s
```

The warning is a pandas/NumPy timedelta deprecation triggered by a validator test that deliberately changes a timestamp. It did not affect test results.

Coverage includes CSV/JSON/JSONL/Parquet input, null and large-integer preservation, duplicate conflicts, dates and provenance, Greek reference values, constrained LangChain/Mistral requests through mocked HTTP, PostgreSQL exports, weekday timing, locks, retries, revision changes, remote commit recovery, S3 quality/checksum gates and conditional encrypted writes.

## Live PostgreSQL and S3 results

Live PostgreSQL connection verification returned `transaction_read_only=on`, `transaction_isolation=repeatable read`, and `timezone=UTC`. Exports joined immutable option records to their matching market snapshots and checked receipt linkage.

| Date | Rows in each S3 table | Quarantined |
| --- | ---: | ---: |
| 2026-09-07 | 1,860 | 0 |
| 2026-09-08 | 1,622 | 0 |
| 2026-09-09 | 8,064 | 0 |
| 2026-09-10 | 8,948 | 0 |
| 2026-09-11 | 8,948 | 0 |
| Total | 29,442 | 0 |

All ten Parquet objects were downloaded from S3 after publication. Their SHA256 hashes matched their manifests, and their actual Parquet row counts matched the table above. Detailed local results are in `runtime/s3_verification.json`.

A second live run completed successfully and reused all five remote commits. S3 contained 25 objects before and after the rerun, with zero new objects. The result is recorded in `runtime/replay_verification.json`.

Bucket: `s3://heremesv0-cleaned-data`, created in the configured AWS region `us-east-1`. Ownership/access were checked with the authenticated account. All four public-access block settings are enabled, object ownership is bucket-owner-enforced, and AES256 default encryption was verified. Objects use conditional writes and encrypted immutable date/revision commits.

The Friday receipt report records 4,474 captured slots out of 4,500, leaving 26 missing. Cleaning PASS does not imply complete collection or verified provider timestamps. V2 `PARTIAL` / `APPLICATION_RECEIPT` / `UNVERIFIED_PROVIDER_TIME` labels remain intact. Legacy timestamps remain explicitly unverified.

Raw PostgreSQL IV is unavailable. Daily table IV and Greek values remain null; days to expiry is calculated. The pipeline does not copy separate provider-calculated Greeks into raw observations.

## Installed schedule

Installed plist: `/Users/viswatej/Library/LaunchAgents/com.openalgo.data-cleaning-agent.plist`.

- Calendar: Monday–Friday 15:45 in this Mac's Asia/Kolkata timezone.
- Additional triggers: user login and every 30 minutes for catch-up/retry.
- Updated 2026-09-13: shared gateway blocks weekends, official NSE holidays and unknown/stale calendars; all scheduled cleaning waits until 15:45 IST.
- First installation on Saturday exited successfully through the weekend guard; `launchctl list com.openalgo.data-cleaning-agent` showed the registered job and exit status 0.
- Updated 2026-09-13: Monday, 2026-09-14 is the NSE Ganesh Chaturthi holiday. The next normal session forecast is Tuesday, 2026-09-15, subject to a fresh calendar at startup. Future execution depends on the Mac running, this user logged in, PostgreSQL availability and AWS connectivity. See [gateway verification](../market_calendar/VERIFICATION.md).

Scheduled cleaning uses the fixed deterministic PostgreSQL plan. LangChain/Mistral remains configurable and is covered by mocked HTTP integration tests; live Mistral authentication was not exercised. No API key was stored in source, plist or logs.

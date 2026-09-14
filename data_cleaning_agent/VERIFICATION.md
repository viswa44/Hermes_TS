# Weekday PostgreSQL → S3 verification — 2026-09-12

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

# ARCH-001 Architecture Audit — V0-001

- **task_id:** V0-001
- **agent_id:** ARCH-001
- **start_time:** 2026-08-15 Asia/Kolkata
- **end_time:** 2026-08-15 Asia/Kolkata
- **status:** HANDOFF_READY (architecture audit only; not a production-readiness approval)
- **artifacts inspected:** source-of-truth documents, Agent_Control state/queue/registry, all Python implementation files, both database DDL files, and repository test/package inventory.
- **artifact produced:** `Agent_Control/logs/ARCH-001_V0-001_architecture_audit_2026-08-15.md`
- **handoff:** DATA-001 for provider verification; BUILD-001 only after the listed architecture/runtime risks are accepted or resolved; QA-001 for adversarial verification.

## Scope and authority

**FACT:** Hermes V0 is specified as a market-data research and evidence system:
`raw observations -> calculated features -> future outcomes -> evidence`.

**FACT:** The stated V0 boundary excludes live/automatic trading, live decisions,
direction scores, automatic rule changes, production Hermes changes, OpenAlgo
changes, and `papertradeone.py` changes.

**FACT:** This audit did not modify application code, database schemas, provider
data, or any production-trading system. The sole write is this requested audit
report in `Agent_Control/logs`.

## Repository inventory

| Area | Files inspected | Current role |
| --- | --- | --- |
| Domain | `domain/models.py` | Frozen snapshot/data models, status enum, version and horizon declarations. |
| Collector | `collector/service.py`, `scheduler.py`, `validator.py`, `adapters/openalgo_adapter.py` | Clock-aligned collection, REST data retrieval, validation, retries, health endpoints. |
| Storage | `storage/writer.py`, `schema.sql`, `schema_pg.sql` | Async PostgreSQL writes and TimescaleDB/PostgreSQL DDL. |
| Configuration | `config/__init__.py`, `config/settings.py` | Two overlapping configuration definitions. |
| Control | `Agent_Control/*.yaml`, `event_logger.py`, `handoff.py`, `orchestrator.py` | Queue and registry exist; the three Python control modules are empty. |
| Verification | repository inventory | No test files, package metadata, dependency manifest, or runnable deployment instructions were found. |

## Architecture and data flow

```text
OpenAlgo REST: /quotes, /optionchain, /optiongreeks
  -> OpenAlgoAdapter
  -> ClockAlignedScheduler (5-second ticks; weekdays, 09:15–15:30 IST)
  -> SnapshotValidator
  -> SnapshotWriter (asyncpg batch/COPY)
  -> PostgreSQL or TimescaleDB

Raw: market_snapshot, option_snapshot
Derived: feature_snapshot
Evidence: future_outcome
Operations: system_event
```

**FACT:** The adapter contains quote, option-chain, and option-Greeks HTTP calls only. It contains no Hermes V0 order-placement call.

**FACT:** `MarketSnapshot` and `OptionSnapshot` are frozen dataclasses. The DDL assigns composite primary keys to raw rows; the normal writer uses COPY inserts rather than updates.

**FACT:** The collector persists a non-null snapshot even when it is marked `REJECTED`, preserving the error payload as evidence. `FeatureSnapshot` and `FutureOutcome` models and writer methods exist, but no feature calculator or outcome-labeling module exists.

## Source-of-truth and implementation reconciliation

### Agreements

- **FACT:** The observation/feature/outcome/evidence layering in source-of-truth agrees with `domain/models.py` and both DDL variants.
- **FACT:** The code inspected does not introduce trade execution, scoring, or automatic strategy/risk changes.
- **FACT:** Source-of-truth correctly labels provider field availability as unverified; DATA-001 is still required to establish actual broker/OpenAlgo coverage.

### Contradictions and material gaps

- **RISK:** `source_of_truth/ARCHITECTURE.md` says the collector runtime references missing drift/backoff settings and the writer reads incompatible pool settings. That is not true for the imported runtime configuration in `config/__init__.py`, which defines the millisecond backoff and drift fields and maps database pool values correctly. The contradictory `config/settings.py` remains present, defines second-based backoff, and is not imported by the collector path. The source-of-truth statement and duplicate configuration files require reconciliation.
- **RISK:** `CollectorService` emits string event types, while `SnapshotWriter.write_event` accesses `event.event_type.value`. If the writer is used as the event callback, event writes fail. In addition, `COLLECTOR_ERROR` emitted by the service is not included in either DDL `event_type` enum.
- **RISK:** `FutureOutcome.outcome_status` is typed as `DataStatus`, but database `outcome_status` accepts `VALID`, `PARTIAL`, `MISSING`, and `MARKET_CLOSED`. A `STALE` or `REJECTED` outcome can be represented in the model and then rejected by the DDL.
- **RISK:** Raw data is intended to be immutable, but neither DDL variant blocks `UPDATE` or `DELETE` through database permissions or triggers. Primary keys only prevent duplicate inserts.
- **RISK:** `init_database()` always reads the TimescaleDB schema, ignores `use_timescaledb`, uses a path not present from this repository root (`hermes_v0/storage/schema.sql`), and splits SQL statements on semicolons. The latter is unsafe for the DDL's PL/pgSQL function bodies.
- **RISK:** The collector increments `snapshots_stored` immediately after `writer.write()`, although that method ordinarily only queues a batch. Reported stored counts can exceed successfully committed rows.
- **RISK:** The scheduler excludes weekends but has no exchange-holiday calendar. A holiday can be treated as a collection day.
- **RISK:** Imports use the `hermes_v0.*` package prefix, but the repository contains no packaging metadata and no top-level package directory from this working-directory view. A documented installation/import context is absent.

## Missing components

- **FACT:** No test suite was found for collector timing, validation, provider mappings, persistence, schema initialization, or look-ahead controls.
- **FACT:** No feature calculator, outcome labeler, evidence report generator, or dashboard implementation was found.
- **FACT:** `Agent_Control/orchestrator.py`, `event_logger.py`, and `handoff.py` are empty, although governance requires agent-state transitions and audit logging.
- **UNKNOWN:** Which database schema variant is the deployment target and whether TimescaleDB is available.
- **UNKNOWN:** Actual OpenAlgo response shapes, authentication behavior, rate limits, VIX symbol support, option-chain/Greeks availability, timestamps, and historical/websocket support.
- **UNKNOWN:** The intended correction/late-arrival policy for immutable snapshots and the required retention/backup policy.

## Recommendations

1. **RECOMMENDATION:** DATA-001 should verify every requested provider field and endpoint with source-backed samples before any field is considered operational.
2. **RECOMMENDATION:** BUILD-001 should resolve the duplicate configuration source, package/run contract, event type representation, outcome-status enum mismatch, schema-initialization behavior, and write-confirmation metric before collector deployment.
3. **RECOMMENDATION:** Define database roles or append-only enforcement, an auditable correction policy, schema migrations, retention, and backups before calling raw observations immutable in operation.
4. **RECOMMENDATION:** QA-001 should test provider failures, rejected snapshots, duplicate keys, database outage/recovery, drift, holiday handling, time-zone boundaries, and look-ahead bias.
5. **RECOMMENDATION:** Implement or explicitly defer Agent_Control orchestration/logging code; current queue/registry YAML alone does not enforce the one-active-agent rule.

## Open questions

1. Is `config/settings.py` a deliberately retained alternative, or stale code that should be retired by an authorized implementation agent?
2. Which status taxonomy is authoritative for future outcomes: `DataStatus` or the DDL `outcome_status` enum?
3. What database-level mechanism will enforce raw-record immutability and support audited corrections?
4. Which PostgreSQL/TimescaleDB deployment model is approved, and how will DDL migrations be executed safely?
5. What provider-backed timestamp and rate-limit rules should govern the five-second collection cadence?

## Handoff determination

**HANDOFF_READY for V0-001 architecture audit.** Major repository areas were inspected; contradictions, missing components, risks, and open questions are documented. This result is **not** approval to run the collector or to make trading decisions. The next valid handoff is DATA-001 for provider verification, with the listed implementation risks retained for BUILD-001 and QA-001.

# Hermes V0 Database Contract

## Supported DDL

- `storage/schema.sql`: PostgreSQL with TimescaleDB hypertables, compression, and optional retention-policy statements.
- `storage/schema_pg.sql`: PostgreSQL-compatible alternative without TimescaleDB.

Choose one schema variant for an environment; do not apply both to the same database because they define the same types and tables.

## Tables

| Table | Classification | Key |
| --- | --- | --- |
| `market_snapshot` | raw observation | `timestamp_ist`, `symbol` |
| `option_snapshot` | raw observation | timestamp, symbol, strike, type, expiry |
| `feature_snapshot` | derived/recalculatable | `timestamp_ist`, `symbol` |
| `future_outcome` | evidence label | source timestamp, symbol, horizon |
| `system_event` | operational evidence | generated event id |

Raw-observation keys deliberately prevent a second observation from replacing the same symbol/timestamp through the normal writer path. Features and outcomes are stored separately so recalculation cannot mutate raw market evidence.

## Operational constraints

- Use `TIMESTAMPTZ` values and derive `trading_date` in Asia/Kolkata.
- Preserve `data_status`, `source_latency_ms`, `ingestion_time`, and version with every raw record.
- The writer uses PostgreSQL COPY batches; duplicate keys will fail rather than overwrite data.
- No schema migration framework, database roles, immutable-write trigger, or backup/restore procedure is supplied. These are deployment prerequisites, not completed guarantees.

# Hermes V0

Hermes V0 captures market observations through OpenAlgo and stores them in PostgreSQL for research and evidence. OpenAlgo connects to Fyers; Hermes V0 consumes OpenAlgo's read-only market-data APIs. Hermes V0 does not execute trades.

## Modules

| Location | Responsibility |
| --- | --- |
| `collector/openalgo_connection.py` | OpenAlgo connectivity checks |
| `collector/adapters/` | Provider access and payload normalization, including the strict adapter |
| `collector/recovery.py` | Raw-first collection, durable journaling, replay, and runtime status |
| `collector/integrity.py` | Observation identity, timestamps, provenance, and integrity rules |
| `collector/live_option_metrics.py` | Option observations and derived IV/Greeks support |
| `collector/service.py`, `scheduler.py`, `validator.py` | Earlier collector service, scheduling, and validation components |
| `config/` | Environment-based settings; package consumers import `config/__init__.py` |
| `domain/models.py` | Typed observations, derived data, and event models |
| `storage/` | PostgreSQL schema, migrations, writers, and the local recovery journal |
| `automation/` | macOS collector launcher, LaunchAgent definitions, and watchdog |
| `runtime/` | Local outbox and operational state; preserve this directory during outages |
| `dashboard.py` | Lightweight local dashboard implementation |
| `tests/` | Local behavior tests and an explicitly enabled PostgreSQL integration test |
| `Agent_Control/` | Development queue, role state, handoffs, and audit reports |
| `source_of_truth/` | Architecture, boundaries, data contracts, and development governance |

The old empty `features/`, `outcomes/`, `quality/`, `research/`, `dashboard/`, and `scripts/` directories were placeholders. Future components should be added when implemented; the existing domain models, schema, and roadmap remain available. The dashboard implementation is `dashboard.py`.

## Run from the workspace

These commands assume the current directory is `/Users/viswatej/Desktop/openalgo`, the parent of `hermes_v0/`. Keep Hermes dependencies in `hermes_v0/.venv/` and OpenAlgo dependencies in `openalgo/.venv/`.

Inspect the existing collector status:

```bash
hermes_v0/.venv/bin/python -B -m hermes_v0.collector.recovery --status
```

Inspect the development queue:

```bash
hermes_v0/.venv/bin/python -B -m hermes_v0.Agent_Control.orchestrator status
```

Start the existing collector manually when required:

```bash
zsh hermes_v0/automation/run_b04_option_metrics.zsh
```

This launcher reads the OpenAlgo API key from the existing macOS Keychain item, sets the workspace import path, uses the dedicated Hermes Python environment, and invokes `collector.recovery`. The collector enforces a single-process lock and its market-session limits. The existing LaunchAgents continue to reference the same paths. OpenAlgo, the Fyers session, PostgreSQL, and the Keychain entry must be available for collection.

## Storage and configuration

`OPENALGO_HOST` selects the OpenAlgo server. `HERMES_DB_HOST`, `HERMES_DB_PORT`, `HERMES_DB_NAME`, `HERMES_DB_USER`, and `HERMES_DB_PASSWORD` configure PostgreSQL. The launcher supplies local host/database/user defaults. Keep credential values out of source and reports.

Raw option OI is stored in `option_snapshot.oi`; unavailable or ambiguous values remain `NULL`. Raw observations, derived Greeks, receipts, gaps, and quarantine evidence have separate storage responsibilities. The SQLite recovery outbox is local runtime data, while PostgreSQL stores persisted research observations.

The schemas and migrations are in `storage/`. Do not rerun base schema creation against the populated database as part of ordinary startup. See [INTEGRITY_AND_RECOVERY.md](source_of_truth/INTEGRITY_AND_RECOVERY.md) and [DATABASE.md](source_of_truth/DATABASE.md) before database work.

## Validate local behavior

```bash
HERMES_RUN_DB_TESTS=0 PYTHONDONTWRITEBYTECODE=1 hermes_v0/.venv/bin/python -B -m pytest -p no:cacheprovider -q hermes_v0/tests --ignore=hermes_v0/tests/test_recovery_postgres.py
```

This suite uses local/mock evidence and excludes the PostgreSQL integration test. Passing it does not approve a development task or establish full-session provider coverage. Preserve the BUILD/QA workflow and existing reports under `Agent_Control/`.

See the [workspace cleanup record](../WORKSPACE_CLEANUP.md) for archived material and recovery instructions.

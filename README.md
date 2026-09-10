# OpenAlgo + Hermes V0 workspace

This folder contains two projects with separate responsibilities and Python environments.

```text
openalgo/                       # Workspace (this directory)
├── openalgo/                   # OpenAlgo server, Fyers integration, web UI
├── hermes_v0/                  # Market-data collection and PostgreSQL storage
├── README.md                   # Start here
├── WORKSPACE_CLEANUP.md         # Cleanup record and recovery instructions
└── openalgo-hermes.code-workspace
```

| Project | Responsibility | Main locations |
| --- | --- | --- |
| [OpenAlgo](openalgo/README.md) | Connect to Fyers and expose broker/market-data APIs and the web UI | `app.py`, `broker/fyers/`, `restx_api/`, `services/`, `frontend/` |
| [Hermes V0](hermes_v0/README.md) | Read data from OpenAlgo, validate observations, retain recovery evidence, and store data in PostgreSQL | `collector/`, `domain/`, `storage/`, `automation/` |

```text
Fyers market data
        ↓
OpenAlgo server (openalgo/, http://127.0.0.1:5000)
        ↓ read-only HTTP market-data APIs
Hermes V0 collector (hermes_v0/)
        ↓ durable local outbox, validation, replay
PostgreSQL (hermes database)
```

OpenAlgo owns the Fyers connection. Hermes V0 communicates with OpenAlgo over HTTP and keeps its own database schema, configuration, dependencies, and runtime evidence. Hermes V0 does not place orders. OpenAlgo's existing application features remain in its project folder.

## Work on OpenAlgo

From this workspace directory, start the server when it is not already running:

```bash
cd openalgo
uv run python app.py
```

The server's local configuration is `openalgo/.env`. Its application databases and logs remain in `openalgo/db/` and `openalgo/log/`. Keep its `.venv/`, `frontend/dist/`, keys, uploads, and other application assets with the server.

The local installation guide is now [openalgo/README_INSTALL.md](openalgo/README_INSTALL.md). It is historical documentation; the move did not revalidate its claims or rerun installation.

## Work on Hermes V0

Run these commands from this workspace directory:

```bash
# Read the collector's existing status file.
hermes_v0/.venv/bin/python -B -m hermes_v0.collector.recovery --status

# Read the development queue; this does not dispatch work.
hermes_v0/.venv/bin/python -B -m hermes_v0.Agent_Control.orchestrator status

# Run local tests without the optional PostgreSQL integration test.
HERMES_RUN_DB_TESTS=0 PYTHONDONTWRITEBYTECODE=1 hermes_v0/.venv/bin/python -B -m pytest -p no:cacheprovider -q hermes_v0/tests --ignore=hermes_v0/tests/test_recovery_postgres.py
```

See the [Hermes V0 guide](hermes_v0/README.md) for collection, storage, and module responsibilities. Tests do not establish live provider availability or full-session collection coverage.

## Development layout

Open `openalgo-hermes.code-workspace` in VS Code to display **OpenAlgo** and **Hermes V0** as separate project roots. Add server/broker work inside `openalgo/`; add observation, persistence, and research work inside `hermes_v0/`. Keep future architecture documents with the project that owns them.

OpenAlgo retains its own Git repository. Use `git -C openalgo status` for server changes and `git status` here for workspace/Hermes changes. The cleanup preserved the existing Git histories and staged entries.

The old `hermes/` trading project, duplicate root source trees, strategy experiments, and generated clutter were moved to a recoverable archive outside this workspace. See [WORKSPACE_CLEANUP.md](WORKSPACE_CLEANUP.md).

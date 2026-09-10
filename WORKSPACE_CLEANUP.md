# Workspace cleanup — 2026-09-09

Task: `WORKSPACE-CLEANUP-20260909`. Owner request: separate OpenAlgo and Hermes V0 by function and remove unrelated or unnecessary workspace content. Executor: workspace maintenance, with a read-only OpenAlgo duplicate/dependency review. This is a layout cleanup, not a Hermes BUILD/QA promotion.

## Result

The workspace contains two project directories: `openalgo/` for the complete OpenAlgo application and Fyers integration, and `hermes_v0/` for read-only market-data collection and PostgreSQL persistence. Both retain their original absolute paths.

The root [README](README.md), [Hermes guide](hermes_v0/README.md), and [VS Code workspace](openalgo-hermes.code-workspace) explain where to work. Generated files and local environments are covered by the new root `.gitignore`.

## Moves

129 entries were archived outside the workspace; one useful installation guide was relocated into OpenAlgo. Every move was checked against a manifest containing file SHA-256 hashes, directory entries, and symlink targets.

| Material | Action and reason |
| --- | --- |
| Root `restx_api/`, `sandbox/`, `scripts/`, `services/`, `subscribers/`, `tmp/`, `upgrade/`, `utils/`, `websocket_proxy/` | Archived redundant copies. Files match the complete nested OpenAlgo project; the two differing `utils` files have comment-only differences and identical parsed Python structure. |
| Root `install.sh`, `start.sh`, `requirements*.txt`, `uv.lock`, `utils.py`, `README_BACKTEST.md` | Archived duplicate OpenAlgo support files. Their owning project retains its copies. |
| Root `test/` | Archived duplicate tests plus a legacy-Hermes-specific verification script. OpenAlgo's own tests remain under `openalgo/test/`. |
| Root `strategies/`, `.agents/`, `.claude/`, `skills-lock.json` | Archived standalone strategy experiments, their results, and backtest skill metadata. Unique files and relative symlinks are preserved. |
| Root `hermes/` | Archived the older automated trading project. It is separate from Hermes V0. No active process or open file handle was found in this project before moving it. |
| Root `.env`, condor state/logs, `system_log.txt`, `trade_log.csv`, `install.log`, `projectstructure.txt` | Archived legacy local configuration and generated artifacts. OpenAlgo's actual `.env` remains in `openalgo/`; Hermes uses environment settings and its Keychain launcher. |
| Root empty `data/`, `docs/` | Archived empty directories. |
| Python/pytest caches and Finder metadata | Archived 90 entries inside the two projects, plus root cache/metadata entries. Virtual environments, installed dependencies, and Git metadata were excluded. Running applications may regenerate caches. |
| Empty Hermes `features/`, `outcomes/`, `quality/`, `research/`, `dashboard/`, `scripts/` | Archived unused placeholders. Domain models, schema, roadmap, and `dashboard.py` remain. |
| Root `README_INSTALL.md` | Moved to `openalgo/README_INSTALL.md`; original content preserved. |

The retained `openalgo/README_BACKTEST.md` now marks its standalone strategy as archived and removes the obsolete launch command. Its original content is backed up. The manual backtest runner already referenced a nonexistent strategy location and expected an absent function; it was not executed, repaired, or treated as a cleanup dependency.

## Preservation and verification

- Baseline and post-cleanup Hermes local suite: **77 passed** in each run. The explicitly enabled PostgreSQL integration test was excluded.
- A snapshot covered **2,602 existing project files**, excluding live runtime/data directories, dependencies, caches, and Git internals. All remain byte-identical except the intentionally updated historical backtest guide. Existing application Python code, configuration, schemas, and migrations were not changed.
- OpenAlgo returned **HTTP 200** before and after cleanup. Its listener remained **PID 11045**, working from `openalgo/`. It was not restarted.
- A read-only PostgreSQL connection succeeded; `market_snapshot`, `option_snapshot`, and `hermes_ingest_receipt` remain present. No schema/data migration or database write was performed by the cleanup.
- Both installed Hermes LaunchAgent files are byte-identical to their pre-cleanup copies; referenced paths still exist. Both repository plist files and the launcher shell syntax validate.
- Existing staged Git entries in both repositories are unchanged. No commit, reset, staging operation, or history rewrite was performed. Pre-existing staged files, including now-archived paths, remain in the index; ignore rules apply only to untracked files. Review staging before any future commit.
- OpenAlgo's databases, logs, `.env`, keys, frontend build, virtual environment, and built-in modules remain in place. Hermes runtime/outbox data, source-of-truth documents, task state, and audit evidence remain in place. The cleanup makes no full-session collection or live Fyers validation claim.

## Archive and recovery

Private archive:

```text
/Users/viswatej/Desktop/openalgo-cleanup-archive/20260909-231722/
├── archived/          # Original relative paths of removed material
├── manifest.json      # Every move and content checksums
├── metadata/          # Before snapshots, original docs/indexes, verification
├── restore.py         # Dry-run by default; refuses existing destination paths
└── README.md
```

The archive contains **848 files**, approximately **17.46 MiB**, excluding the installation guide relocated into OpenAlgo. No archived content was permanently deleted. The archive directory is private because it includes old local configuration files.

Preview restoration of the old Hermes project:

```bash
python3 /Users/viswatej/Desktop/openalgo-cleanup-archive/20260909-231722/restore.py --source hermes
```

To restore a selected entry, repeat its preview command with `--apply`. `--source strategies` selects the old strategy tree. Omit `--source` to verify all moved entries; regenerated files can cause restore conflicts, which are reported without overwriting them. Restoration does not start any archived program, revert new documentation, or change Git staging.

Handoff: work within the owning project folder using the root README. Keep this archive until the removed material is no longer needed.

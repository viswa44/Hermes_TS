# Hermes evidence dashboard

Open **[http://127.0.0.1:8767](http://127.0.0.1:8767)** on this Mac.

The evidence dashboard is separate from the market operations dashboard on
port 8765. It explores saved historical observations and controls the existing
LangGraph evidence engine.

See [latest data and joint momentum verification](VERIFICATION_2026-09-28.md)
for the completed run through September 24, evaluation results and desktop/mobile
checks. The [September 23 verification](VERIFICATION_2026-09-23.md) and
[initial dashboard verification](VERIFICATION.md) are retained.

## Explore observations

Select a research run to inspect its source coverage, five-minute events,
conditions, evaluation results and QA checks. The observation explorer includes
spot, option price, IV, OI, volume, contract identity, Greeks and availability
timestamps. Filter by session or option side, search identities, and page
through the results. The other views show detected events, all sampled windows
and condition matches. **Joint observations** places CE and PE returns, OI,
volume and both IV values on the same pre-event cutoff. The spot chart shows
the selected historical session.

Completed research is checked against its saved registry and artifact hashes
before results are displayed. Missing or altered evidence is clearly marked.
Unknown outcomes remain unknown; counts distinguish raw observations from
sampled windows and condition matches. Times are displayed in Asia/Kolkata.

## Activate research

Click **Start research run**, choose an optional last discovery session and
the minimum support/maximum condition settings, then start. The default split
uses the first 60% of eligible sessions for discovery and the rest for evaluation.
The form can also request S3 publication to the existing
`heremesv0-cleaned-data/evidence-engine` prefix; this is off by default.

A new run discovers the latest completed cleaner revisions from
`data_cleaning_agent/runtime/progress.json`. It verifies their local manifests,
quality reports, artifact checksums and linked table identities, then saves an
immutable source registry for that run. New completed sessions are picked up
without changing a date in the code. Invalid current revisions block activation
with a source issue instead of silently falling back to older data. Verification
is local; it does not imply a fresh S3 download or live database check.

The header and sidebar describe the selected run's saved data. The start dialog
shows the latest available coverage; a notice identifies sessions missing from
an older run. In-progress market data becomes eligible after the daily cleaner
finishes and its completed export passes verification. Existing runs retain
their original dataset. Workspaces without daily progress can still use the
historical September 19 registry as an explicit legacy fallback.

New runs study **at least 50 NIFTY points up or down over five minutes**, with no
upper cap. Their hypotheses combine a CE observation, a PE observation, CE IV
and PE IV using **AND**. The bounded search considers paired option returns,
OI changes and volume increments with both IV regimes. Thresholds come only
from discovery sessions; later sessions evaluate the frozen combinations.
Every participating feature must be available before the event starts. Missing
features remain unknown and never count as observed nonmatches.

Condition cards show every predicate, known outcomes, successes, unknowns and
the baseline among windows where all participating features were available.
An above-baseline result is a descriptive research lead. The dashboard does
not classify it as proven predictive value. Historical single-feature runs
remain readable with their original 50–80-point definitions.

One dashboard research job runs at a time. Historical runs can take several
minutes. Closing the page does not cancel it.
The dashboard records each stage, final QA outcome and job failure. An
interrupted run can resume when its saved engine version still matches.
Finished failed/insufficient research needs a new run; already completed
computation is never silently rewritten. A failed S3 publication can resume
the same completed local run and retry immutable publication.

The three research agents are deterministic LangGraph stages. QA PASS means
the evidence passed integrity checks; discovered conditions remain exploratory.
No broker orders, market collector controls or LLM calls are made.

## Run and service

From `/Users/viswatej/Desktop/openalgo`:

```bash
# Foreground server; reuse the tested evidence engine environment.
evidence_engine/.venv/bin/python -m evidence_dashboard.server --port 8767

# Install/start the dashboard at login and restart it on failure.
evidence_engine/.venv/bin/python -m evidence_dashboard.install_launchagent --install

# Inspect the installed service.
launchctl print gui/$(id -u)/com.openalgo.evidence-dashboard

# Run dashboard tests.
evidence_engine/.venv/bin/python -m pytest -q evidence_dashboard/tests
```

The local service is `com.openalgo.evidence-dashboard`. Its logs and job state
are in ignored `evidence_dashboard/runtime/`; research artifacts remain in
`evidence_engine/output/`. The market operations service is unchanged.
The local server binds only to `127.0.0.1`. Run controls require same-origin
JSON requests and a per-server token, accept only fixed research settings, and
start a detached worker with an operating-system lock. There is no generic
command execution endpoint. GET requests read local evidence and never start
research or contact AWS.

## API

- `GET /health`: dashboard HTTP health only.
- `GET /api/status`: source, saved runs, active job and form defaults.
- `GET /api/runs/<run_id>`: evidence metrics, stages, conditions and QA.
- `GET /api/runs/<run_id>/observations`: paginated `observations`, `events`,
  `samples` or `occurrences`; session, side, label, condition and identity filters.
- `GET /api/runs/<run_id>/series`: bounded historical spot chart data.
- `POST /api/runs`: start a new historical research run.
- `POST /api/runs/<run_id>/resume`: resume the saved job.

S3 publication indicators describe saved upload receipts. Download verification
is identified separately and is never implied by upload success.

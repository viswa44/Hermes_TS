# Evidence dashboard verification — 21 September 2026

The dashboard is installed and running at **http://127.0.0.1:8767** as the
macOS user service `com.openalgo.evidence-dashboard`. It starts at login.
Observations, event windows, sampled windows, condition matches, QA checks and
saved publication receipts are available from the browser. The same browser
can start historical research and resume eligible interrupted runs.

## Complete run started from the browser

Clicked **Start research run** using the defaults: minimum support 5, maximum
conditions 6, automatic discovery/evaluation split and S3 publication off.

- Run: `research-20260921-081946-8ab02c94`.
- Started: `2026-09-21T08:19:46.674338+00:00` (13:49 IST).
- Finished: `2026-09-21T08:35:23.127626+00:00` (14:05 IST), about 15 minutes 36 seconds.
- Worker result: **SUCCEEDED**; evidence registry: **REGISTERED**.
- QA: **PASS**, all **13 checks** passed.
- Dashboard integrity: **VERIFIED**, after checking the external registry,
  sealed manifest and every declared artifact's SHA-256 digest.
- Results: **57,574 eligible observations**, **2,394 sampled windows**,
  **10 event windows**, **290 unknown outcomes** and **6 conditions**.

The detached worker survived a restart of the dashboard server while research
was running. A second authenticated start request returned HTTP **409** and
did not launch another job. After completion, the browser displayed the new
run's results and enabled the start control again.

This run used the pinned September 19 source registry: 61,056 supplied rows
across September 7–18, with 3,482 legacy rows excluded from research. It was a
local historical run; S3 publication was not requested. Source PostgreSQL,
broker orders and live collection were not involved. QA PASS describes the
implemented evidence integrity checks; conditions remain exploratory.

## Application and browser checks

- Dashboard suite: **95 tests passed, 16 subtests passed**. Coverage includes
  artifact tampering, observation filters and pagination, chart continuity,
  restricted HTTP controls, real subprocess lock inheritance, duplicate-job
  rejection and saved-run resume behavior.
- Python compilation, JavaScript syntax and `git diff --check` passed.
- Browser checks passed for date/side filters, identity search, pagination,
  event/sample tabs, unknown outcomes, keyboard chart tooltips and QA details.
  September 18 CE observations returned 4,488 records; the second page showed
  records 26–50. Event filters returned 10 total windows and 4 downward windows;
  the unknown outcome filter returned 290 sampled windows.
- The completed run retained source precision in the table: gamma `0.001496`,
  IV `0.1055` (decimal), and receipt time `09:15:05.902` in the checked row.
- No browser JavaScript errors were observed.
- Final axe checks reported **zero violations** for the tested desktop and
  mobile pages under WCAG 2 A/AA and 2.1 AA rules. At a 390-pixel mobile viewport,
  document width was also 390 pixels. This is automated coverage of the tested
  views, not a complete accessibility certification.

## Saved evidence

- [Completed browser run and API results](runtime/browser/completed-run.json)
- [Completed dashboard screenshot](runtime/browser/completed-desktop.png)
- [Final desktop/mobile accessibility results](runtime/browser/accessibility-final.json)
- [Mobile screenshot](runtime/browser/mobile-final.png)
- [Initial interaction and activation record](runtime/browser/interaction-results.json)
  (its accessibility findings precede the fixes; use the final report above).
- [Run manifest](../evidence_engine/output/runs/research-20260921-081946-8ab02c94/manifest.json)
- [Evidence QA report](../evidence_engine/output/runs/research-20260921-081946-8ab02c94/agent3/qa_report.json)

Browser artifacts and runtime state are retained locally in ignored directories.
The dashboard's [README](README.md) contains startup commands and API routes.

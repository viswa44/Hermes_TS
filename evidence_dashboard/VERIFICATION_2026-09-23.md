# Latest data and joint momentum verification — 23 September 2026

Dashboard: **http://127.0.0.1:8767**. Completed browser-started run:
`research-20260923-035912-0e0a822e`. Worker **SUCCEEDED**, evidence **REGISTERED**, all
**13 integrity checks passed**. Source observations and prior runs were preserved.

## New data selection

The dashboard now discovers current completed cleaner revisions from
`data_cleaning_agent/runtime/progress.json`, verifies their local manifests,
quality gates, linked table identities, availability timestamps and artifact
hashes, and freezes a separate source registry for each run. It does not
overwrite the September 19 historical registry. Resuming uses the saved run
snapshot rather than refreshing its sources.

This run includes **11 supplied sessions, September 7–22**, with **78,754 source
rows**. It admits **75,272 version-2 observations across nine sessions**, excluding
3,482 legacy rows. New source sessions are September 21 (**8,722 rows**) and
September 22 (**8,976 rows**). Verification is local; no fresh PostgreSQL or S3
claim is made. Future sessions become eligible after completed cleaner exports
pass the same gates.

The selected run's header/sidebar reflect its own snapshot. Older runs display
a notice when newer sessions are available; the launch dialog shows the current
catalog. Invalid source revisions display their reason and block activation.

## Combined observations and momentum

The research defaults are `joint-ce-pe-iv-training-v2` and
`spot-endpoint-momentum-v2`. Every candidate requires all four predicates:
CE and PE observations from a shared family (option midpoint returns, OI
changes, or volume increments), CE IV and PE IV. Past changes cover 60 seconds.
Both sides' features must already be available at the same pre-event cutoff;
contract continuity and individual availability timestamps remain enforced.

Momentum means an endpoint move of **at least 50 spot points up or down over
five minutes**, including movements beyond 80 points. There is no upper cap.
This run contains **3,131 sampled windows**: **10 upward**, **4 downward**,
2,741 other complete windows and **376 unknown outcomes**. Four upward events
are from September 22. No observed qualifying move exceeded 80 points in this
run; synthetic tests explicitly cover those larger moves and both boundaries.

The engine tested **288 joint hypotheses** using discovery data only and froze
six. Discovery dates: September 9, 10, 11, 15 and 16. Evaluation dates:
September 17, 18, 21 and 22. Every tested combination is saved in the search log.

| Condition | Target | Evaluation successes/known | Unknown outcomes | Eligible baseline successes/known | Spaced successes/known |
| --- | --- | ---: | ---: | ---: | ---: |
| C01 | UP_MOMENTUM | 0/1 | 0 | 2/430 | 0/1 |
| C02 | DOWN_MOMENTUM | 0/1 | 0 | 0/430 | 0/1 |
| C03 | UP_MOMENTUM | 0/2 | 0 | 2/430 | 0/2 |
| C04 | DOWN_MOMENTUM | 0/5 | 0 | 0/430 | 0/5 |
| C05 | UP_MOMENTUM | 0/59 | 2 | 2/430 | 0/41 |
| C06 | DOWN_MOMENTUM | 0/82 | 3 | 0/430 | 0/58 |

**None of the six has demonstrated predictive usefulness on evaluation data.**
All have zero observed target successes there. Some have only one or two known
matches. All four required inputs were available for 455 of 1,481 evaluation
anchors; 1,026 anchors lacked at least one required input at the cutoff. These
are missing inputs, not negative outcomes. The downward baseline itself has
zero eligible successes, further limiting interpretation. Unknown outcomes,
overlapping matches and conservatively spaced matches remain separate. Event
hit rates do not establish profitability or causality.

## Validation

- **249 tests passed, 46 subtests passed** across the engine and dashboard.
- Added tests prove new dates enter the next source snapshot without code edits;
  old snapshots remain unchanged; tampered sources are rejected; and resume does
  not consult the current catalog.
- Joint research tests cover a four-variable signal with no marginal single-feature
  lift, missing either side/IV, heldout-data changes that cannot alter discovery,
  future availability, rehashed rule tampering, exact occurrence values,
  baseline reconciliation and actual Parquet/graph execution.
- Python compilation, JavaScript syntax and `git diff --check` passed.
- Real browser activation used default local settings. Start:
  `2026-09-23T03:59:12.023568+00:00`; finish: `2026-09-23T04:12:09.573352+00:00`;
  elapsed approximately 13.0 minutes. S3 publication was off.
- Browser checks verified the old-run newer-data notice, updated launch coverage,
  September 22's 8,976 rows, paired CE/PE/IV sample columns, both momentum filters,
  all 24 predicates and readable matching feature values.
- Completed desktop/mobile axe checks: **zero violations** under the tested
  WCAG A/AA rule sets; mobile document width equals its 390-pixel viewport.
  No browser JavaScript errors. A mocked failed-source response also exposed the
  reason and disabled activation without accessibility findings.

## Evidence

- [Completed browser/API report](runtime/browser/joint-completed-run.json)
- [Launch checks](runtime/browser/joint-before-run.json)
- [Source failure fixture check](runtime/browser/joint-source-error-fixture.json)
- [Desktop screenshot](runtime/browser/joint-completed-desktop.png)
- [Joint condition screenshot](runtime/browser/joint-conditions.png)
- [Mobile screenshot](runtime/browser/joint-completed-mobile.png)
- [Run manifest](../evidence_engine/output/runs/research-20260923-035912-0e0a822e/manifest.json)
- [Frozen joint hypotheses](../evidence_engine/output/runs/research-20260923-035912-0e0a822e/agent1/conditions.json)
- [Evaluation statistics](../evidence_engine/output/runs/research-20260923-035912-0e0a822e/agent2/statistics.json)
- [QA report](../evidence_engine/output/runs/research-20260923-035912-0e0a822e/agent3/qa_report.json)

Runtime artifacts remain local in ignored directories. Historical single-feature
runs remain readable. Because the engine version changed, prior interrupted runs
cannot resume under the new engine code; their saved artifacts are preserved.

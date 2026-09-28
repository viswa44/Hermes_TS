# Continued joint momentum verification — 28 September 2026

Dashboard: **http://127.0.0.1:8767**. Run
`research-20260927-163237-cb2700b4` completed on September 27 with worker
**SUCCEEDED**, evidence **REGISTERED**, and all **13 integrity checks passed**.
This report closes the interrupted handoff and rechecks the completed evidence.

**None of the six combined CE/PE/IV hypotheses has demonstrated predictive
usefulness on evaluation data.** All have zero observed target successes there.
Integrity PASS verifies the recorded research; it does not establish predictive
power, statistical significance or profitability.

## Data coverage

The completed run contains **13 supplied sessions, September 7–24**, with
**94,758 source observations**. It admits **91,276 version-2 observations across
11 sessions**, excluding 3,482 legacy observations. Each supplied observation
has linked observation and option rows; these are not added together as separate
observations.

Relative to `research-20260923-035912-0e0a822e`, the new sessions are September 23
(**8,306 observations**) and September 24 (**7,698 observations**). The dashboard
selects the latest completed run, and its observation explorer exposes both dates.
New sessions become eligible when their completed cleaner revisions pass local
manifest, quality, table-identity and checksum verification.

September 25 is absent from these exports. A saved read-only PostgreSQL check
performed on September 27 found **zero September 25 option observations and RAW
receipts**. The saved collector status reported `captured=0`, `rejected=40` and
`source_error=HTTP_500`. This explains why there were no stored rows for the
cleaner to select; the upstream cause of that HTTP error remains unresolved.
This is dated diagnostic evidence, not a fresh database or provider check on
September 28. See the [source-gap report](runtime/browser/continued-source-gap-2026-09-27.json).

## Frozen hypotheses and evaluation

The discovery cutoff stays **September 16**. Discovery sessions are September 9,
10, 11, 15 and 16. Evaluation sessions are September 17, 18, 21, 22, 23 and 24.
The six expressions and thresholds remain the same as in the prior run, with
all 288 discovery candidates retained. Conditions combine CE and PE observations
with both IV values, using AND at a shared pre-event cutoff.

Momentum is an endpoint move of **at least 50 NIFTY spot points up or down over
five minutes**, including moves beyond 80 points. The full sample has **3,798
windows**: 10 upward momentum events, 5 downward momentum events, 3,248 other
complete outcomes and **535 unknown outcomes**.

| Condition | Target | Evaluation successes/known | Unknown outcomes | Eligible baseline successes/known | Spaced successes/known |
| --- | --- | ---: | ---: | ---: | ---: |
| C01 | UP_MOMENTUM | 0/1 | 0 | 2/610 | 0/1 |
| C02 | DOWN_MOMENTUM | 0/1 | 0 | 1/610 | 0/1 |
| C03 | UP_MOMENTUM | 0/11 | 1 | 2/610 | 0/8 |
| C04 | DOWN_MOMENTUM | 0/5 | 0 | 1/610 | 0/5 |
| C05 | UP_MOMENTUM | 0/69 | 5 | 2/610 | 0/46 |
| C06 | DOWN_MOMENTUM | 0/94 | 8 | 1/610 | 0/65 |

All required inputs were available for **683 of 2,148 evaluation anchors**;
1,465 lacked at least one required input. Among the 683 feature-eligible anchors,
610 have known outcomes and 73 have unknown outcomes. These missing-input and
unknown-outcome categories must not be counted as observed failures. Rules can
match the same anchors, and consecutive windows overlap; condition denominators
must not be summed as independent trials. Spaced counts still do not establish
independence between observations.

September 23–24 add **667 sampled windows**: 507 other complete outcomes,
**159 unknown outcomes**, and one **54.25-point downward move on September 24**
at the 13:42 IST anchor. C05, an upward hypothesis, matched that downward event;
none of the downward hypotheses matched it. The added sessions produce zero
target successes for all six rules. Only 228 of the 667 new anchors have all
required inputs; 439 lack at least one. Of the 159 unknown outcomes, 135 contain
an excessive gap, 14 lack a fresh endpoint, and 10 extend outside the session.

## Verification and evidence

On September 28, a fresh local source scan verified all current cleaner
revisions and still found **13 sessions through September 24**, with no source
issues. Fresh run checks verified the sealed artifacts for the current and
previous runs. The independent follow-up passed **31 checks**, including the
26 original source Parquet files, unchanged discovery source rows and samples,
all 288 search results, source-reconstructed outcomes for all 667 new windows,
and every condition's occurrence set and summary denominators. Rule IDs differ
because they bind to different datasets/configurations; the six predicate
expressions and thresholds are unchanged.

The follow-up uses saved feature values for its predicate calculations; it does
not independently derive every option feature again. The completed engine QA
separately passed its source-based feature arithmetic and availability checks.

The engine and dashboard suite passed **249 tests and 46 subtests** in 236.71
seconds. JavaScript syntax and `git diff --check` passed; the report and README
links were also checked locally.

The September 27 browser report's overall `PASS` was incorrect: its desktop
results contained **19 serious color-contrast violations** on neutral `OTHER`
badges. The failure was reproduced with the outcome column scrolled into view;
checking only the left edge of the wide table skipped these badges. Their text
color changed from `#7c8b7d` to `#53675e`, preserving the semantic badge colors.
The original contrast was **3.12:1**, below the required 4.5:1.

The replacement browser check passed all four views: observations and condition
matches on both desktop and mobile. Each occurrence view explicitly exposed
and verified all **19 neutral badges at 5.26:1**. There were **zero automated
accessibility violations**, no page/console/request errors, and no document
overflow. The runner exits nonzero for violations, missing badge checks,
browser errors or an incomplete viewport/view matrix. It retains axe's
incomplete checks (44 desktop, 40 mobile, involving symbols and SVG axis text)
for manual review; this result is not a complete accessibility certification.

The sealed research artifacts and older browser reports are retained. The
September 27 incremental results file's statement that pipeline QA was pending
is superseded by the completed QA report and this handoff.

- [Fresh independent verification](runtime/continued-independent-verification-20260928T071419Z.json)
- [Test result and Python source hashes](runtime/test-verification-2026-09-28.json)
- [Reproduced contrast failures and affected selectors](runtime/browser/contrast-before-right-2026-09-28.json)
- [Corrected desktop/mobile browser verification](runtime/browser/contrast-after-2026-09-28.json)
- [Browser verification runner](runtime/browser/contrast-qa-2026-09-28.cjs)
- [Desktop condition matches](runtime/browser/contrast-after-2026-09-28-desktop-occurrences.png)
- [Mobile condition matches](runtime/browser/contrast-after-2026-09-28-mobile-occurrences.png)
- [Run manifest](../evidence_engine/output/runs/research-20260927-163237-cb2700b4/manifest.json)
- [Frozen combined hypotheses](../evidence_engine/output/runs/research-20260927-163237-cb2700b4/agent1/conditions.json)
- [Evaluation statistics](../evidence_engine/output/runs/research-20260927-163237-cb2700b4/agent2/statistics.json)
- [All 13 integrity checks](../evidence_engine/output/runs/research-20260927-163237-cb2700b4/agent3/qa_report.json)
- [Incremental September 23–24 analysis](runtime/browser/continued-results-2026-09-27.json)

Verification here concerns local saved evidence. This research run did not
publish to S3. No new research computation is necessary while the verified
source catalog and frozen research configuration are unchanged.

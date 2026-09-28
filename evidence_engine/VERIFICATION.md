# Evidence engine verification — 21 September 2026

Final run: `research-20260920-003`. The complete sequential LangGraph pipeline returned `REGISTERED`, with all **13 integrity checks passing**. Evidence classification remains `EXPLORATORY`.

## Implementation and checks

- Source alignment → five-minute event detection → frozen condition discovery → exhaustive occurrence scan → evidence QA → registry → encrypted S3 publication.
- Discovery, matching and QA are deterministic graph nodes; no LLM was called.
- SQLite checkpoints and synchronous durability support resuming interrupted work. Integration tests cover interruption after an artifact write, completed resume, source/config/code changes, quarantine branches and publication retry receipts.
- Completed: **98 tests passed, 30 subtests passed**. All **56 locked package versions** match the installed environment; `uv pip check` passes.
- Fixed the runtime-metadata replay mismatch and missing-value comparison after Parquet round trips. Added rejection of unusable spot prices and complete manifest/checkpoint verification before S3 writes.
- Raw PostgreSQL and cleaned source artifacts were not modified. Earlier diagnostic attempts `research-20260920-001` (QA failure before the null-comparison fix) and `research-20260920-002` (stopped before final QA) remain locally for audit; neither was published.

```bash
evidence_engine/.venv/bin/python -m pytest -q evidence_engine/tests
uv pip check --python evidence_engine/.venv/bin/python
```

## Source coverage

Rechecked all 18 source Parquet hashes and sizes: **61,056 joined rows** across nine supplied sessions. Analysis admitted **57,574 rows** from seven sessions and excluded **3,482 legacy rows** without version-2 receipt provenance.

Discovery sessions: 2026-09-09, 2026-09-10, 2026-09-11, 2026-09-15.
Evaluation sessions: 2026-09-16, 2026-09-17, 2026-09-18.

There are **2,394 sampled minute anchors**: 6 upward 50–80-point windows, 4 downward 50–80-point windows, 2,094 other complete windows and 290 unknown outcomes. These are overlapping five-minute endpoint windows, not ten independent market episodes. Another 231 session clock anchors lacked a sufficiently fresh starting quote and were excluded with counts retained.

Unknown outcomes: 233 windows crossed collection gaps, 27 lacked a sufficiently fresh endpoint and 30 extended outside the session. Unknown outcomes never count as failures.

## Frozen conditions and later-session results

Agent 1 tested **228 predicates** on discovery sessions and froze **6 conditions**. Agent 2 recorded **1,839 condition-occurrence rows**; one sampled window can match multiple conditions. All occurrences and all 12 discovery/evaluation summary records were independently reconciled.

| Condition | Target | Evaluation matches | Known outcomes | Successes | Unknown | Success rate among known |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| `PE_delta <= -0.486125` | UP_50_80 | 169 | 164 | 0 | 5 | 0.00% |
| `CE_vega <= 9.844325` | UP_50_80 | 78 | 74 | 0 | 4 | 0.00% |
| `PE_vega <= 9.84445` | UP_50_80 | 77 | 73 | 0 | 4 | 0.00% |
| `PE_gamma <= 0.00136075` | DOWN_50_80 | 258 | 233 | 3 | 25 | 1.29% |
| `CE_delta <= 0.514275` | UP_50_80 | 171 | 166 | 0 | 5 | 0.00% |
| `PE_gamma >= 0.00154425` | UP_50_80 | 122 | 116 | 0 | 6 | 0.00% |

The downward gamma condition had 3 successes among 233 known matches (1.29%), compared with 3 among 426 feature-eligible baseline windows (0.70%). Conservatively spaced matches had 2 successes among 79 known outcomes. The other five conditions had zero evaluation successes. This small exploratory result does not establish forecasting value; rules were selected after testing many candidates and windows overlap.

Timing is application receipt time; exchange/provider event time remains unverified. IV/Greeks are only eligible after their recorded availability, inside continuous contract segments. The source Greek model assumptions remain in the artifacts. Evaluation had 463 of 1,110 anchors with each selected Greek feature available; the remaining 647 are explicitly unavailable.

## Local and S3 evidence

All **13 run artifacts**, including the final manifest, were downloaded from S3 and checked against local/checkpoint SHA-256 digests and exact sizes. Each object reports AES256 encryption and matching checksum metadata. Verification time: `2026-09-21T02:59:23.457299+00:00`.

Manifest:

`s3://heremesv0-cleaned-data/evidence-engine/execution_date=2026-09-20/run_id=research-20260920-003/manifest.json`

Manifest SHA-256: `1ae36e211f9a6d8bbd0adf508d186fc7708ee1cf02f25cb521c7617a35a6f73d`.

- [Run manifest](output/runs/research-20260920-003/manifest.json)
- [Agent 1 conditions and timestamps](output/runs/research-20260920-003/agent1/conditions.json)
- [Agent 2 full statistics](output/runs/research-20260920-003/agent2/statistics.json)
- [Agent 3 QA](output/runs/research-20260920-003/agent3/qa_report.json)
- [S3 verification receipt](runtime/research-20260920-003-s3-verification.json)

Resume the completed run with its saved configuration:

```bash
evidence_engine/.venv/bin/python -m evidence_engine --run-id research-20260920-003 --resume
```

Start a new run ID after changing engine source or selecting a new source registry. The default registry stays pinned to the verified September 19 export; new market sessions require an updated registry.

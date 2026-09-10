# Hermes V0 Architecture Decisions

## ADR-001: Evidence pipeline, not a trading pipeline

**Decision:** V0 stores observations, derived research features, future outcomes, and operational evidence only.

**Consequence:** no execution, recommendation, direction score, strategy selection, or automatic rule modification belongs in this repository.

## ADR-002: Immutable raw observations

**Decision:** `market_snapshot` and `option_snapshot` are the raw evidence layer. They are identified by natural time/contract keys and are never updated by normal collection.

**Consequence:** corrections require an explicitly auditable process; they cannot silently replace historic evidence.

## ADR-003: Separate derived layers

**Decision:** features and outcomes are separate versioned records.

**Consequence:** formulas can evolve and be recomputed without changing the raw observation that supports them. Outcome labels may only be used after the specified horizon has elapsed.

## ADR-004: Provider capabilities are evidence-backed

**Decision:** a field is not considered available merely because the model has a place for it.

**Consequence:** DATA-001 must verify OpenAlgo/provider payloads, semantics, timestamps, rate limits, and historical availability before production use.

## ADR-005: 5-second clock alignment

**Decision:** collection is scheduled at market-hour 5-second boundaries, rather than by repeatedly sleeping for five seconds.

**Consequence:** timing drift and missed intervals are measurable. Validation and runtime configuration still require QA before the collector is relied on.

## ADR-006: Integrity takes precedence over coverage (owner, 2026-09-08)

The owner accepts data loss and requires strict prevention of misleading data.
Gaps may remain; invented values, silent overwrite, false freshness, and live
relabelling of historical candles are prohibited. The active B06 attempt now
includes the recovery controls and acceptance cases in
`INTEGRITY_AND_RECOVERY.md`. This explicitly revises the former no-gap gate.

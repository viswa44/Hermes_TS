# Hermes V0 Boundaries

## In scope

- Collect timestamped NIFTY spot, India VIX, and option-chain observations.
- Validate and persist market snapshots and operational events.
- Define versioned calculated features and future outcome labels for research.
- Produce data-quality and evidence-integrity reports.

## Prohibited

- Place, modify, cancel, route, or simulate live orders.
- Generate a live trade decision, final direction score, or strategy ranking.
- Automatically alter trading, risk, or strategy rules.
- Modify OpenAlgo, `papertradeone.py`, or production Hermes code.
- Represent a desired provider field as available before it is verified.

## Data boundary

Raw observations are append-only evidence. A correction must be represented by a new, separately identifiable record or documented migration; it must not silently rewrite the original observation. Derived features and outcomes are recalculatable and must retain their calculation/version metadata.

## Authority boundary

Agent authority is defined by `source_of_truth/AGENTS/` and enforced by `Agent_Control/`. Only one agent may run at a time. Architecture, data, and safety agents do not change runtime code; BUILD-001 is the only listed agent with Hermes V0 code-modification authority.

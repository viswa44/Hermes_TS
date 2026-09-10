# Hermes V0 — Codex Operating Rules

## Project

Hermes V0 is a research and evidence-generation system for market data.

Primary pipeline:

Raw observations
→ Calculated features
→ Future outcomes
→ Evidence

## Critical boundary

V0 MUST NOT:

- perform live auto-trading
- make live trading decisions
- generate final direction scores
- automatically change trading rules
- operate as a strategy marketplace
- modify the existing production Hermes system
- modify OpenAlgo
- modify papertradeone.py
- do not modify anything
## Agent governance

Only ONE Hermes agent may be ACTIVE at a time.

Agents:

ARCH-001
DATA-001
BUILD-001
QA-001
EVID-001
SAFE-001

The Agent_Control system controls agent transitions.

An agent MUST NOT start another agent.

An agent MUST respect its assigned authority.

## Source of truth

Before performing work, read the applicable documents in:

source_of_truth/

Do not silently override source-of-truth decisions.

If documentation conflicts with implementation:

REPORT the conflict.

Do not silently fix it unless the current agent has authority to do so.

## Auditability

Important actions must be logged.

Every task must have:

task_id
agent_id
start_time
end_time
status
artifacts
handoff

## Safety

Never modify production trading systems during Hermes V0 research work.
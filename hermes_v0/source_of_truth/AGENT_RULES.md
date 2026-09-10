ONLY ONE AGENT MAY BE RUNNING AT ANY TIME.

The orchestrator must reject a start request if
another agent has status RUNNING.

An agent must transition:

PENDING
→ READY
→ RUNNING
→ COMPLETED / FAILED / BLOCKED

before another agent can become RUNNING.

No agent may start another agent directly.

Only the orchestrator controls agent transitions.
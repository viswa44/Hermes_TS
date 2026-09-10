# Hermes V0 Vision

Hermes V0 is a market-data research and evidence-generation foundation for NIFTY options. Its purpose is to preserve reliable observations that can later support research; it does not produce or execute trading decisions.

The evidence pipeline is:

`raw observations -> calculated features -> future outcomes -> evidence`

V0 success means that a researcher can trace an evidence result back to its timestamped source observation, its calculation version, and its data-quality status.

## Non-goals

- Live or automated order placement.
- Live directional calls, trade scores, or strategy selection.
- Automatic changes to strategy or risk rules.
- A strategy marketplace.
- Changes to OpenAlgo, `papertradeone.py`, or the existing production Hermes system.

## Design principles

- Preserve raw provider observations immutably.
- Keep derived data separable and versioned so it can be recalculated.
- Mark incomplete, stale, or rejected data rather than silently filling it.
- Treat provider capability as unverified until DATA-001 has confirmed it.

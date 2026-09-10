# Hermes V0 Roadmap

## V0-001 — Architecture audit

Inventory the implementation, document data flow, contradictions, missing components, and handoff status. Owner: ARCH-001.

## V0-002 — Provider verification

Verify actual OpenAlgo and broker exposure for spot, option chain, OI, OI change, IV, Greeks, bid/ask, volume, timestamps, expiry, VIX, rate limits, historical data, and websocket behavior. Owner: DATA-001 with ARCH-001 review.

## V0-003 — Immutable data contract

Reconcile the documented contract, Python models, validator behavior, adapter payload mapping, and physical schemas. Establish field-level availability and quality semantics. Owners: ARCH-001 and DATA-001.

## V0-004 — Safe database implementation and verification

Implement only approved Hermes V0 persistence work, then adversarially test data quality, timestamps, storage failures, disconnects, and look-ahead controls. Owners: BUILD-001, QA-001, EVID-001, and SAFE-001.

## Exit criteria

V0 is ready for evidence generation only when source observations are traceable, provider coverage is verified, derived data is reproducible, safety boundaries are tested, and outstanding runtime/schema contradictions are resolved or explicitly accepted. Trading functionality is outside this roadmap.

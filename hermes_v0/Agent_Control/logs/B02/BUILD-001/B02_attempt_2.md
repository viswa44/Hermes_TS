# B02-FIX-02 build report

## Status

HANDOFF_READY

## Change

Updated `collector/adapters/openalgo_adapter.py` so `_extract_quote_data` validates that both the provider response and its `data` field are dictionaries. Invalid shapes now raise `ProviderResponseError` before any `.get()` call.

## Tests

`python3 -m unittest hermes_v0.tests.test_underlying_snapshot -v` from the repository root: PASS (6 tests), including `None` and `{"data": []}` provider responses.

## Handoff

QA-001 must retest the two malformed payload shapes and B02 regression suite.

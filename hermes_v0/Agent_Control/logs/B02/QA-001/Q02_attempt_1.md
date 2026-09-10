# Q02 QA report

## Status

BLOCKED

## Scope

Adversarial QA of BUILD-001/B02 configurable underlying snapshot normalization.
No implementation files were modified.

## Tests run

- `python3 -m unittest hermes_v0.tests.test_underlying_snapshot -v` from the repository root: PASS (4 tests).
- Mocked provider response `None`: FAIL. `get_underlying_snapshot("NIFTY")` raises `AttributeError: 'NoneType' object has no attribute 'get'`.
- Mocked provider response `{"data": []}`: FAIL. `get_underlying_snapshot("NIFTY")` raises `AttributeError: 'list' object has no attribute 'get'`.

## Blocker

`OpenAlgoAdapter._extract_quote_data` assumes both the provider response and its `data` field are dictionaries. It must validate those shapes and raise `ProviderResponseError` (or an equally explicit normalization error) before calling `.get()`.

## Handoff

BUILD-001 must fix only the malformed-response normalization defect and rerun B02 regression tests. Q02 must then retest the two payload shapes above before a PASS can approve B02 and start B03.

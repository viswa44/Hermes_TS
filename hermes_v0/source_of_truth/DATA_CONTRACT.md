# Hermes V0 Data Contract

**Contract version:** 1  
**Canonical model:** `domain/models.py`  
**Time zone:** `timestamp_ist` is an Asia/Kolkata market timestamp; `ingestion_time` and calculation times are recorded by the application.

## Contract rules

- Every record has a contract version and data-quality status.
- `timestamp_ist`, `trading_date`, and `symbol` identify a market snapshot.
- Raw fields may be null when absent from the provider; null is not a zero.
- `data_status` is one of `VALID`, `PARTIAL`, `STALE`, `MISSING`, or `REJECTED`.
- Provider payload is retained, when available, for audit. It is evidence of the response received, not proof that every field is provider-supported.
- No raw snapshot may contain calculated signals, trade instructions, or scores.

## Records

| Record | Identity | Purpose |
| --- | --- | --- |
| `MarketSnapshot` | timestamp, symbol | Spot, VIX, expiry/ATM reference, and ATM CE/PE quote, OI, IV, and Greeks fields. |
| `OptionSnapshot` | timestamp, symbol, strike, type, expiry | One option contract beyond the ATM summary. |
| `FeatureSnapshot` | timestamp, symbol, calculation version | Recalculatable returns, EMA, OI, IV, PCR, spread, and latency measures. |
| `FutureOutcome` | source timestamp, symbol, horizon | Future spot, return, direction, and MFE/MAE evidence label. |
| `SystemEvent` | event time | Collector, validation, feed, database, and health observability event. |

## Immutability and look-ahead control

Raw snapshots are immutable observations. Features are derived only from data available no later than the feature timestamp. Outcomes reference a past snapshot and a positive future horizon; they are evidence labels, not input to a live decision. Calculation metadata (`calc_version`, `calc_time`) makes derived results reproducible.

## Provider verification status

The adapter requests spot/VIX quotes, option chains, and option Greeks. The actual availability, semantics, timestamps, rate limits, and historical coverage of bid/ask, quantities, OI, IV, and Greeks remain **UNVERIFIED** until DATA-001 records source-backed results. Consumers must not assume a non-null field will be supplied in production.

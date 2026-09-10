# B04 BUILD-001 handoff — ATM IV and Greeks PostgreSQL collector

Completed: 2026-09-07T08:21:51Z

## Scope delivered

- Added a dedicated read-only NIFTY ATM CE/PE collector with a strict 5- or
  10-second clock-aligned cadence. It calls only OpenAlgo `expiry`, `quotes`,
  `optionchain`, and `optiongreeks` endpoints.
- Persisted one raw NIFTY quote in `market_snapshot` and the real ATM CE/PE
  chain quotes in `option_snapshot` for every successful cycle.
- Added additive `option_greeks_snapshot` storage for separately versioned
  OpenAlgo Black-76 IV, delta, gamma, theta, vega, rho, interest rate, and the
  actual calculation inputs. Raw chain LTP and the later Greeks-calculation
  option LTP remain distinct for provenance.
- Corrected the adapter for the live top-level OpenAlgo Greeks response,
  live expiry selection, numeric normalization, non-positive/missing values,
  timezone-aware ingestion timestamps, and versioned calculated evidence.
- Prevented duplicate local collectors with an advisory process lock. At five
  seconds, the runner disables retries and makes only two Greeks requests per
  cycle (24/minute, within the 30/minute OpenAlgo endpoint limit).

## Files changed

- `collector/adapters/openalgo_adapter.py`
- `collector/live_option_metrics.py`
- `collector/scheduler.py`
- `domain/models.py`
- `storage/option_metrics_writer.py`
- `storage/migrations/20260907_option_greeks_snapshot.sql`
- `requirements.txt`
- `tests/test_b04_option_metrics.py`
- `Agent_Control/queue.yaml`

## Automated evidence

Command:

```text
PYTHONPATH=/Users/viswatej/Desktop/openalgo .venv/bin/python -m unittest discover -s tests -v
```

Result: 22 tests passed, including B01–B04 focused coverage. B04 tests cover
top-level current Greeks responses, provider-status errors, sorted expiry
selection, 10-second missed-interval accounting, raw-vs-calculation LTP
provenance, timestamp ordering, atomic writer inputs, and the dedicated runner.

## Live read-only verification

Command form used (API key entered only through a hidden macOS field and never
saved or printed):

```text
python -m hermes_v0.collector.live_option_metrics --interval-seconds 5 --cycles 12
```

Result from 13:50:55 to 13:51:50 IST:

- 12 / 12 cycles persisted.
- 12 / 12 cycles had valid CE and PE IV plus delta, gamma, theta, vega, and rho.
- 0 failed cycles, 0 partial cycles, 0 missed intervals.
- Mean scheduler drift: 1.316 ms; maximum: 1.698 ms.
- Database rows written: 12 `market_snapshot`, 24 `option_snapshot`, and 24
  `option_greeks_snapshot` rows.
- All 60 new rows satisfied `ingestion_time >= timestamp_ist`.

Latest observed row at 13:51:50 IST (normalized): raw NIFTY LTP 23766.85; CE
raw/calculation LTP 112.90 / 113.10, IV 13.41; PE raw/calculation LTP 37.85 /
37.65, IV 13.41. The OpenAlgo calculation input was a resolved forward around
23825, intentionally retained separately from the raw cash NIFTY LTP.

No order, position, account, or WebSocket endpoint was invoked.

## Remaining scope note

This task intentionally covers the live ATM CE/PE pair only. Multi-strike
capture and longer-running operational monitoring need separate approved work.

HANDOFF_READY

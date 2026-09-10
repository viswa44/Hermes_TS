# Integrity and recovery — owner revision, 2026-09-08

The owner explicitly accepts collection gaps and requires strict prevention of
wrong or misleading data. This supersedes B06's earlier zero-missed-interval
acceptance rule and expands the current attempt to implement recovery controls.

## Non-negotiable contract

- Never forward-fill, interpolate, zero-fill, invent expiry dates, or relabel a
  later observation as an earlier missing tick. Invalid responses are rejected.
- Quotes must have finite positive prices and matching underlying, strike,
  side, and expiry. Counts must be nonnegative integral values representable
  without loss. Conflicting OI aliases and crossed bid/ask quotes are rejected.
- The current OpenAlgo boundary can synthesize zero OI. Without field-level
  evidence of a genuine zero, Hermes stores NULL and an explicit issue code.
- OpenAlgo's observed quote shape does not carry a verified provider-event
  timestamp. Version-2 rows therefore have PARTIAL status and
  UNVERIFIED_PROVIDER_TIME provenance. The timestamp is explicitly the local
  receipt time; scheduled and request-start times are separate metadata.
- `hermes_verified_options` excludes these rows. Successfully receiving or
  storing a response never proves that its source value is fresh or correct.
  Future support for verified provider clocks requires a documented adapter
  contract and new validation; unknown data cannot silently become verified.
- These controls prevent identified classes of corruption and mislabeling.
  They do not guarantee that an upstream provider never supplies a false value.
- Version-1 records remain immutable legacy evidence. They are not silently
  upgraded, corrected, deleted, or certified by the version-2 rollout.
  Database gates reject new legacy-version inserts, including obsolete writer
  entry points. Changing the accepted version requires an explicit migration.

## Collection and recovery behavior

The supported CLI and LaunchAgent use `collector.recovery`. Raw acquisition
has a 3.5-second bound; late scheduler wakes are skipped. Calendar and expiry
discovery are bounded data requests, and no calendar weekday expiry is guessed.
The session is restricted to the existing weekday 09:15–15:30 IST scope plus
the OpenAlgo exchange-calendar response. Unknown calendar state fails closed.

Each normalized raw capture is committed to a local SQLite outbox with WAL
and synchronous=FULL before optional Greeks requests. Independent derived
work is bounded to two seconds and one job at a time; it can be skipped.
There is no automatic calculation backlog using today's quotes for old slots.

A separate database worker replays buffered observations. It backs off up to
60 seconds on outages; source failures also back off up to 60 seconds without
in-request retries. The outbox retains original payloads and timestamps, even
after acknowledgement, up to its 256 MiB payload budget. Once full it refuses
new data visibly. Disk-full or corruption must never trigger fabricated rows.
Skipped/rejected scheduled windows are recorded separately in the outbox's
`gaps` table; they never create price observations. A process that is completely
down cannot log its own gap, so reconcile full-session coverage against actual
RAW receipts as well. A gap report measures local captures, not provider freshness.

The PostgreSQL receipt and raw inserts share a transaction. An exact replay
is acknowledged once. A different payload with the same observation key,
an invalid parent, or a database constraint violation is quarantined. Raw OI
stays in `option_snapshot.oi`. Calculations link to the raw receipt and retain
their own request/receipt timestamps and calculation inputs. UPDATE/DELETE
triggers enforce append-only writes for the observation tables, including
rejection of TRUNCATE. These are application-integrity safeguards, not a
security boundary against a PostgreSQL superuser deliberately disabling them.

The Greeks endpoint's `spot_price` is retained as `calculation_spot_ltp`,
with its provider field name intact. The local service may use either a
synthetic forward or a spot fallback internally; Hermes does not certify
which one was used. `forward_price` stays NULL unless explicitly returned.
Every derived response must match the requested contract, side and expiry.

The watchdog runs every 20 seconds during the existing session window. It
reads the newest PostgreSQL RAW receipt, the sampler heartbeat, and OpenAlgo
health separately. It attempts no more than three collector restarts per IST
date; it never restarts OpenAlgo or PostgreSQL or runs trading APIs. Login
failures and stale rows produce local desktop notifications (subject to macOS
notification permissions), watchdog state, and durable incident codes. No
Telegram, email, account, position, order, or WebSocket integration is used.

The collector holds a single-process lock and an idle-sleep prevention
assertion while running. It exits on market close and discards late wake ticks.
Explicit sleep, shutdown, lost power, expired broker login, or provider downtime
can still cause gaps. Neither watchdog nor scheduler claims otherwise.

## Historical recovery

Historical fetches are operator-requested, exact-symbol, five-second candle
requests. They retain candle-start timestamps and are stored only in
`hermes_history_backfill`, classified HISTORICAL_BACKFILL. They never populate
the live raw tables or synthesize historical IV/Greeks. Duplicate/conflicting
or malformed candles are rejected, and ambiguous zero OI becomes NULL.
An unavailable/expired contract or an unavailable service leaves a gap.

## Operating commands

Run from `/Users/viswatej/Desktop/openalgo`:

```zsh
# Current runtime evidence; watchdog.json includes latest database receipt.
hermes_v0/.venv/bin/python -m hermes_v0.collector.recovery --status
less hermes_v0/runtime/watchdog.json

# Start the one configured collector during the approved session.
launchctl kickstart gui/501/com.openalgo.hermes-v0-option-metrics

# Flush durable buffered records without making market-data requests.
hermes_v0/automation/run_b04_option_metrics.zsh --replay

# Backfill a particular historical series, separately from live data.
hermes_v0/automation/run_b04_option_metrics.zsh --history-date 2026-09-08 --symbol NIFTY --exchange NSE_INDEX
```

The launcher retrieves credentials from Keychain; never put an API key in a
command, source file, incident, or status report. Resume live capture first
during a session; backfill holds the same lock and should run after the close.

```sql
-- Explicit provenance for received but unverified version-2 option values:
SELECT timestamp_ist, symbol, option_type, oi, data_status,
       timestamp_basis, freshness, scheduled_at, received_at, persisted_at
FROM hermes_observation_quality ORDER BY received_at DESC LIMIT 10;

-- Only evidence that actually satisfies a verified provider-time contract:
SELECT * FROM hermes_verified_options ORDER BY timestamp_ist DESC LIMIT 10;

-- Elapsed five-second slot coverage for the current IST date. Historical
-- backfill and derived rows cannot make a missing live slot appear complete.
WITH bounds AS (
  SELECT (now() AT TIME ZONE 'Asia/Kolkata')::date AS day
), slots AS (
  SELECT generate_series(
    (day + time '09:15') AT TIME ZONE 'Asia/Kolkata',
    LEAST((day + time '15:30') AT TIME ZONE 'Asia/Kolkata', now())
      - interval '5 seconds', interval '5 seconds') AS scheduled_at FROM bounds
  WHERE extract(isodow FROM day) < 6
)
SELECT count(*) AS elapsed_slots,
       count(r.observation_id) AS captured_slots,
       count(*) - count(r.observation_id) AS missing_slots
FROM slots s LEFT JOIN hermes_ingest_receipt r
  ON r.kind='RAW' AND r.symbol='NIFTY' AND r.scheduled_at=s.scheduled_at;
```

QA must exercise malformed input, alias conflicts, missing/zero OI, stale/late
ticks, process failure, source/auth and database outages, replay after restart,
conflicting replays, immutable writes, bounded restart policy, market-close
shutdown, and historical/live separation. Gaps are measured and allowed;
misclassified or fabricated observations fail QA. Live validation is separate
from simulated tests and is still required before live-readiness claims.

-- New provenance records only; prior observations are neither corrected nor erased.
CREATE TABLE IF NOT EXISTS hermes_ingest_receipt (
    observation_id TEXT PRIMARY KEY,
    digest TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('RAW','DERIVED','HISTORY')),
    symbol TEXT NOT NULL,
    scheduled_at TIMESTAMPTZ NOT NULL,
    request_started_at TIMESTAMPTZ NOT NULL,
    received_at TIMESTAMPTZ NOT NULL,
    persisted_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    timestamp_basis TEXT NOT NULL,
    freshness TEXT NOT NULL,
    issues JSONB NOT NULL,
    parent_observation_id TEXT REFERENCES hermes_ingest_receipt(observation_id),
    CHECK(scheduled_at <= request_started_at AND request_started_at <= received_at),
    CHECK ((kind='RAW' AND timestamp_basis='APPLICATION_RECEIPT'
            AND freshness='UNVERIFIED_PROVIDER_TIME' AND parent_observation_id IS NULL
            AND received_at < scheduled_at + interval '5 seconds')
        OR (kind='DERIVED' AND timestamp_basis='CALCULATION_RECEIPT'
            AND freshness='DERIVED_UNVERIFIED_INPUT_TIME' AND parent_observation_id IS NOT NULL)
        OR (kind='HISTORY' AND timestamp_basis='CANDLE_START'
            AND freshness='HISTORICAL_BACKFILL' AND parent_observation_id IS NULL))
);
ALTER TABLE hermes_ingest_receipt ADD COLUMN IF NOT EXISTS parent_observation_id TEXT
    REFERENCES hermes_ingest_receipt(observation_id);
CREATE INDEX IF NOT EXISTS hermes_receipt_latest_raw
    ON hermes_ingest_receipt(kind, received_at DESC);

CREATE TABLE IF NOT EXISTS hermes_history_backfill (
    observation_id TEXT PRIMARY KEY REFERENCES hermes_ingest_receipt(observation_id),
    payload JSONB NOT NULL,
    classification TEXT NOT NULL CHECK(classification='HISTORICAL_BACKFILL')
);

-- A consumer must explicitly join the provenance record for newly captured data.
-- Legacy rows have no such record and are not silently relabeled as verified.
CREATE OR REPLACE VIEW hermes_observation_quality AS
SELECT o.*, r.observation_id, r.scheduled_at, r.request_started_at,
       r.received_at, r.persisted_at, r.timestamp_basis, r.freshness, r.issues
FROM option_snapshot o JOIN hermes_ingest_receipt r
 ON r.kind='RAW' AND r.symbol=o.symbol AND r.received_at=o.timestamp_ist;

CREATE OR REPLACE VIEW hermes_verified_options AS
SELECT * FROM hermes_observation_quality
WHERE freshness='VERIFIED_PROVIDER_TIME' AND data_status='VALID';

CREATE OR REPLACE VIEW hermes_derived_quality AS
SELECT g.*, r.request_started_at AS calculation_requested_at,
       r.received_at AS calculation_received_at, r.persisted_at,
       r.parent_observation_id, r.freshness
FROM option_greeks_snapshot g JOIN hermes_ingest_receipt p
 ON p.kind='RAW' AND p.received_at=g.timestamp_ist
JOIN hermes_ingest_receipt r
 ON r.kind='DERIVED' AND r.parent_observation_id=p.observation_id
 AND r.symbol=g.option_symbol AND g.version=2;

CREATE OR REPLACE FUNCTION hermes_reject_observation_mutation() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'Hermes observations are append-only; mutation is forbidden';
END;
$$ LANGUAGE plpgsql;
DO $$
DECLARE target TEXT;
BEGIN
    FOREACH target IN ARRAY ARRAY['market_snapshot','option_snapshot','option_greeks_snapshot',
                                  'hermes_ingest_receipt','hermes_history_backfill'] LOOP
        IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgrelid=target::regclass
                       AND tgname='hermes_immutable_observation') THEN
            EXECUTE format('CREATE TRIGGER hermes_immutable_observation BEFORE UPDATE OR DELETE ON %I
                            FOR EACH ROW EXECUTE FUNCTION hermes_reject_observation_mutation()', target);
        END IF;
        IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgrelid=target::regclass
                       AND tgname='hermes_no_truncate') THEN
            EXECUTE format('CREATE TRIGGER hermes_no_truncate BEFORE TRUNCATE ON %I
                            FOR EACH STATEMENT EXECUTE FUNCTION hermes_reject_observation_mutation()', target);
        END IF;
    END LOOP;
    -- Preserve old evidence, but do not let an obsolete writer bypass the new
    -- contract by inserting newly captured observations as legacy version 1.
    FOREACH target IN ARRAY ARRAY['market_snapshot','option_snapshot','option_greeks_snapshot'] LOOP
        IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid=target::regclass
                       AND conname='hermes_recovery_version_gate') THEN
            EXECUTE format('ALTER TABLE %I ADD CONSTRAINT hermes_recovery_version_gate
                            CHECK (version=2 AND data_status=''PARTIAL'') NOT VALID', target);
        END IF;
    END LOOP;
END $$;

-- NOT VALID preserves legacy evidence without certifying it, while PostgreSQL
-- checks all future inserts. These constraints apply to version-2 raw rows only.
DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid='option_snapshot'::regclass
                   AND conname='hermes_v2_option_integrity') THEN
        ALTER TABLE option_snapshot ADD CONSTRAINT hermes_v2_option_integrity CHECK (
            version < 2 OR (data_status='PARTIAL' AND ltp IS NOT NULL AND ltp > 0
              AND ltp NOT IN ('NaN'::float8,'Infinity'::float8,'-Infinity'::float8)
              AND (oi IS NULL OR (oi > 0 AND oi <= 9007199254740991 AND oi=floor(oi)))
              AND (bid IS NULL OR (bid > 0 AND bid < 'Infinity'::float8))
              AND (ask IS NULL OR (ask > 0 AND ask < 'Infinity'::float8))
              AND (bid IS NULL OR ask IS NULL OR bid <= ask))
        ) NOT VALID;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid='market_snapshot'::regclass
                   AND conname='hermes_v2_market_integrity') THEN
        ALTER TABLE market_snapshot ADD CONSTRAINT hermes_v2_market_integrity CHECK (
            version < 2 OR (data_status='PARTIAL' AND spot_ltp IS NOT NULL AND spot_ltp > 0
              AND spot_ltp NOT IN ('NaN'::float8,'Infinity'::float8,'-Infinity'::float8))
        ) NOT VALID;
    END IF;
END $$;

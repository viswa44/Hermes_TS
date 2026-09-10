-- B04: calculated IV/Greeks are intentionally separate from raw option quotes.
-- This migration is idempotent and safe to apply to the existing native
-- PostgreSQL Hermes schema.  It never modifies or replaces prior observations.

CREATE TABLE IF NOT EXISTS option_greeks_snapshot (
    timestamp_ist       TIMESTAMPTZ       NOT NULL,
    underlying_symbol   VARCHAR(20)       NOT NULL,
    option_symbol       VARCHAR(80)       NOT NULL,
    strike              DOUBLE PRECISION  NOT NULL,
    option_type         VARCHAR(2)        NOT NULL CHECK (option_type IN ('CE', 'PE')),
    expiry_date         VARCHAR(10)       NOT NULL,
    trading_date        DATE              NOT NULL,

    option_ltp          DOUBLE PRECISION,
    underlying_ltp      DOUBLE PRECISION,
    calculation_option_ltp DOUBLE PRECISION,
    calculation_spot_ltp   DOUBLE PRECISION,
    implied_volatility  DOUBLE PRECISION,
    delta               DOUBLE PRECISION,
    gamma               DOUBLE PRECISION,
    theta               DOUBLE PRECISION,
    vega                DOUBLE PRECISION,
    rho                 DOUBLE PRECISION,
    interest_rate       DOUBLE PRECISION,
    forward_price       DOUBLE PRECISION,
    calculation_method  VARCHAR(40)       NOT NULL,

    data_status         data_status       NOT NULL DEFAULT 'VALID',
    source_latency_ms   INTEGER,
    error_class         VARCHAR(120),
    ingestion_time      TIMESTAMPTZ       NOT NULL DEFAULT NOW(),
    version             SMALLINT          NOT NULL DEFAULT 1,

    PRIMARY KEY (timestamp_ist, option_symbol, version)
);

-- These are calculation inputs returned by the Greeks endpoint. They remain
-- separate from the raw option-chain and index-quote LTPs for provenance.
ALTER TABLE option_greeks_snapshot
    ADD COLUMN IF NOT EXISTS calculation_option_ltp DOUBLE PRECISION;
ALTER TABLE option_greeks_snapshot
    ADD COLUMN IF NOT EXISTS calculation_spot_ltp DOUBLE PRECISION;

-- Calculations are a derived, versioned layer. Upgrade the short-lived B04
-- preview key only when necessary, preserving every existing row.
DO $$
DECLARE
    existing_primary_key TEXT;
BEGIN
    SELECT conname INTO existing_primary_key
    FROM pg_constraint
    WHERE conrelid = 'public.option_greeks_snapshot'::regclass
      AND contype = 'p';

    IF existing_primary_key IS NULL THEN
        ALTER TABLE option_greeks_snapshot
            ADD CONSTRAINT option_greeks_snapshot_pkey
            PRIMARY KEY (timestamp_ist, option_symbol, version);
    ELSIF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conrelid = 'public.option_greeks_snapshot'::regclass
          AND contype = 'p'
          AND pg_get_constraintdef(oid) = 'PRIMARY KEY (timestamp_ist, option_symbol, version)'
    ) THEN
        EXECUTE format(
            'ALTER TABLE public.option_greeks_snapshot DROP CONSTRAINT %I',
            existing_primary_key
        );
        ALTER TABLE option_greeks_snapshot
            ADD CONSTRAINT option_greeks_snapshot_pkey
            PRIMARY KEY (timestamp_ist, option_symbol, version);
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_option_greeks_snapshot_time
    ON option_greeks_snapshot (underlying_symbol, timestamp_ist DESC);
CREATE INDEX IF NOT EXISTS idx_option_greeks_snapshot_contract
    ON option_greeks_snapshot (option_symbol, timestamp_ist DESC);
CREATE INDEX IF NOT EXISTS idx_option_greeks_snapshot_status
    ON option_greeks_snapshot (data_status, timestamp_ist DESC);

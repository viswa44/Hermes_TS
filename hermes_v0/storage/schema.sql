-- Hermes V0 Database Schema
-- Phase 3: PostgreSQL DDL with TimescaleDB hypertable support
-- Run with: psql -d hermes -f schema.sql

-- Enable TimescaleDB extension (if available)
CREATE EXTENSION IF NOT EXISTS timescaledb CASCADE;
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- ============================================================================
-- ENUMS
-- ============================================================================

CREATE TYPE data_status AS ENUM (
    'VALID', 'PARTIAL', 'STALE', 'MISSING', 'REJECTED'
);

CREATE TYPE event_type AS ENUM (
    'COLLECTOR_START', 'COLLECTOR_STOP', 'FEED_DISCONNECT', 'FEED_RECONNECT',
    'MISSED_INTERVAL', 'VALIDATION_FAIL', 'DB_WRITE_FAIL', 'HEALTH_CHECK'
);

CREATE TYPE event_severity AS ENUM (
    'INFO', 'WARN', 'ERROR', 'CRITICAL'
);

CREATE TYPE outcome_status AS ENUM (
    'VALID', 'PARTIAL', 'MISSING', 'MARKET_CLOSED'
);

-- ============================================================================
-- RAW OBSERVATIONS (Immutable)
-- ============================================================================

-- Main market snapshot table (5-second aligned)
CREATE TABLE market_snapshot (
    -- Primary key: timestamp + symbol (composite for partitioning)
    timestamp_ist     TIMESTAMPTZ       NOT NULL,
    symbol            VARCHAR(20)       NOT NULL DEFAULT 'NIFTY',
    
    -- Trading date (IST) for partitioning
    trading_date      DATE              NOT NULL,
    
    -- Spot market data
    spot_ltp          DOUBLE PRECISION,
    spot_bid          DOUBLE PRECISION,
    spot_ask          DOUBLE PRECISION,
    spot_prev_close   DOUBLE PRECISION,
    spot_open         DOUBLE PRECISION,
    spot_high         DOUBLE PRECISION,
    spot_low          DOUBLE PRECISION,
    spot_volume       DOUBLE PRECISION,
    
    -- VIX
    vix               DOUBLE PRECISION,
    vix_prev_close    DOUBLE PRECISION,
    
    -- Option chain reference
    atm_strike        DOUBLE PRECISION,
    expiry_date       VARCHAR(10),      -- DDMMMYY format
    days_to_expiry    INTEGER,
    
    -- ATM CE (Call Option)
    atm_ce_ltp        DOUBLE PRECISION,
    atm_ce_bid        DOUBLE PRECISION,
    atm_ce_ask        DOUBLE PRECISION,
    atm_ce_bid_qty    DOUBLE PRECISION,
    atm_ce_ask_qty    DOUBLE PRECISION,
    atm_ce_volume     DOUBLE PRECISION,
    atm_ce_oi         DOUBLE PRECISION,
    atm_ce_iv         DOUBLE PRECISION,
    atm_ce_delta      DOUBLE PRECISION,
    atm_ce_gamma      DOUBLE PRECISION,
    atm_ce_theta      DOUBLE PRECISION,
    atm_ce_vega       DOUBLE PRECISION,
    
    -- ATM PE (Put Option)
    atm_pe_ltp        DOUBLE PRECISION,
    atm_pe_bid        DOUBLE PRECISION,
    atm_pe_ask        DOUBLE PRECISION,
    atm_pe_bid_qty    DOUBLE PRECISION,
    atm_pe_ask_qty    DOUBLE PRECISION,
    atm_pe_volume     DOUBLE PRECISION,
    atm_pe_oi         DOUBLE PRECISION,
    atm_pe_iv         DOUBLE PRECISION,
    atm_pe_delta      DOUBLE PRECISION,
    atm_pe_gamma      DOUBLE PRECISION,
    atm_pe_theta      DOUBLE PRECISION,
    atm_pe_vega       DOUBLE PRECISION,
    
    -- Data quality
    data_status       data_status       NOT NULL DEFAULT 'VALID',
    source_latency_ms INTEGER,
    ingestion_time    TIMESTAMPTZ       NOT NULL DEFAULT NOW(),
    provider_payload  JSONB,
    
    -- Contract version
    version           SMALLINT          NOT NULL DEFAULT 1,
    
    PRIMARY KEY (timestamp_ist, symbol)
);

-- Convert to TimescaleDB hypertable (partition by time)
SELECT create_hypertable('market_snapshot', 'timestamp_ist', 
    chunk_time_interval => INTERVAL '1 day',
    if_not_exists => TRUE
);

-- Indexes for common query patterns
CREATE INDEX idx_market_snapshot_trading_date ON market_snapshot (trading_date, symbol, timestamp_ist DESC);
CREATE INDEX idx_market_snapshot_symbol_time ON market_snapshot (symbol, timestamp_ist DESC);
CREATE INDEX idx_market_snapshot_status ON market_snapshot (data_status, timestamp_ist DESC);

-- Enable compression for older chunks (after 7 days)
ALTER TABLE market_snapshot SET (
    timescaledb.compress,
    timescaledb.compress_segmentby = 'symbol'
);

-- Compression policy (compress chunks older than 7 days)
SELECT add_compression_policy('market_snapshot', INTERVAL '7 days', if_not_exists => TRUE);


-- Individual option contracts (for strikes beyond ATM window)
CREATE TABLE option_snapshot (
    timestamp_ist     TIMESTAMPTZ       NOT NULL,
    symbol            VARCHAR(20)       NOT NULL,
    strike            DOUBLE PRECISION  NOT NULL,
    option_type       VARCHAR(2)        NOT NULL CHECK (option_type IN ('CE', 'PE')),
    expiry_date       VARCHAR(10)       NOT NULL,
    
    trading_date      DATE              NOT NULL,
    
    -- Market data
    ltp               DOUBLE PRECISION,
    bid               DOUBLE PRECISION,
    ask               DOUBLE PRECISION,
    bid_qty           DOUBLE PRECISION,
    ask_qty           DOUBLE PRECISION,
    volume            DOUBLE PRECISION,
    oi                DOUBLE PRECISION,
    iv                DOUBLE PRECISION,
    delta             DOUBLE PRECISION,
    gamma             DOUBLE PRECISION,
    theta             DOUBLE PRECISION,
    vega              DOUBLE PRECISION,
    
    -- Metadata
    moneyness         VARCHAR(10),      -- ATM, ITM1, OTM2, etc.
    lotsize           INTEGER,
    tick_size         DOUBLE PRECISION,
    
    -- Data quality
    data_status       data_status       NOT NULL DEFAULT 'VALID',
    source_latency_ms INTEGER,
    ingestion_time    TIMESTAMPTZ       NOT NULL DEFAULT NOW(),
    
    version           SMALLINT          NOT NULL DEFAULT 1,
    
    PRIMARY KEY (timestamp_ist, symbol, strike, option_type, expiry_date)
);

SELECT create_hypertable('option_snapshot', 'timestamp_ist',
    chunk_time_interval => INTERVAL '1 day',
    if_not_exists => TRUE
);

CREATE INDEX idx_option_snapshot_trading_date ON option_snapshot (trading_date, symbol, timestamp_ist DESC);
CREATE INDEX idx_option_snapshot_strike ON option_snapshot (symbol, strike, option_type, timestamp_ist DESC);
CREATE INDEX idx_option_snapshot_status ON option_snapshot (data_status, timestamp_ist DESC);

ALTER TABLE option_snapshot SET (
    timescaledb.compress,
    timescaledb.compress_segmentby = 'symbol, strike, option_type'
);

SELECT add_compression_policy('option_snapshot', INTERVAL '7 days', if_not_exists => TRUE);


-- ============================================================================
-- SYSTEM EVENTS (Observability)
-- ============================================================================

CREATE TABLE system_event (
    id                BIGSERIAL         PRIMARY KEY,
    event_time        TIMESTAMPTZ       NOT NULL DEFAULT NOW(),
    event_type        event_type        NOT NULL,
    severity          event_severity    NOT NULL,
    message           TEXT              NOT NULL,
    details           JSONB,
    trading_date      DATE,
    snapshot_timestamp_ist TIMESTAMPTZ,
    version           SMALLINT          NOT NULL DEFAULT 1
);

SELECT create_hypertable('system_event', 'event_time',
    chunk_time_interval => INTERVAL '1 day',
    if_not_exists => TRUE
);

CREATE INDEX idx_system_event_type_time ON system_event (event_type, event_time DESC);
CREATE INDEX idx_system_event_severity ON system_event (severity, event_time DESC);
CREATE INDEX idx_system_event_trading_date ON system_event (trading_date, event_time DESC);


-- ============================================================================
-- CALCULATED FEATURES (Recalculatable, Versioned)
-- ============================================================================

CREATE TABLE feature_snapshot (
    timestamp_ist     TIMESTAMPTZ       NOT NULL,
    symbol            VARCHAR(20)       NOT NULL DEFAULT 'NIFTY',
    trading_date      DATE              NOT NULL,
    
    -- Price returns (log returns)
    ret_5s            DOUBLE PRECISION,
    ret_30s           DOUBLE PRECISION,
    ret_1m            DOUBLE PRECISION,
    ret_3m            DOUBLE PRECISION,
    ret_5m            DOUBLE PRECISION,
    
    -- EMA levels
    ema_5             DOUBLE PRECISION,
    ema_20            DOUBLE PRECISION,
    ema_50            DOUBLE PRECISION,
    
    -- EMA slopes (points per 5s interval)
    ema_5_slope       DOUBLE PRECISION,
    ema_20_slope      DOUBLE PRECISION,
    ema_50_slope      DOUBLE PRECISION,
    
    -- Price distance from EMAs
    dist_ema_5        DOUBLE PRECISION,
    dist_ema_20       DOUBLE PRECISION,
    dist_ema_50       DOUBLE PRECISION,
    
    -- OI metrics (ATM window aggregate)
    atm_ce_oi_change         DOUBLE PRECISION,
    atm_pe_oi_change         DOUBLE PRECISION,
    atm_ce_oi_change_rate    DOUBLE PRECISION,
    atm_pe_oi_change_rate    DOUBLE PRECISION,
    total_oi_change          DOUBLE PRECISION,
    
    -- IV metrics
    atm_ce_iv_change     DOUBLE PRECISION,
    atm_pe_iv_change     DOUBLE PRECISION,
    atm_iv_avg_change    DOUBLE PRECISION,
    
    -- PCR & aggregate
    pcr_oi              DOUBLE PRECISION,
    pcr_volume          DOUBLE PRECISION,
    pcr_oi_change       DOUBLE PRECISION,
    
    -- Spread & latency
    atm_spread_pts      DOUBLE PRECISION,
    atm_spread_pct      DOUBLE PRECISION,
    data_latency_ms     INTEGER,
    
    -- Calculation metadata
    calc_version        SMALLINT          NOT NULL DEFAULT 1,
    calc_time           TIMESTAMPTZ       NOT NULL DEFAULT NOW(),
    
    PRIMARY KEY (timestamp_ist, symbol)
);

SELECT create_hypertable('feature_snapshot', 'timestamp_ist',
    chunk_time_interval => INTERVAL '1 day',
    if_not_exists => TRUE
);

CREATE INDEX idx_feature_snapshot_trading_date ON feature_snapshot (trading_date, symbol, timestamp_ist DESC);
CREATE INDEX idx_feature_snapshot_calc_version ON feature_snapshot (calc_version, timestamp_ist DESC);

ALTER TABLE feature_snapshot SET (
    timescaledb.compress,
    timescaledb.compress_segmentby = 'symbol'
);

SELECT add_compression_policy('feature_snapshot', INTERVAL '7 days', if_not_exists => TRUE);


-- ============================================================================
-- FUTURE OUTCOMES (Evidence Layer)
-- ============================================================================

CREATE TABLE future_outcome (
    -- Reference to source snapshot
    snapshot_timestamp_ist TIMESTAMPTZ       NOT NULL,
    symbol                 VARCHAR(20)       NOT NULL DEFAULT 'NIFTY',
    trading_date           DATE              NOT NULL,
    
    -- Horizon
    horizon_seconds        INTEGER           NOT NULL CHECK (horizon_seconds > 0),
    horizon_label          VARCHAR(10)       NOT NULL,
    
    -- Future spot
    future_spot            DOUBLE PRECISION,
    future_timestamp_ist   TIMESTAMPTZ,
    
    -- Returns
    future_return_pts      DOUBLE PRECISION,
    future_return_pct      DOUBLE PRECISION,
    future_direction       VARCHAR(4)        CHECK (future_direction IN ('UP', 'DOWN', 'FLAT')),
    
    -- Excursions within horizon
    mfe_pts                DOUBLE PRECISION,
    mae_pts                DOUBLE PRECISION,
    mfe_pct                DOUBLE PRECISION,
    mae_pct                DOUBLE PRECISION,
    
    -- Data quality
    outcome_status         outcome_status    NOT NULL DEFAULT 'VALID',
    missing_reason         TEXT,
    calc_time              TIMESTAMPTZ       NOT NULL DEFAULT NOW(),
    
    version                SMALLINT          NOT NULL DEFAULT 1,
    
    PRIMARY KEY (snapshot_timestamp_ist, symbol, horizon_seconds)
);

-- Not a hypertable (partitioned by snapshot_timestamp_ist which references market_snapshot)
CREATE INDEX idx_future_outcome_trading_date ON future_outcome (trading_date, symbol, snapshot_timestamp_ist DESC);
CREATE INDEX idx_future_outcome_horizon ON future_outcome (horizon_seconds, snapshot_timestamp_ist DESC);
CREATE INDEX idx_future_outcome_status ON future_outcome (outcome_status, snapshot_timestamp_ist DESC);

-- Foreign key to market_snapshot (optional, for referential integrity)
-- ALTER TABLE future_outcome 
--     ADD CONSTRAINT fk_future_outcome_snapshot 
--     FOREIGN KEY (snapshot_timestamp_ist, symbol) 
--     REFERENCES market_snapshot (timestamp_ist, symbol)
--     ON DELETE CASCADE;


-- ============================================================================
-- RETENTION POLICIES (Optional - uncomment to enable)
-- ============================================================================

-- Raw data: keep 90 days (regulatory)
-- SELECT add_retention_policy('market_snapshot', INTERVAL '90 days', if_not_exists => TRUE);
-- SELECT add_retention_policy('option_snapshot', INTERVAL '90 days', if_not_exists => TRUE);

-- Features: keep 180 days (recalculatable)
-- SELECT add_retention_policy('feature_snapshot', INTERVAL '180 days', if_not_exists => TRUE);

-- Outcomes: keep 180 days
-- SELECT add_retention_policy('future_outcome', INTERVAL '180 days', if_not_exists => TRUE);

-- System events: keep 30 days
-- SELECT add_retention_policy('system_event', INTERVAL '30 days', if_not_exists => TRUE);


-- ============================================================================
-- VIEWS FOR COMMON QUERIES
-- ============================================================================

-- Latest market snapshot per symbol
CREATE OR REPLACE VIEW latest_market_snapshot AS
SELECT DISTINCT ON (symbol) *
FROM market_snapshot
WHERE data_status = 'VALID'
ORDER BY symbol, timestamp_ist DESC;

-- Data quality summary per trading day
CREATE OR REPLACE VIEW daily_data_quality AS
SELECT 
    trading_date,
    symbol,
    COUNT(*) as total_snapshots,
    COUNT(*) FILTER (WHERE data_status = 'VALID') as valid_snapshots,
    COUNT(*) FILTER (WHERE data_status = 'PARTIAL') as partial_snapshots,
    COUNT(*) FILTER (WHERE data_status = 'STALE') as stale_snapshots,
    COUNT(*) FILTER (WHERE data_status = 'MISSING') as missing_snapshots,
    COUNT(*) FILTER (WHERE data_status = 'REJECTED') as rejected_snapshots,
    ROUND(100.0 * COUNT(*) FILTER (WHERE data_status = 'VALID') / NULLIF(COUNT(*), 0), 2) as valid_pct,
    MIN(timestamp_ist) as first_snapshot,
    MAX(timestamp_ist) as last_snapshot,
    AVG(source_latency_ms) as avg_source_latency_ms,
    MAX(source_latency_ms) as max_source_latency_ms
FROM market_snapshot
GROUP BY trading_date, symbol
ORDER BY trading_date DESC, symbol;

-- Feature completeness
CREATE OR REPLACE VIEW daily_feature_completeness AS
SELECT 
    fs.trading_date,
    fs.symbol,
    COUNT(*) as total_features,
    COUNT(ret_5m) as has_ret_5m,
    COUNT(ema_20) as has_ema_20,
    COUNT(pcr_oi) as has_pcr,
    ROUND(100.0 * COUNT(ret_5m) / NULLIF(COUNT(*), 0), 2) as ret_5m_pct
FROM feature_snapshot fs
GROUP BY fs.trading_date, fs.symbol
ORDER BY fs.trading_date DESC, fs.symbol;

-- Outcome labelling status
CREATE OR REPLACE VIEW daily_outcome_status AS
SELECT 
    fo.trading_date,
    fo.symbol,
    fo.horizon_label,
    COUNT(*) as total_outcomes,
    COUNT(*) FILTER (WHERE outcome_status = 'VALID') as valid_outcomes,
    COUNT(*) FILTER (WHERE outcome_status = 'MISSING') as missing_outcomes,
    COUNT(*) FILTER (WHERE outcome_status = 'MARKET_CLOSED') as closed_outcomes,
    ROUND(100.0 * COUNT(*) FILTER (WHERE outcome_status = 'VALID') / NULLIF(COUNT(*), 0), 2) as valid_pct
FROM future_outcome fo
GROUP BY fo.trading_date, fo.symbol, fo.horizon_label
ORDER BY fo.trading_date DESC, fo.symbol, fo.horizon_seconds;


-- ============================================================================
-- FUNCTIONS
-- ============================================================================

-- Get expected snapshot count for a trading day (9:15 - 15:30 = 375 min = 4500 intervals @ 5s)
CREATE OR REPLACE FUNCTION expected_snapshots_per_day()
RETURNS INTEGER AS $$
BEGIN
    RETURN 4500; -- 6.25 hours * 60 min * 12 intervals per min
END;
$$ LANGUAGE plpgsql IMMUTABLE;

-- Check if a timestamp is market hours (IST)
CREATE OR REPLACE FUNCTION is_market_hours(ts TIMESTAMPTZ)
RETURNS BOOLEAN AS $$
BEGIN
    RETURN EXTRACT(HOUR FROM ts AT TIME ZONE 'Asia/Kolkata') >= 9
       AND EXTRACT(HOUR FROM ts AT TIME ZONE 'Asia/Kolkata') < 15
       OR (EXTRACT(HOUR FROM ts AT TIME ZONE 'Asia/Kolkata') = 15 
           AND EXTRACT(MINUTE FROM ts AT TIME ZONE 'Asia/Kolkata') <= 30);
END;
$$ LANGUAGE plpgsql IMMUTABLE;

-- Align timestamp to 5-second boundary
CREATE OR REPLACE FUNCTION align_to_5s(ts TIMESTAMPTZ)
RETURNS TIMESTAMPTZ AS $$
BEGIN
    RETURN date_trunc('minute', ts) 
         + FLOOR(EXTRACT(SECOND FROM ts) / 5) * INTERVAL '5 seconds';
END;
$$ LANGUAGE plpgsql IMMUTABLE;


-- ============================================================================
-- GRANTS (Adjust for your user)
-- ============================================================================

-- GRANT SELECT, INSERT ON ALL TABLES IN SCHEMA public TO hermes_user;
-- GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO hermes_user;
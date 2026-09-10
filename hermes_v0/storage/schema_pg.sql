-- Hermes V0 Database Schema (PostgreSQL 14+ compatible, no TimescaleDB required)
-- Run with: psql -d hermes -f schema_pg.sql

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
    timestamp_ist     TIMESTAMPTZ       NOT NULL,
    symbol            VARCHAR(20)       NOT NULL DEFAULT 'NIFTY',
    trading_date      DATE              NOT NULL,
    
    spot_ltp          DOUBLE PRECISION,
    spot_bid          DOUBLE PRECISION,
    spot_ask          DOUBLE PRECISION,
    spot_prev_close   DOUBLE PRECISION,
    spot_open         DOUBLE PRECISION,
    spot_high         DOUBLE PRECISION,
    spot_low          DOUBLE PRECISION,
    spot_volume       DOUBLE PRECISION,
    
    vix               DOUBLE PRECISION,
    vix_prev_close    DOUBLE PRECISION,
    
    atm_strike        DOUBLE PRECISION,
    expiry_date       VARCHAR(10),
    days_to_expiry    INTEGER,
    
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
    
    data_status       data_status       NOT NULL DEFAULT 'VALID',
    source_latency_ms INTEGER,
    ingestion_time    TIMESTAMPTZ       NOT NULL DEFAULT NOW(),
    provider_payload  JSONB,
    version           SMALLINT          NOT NULL DEFAULT 1,
    
    PRIMARY KEY (timestamp_ist, symbol)
);

-- Indexes for common query patterns
CREATE INDEX idx_market_snapshot_trading_date ON market_snapshot (trading_date, symbol, timestamp_ist DESC);
CREATE INDEX idx_market_snapshot_symbol_time ON market_snapshot (symbol, timestamp_ist DESC);
CREATE INDEX idx_market_snapshot_status ON market_snapshot (data_status, timestamp_ist DESC);

-- Optional: Native partitioning by trading_date (PostgreSQL 11+)
-- Uncomment to enable:
-- CREATE TABLE market_snapshot_partitioned (LIKE market_snapshot INCLUDING ALL) PARTITION BY RANGE (trading_date);
-- CREATE TABLE market_snapshot_2024 PARTITION OF market_snapshot_partitioned FOR VALUES FROM ('2024-01-01') TO ('2025-01-01');
-- CREATE TABLE market_snapshot_2025 PARTITION OF market_snapshot_partitioned FOR VALUES FROM ('2025-01-01') TO ('2026-01-01');


-- Individual option contracts (for strikes beyond ATM window)
CREATE TABLE option_snapshot (
    timestamp_ist     TIMESTAMPTZ       NOT NULL,
    symbol            VARCHAR(20)       NOT NULL,
    strike            DOUBLE PRECISION  NOT NULL,
    option_type       VARCHAR(2)        NOT NULL CHECK (option_type IN ('CE', 'PE')),
    expiry_date       VARCHAR(10)       NOT NULL,
    trading_date      DATE              NOT NULL,
    
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
    
    moneyness         VARCHAR(10),
    lotsize           INTEGER,
    tick_size         DOUBLE PRECISION,
    
    data_status       data_status       NOT NULL DEFAULT 'VALID',
    source_latency_ms INTEGER,
    ingestion_time    TIMESTAMPTZ       NOT NULL DEFAULT NOW(),
    version           SMALLINT          NOT NULL DEFAULT 1,
    
    PRIMARY KEY (timestamp_ist, symbol, strike, option_type, expiry_date)
);

CREATE INDEX idx_option_snapshot_trading_date ON option_snapshot (trading_date, symbol, timestamp_ist DESC);
CREATE INDEX idx_option_snapshot_strike ON option_snapshot (symbol, strike, option_type, timestamp_ist DESC);
CREATE INDEX idx_option_snapshot_status ON option_snapshot (data_status, timestamp_ist DESC);


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
    
    ret_5s            DOUBLE PRECISION,
    ret_30s           DOUBLE PRECISION,
    ret_1m            DOUBLE PRECISION,
    ret_3m            DOUBLE PRECISION,
    ret_5m            DOUBLE PRECISION,
    
    ema_5             DOUBLE PRECISION,
    ema_20            DOUBLE PRECISION,
    ema_50            DOUBLE PRECISION,
    
    ema_5_slope       DOUBLE PRECISION,
    ema_20_slope      DOUBLE PRECISION,
    ema_50_slope      DOUBLE PRECISION,
    
    dist_ema_5        DOUBLE PRECISION,
    dist_ema_20       DOUBLE PRECISION,
    dist_ema_50       DOUBLE PRECISION,
    
    atm_ce_oi_change         DOUBLE PRECISION,
    atm_pe_oi_change         DOUBLE PRECISION,
    atm_ce_oi_change_rate    DOUBLE PRECISION,
    atm_pe_oi_change_rate    DOUBLE PRECISION,
    total_oi_change          DOUBLE PRECISION,
    
    atm_ce_iv_change     DOUBLE PRECISION,
    atm_pe_iv_change     DOUBLE PRECISION,
    atm_iv_avg_change    DOUBLE PRECISION,
    
    pcr_oi              DOUBLE PRECISION,
    pcr_volume          DOUBLE PRECISION,
    pcr_oi_change       DOUBLE PRECISION,
    
    atm_spread_pts      DOUBLE PRECISION,
    atm_spread_pct      DOUBLE PRECISION,
    data_latency_ms     INTEGER,
    
    calc_version        SMALLINT          NOT NULL DEFAULT 1,
    calc_time           TIMESTAMPTZ       NOT NULL DEFAULT NOW(),
    
    PRIMARY KEY (timestamp_ist, symbol)
);

CREATE INDEX idx_feature_snapshot_trading_date ON feature_snapshot (trading_date, symbol, timestamp_ist DESC);
CREATE INDEX idx_feature_snapshot_calc_version ON feature_snapshot (calc_version, timestamp_ist DESC);


-- ============================================================================
-- FUTURE OUTCOMES (Evidence Layer)
-- ============================================================================

CREATE TABLE future_outcome (
    snapshot_timestamp_ist TIMESTAMPTZ       NOT NULL,
    symbol                 VARCHAR(20)       NOT NULL DEFAULT 'NIFTY',
    trading_date           DATE              NOT NULL,
    horizon_seconds        INTEGER           NOT NULL CHECK (horizon_seconds > 0),
    horizon_label          VARCHAR(10)       NOT NULL,
    future_spot            DOUBLE PRECISION,
    future_timestamp_ist   TIMESTAMPTZ,
    future_return_pts      DOUBLE PRECISION,
    future_return_pct      DOUBLE PRECISION,
    future_direction       VARCHAR(4)        CHECK (future_direction IN ('UP', 'DOWN', 'FLAT')),
    mfe_pts                DOUBLE PRECISION,
    mae_pts                DOUBLE PRECISION,
    mfe_pct                DOUBLE PRECISION,
    mae_pct                DOUBLE PRECISION,
    outcome_status         outcome_status    NOT NULL DEFAULT 'VALID',
    missing_reason         TEXT,
    calc_time              TIMESTAMPTZ       NOT NULL DEFAULT NOW(),
    version                SMALLINT          NOT NULL DEFAULT 1,
    
    PRIMARY KEY (snapshot_timestamp_ist, symbol, horizon_seconds)
);

CREATE INDEX idx_future_outcome_trading_date ON future_outcome (trading_date, symbol, snapshot_timestamp_ist DESC);
CREATE INDEX idx_future_outcome_horizon ON future_outcome (horizon_seconds, snapshot_timestamp_ist DESC);
CREATE INDEX idx_future_outcome_status ON future_outcome (outcome_status, snapshot_timestamp_ist DESC);


-- ============================================================================
-- VIEWS FOR COMMON QUERIES
-- ============================================================================

CREATE OR REPLACE VIEW latest_market_snapshot AS
SELECT DISTINCT ON (symbol) *
FROM market_snapshot
WHERE data_status = 'VALID'
ORDER BY symbol, timestamp_ist DESC;

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

CREATE OR REPLACE VIEW daily_outcome_status AS
SELECT 
    fo.trading_date,
    fo.symbol,
    fo.horizon_label,
    fo.horizon_seconds,
    COUNT(*) as total_outcomes,
    COUNT(*) FILTER (WHERE outcome_status = 'VALID') as valid_outcomes,
    COUNT(*) FILTER (WHERE outcome_status = 'MISSING') as missing_outcomes,
    COUNT(*) FILTER (WHERE outcome_status = 'MARKET_CLOSED') as closed_outcomes,
    ROUND(100.0 * COUNT(*) FILTER (WHERE outcome_status = 'VALID') / NULLIF(COUNT(*), 0), 2) as valid_pct
FROM future_outcome fo
GROUP BY fo.trading_date, fo.symbol, fo.horizon_label, fo.horizon_seconds
ORDER BY fo.trading_date DESC, fo.symbol, fo.horizon_seconds;


-- ============================================================================
-- FUNCTIONS
-- ============================================================================

CREATE OR REPLACE FUNCTION expected_snapshots_per_day()
RETURNS INTEGER AS $$
BEGIN
    RETURN 4500;
END;
$$ LANGUAGE plpgsql IMMUTABLE;

CREATE OR REPLACE FUNCTION is_market_hours(ts TIMESTAMPTZ)
RETURNS BOOLEAN AS $$
BEGIN
    RETURN EXTRACT(HOUR FROM ts AT TIME ZONE 'Asia/Kolkata') >= 9
       AND EXTRACT(HOUR FROM ts AT TIME ZONE 'Asia/Kolkata') < 15
       OR (EXTRACT(HOUR FROM ts AT TIME ZONE 'Asia/Kolkata') = 15 
           AND EXTRACT(MINUTE FROM ts AT TIME ZONE 'Asia/Kolkata') <= 30);
END;
$$ LANGUAGE plpgsql IMMUTABLE;

CREATE OR REPLACE FUNCTION align_to_5s(ts TIMESTAMPTZ)
RETURNS TIMESTAMPTZ AS $$
BEGIN
    RETURN date_trunc('minute', ts) 
         + FLOOR(EXTRACT(SECOND FROM ts) / 5) * INTERVAL '5 seconds';
END;
$$ LANGUAGE plpgsql IMMUTABLE;
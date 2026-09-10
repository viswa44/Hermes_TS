"""Hermes V0 Configuration.

Centralized settings for collector, database, and feature pipeline.
"""

import os
from dataclasses import dataclass
from typing import Optional


DEFAULT_INDEX_EXCHANGE_MAP = (
    ("NIFTY", "NSE_INDEX"),
    ("BANKNIFTY", "NSE_INDEX"),
    ("SENSEX", "BSE_INDEX"),
)


@dataclass(frozen=True)
class OpenAlgoConfig:
    """Read-only OpenAlgo connectivity settings."""
    host: str = os.environ.get("OPENALGO_HOST", "http://127.0.0.1:5000").rstrip("/")
    api_key: str = os.environ.get("OPENALGO_API_KEY", "")
    health_path: str = "/health/status"
    connect_timeout_seconds: float = float(
        os.environ.get("OPENALGO_CONNECT_TIMEOUT_SECONDS", "5")
    )
    connect_max_attempts: int = int(os.environ.get("OPENALGO_CONNECT_MAX_ATTEMPTS", "3"))
    connect_backoff_seconds: float = float(
        os.environ.get("OPENALGO_CONNECT_BACKOFF_SECONDS", "0.5")
    )
    connect_max_backoff_seconds: float = float(
        os.environ.get("OPENALGO_CONNECT_MAX_BACKOFF_SECONDS", "3")
    )
    
    # Symbols
    underlying: str = os.environ.get("HERMES_UNDERLYING", "NIFTY").upper()
    vix_symbol: str = "INDIA VIX"
    index_exchange: str = "NSE_INDEX"
    index_exchange_map: tuple[tuple[str, str], ...] = DEFAULT_INDEX_EXCHANGE_MAP
    options_exchange: str = "NFO"
    strike_interval: int = 50


@dataclass(frozen=True)
class CollectorConfig:
    """Collector runtime configuration."""
    
    # OpenAlgo connection
    openalgo_host: str = os.environ.get("OPENALGO_HOST", "http://127.0.0.1:5000")
    openalgo_api_key: str = os.environ.get("OPENALGO_API_KEY", "")
    
    # Symbols to collect
    symbols: tuple = ("NIFTY",)
    vix_symbol: str = "INDIA VIX"
    index_exchange: str = "NSE_INDEX"
    options_exchange: str = "NFO"
    
    # Collection schedule
    interval_seconds: int = 5
    market_open: str = "09:15"
    market_close: str = "15:30"
    timezone: str = "Asia/Kolkata"
    
    # Data quality thresholds
    max_source_latency_ms: int = 5000
    max_staleness_seconds: int = 10
    validation_enabled: bool = True
    
    # Retry policy
    max_retries: int = 3
    base_backoff_seconds: float = 0.5
    max_backoff_seconds: float = 5.0
    request_timeout_seconds: float = 10.0
    
    # Health check
    health_port: int = 8080
    health_enabled: bool = True


@dataclass(frozen=True)
class DatabaseConfig:
    """Database connection configuration."""
    
    # PostgreSQL/TimescaleDB
    host: str = os.environ.get("HERMES_DB_HOST", "localhost")
    port: int = int(os.environ.get("HERMES_DB_PORT", "5432"))
    database: str = os.environ.get("HERMES_DB_NAME", "hermes")
    user: str = os.environ.get("HERMES_DB_USER", "postgres")
    password: str = os.environ.get("HERMES_DB_PASSWORD", "")
    
    # Connection pool
    pool_size: int = 5
    max_overflow: int = 10
    pool_timeout: int = 30
    pool_recycle: int = 3600
    
    # TimescaleDB specific
    use_timescaledb: bool = True
    compression_enabled: bool = True
    retention_days: int = 90


@dataclass(frozen=True)
class FeatureConfig:
    """Feature calculation configuration."""
    
    # EMA periods
    ema_periods: tuple = (5, 20, 50)
    
    # Return horizons (seconds)
    return_horizons: tuple = (5, 30, 60, 180, 300)
    
    # Feature version
    calc_version: int = 1
    
    # Minimum data points for EMA
    min_ema_points: int = 50


@dataclass(frozen=True)
class OutcomeConfig:
    """Future outcome labelling configuration."""
    
    # Horizons in seconds
    horizons: tuple = (30, 60, 180, 300, 600)
    horizon_labels: tuple = ("30s", "1m", "3m", "5m", "10m")
    
    # Minimum future data points required
    min_future_points: int = 1
    
    # Market hours buffer (seconds before close to stop labeling)
    close_buffer_seconds: int = 300


@dataclass(frozen=True)
class Config:
    """Root configuration."""
    openalgo: OpenAlgoConfig = OpenAlgoConfig()
    collector: CollectorConfig = CollectorConfig()
    database: DatabaseConfig = DatabaseConfig()
    features: FeatureConfig = FeatureConfig()
    outcomes: OutcomeConfig = OutcomeConfig()


CFG = Config()

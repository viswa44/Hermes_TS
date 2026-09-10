"""Hermes V0 Configuration.

Centralized settings for collector, database, and feature pipeline.
"""

import os
from dataclasses import dataclass, field
from typing import Optional


# Kept as data rather than conditionals in the collector so changing the
# selected index never requires a code change.  SENSEX is included because the
# local OpenAlgo contract recognises it on BSE_INDEX; entitlement remains a
# live-provider concern.
DEFAULT_INDEX_EXCHANGE_MAP = (
    ("NIFTY", "NSE_INDEX"),
    ("BANKNIFTY", "NSE_INDEX"),
    ("SENSEX", "BSE_INDEX"),
)


@dataclass(frozen=True)
class OpenAlgoConfig:
    """Read-only OpenAlgo connectivity settings.

    Credentials deliberately come from the environment so Hermes never embeds
    or logs an API key.  The B01 probe only calls ``GET /health/status``.
    """
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
    interval_seconds: int = 5
    market_open: str = "09:15"  # HH:MM format
    market_close: str = "15:30"
    timezone: str = "Asia/Kolkata"
    
    # Data quality thresholds
    max_source_latency_ms: int = 5000
    max_staleness_seconds: int = 10
    validation_enabled: bool = True
    
    # Retry policy
    max_retries: int = 3
    base_backoff_ms: int = 100
    max_backoff_ms: int = 5000
    request_timeout_seconds: float = 10.0
    
    # Health
    health_port: int = 8080
    health_enabled: bool = True
    max_drift_ms: float = 100.0


@dataclass(frozen=True)
class DatabaseConfig:
    """Database connection configuration."""
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
    ema_periods: tuple = (5, 20, 50)
    return_horizons: tuple = (5, 30, 60, 180, 300)  # seconds
    calc_version: int = 1
    min_ema_points: int = 50


@dataclass(frozen=True)
class OutcomeConfig:
    """Future outcome labelling configuration."""
    horizons: tuple = (30, 60, 180, 300, 600)
    horizon_labels: tuple = ("30s", "1m", "3m", "5m", "10m")
    min_future_points: int = 1
    close_buffer_seconds: int = 300


@dataclass(frozen=True)
class Config:
    """Root configuration."""
    openalgo: OpenAlgoConfig = field(default_factory=OpenAlgoConfig)
    collector: CollectorConfig = field(default_factory=CollectorConfig)
    database: DatabaseConfig = field(default_factory=DatabaseConfig)
    features: FeatureConfig = field(default_factory=FeatureConfig)
    outcomes: OutcomeConfig = field(default_factory=OutcomeConfig)


# Global config instance
_CFG: Optional[Config] = None


def get_config() -> Config:
    """Get global config instance (singleton)."""
    global _CFG
    if _CFG is None:
        _CFG = Config()
    return _CFG


# Backward compatibility
CFG = get_config()


def set_config(config: Config):
    """Set global config (for testing)."""
    global _CFG
    _CFG = config

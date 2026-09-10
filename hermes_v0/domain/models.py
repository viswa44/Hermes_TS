"""Hermes V0 Data Contract — Versioned snapshot and feature models.
models.py
Phase 0: Data Contract
Phase 3: Database DDL

Raw observations (immutable) → Calculated features (recalculatable) → Future outcomes (evidence)
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional, Literal
from enum import Enum
from zoneinfo import ZoneInfo


IST = ZoneInfo("Asia/Kolkata")


class DataStatus(str, Enum):
    """Data quality status for every snapshot."""
    VALID = "VALID"
    PARTIAL = "PARTIAL"
    STALE = "STALE"
    MISSING = "MISSING"
    REJECTED = "REJECTED"


class SnapshotVersion(int, Enum):
    """Contract version for schema evolution."""
    V1 = 1


@dataclass(frozen=True)
class MarketSnapshot:
    """Raw market observation at a single timestamp (5-second aligned).
    
    Immutable once written. Contains only what the broker/provider actually exposes.
    No derived/calculated fields here.
    """
    # Identity & timing (required, no defaults)
    timestamp_ist: datetime
    trading_date: str  # YYYY-MM-DD in IST
    symbol: str = "NIFTY"
    
    # Contract
    version: int = SnapshotVersion.V1
    
    # Spot market
    spot_ltp: Optional[float] = None
    spot_bid: Optional[float] = None
    spot_ask: Optional[float] = None
    spot_prev_close: Optional[float] = None
    spot_open: Optional[float] = None
    spot_high: Optional[float] = None
    spot_low: Optional[float] = None
    spot_volume: Optional[float] = None
    
    # VIX
    vix: Optional[float] = None
    vix_prev_close: Optional[float] = None
    
    # Option chain reference
    atm_strike: Optional[float] = None
    expiry_date: Optional[str] = None  # DDMMMYY
    days_to_expiry: Optional[int] = None
    
    # ATM option snapshot (core window: ATM ±2 strikes + high OI)
    atm_ce_ltp: Optional[float] = None
    atm_ce_bid: Optional[float] = None
    atm_ce_ask: Optional[float] = None
    atm_ce_bid_qty: Optional[float] = None
    atm_ce_ask_qty: Optional[float] = None
    atm_ce_volume: Optional[float] = None
    atm_ce_oi: Optional[float] = None
    atm_ce_iv: Optional[float] = None
    atm_ce_delta: Optional[float] = None
    atm_ce_gamma: Optional[float] = None
    atm_ce_theta: Optional[float] = None
    atm_ce_vega: Optional[float] = None
    
    atm_pe_ltp: Optional[float] = None
    atm_pe_bid: Optional[float] = None
    atm_pe_ask: Optional[float] = None
    atm_pe_bid_qty: Optional[float] = None
    atm_pe_ask_qty: Optional[float] = None
    atm_pe_volume: Optional[float] = None
    atm_pe_oi: Optional[float] = None
    atm_pe_iv: Optional[float] = None
    atm_pe_delta: Optional[float] = None
    atm_pe_gamma: Optional[float] = None
    atm_pe_theta: Optional[float] = None
    atm_pe_vega: Optional[float] = None
    
    # Data quality
    data_status: DataStatus = DataStatus.VALID
    source_latency_ms: Optional[int] = None
    ingestion_time: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    provider_payload: Optional[str] = None  # JSON string for audit
    
    def __post_init__(self):
        if self.timestamp_ist.tzinfo is None:
            # Legacy callers provide a local market clock without tzinfo.
            # Make its intended IST meaning explicit before PostgreSQL writes.
            object.__setattr__(self, 'timestamp_ist', self.timestamp_ist.replace(tzinfo=IST))
        if self.ingestion_time.tzinfo is None:
            object.__setattr__(self, 'ingestion_time', self.ingestion_time.replace(tzinfo=timezone.utc))


@dataclass(frozen=True)
class OptionSnapshot:
    """Individual option contract snapshot (for strikes beyond ATM window).
    
    One row per contract per timestamp.
    """
    # Identity (required)
    timestamp_ist: datetime
    trading_date: str
    symbol: str
    strike: float
    option_type: Literal["CE", "PE"]
    expiry_date: str
    
    # Contract
    version: int = SnapshotVersion.V1
    
    # Market data
    ltp: Optional[float] = None
    bid: Optional[float] = None
    ask: Optional[float] = None
    bid_qty: Optional[float] = None
    ask_qty: Optional[float] = None
    volume: Optional[float] = None
    oi: Optional[float] = None
    iv: Optional[float] = None
    delta: Optional[float] = None
    gamma: Optional[float] = None
    theta: Optional[float] = None
    vega: Optional[float] = None
    
    # Metadata
    moneyness: Optional[str] = None  # ATM, ITM1, OTM2, etc.
    lotsize: Optional[int] = None
    tick_size: Optional[float] = None
    
    # Data quality
    data_status: DataStatus = DataStatus.VALID
    source_latency_ms: Optional[int] = None
    ingestion_time: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def __post_init__(self):
        if self.timestamp_ist.tzinfo is None:
            object.__setattr__(self, 'timestamp_ist', self.timestamp_ist.replace(tzinfo=IST))
        if self.ingestion_time.tzinfo is None:
            object.__setattr__(self, 'ingestion_time', self.ingestion_time.replace(tzinfo=timezone.utc))


@dataclass(frozen=True)
class OptionGreeksSnapshot:
    """Versioned Black-76 calculation attached to one raw option observation.

    IV and Greeks returned by OpenAlgo are calculations, not raw broker fields.
    They live in their own append-only record so research can distinguish the
    quoted option LTP from the model output derived from it.
    """

    timestamp_ist: datetime
    trading_date: str
    underlying_symbol: str
    option_symbol: str
    strike: float
    option_type: Literal["CE", "PE"]
    expiry_date: str

    option_ltp: Optional[float] = None
    underlying_ltp: Optional[float] = None
    # The Greeks endpoint fetches its own option quote. Keep its calculation
    # inputs distinct from the simultaneous raw option-chain observation.
    calculation_option_ltp: Optional[float] = None
    calculation_spot_ltp: Optional[float] = None
    implied_volatility: Optional[float] = None
    delta: Optional[float] = None
    gamma: Optional[float] = None
    theta: Optional[float] = None
    vega: Optional[float] = None
    rho: Optional[float] = None
    interest_rate: Optional[float] = None
    forward_price: Optional[float] = None
    calculation_method: str = "OPENALGO_BLACK76"
    data_status: DataStatus = DataStatus.VALID
    source_latency_ms: Optional[int] = None
    error_class: Optional[str] = None
    ingestion_time: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    version: int = SnapshotVersion.V1

    def __post_init__(self):
        if self.timestamp_ist.tzinfo is None:
            object.__setattr__(self, 'timestamp_ist', self.timestamp_ist.replace(tzinfo=IST))
        if self.ingestion_time.tzinfo is None:
            object.__setattr__(self, 'ingestion_time', self.ingestion_time.replace(tzinfo=timezone.utc))


@dataclass(frozen=True)
class FeatureSnapshot:
    """Calculated features derived from raw snapshots.
    
    Versioned so formulas can change without corrupting history.
    Never written by collector — only by feature pipeline.
    """
    # Identity (required)
    timestamp_ist: datetime
    trading_date: str
    symbol: str = "NIFTY"
    
    # Contract
    version: int = SnapshotVersion.V1
    
    # Price returns (log returns)
    ret_5s: Optional[float] = None
    ret_30s: Optional[float] = None
    ret_1m: Optional[float] = None
    ret_3m: Optional[float] = None
    ret_5m: Optional[float] = None
    
    # EMA levels
    ema_5: Optional[float] = None
    ema_20: Optional[float] = None
    ema_50: Optional[float] = None
    
    # EMA slopes (points per 5s interval)
    ema_5_slope: Optional[float] = None
    ema_20_slope: Optional[float] = None
    ema_50_slope: Optional[float] = None
    
    # Price distance from EMAs
    dist_ema_5: Optional[float] = None
    dist_ema_20: Optional[float] = None
    dist_ema_50: Optional[float] = None
    
    # OI metrics (ATM window aggregate)
    atm_ce_oi_change: Optional[float] = None
    atm_pe_oi_change: Optional[float] = None
    atm_ce_oi_change_rate: Optional[float] = None
    atm_pe_oi_change_rate: Optional[float] = None
    total_oi_change: Optional[float] = None
    
    # IV metrics
    atm_ce_iv_change: Optional[float] = None
    atm_pe_iv_change: Optional[float] = None
    atm_iv_avg_change: Optional[float] = None
    
    # PCR & aggregate
    pcr_oi: Optional[float] = None
    pcr_volume: Optional[float] = None
    pcr_oi_change: Optional[float] = None
    
    # Spread & latency
    atm_spread_pts: Optional[float] = None
    atm_spread_pct: Optional[float] = None
    data_latency_ms: Optional[int] = None
    
    # Calculation metadata
    calc_version: int = 1
    calc_time: datetime = field(default_factory=datetime.utcnow)


@dataclass(frozen=True)
class FutureOutcome:
    """Future market state attached to a past snapshot (evidence layer).
    
    One row per snapshot per horizon. Prevents look-ahead bias by design.
    """
    # Reference to source snapshot (required)
    snapshot_timestamp_ist: datetime
    trading_date: str
    symbol: str = "NIFTY"
    horizon_seconds: int = 0
    horizon_label: str = ""
    
    # Contract
    version: int = SnapshotVersion.V1
    
    # Future spot
    future_spot: Optional[float] = None
    future_timestamp_ist: Optional[datetime] = None
    
    # Returns
    future_return_pts: Optional[float] = None
    future_return_pct: Optional[float] = None
    future_direction: Optional[Literal["UP", "DOWN", "FLAT"]] = None
    
    # Excursions within horizon
    mfe_pts: Optional[float] = None   # Max Favourable Excursion
    mae_pts: Optional[float] = None   # Max Adverse Excursion
    mfe_pct: Optional[float] = None
    mae_pct: Optional[float] = None
    
    # Data quality
    outcome_status: DataStatus = DataStatus.VALID
    missing_reason: Optional[str] = None
    calc_time: datetime = field(default_factory=datetime.utcnow)


@dataclass(frozen=True)
class SystemEvent:
    """Operational events for observability and debugging."""
    # Required
    event_time: datetime
    event_type: str  # "COLLECTOR_START", "COLLECTOR_STOP", "FEED_DISCONNECT", "FEED_RECONNECT", "MISSED_INTERVAL", "VALIDATION_FAIL", "DB_WRITE_FAIL"
    severity: Literal["INFO", "WARN", "ERROR", "CRITICAL"]
    message: str
    
    # Contract
    version: int = SnapshotVersion.V1
    details: Optional[str] = None
    trading_date: str = ""
    snapshot_timestamp_ist: Optional[datetime] = None


# Field metadata for data contract documentation
FIELD_METADATA = {
    "market_snapshot": {
        "timestamp_ist": {"type": "TIMESTAMPTZ", "unit": "IST", "nullable": False, "source": "collector_clock", "validation": "aligned to 5s boundary", "rationale": "Primary key, IST for market alignment"},
        "trading_date": {"type": "DATE", "unit": "IST", "nullable": False, "source": "derived", "validation": "matches timestamp_ist date", "rationale": "Partition key"},
        "symbol": {"type": "VARCHAR(20)", "unit": "", "nullable": False, "source": "config", "validation": "NIFTY|BANKNIFTY|FINNIFTY", "rationale": "Multi-symbol support"},
        "spot_ltp": {"type": "DOUBLE PRECISION", "unit": "INR", "nullable": True, "source": "broker_quote", "validation": "> 0", "rationale": "Core price observation"},
        "spot_bid": {"type": "DOUBLE PRECISION", "unit": "INR", "nullable": True, "source": "broker_quote", "validation": "> 0", "rationale": "Bid for spread calc"},
        "spot_ask": {"type": "DOUBLE PRECISION", "unit": "INR", "nullable": True, "source": "broker_quote", "validation": "> 0", "rationale": "Ask for spread calc"},
        "vix": {"type": "DOUBLE PRECISION", "unit": "%", "nullable": True, "source": "broker_quote", "validation": "> 0", "rationale": "Volatility regime"},
        "atm_strike": {"type": "DOUBLE PRECISION", "unit": "INR", "nullable": True, "source": "derived", "validation": "multiple of 50", "rationale": "Option chain anchor"},
        "expiry_date": {"type": "VARCHAR(10)", "unit": "DDMMMYY", "nullable": True, "source": "calendar", "validation": "valid future date", "rationale": "Contract identification"},
        "days_to_expiry": {"type": "INTEGER", "unit": "days", "nullable": True, "source": "derived", "validation": ">= 0", "rationale": "Time decay context"},
        "data_status": {"type": "VARCHAR(20)", "unit": "", "nullable": False, "source": "validator", "validation": "enum", "rationale": "Quality flag, never NULL"},
        "source_latency_ms": {"type": "INTEGER", "unit": "ms", "nullable": True, "source": "collector", "validation": ">= 0", "rationale": "Feed health"},
        "ingestion_time": {"type": "TIMESTAMPTZ", "unit": "UTC", "nullable": False, "source": "collector", "validation": ">= timestamp_ist", "rationale": "Pipeline latency"},
        "provider_payload": {"type": "JSONB", "unit": "", "nullable": True, "source": "broker", "validation": "valid JSON", "rationale": "Audit trail"},
    }
}


HORIZONS = [
    (30, "30s"),
    (60, "1m"),
    (180, "3m"),
    (300, "5m"),
    (600, "10m"),
]

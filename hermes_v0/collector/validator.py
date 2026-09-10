"""Data validation and quality assessment for Hermes V0 snapshots
validator.py
."""

from dataclasses import dataclass
from datetime import datetime
from typing import Optional
from enum import Enum

from hermes_v0.domain.models import (
    MarketSnapshot, DataStatus, SnapshotVersion
)


class ValidationRule:
    """Validation rule definition."""
    
    def __init__(
        self, 
        name: str, 
        check_fn, 
        severity: str = "ERROR",
        description: str = ""
    ):
        self.name = name
        self.check_fn = check_fn
        self.severity = severity  # "ERROR", "WARN", "INFO"
        self.description = description


@dataclass
class ValidationResult:
    """Result of validating a snapshot."""
    is_valid: bool
    data_status: DataStatus
    errors: list[str]
    warnings: list[str]
    info: list[str]
    checked_fields: int
    failed_fields: int


class SnapshotValidator:
    """Validates market snapshots and assigns data quality status."""
    
    # Required fields that must be present for VALID status
    REQUIRED_FIELDS = [
        "timestamp_ist",
        "trading_date", 
        "symbol",
        "spot_ltp",
        "atm_strike",
        "expiry_date",
        "days_to_expiry",
    ]
    
    # Core fields that should have values (but can be PARTIAL)
    CORE_FIELDS = [
        "spot_ltp",
        "vix",
        "atm_strike",
        "atm_ce_ltp",
        "atm_ce_oi", 
        "atm_ce_iv",
        "atm_pe_ltp",
        "atm_pe_oi",
        "atm_pe_iv",
    ]
    
    def __init__(self, config: Optional[dict] = None):
        self.config = config or {}
        self.max_spread_pct = self.config.get("max_spread_pct", 5.0)
        self.max_staleness_seconds = self.config.get("max_staleness_seconds", 10)
        self.spot_price_range = self.config.get("spot_price_range", (20000, 30000))
        self.vix_range = self.config.get("vix_range", (5, 40))
        
        # Build validation rules
        self.rules = self._build_rules()
    
    def _build_rules(self) -> list[ValidationRule]:
        """Build validation rules."""
        rules = []
        
        # Required field presence
        for field in self.REQUIRED_FIELDS:
            rules.append(ValidationRule(
                name=f"required_{field}",
                check_fn=lambda s, f=field: getattr(s, f) is not None,
                severity="ERROR",
                description=f"Required field {field} must not be None"
            ))
        
        # Spot price range
        rules.append(ValidationRule(
            name="spot_price_range",
            check_fn=lambda s: (
                s.spot_ltp is None or 
                (self.spot_price_range[0] <= s.spot_ltp <= self.spot_price_range[1])
            ),
            severity="ERROR",
            description=f"Spot price must be in range {self.spot_price_range}"
        ))
        
        # VIX range
        rules.append(ValidationRule(
            name="vix_range",
            check_fn=lambda s: (
                s.vix is None or 
                (self.vix_range[0] <= s.vix <= self.vix_range[1])
            ),
            severity="WARN",
            description=f"VIX must be in range {self.vix_range}"
        ))
        
        # ATM strike multiple of 50
        rules.append(ValidationRule(
            name="atm_strike_interval",
            check_fn=lambda s: (
                s.atm_strike is None or 
                s.atm_strike % 50 == 0
            ),
            severity="WARN",
            description="ATM strike should be multiple of 50"
        ))
        
        # Option price sanity (not negative)
        option_fields = [
            "atm_ce_ltp", "atm_ce_bid", "atm_ce_ask",
            "atm_pe_ltp", "atm_pe_bid", "atm_pe_ask",
        ]
        for field in option_fields:
            rules.append(ValidationRule(
                name=f"positive_{field}",
                check_fn=lambda s, f=field: (
                    getattr(s, f) is None or getattr(s, f) >= 0
                ),
                severity="ERROR",
                description=f"{field} must be non-negative"
            ))
        
        # Bid <= Ask for options
        rules.append(ValidationRule(
            name="ce_bid_ask_order",
            check_fn=lambda s: (
                s.atm_ce_bid is None or s.atm_ce_ask is None or 
                s.atm_ce_bid <= s.atm_ce_ask
            ),
            severity="WARN",
            description="CE bid must not exceed ask"
        ))
        
        rules.append(ValidationRule(
            name="pe_bid_ask_order",
            check_fn=lambda s: (
                s.atm_pe_bid is None or s.atm_pe_ask is None or 
                s.atm_pe_bid <= s.atm_pe_ask
            ),
            severity="WARN",
            description="PE bid must not exceed ask"
        ))
        
        # OI non-negative
        for field in ["atm_ce_oi", "atm_pe_oi"]:
            rules.append(ValidationRule(
                name=f"nonneg_{field}",
                check_fn=lambda s, f=field: (
                    getattr(s, f) is None or getattr(s, f) >= 0
                ),
                severity="WARN",
                description=f"{field} must be non-negative"
            ))
        
        # IV in reasonable range (0-100%)
        for field in ["atm_ce_iv", "atm_pe_iv"]:
            rules.append(ValidationRule(
                name=f"iv_range_{field}",
                check_fn=lambda s, f=field: (
                    getattr(s, f) is None or 
                    (0 <= getattr(s, f) <= 100)
                ),
                severity="WARN",
                description=f"{field} must be 0-100%"
            ))
        
        # Greeks sanity
        greek_fields = [
            "atm_ce_delta", "atm_ce_gamma", "atm_ce_theta", "atm_ce_vega",
            "atm_pe_delta", "atm_pe_gamma", "atm_pe_theta", "atm_pe_vega",
        ]
        for field in greek_fields:
            rules.append(ValidationRule(
                name=f"greek_range_{field}",
                check_fn=lambda s, f=field: (
                    getattr(s, f) is None or 
                    abs(getattr(s, f)) < 1000  # Arbitrary large bound
                ),
                severity="WARN",
                description=f"{field} should be within reasonable bounds"
            ))
        
        # Spread check
        rules.append(ValidationRule(
            name="spread_reasonable",
            check_fn=lambda s: self._check_spread(s),
            severity="WARN",
            description=f"Bid-ask spread should not exceed {self.max_spread_pct}%"
        ))
        
        return rules
    
    def _check_spread(self, snapshot: MarketSnapshot) -> bool:
        """Check if bid-ask spreads are reasonable."""
        # Spot spread
        if snapshot.spot_bid and snapshot.spot_ask and snapshot.spot_ltp:
            spread_pct = ((snapshot.spot_ask - snapshot.spot_bid) / snapshot.spot_ltp) * 100
            if spread_pct > self.max_spread_pct:
                return False
        
        # ATM CE spread
        if (snapshot.atm_ce_bid and snapshot.atm_ce_ask and 
            snapshot.atm_ce_ltp and snapshot.atm_ce_ltp > 0):
            spread_pct = ((snapshot.atm_ce_ask - snapshot.atm_ce_bid) / snapshot.atm_ce_ltp) * 100
            if spread_pct > self.max_spread_pct * 2:  # Options can have wider spreads
                return False
        
        # ATM PE spread
        if (snapshot.atm_pe_bid and snapshot.atm_pe_ask and 
            snapshot.atm_pe_ltp and snapshot.atm_pe_ltp > 0):
            spread_pct = ((snapshot.atm_pe_ask - snapshot.atm_pe_bid) / snapshot.atm_pe_ltp) * 100
            if spread_pct > self.max_spread_pct * 2:
                return False
        
        return True
    
    def validate(self, snapshot: MarketSnapshot) -> ValidationResult:
        """Validate a snapshot and return result with data status."""
        errors = []
        warnings = []
        info = []
        failed = 0
        
        for rule in self.rules:
            try:
                passed = rule.check_fn(snapshot)
                if not passed:
                    if rule.severity == "ERROR":
                        errors.append(f"{rule.name}: {rule.description}")
                        failed += 1
                    elif rule.severity == "WARN":
                        warnings.append(f"{rule.name}: {rule.description}")
                        failed += 1
                    else:
                        info.append(f"{rule.name}: {rule.description}")
            except Exception as e:
                errors.append(f"{rule.name}: validation error - {e}")
                failed += 1
        
        # Determine data status
        if errors:
            data_status = DataStatus.REJECTED
        elif failed > 0:
            # Some core fields missing or warnings
            missing_core = sum(
                1 for f in self.CORE_FIELDS 
                if getattr(snapshot, f) is None
            )
            if missing_core > len(self.CORE_FIELDS) / 2:
                data_status = DataStatus.PARTIAL
            else:
                data_status = DataStatus.VALID
        else:
            data_status = DataStatus.VALID
        
        return ValidationResult(
            is_valid=len(errors) == 0,
            data_status=data_status,
            errors=errors,
            warnings=warnings,
            info=info,
            checked_fields=len(self.rules),
            failed_fields=failed,
        )
    
    def validate_and_enrich(
        self, 
        snapshot: MarketSnapshot
    ) -> MarketSnapshot:
        """Validate snapshot and return enriched version with status."""
        result = self.validate(snapshot)
        
        # Create new snapshot with validation results
        return MarketSnapshot(
            version=snapshot.version,
            timestamp_ist=snapshot.timestamp_ist,
            trading_date=snapshot.trading_date,
            symbol=snapshot.symbol,
            spot_ltp=snapshot.spot_ltp,
            spot_bid=snapshot.spot_bid,
            spot_ask=snapshot.spot_ask,
            spot_prev_close=snapshot.spot_prev_close,
            spot_open=snapshot.spot_open,
            spot_high=snapshot.spot_high,
            spot_low=snapshot.spot_low,
            spot_volume=snapshot.spot_volume,
            vix=snapshot.vix,
            vix_prev_close=snapshot.vix_prev_close,
            atm_strike=snapshot.atm_strike,
            expiry_date=snapshot.expiry_date,
            days_to_expiry=snapshot.days_to_expiry,
            atm_ce_ltp=snapshot.atm_ce_ltp,
            atm_ce_bid=snapshot.atm_ce_bid,
            atm_ce_ask=snapshot.atm_ce_ask,
            atm_ce_bid_qty=snapshot.atm_ce_bid_qty,
            atm_ce_ask_qty=snapshot.atm_ce_ask_qty,
            atm_ce_volume=snapshot.atm_ce_volume,
            atm_ce_oi=snapshot.atm_ce_oi,
            atm_ce_iv=snapshot.atm_ce_iv,
            atm_ce_delta=snapshot.atm_ce_delta,
            atm_ce_gamma=snapshot.atm_ce_gamma,
            atm_ce_theta=snapshot.atm_ce_theta,
            atm_ce_vega=snapshot.atm_ce_vega,
            atm_pe_ltp=snapshot.atm_pe_ltp,
            atm_pe_bid=snapshot.atm_pe_bid,
            atm_pe_ask=snapshot.atm_pe_ask,
            atm_pe_bid_qty=snapshot.atm_pe_bid_qty,
            atm_pe_ask_qty=snapshot.atm_pe_ask_qty,
            atm_pe_volume=snapshot.atm_pe_volume,
            atm_pe_oi=snapshot.atm_pe_oi,
            atm_pe_iv=snapshot.atm_pe_iv,
            atm_pe_delta=snapshot.atm_pe_delta,
            atm_pe_gamma=snapshot.atm_pe_gamma,
            atm_pe_theta=snapshot.atm_pe_theta,
            atm_pe_vega=snapshot.atm_pe_vega,
            data_status=result.data_status,
            source_latency_ms=snapshot.source_latency_ms,
            ingestion_time=snapshot.ingestion_time,
            provider_payload=snapshot.provider_payload,
        )


# Default validator instance
default_validator = SnapshotValidator()
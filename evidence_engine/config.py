"""Versioned research choices. All thresholds are fixed before discovery."""
from dataclasses import asdict, dataclass
import math


@dataclass(frozen=True)
class ResearchConfig:
    horizon_seconds: int = 300
    anchor_seconds: int = 60
    lookback_seconds: int = 60
    quote_max_age_seconds: int = 5
    max_gap_seconds: int = 10
    min_move: float = 50.0
    max_move: float = 80.0
    discovery_fraction: float = 0.6
    discovery_end: str | None = None
    min_support: int = 5
    max_conditions: int = 6
    detector_version: str = "spot-endpoint-momentum-v2"
    discovery_version: str = "joint-ce-pe-iv-training-v2"

    def __post_init__(self):
        for name in ("horizon_seconds", "anchor_seconds", "lookback_seconds",
                     "quote_max_age_seconds", "max_gap_seconds", "min_support", "max_conditions"):
            value = getattr(self, name)
            if type(value) is not int or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        if not all(math.isfinite(v) for v in (self.min_move, self.max_move, self.discovery_fraction)):
            raise ValueError("Thresholds must be finite")
        if self.detector_version not in ("spot-endpoint-band-v1", "spot-endpoint-momentum-v2"):
            raise ValueError("Unsupported event detector version")
        if self.discovery_version not in ("single-feature-training-quantiles-v1", "joint-ce-pe-iv-training-v2"):
            raise ValueError("Unsupported discovery version")
        if (not 0 < self.min_move or not 0 < self.discovery_fraction < 1
                or (self.detector_version == "spot-endpoint-band-v1" and self.min_move > self.max_move)):
            raise ValueError("Invalid event band or discovery fraction")
        if self.quote_max_age_seconds >= self.horizon_seconds:
            raise ValueError("Quote age must be shorter than the horizon")
        if self.discovery_end:
            from datetime import date
            date.fromisoformat(self.discovery_end)

    def to_dict(self):
        return asdict(self)

"""Independent quality checks for the two linked cleaned output tables."""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any

import pandas as pd

from .cleaner import OBSERVATION_DTYPES, OPTION_DTYPES


@dataclass
class ValidationResult:
    passed: bool
    errors: list[str]

    def to_dict(self) -> dict[str, Any]:
        return {"passed": self.passed, "errors": list(self.errors)}


def validate_tables(observations: pd.DataFrame, options: pd.DataFrame) -> ValidationResult:
    """Reject missing keys, nonfinite values, domain errors and broken provenance."""
    errors: list[str] = []
    for label, frame, expected in (("observations", observations, OBSERVATION_DTYPES),
                                   ("options", options, OPTION_DTYPES)):
        if not frame.columns.is_unique:
            errors.append(f"{label}: duplicate column names")
        missing = set(expected) - set(frame.columns)
        if missing:
            errors.append(f"{label}: missing columns: {', '.join(sorted(missing))}")
    if errors:
        return ValidationResult(False, errors)

    for label, frame, required in (
        ("observations", observations, ("observation_id", "source_row", "timestamps", "spot", "timestamp_source", "data_status")),
        ("options", options, ("observation_id", "timestamps", "strike", "optiontype", "expirydate", "daystoexpiry", "greeks_source", "derivation_status")),
    ):
        for name in required:
            if frame[name].isna().any() or frame[name].astype("string").str.strip().eq("").any():
                errors.append(f"{label}.{name}: required value missing")
        if frame["observation_id"].duplicated().any():
            errors.append(f"{label}: duplicate observation_id")
        if not frame["observation_id"].dropna().astype("string").str.fullmatch(r"[0-9a-f]{64}").all():
            errors.append(f"{label}: malformed observation_id")

    obs_ids = set(observations["observation_id"].dropna())
    opt_ids = set(options["observation_id"].dropna())
    if obs_ids != opt_ids or len(observations) != len(options):
        errors.append("tables: observation_id foreign keys must match one-to-one")

    numeric_rules = (
        ("observations", observations, "source_row", "integer"),
        ("observations", observations, "spot", "positive"),
        ("observations", observations, "iv", "positive"),
        ("observations", observations, "volume", "integer"),
        ("options", options, "oi", "integer"),
        ("options", options, "ltp", "nonnegative"),
        ("options", options, "strike", "positive"),
        ("options", options, "daystoexpiry", "nonnegative"),
        ("options", options, "delta", "finite"),
        ("options", options, "theta", "finite"),
        ("options", options, "gamma", "nonnegative"),
        ("options", options, "vega", "nonnegative"),
    )
    for label, frame, name, rule in numeric_rules:
        for value in frame[name].dropna():
            try:
                number = float(value)
                valid = not isinstance(value, (bool, str)) and math.isfinite(number)
                if rule == "positive":
                    valid = valid and number > 0
                elif rule in ("nonnegative", "integer"):
                    valid = valid and number >= 0
                if rule == "integer":
                    valid = valid and number.is_integer() and value <= 2**63 - 1
            except (ValueError, TypeError, OverflowError):
                valid = False
            if not valid:
                errors.append(f"{label}.{name}: invalid {rule} numeric value")
                break

    if not options["optiontype"].dropna().isin(["CE", "PE"]).all():
        errors.append("options.optiontype: expected CE or PE")
    time_columns = (("observations", observations, "timestamps"),
                    ("observations", observations, "provider_timestamp"),
                    ("options", options, "timestamps"), ("options", options, "expirydate"))
    valid_time_types = True
    for label, frame, name in time_columns:
        if not isinstance(frame[name].dtype, pd.DatetimeTZDtype) or str(frame[name].dt.tz) != "UTC":
            errors.append(f"{label}.{name}: expected timezone-aware UTC datetime")
            valid_time_types = False
    if valid_time_types:
        actual_days = (options["expirydate"] - options["timestamps"]).dt.total_seconds() / 86400.0
        if actual_days.lt(0).any():
            errors.append("options: expiry precedes observation timestamp")
        try:
            if ((actual_days - options["daystoexpiry"]).abs() > 1e-9).any():
                errors.append("options.daystoexpiry: inconsistent with timestamps and expirydate")
        except (ValueError, TypeError):
            errors.append("options.daystoexpiry: inconsistent numeric type")

    greek_fields = ["delta", "theta", "gamma", "vega"]
    derived = options["derivation_status"].eq("derived").fillna(False)
    if options.loc[derived, greek_fields].isna().any(axis=None):
        errors.append("options: derived Greeks must all be present")
    if options.loc[~derived, greek_fields].notna().any(axis=None):
        errors.append("options: unavailable Greeks must remain null")
    if not options.loc[derived, "greeks_source"].eq("black_scholes_european").all():
        errors.append("options: derived Greeks must identify black_scholes_european")
    allowed_status = ["disabled", "missing_parameters", "missing_iv", "nonpositive_time", "derived"]
    if not options["derivation_status"].isin(allowed_status).all():
        errors.append("options: unknown derivation_status")
    if not options.loc[~derived, "greeks_source"].eq("unavailable").all():
        errors.append("options: unavailable Greeks must identify unavailable source")

    if not observations["observation_id"].duplicated().any() and not options["observation_id"].duplicated().any():
        shared = observations.merge(options, on="observation_id", suffixes=("_obs", "_opt"))
        for name in ("timestamps", "underlying", "symbol", "exchange"):
            left, right = shared[f"{name}_obs"], shared[f"{name}_opt"]
            matches = left.eq(right).fillna(False) | (left.isna() & right.isna())
            if not matches.all():
                errors.append(f"tables: mismatched {name} for observation_id")
        if "derivation_status" in shared:
            derived_shared = shared["derivation_status"].eq("derived")
            if shared.loc[derived_shared, "iv"].isna().any():
                errors.append("tables: derived Greeks require supplied IV")
            if shared.loc[derived_shared, "daystoexpiry"].le(0).any():
                errors.append("tables: derived Greeks require positive time to expiry")
    natural_key = ["timestamps", "underlying", "symbol", "exchange", "strike", "optiontype", "expirydate"]
    if options.duplicated(natural_key).any():
        errors.append("options: repeated natural key")
    return ValidationResult(not errors, errors)

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
        ("options", options, "rho", "finite"),
        ("options", options, "bid", "nonnegative"),
        ("options", options, "ask", "nonnegative"),
        ("options", options, "calculation_lag_ms", "nonnegative"),
        ("options", options, "calculation_interest_rate", "finite"),
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
    time_columns = [(label, frame, name)
                    for label, frame, schema in (("observations", observations, OBSERVATION_DTYPES),
                                                 ("options", options, OPTION_DTYPES))
                    for name, dtype in schema.items() if dtype.startswith("datetime64")]
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
    else:
        # Avoid comparing timezone-naive or non-temporal values below.
        return ValidationResult(False, errors)

    greek_fields = ["delta", "theta", "gamma", "vega"]
    derived = options["derivation_status"].eq("derived").fillna(False)
    stored = options["derivation_status"].isin(["stored_derived", "stored_partial"])
    complete = derived | options["derivation_status"].eq("stored_derived").fillna(False)
    if options.loc[complete, greek_fields].isna().any(axis=None):
        errors.append("options: derived Greeks must all be present")
    if options.loc[~(derived | stored), greek_fields].notna().any(axis=None):
        errors.append("options: unavailable Greeks must remain null")
    if not options.loc[derived, "greeks_source"].eq("black_scholes_european").all():
        errors.append("options: derived Greeks must identify black_scholes_european")
    allowed_status = ["disabled", "missing_parameters", "missing_iv", "nonpositive_time", "derived",
                      "stored_derived", "stored_partial", "missing_stored_derived", "ambiguous_stored_derived", "invalid_stored_derived"]
    if not options["derivation_status"].isin(allowed_status).all():
        errors.append("options: unknown derivation_status")
    if not options.loc[~(derived | stored), "greeks_source"].eq("unavailable").all():
        errors.append("options: unavailable Greeks must identify unavailable source")
    if not options.loc[stored, "greeks_source"].eq("OPENALGO_BLACK76").all():
        errors.append("options: stored Greeks must identify their model")
    if stored.any():
        for name in ("greeks_available_at", "derived_received_at", "calculation_model", "derived_source_version"):
            if options.loc[stored, name].isna().any():
                errors.append(f"options.{name}: stored calculation provenance is required")
        if options.loc[stored, "greeks_available_at"].lt(options.loc[stored, "timestamps"]).any():
            errors.append("options: stored Greeks cannot be available before the raw observation")
        if not options.loc[stored, "greeks_available_at"].eq(options.loc[stored, "derived_received_at"]).all():
            errors.append("options: stored Greek availability must match calculation receipt time")
        if not options.loc[stored, "calculation_model"].eq(options.loc[stored, "greeks_source"]).all():
            errors.append("options: calculation model must match Greek source")
        expected_lag = (options.loc[stored, "derived_received_at"] - options.loc[stored, "timestamps"]).dt.total_seconds() * 1000
        supplied_lag = options.loc[stored, "calculation_lag_ms"]
        if supplied_lag.isna().any() or ((expected_lag - supplied_lag).abs() > 1e-6).any():
            errors.append("options: calculation lag must match receipt availability")
        v2 = stored & options["derived_source_version"].eq(2).fillna(False)
        for name in ("derived_receipt_id", "derived_parent_receipt_id"):
            if options.loc[v2, name].isna().any() or not options.loc[v2, name].str.fullmatch(r"[0-9a-f]{64}").all():
                errors.append(f"options.{name}: version 2 calculations require receipt identity")

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
            stored_shared = shared["derivation_status"].isin(["stored_derived", "stored_partial"])
            stored_complete = shared["derivation_status"].eq("stored_derived")
            if not shared.loc[stored_shared, "derived_source_version"].astype("string").eq(shared.loc[stored_shared, "source_version"]).all():
                errors.append("tables: stored calculation version must match RAW source version")
            if shared.loc[stored_complete, "iv"].isna().any():
                errors.append("tables: complete stored Greeks require IV")
            with_iv = stored_shared & shared["iv"].notna()
            if not shared.loc[with_iv, "iv_source"].eq("OPENALGO_BLACK76").all():
                errors.append("tables: stored IV must identify its model")
            if not shared.loc[with_iv, "iv_available_at"].eq(shared.loc[with_iv, "greeks_available_at"]).all():
                errors.append("tables: stored IV and Greeks must share calculation availability")
            v2 = stored_shared & shared["source_version"].eq("2").fillna(False)
            if not shared.loc[v2, "derived_parent_receipt_id"].eq(shared.loc[v2, "source_receipt_id"]).all():
                errors.append("tables: derived receipt must identify the RAW parent")
    natural_key = ["timestamps", "underlying", "symbol", "exchange", "strike", "optiontype", "expirydate"]
    if options.duplicated(natural_key).any():
        errors.append("options: repeated natural key")
    return ValidationResult(not errors, errors)

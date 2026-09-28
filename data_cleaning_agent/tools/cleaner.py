"""Deterministic normalization and optional European Black-Scholes analytics.

No market observation is invented. In particular, IV must be supplied, absent
OI is not zero, and rows that cannot be normalized go to quarantine. ``source_row``
is the zero-based position in the input. All output timestamps are UTC.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, time
from decimal import Decimal, InvalidOperation
import hashlib
import json
import math
from numbers import Real
import re
from typing import TYPE_CHECKING, Any

import pandas as pd

from .enrichment import OBSERVATION_EXTRAS, OPTION_EXTRAS

if TYPE_CHECKING:
    from ..config.settings import Settings
    from ..models.cleaning_plan import CleaningPlan


OBSERVATION_DTYPES = {
    "observation_id": "string", "source_row": "Int64",
    "timestamps": "datetime64[ns, UTC]", "underlying": "string",
    "symbol": "string", "exchange": "string", "spot": "Float64",
    "iv": "Float64", "volume": "Int64", "timestamp_source": "string",
    "source_receipt_id": "string", "source_freshness": "string", "source_version": "string",
    "provider_timestamp": "datetime64[ns, UTC]", "data_status": "string",
    **OBSERVATION_EXTRAS,
}
OPTION_DTYPES = {
    "observation_id": "string", "timestamps": "datetime64[ns, UTC]",
    "underlying": "string", "symbol": "string", "exchange": "string",
    "oi": "Int64", "ltp": "Float64", "strike": "Float64",
    "optiontype": "string", "expirydate": "datetime64[ns, UTC]",
    "daystoexpiry": "Float64", "delta": "Float64", "theta": "Float64",
    "gamma": "Float64", "vega": "Float64", "greeks_source": "string",
    "derivation_status": "string",
    **OPTION_EXTRAS,
}
QUARANTINE_DTYPES = {
    "source_row": "Int64", "reason": "string", "raw_record_json": "string",
}
CANONICAL_INPUTS = {
    "timestamps", "spot", "iv", "volume", "oi", "ltp", "strike",
    "optiontype", "expirydate", "underlying", "symbol", "exchange",
    "timestamp_source", "provider_timestamp", "data_status",
    "source_receipt_id", "source_freshness", "source_version",
}
_INTEGER_MAX = 2**63 - 1
_NUMBER = re.compile(r"^[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?$")
_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_ISO_DATETIME = re.compile(r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d{1,9})?)?(?:Z|[+-]\d{2}:?\d{2})?$")
_NAMED_DATE = re.compile(r"^(\d{1,2})-([A-Za-z]{3})-(\d{4})$")
_CONTRACT_DATE = re.compile(r"^(\d{2})([A-Za-z]{3})(\d{2})$")
_MONTHS = {name: i for i, name in enumerate(
    ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), 1
)}


@dataclass
class CleaningResult:
    observations: pd.DataFrame
    options: pd.DataFrame
    quarantine: pd.DataFrame
    duplicates_removed: int


def _frame(rows: list[dict[str, Any]], dtypes: dict[str, str]) -> pd.DataFrame:
    """Give even empty/all-null tables stable Arrow-compatible schemas."""
    # Construct each nullable integer column with its final dtype immediately.
    # DataFrame inference first would round large integers when nulls coexist.
    return pd.DataFrame({
        column: pd.Series([row.get(column) for row in rows], dtype=dtype)
        for column, dtype in dtypes.items()
    })


def _missing(value: Any) -> bool:
    if value is None or value is pd.NA or value is pd.NaT:
        return True
    if isinstance(value, str):
        return not value.strip()
    if isinstance(value, Real):
        return math.isnan(float(value))
    return False


def _json_value(value: Any) -> Any:
    if value is None or value is pd.NA or value is pd.NaT:
        return None
    if isinstance(value, dict):
        return {str(k): _json_value(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(v) for v in value]
    if hasattr(value, "item"):
        return _json_value(value.item())
    if isinstance(value, (datetime, date, pd.Timestamp)):
        return value.isoformat()
    if isinstance(value, float) and not math.isfinite(value):
        return "NaN" if math.isnan(value) else str(value)
    if isinstance(value, (str, bool, int, float)):
        return value
    return str(value)


def _json(record: dict[str, Any]) -> str:
    return json.dumps(_json_value(record), sort_keys=True, ensure_ascii=False,
                      separators=(",", ":"), allow_nan=False)


def _number(value: Any, field: str, *, required: bool = False,
            integer: bool = False, positive: bool = False) -> int | float | None:
    if _missing(value):
        if required:
            raise ValueError(f"{field}: required value is missing")
        return None
    if isinstance(value, bool) or not isinstance(value, (str, Real, Decimal)):
        raise ValueError(f"{field}: expected a numeric scalar")
    try:
        parsed = Decimal(str(value).strip())
    except InvalidOperation as exc:
        raise ValueError(f"{field}: invalid numeric value") from exc
    if not parsed.is_finite():
        raise ValueError(f"{field}: nonfinite value")
    if parsed < 0 or (positive and parsed == 0):
        raise ValueError(f"{field}: must be {'positive' if positive else 'nonnegative'}")
    if integer:
        if parsed != parsed.to_integral_value() or parsed > _INTEGER_MAX:
            raise ValueError(f"{field}: must be an integer within signed 64-bit range")
        return int(parsed)
    result = float(parsed)
    if not math.isfinite(result) or (positive and result == 0):
        raise ValueError(f"{field}: outside supported finite numeric range")
    return result


def _utc(value: Any, field: str, timezone: str, unit: str | None = None) -> pd.Timestamp:
    if _missing(value):
        raise ValueError(f"{field}: required value is missing")
    if isinstance(value, bool):
        raise ValueError(f"{field}: boolean is not a timestamp")
    numeric = isinstance(value, (Real, Decimal))
    if isinstance(value, str):
        value = value.strip()
        numeric = bool(_NUMBER.fullmatch(value))
        if not numeric and not (_ISO_DATE.fullmatch(value) or _ISO_DATETIME.fullmatch(value)):
            raise ValueError(f"{field}: expected an unambiguous ISO-8601 timestamp")
    try:
        if numeric:
            if unit is None:
                raise ValueError("numeric timestamps require timestamp_unit")
            number = Decimal(str(value))
            if not number.is_finite():
                raise ValueError("nonfinite timestamp")
            raw = int(number) if number == number.to_integral_value() else float(number)
            stamp = pd.Timestamp(pd.to_datetime(raw, unit=unit, utc=True))
        else:
            if not isinstance(value, (str, date, datetime, pd.Timestamp)):
                raise ValueError("expected an ISO-8601 timestamp")
            stamp = pd.Timestamp(value)
            if pd.isna(stamp):
                raise ValueError("missing timestamp")
            if stamp.tzinfo is None:
                stamp = stamp.tz_localize(timezone, ambiguous="raise", nonexistent="raise")
            stamp = stamp.tz_convert("UTC")
        # Verify the timestamp fits the declared nanosecond output schema now,
        # so a bad row cannot crash conversion of the whole batch later.
        return pd.Series([stamp], dtype="datetime64[ns, UTC]").iloc[0]
    except Exception as exc:
        raise ValueError(f"{field}: {exc}") from exc


def _expiry(value: Any, settings: Settings) -> pd.Timestamp:
    if _missing(value):
        raise ValueError("expirydate: required value is missing")
    expiry_date: date | None = None
    if isinstance(value, str):
        value = value.strip()
        if _ISO_DATE.fullmatch(value):
            expiry_date = date.fromisoformat(value)
        else:
            named = _NAMED_DATE.fullmatch(value)
            contract_date = _CONTRACT_DATE.fullmatch(value)
            if contract_date:
                # Option contract DDMMMYY names use the explicit 2000-2099
                # century convention, rather than strptime's sliding pivot.
                day, month, year = contract_date.groups()
                if month.lower() not in _MONTHS:
                    raise ValueError("expirydate: invalid month")
                expiry_date = date(2000 + int(year), _MONTHS[month.lower()], int(day))
            if named:
                day, month, year = named.groups()
                if month.lower() not in _MONTHS:
                    raise ValueError("expirydate: invalid month")
                expiry_date = date(int(year), _MONTHS[month.lower()], int(day))
    elif isinstance(value, date) and not isinstance(value, (datetime, pd.Timestamp)):
        expiry_date = value
    if expiry_date is not None:
        value = datetime.combine(expiry_date, time.fromisoformat(settings.expiry_time))
    if isinstance(value, (Real, Decimal)):
        raise ValueError("expirydate: numeric expiry dates are ambiguous")
    return _utc(value, "expirydate", settings.expiry_timezone)


def _text(value: Any, field: str) -> str | None:
    if _missing(value):
        return None
    if not isinstance(value, (str, Real)) or isinstance(value, bool):
        raise ValueError(f"{field}: expected a text scalar")
    if isinstance(value, Real) and not math.isfinite(float(value)):
        raise ValueError(f"{field}: nonfinite value")
    return str(value).strip()


def _greeks(spot: float, strike: float, sigma: float, years: float,
            rate: float, dividend: float, optiontype: str) -> dict[str, float]:
    """European BS: theta per calendar day; vega per one IV percentage point."""
    root_t = math.sqrt(years)
    d1 = (math.log(spot / strike) + (rate - dividend + sigma**2 / 2) * years) / (sigma * root_t)
    d2 = d1 - sigma * root_t
    cdf = lambda x: 0.5 * math.erfc(-x / math.sqrt(2))
    density = math.exp(-d1**2 / 2) / math.sqrt(2 * math.pi)
    discounted_q, discounted_r = math.exp(-dividend * years), math.exp(-rate * years)
    shared_theta = -(spot * discounted_q * density * sigma) / (2 * root_t)
    if optiontype == "CE":
        delta = discounted_q * cdf(d1)
        theta = shared_theta - rate * strike * discounted_r * cdf(d2) + dividend * spot * discounted_q * cdf(d1)
    else:
        delta = discounted_q * (cdf(d1) - 1)
        theta = shared_theta + rate * strike * discounted_r * cdf(-d2) - dividend * spot * discounted_q * cdf(-d1)
    values = {
        "delta": delta, "theta": theta / 365.0,
        "gamma": discounted_q * density / (spot * sigma * root_t),
        "vega": spot * discounted_q * density * root_t / 100.0,
    }
    if not all(math.isfinite(v) for v in values.values()):
        raise ValueError("greeks: calculation produced a nonfinite result")
    return values


def _identity(raw: dict[str, Any], settings: Settings) -> dict[str, Any]:
    stamp = _utc(raw.get("timestamps"), "timestamps", settings.input_timezone, settings.timestamp_unit)
    expiry = _expiry(raw.get("expirydate"), settings)
    seconds = (expiry - stamp).total_seconds()
    if seconds < 0:
        raise ValueError("expirydate: expiry precedes observation timestamp")
    optiontype = _text(raw.get("optiontype"), "optiontype")
    optiontype = {"CE": "CE", "CALL": "CE", "C": "CE", "PE": "PE", "PUT": "PE", "P": "PE"}.get((optiontype or "").upper())
    if optiontype is None:
        raise ValueError("optiontype: expected CE/CALL/C or PE/PUT/P")
    return {
        "timestamps": stamp, "expirydate": expiry,
        "strike": _number(raw.get("strike"), "strike", required=True, positive=True),
        "optiontype": optiontype, "daystoexpiry": seconds / 86400.0,
        **{field: _text(raw.get(field), field) for field in ("underlying", "symbol", "exchange")},
    }


def _normalize(raw: dict[str, Any], settings: Settings,
               identity: dict[str, Any]) -> dict[str, Any]:
    result = {
        **identity,
        "spot": _number(raw.get("spot"), "spot", required=True, positive=True),
        "iv": _number(raw.get("iv"), "iv", positive=True),
        "volume": _number(raw.get("volume"), "volume", integer=True),
        "oi": _number(raw.get("oi"), "oi", integer=True),
        "ltp": _number(raw.get("ltp"), "ltp"),
        "timestamp_source": _text(raw.get("timestamp_source"), "timestamp_source") or "unspecified",
        "data_status": _text(raw.get("data_status"), "data_status") or "unspecified",
        "provider_timestamp": None,
        **{field: _text(raw.get(field), field) for field in ('source_receipt_id', 'source_freshness', 'source_version')},
    }
    if result["iv"] is not None and settings.iv_unit == "percent":
        result["iv"] /= 100.0
        if result["iv"] <= 0:
            raise ValueError("iv: outside supported numeric range after unit conversion")
    # Preserve supplied provider time in the ID without replacing receipt time
    # or inferring that a timestamp has been provider-verified.
    if not _missing(raw.get("provider_timestamp")):
        result["provider_timestamp"] = _utc(raw["provider_timestamp"], "provider_timestamp", settings.input_timezone, settings.timestamp_unit)
    result["observation_id"] = hashlib.sha256(_json(result).encode("utf-8")).hexdigest()
    result.update({name: None for name in ("delta", "theta", "gamma", "vega")})
    result["greeks_source"] = "unavailable"
    if not settings.derive_greeks:
        result["derivation_status"] = "disabled"
    elif settings.risk_free_rate is None or settings.dividend_yield is None:
        result["derivation_status"] = "missing_parameters"
    elif result["iv"] is None:
        result["derivation_status"] = "missing_iv"
    elif result["daystoexpiry"] == 0:
        result["derivation_status"] = "nonpositive_time"
    else:
        try:
            result.update(_greeks(result["spot"], result["strike"], result["iv"],
                                  result["daystoexpiry"] / 365.0,
                                  settings.risk_free_rate, settings.dividend_yield, result["optiontype"]))
        except (ValueError, OverflowError, ZeroDivisionError) as exc:
            raise ValueError(f"greeks: calculation failed ({exc})") from exc
        result["derivation_status"] = "derived"
        result["greeks_source"] = "black_scholes_european"
    return result


def clean_data(df: pd.DataFrame, plan: CleaningPlan, settings: Settings) -> CleaningResult:
    """Apply an approved column mapping, quarantine invalid rows, emit two tables."""
    if not df.columns.is_unique:
        raise ValueError("Input contains duplicate column names")
    mapping = dict(plan.column_mapping)
    if len(set(mapping.values())) != len(mapping):
        raise ValueError("Column mapping contains duplicate canonical targets")
    if not set(mapping.values()).issubset(CANONICAL_INPUTS):
        raise ValueError("Column mapping contains unsupported canonical targets")
    if not set(mapping).issubset(df.columns):
        raise ValueError("Column mapping references absent source columns")
    renamed = df.rename(columns=mapping)
    if not renamed.columns.is_unique:
        raise ValueError("Column mapping creates duplicate canonical targets")
    for name in ("risk_free_rate", "dividend_yield"):
        value = getattr(settings, name)
        if value is not None and (isinstance(value, bool) or not math.isfinite(value)):
            raise ValueError(f"{name} must be an explicit finite decimal rate")
    if settings.iv_unit not in ("decimal", "percent"):
        raise ValueError("iv_unit must be decimal or percent")

    quarantined: list[dict[str, Any]] = []
    candidates: list[tuple[dict[str, Any], str]] = []
    seen_raw: set[str] = set()
    duplicates_removed = 0
    keys: dict[tuple[Any, ...], list[int]] = defaultdict(list)
    key_columns = ("timestamps", "underlying", "symbol", "exchange", "strike", "optiontype", "expirydate")
    for source_row, (original, record) in enumerate(zip(df.to_dict("records"), renamed.to_dict("records"))):
        raw_json = _json(original)
        if plan.drop_exact_duplicates and raw_json in seen_raw:
            duplicates_removed += 1
            continue
        seen_raw.add(raw_json)
        try:
            identity = _identity(record, settings)
            # Register identity before optional/price validation. A malformed
            # conflicting observation still makes its counterpart ambiguous.
            keys[tuple(identity.get(name) for name in key_columns)].append(source_row)
            normalized = _normalize(record, settings, identity)
            normalized["source_row"] = source_row
            candidates.append((normalized, raw_json))
        except (ValueError, TypeError, OverflowError) as exc:
            quarantined.append({"source_row": source_row, "reason": str(exc), "raw_record_json": raw_json})

    conflicts = {source_row for indices in keys.values() if len(indices) > 1 for source_row in indices}
    accepted = []
    for record, raw_json in candidates:
        if record["source_row"] in conflicts:
            quarantined.append({"source_row": record["source_row"],
                                "reason": "conflicting_or_repeated_natural_key: all matching rows quarantined",
                                "raw_record_json": raw_json})
        else:
            accepted.append(record)
    quarantined.sort(key=lambda row: row["source_row"])
    return CleaningResult(
        observations=_frame(accepted, OBSERVATION_DTYPES),
        options=_frame(accepted, OPTION_DTYPES),
        quarantine=_frame(quarantined, QUARANTINE_DTYPES),
        duplicates_removed=duplicates_removed,
    )

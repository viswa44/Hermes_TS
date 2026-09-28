"""Carry stored analytics into the two tables without changing RAW identities.

Enrichment is applied after normalization/ID creation. A derived value keeps its
own availability time; a parent quote timestamp is never its availability time.
"""

from __future__ import annotations

from datetime import date
import json
import math
import re

import pandas as pd


UTC = "datetime64[ns, UTC]"
OBSERVATION_EXTRAS = {
    "timestamps_ist": "string", "trading_date": "string",
    "iv_source": "string", "iv_unit": "string", "iv_available_at": UTC,
    "supplied_iv": "Float64", "raw_iv": "Float64", "raw_iv_unit": "string",
    "spot_bid": "Float64", "spot_ask": "Float64", "spot_open": "Float64",
    "spot_high": "Float64", "spot_low": "Float64", "spot_prev_close": "Float64",
    "spot_volume": "Int64", "vix": "Float64", "vix_prev_close": "Float64",
    "source_latency_ms": "Int64", "source_ingestion_time": UTC,
    "scheduled_at": UTC, "request_started_at": UTC,
    "raw_market_fields_json": "string", "enrichment_issues": "string",
}
OPTION_EXTRAS = {
    "timestamps_ist": "string", "expiry_at_ist": "string", "expiry_date_local": "string",
    "contract_key": "string", "contract_identity_source": "string", "exchange_source": "string",
    "bid": "Float64", "ask": "Float64", "bid_qty": "Int64", "ask_qty": "Int64",
    "lotsize": "Int64", "tick_size": "Float64", "moneyness": "string", "rho": "Float64",
    "derived_receipt_id": "string", "derived_parent_receipt_id": "string",
    "derived_received_at": UTC, "derived_freshness": "string", "derived_source_version": "Int64",
    "greeks_available_at": UTC, "derivation_reason": "string",
    "calculation_option_ltp": "Float64", "calculation_underlying_price": "Float64",
    "calculation_underlying_kind": "string", "calculation_forward_price": "Float64",
    "calculation_interest_rate": "Float64", "calculation_model": "string",
    "calculation_lag_ms": "Float64", "model_risk_flags": "string",
    "calculation_data_status": "string", "calculation_error_class": "string",
    "raw_option_fields_json": "string", "stored_calculation_fields_json": "string",
}


def _missing(value):
    return value is None or value is pd.NA or value is pd.NaT or (
        isinstance(value, float) and math.isnan(value))


def _json(value, default):
    if _missing(value):
        return default
    parsed = json.loads(value) if isinstance(value, str) else value
    if type(parsed) is not type(default):
        raise ValueError("Invalid PostgreSQL enrichment evidence shape")
    return parsed


def _stamp(value):
    if _missing(value):
        return None
    value = pd.Timestamp(value)
    if value.tzinfo is None or pd.isna(value):
        raise ValueError("Enrichment timestamps require an explicit timezone")
    return value.tz_convert("UTC")


def _expiry_day(value):
    if isinstance(value, str) and len(value) == 7:
        return pd.to_datetime(value, format="%d%b%y").date()
    return date.fromisoformat(str(value)[:10])


def _number(value, name, issues, *, signed=False, integer=False, positive=False):
    if _missing(value):
        return None
    try:
        if isinstance(value, bool):
            raise ValueError()
        # Preserve integers without passing through a float (OI/volume > 2**53).
        from decimal import Decimal
        decimal = Decimal(str(value))
        if not decimal.is_finite() or (not signed and decimal < 0) or (positive and decimal <= 0):
            raise ValueError()
        if integer:
            if decimal != decimal.to_integral_value() or abs(decimal) > 2**63 - 1:
                raise ValueError()
            return int(decimal)
        result = float(decimal)
        if not math.isfinite(result):
            raise ValueError()
        return result
    except (ValueError, ArithmeticError):
        issues.append("invalid_" + name)
        return None


def _contract_key(row):
    return "|".join((str(row["underlying"]), row["expirydate"].tz_convert("Asia/Kolkata").date().isoformat(),
                     format(float(row["strike"]), ".15g"), row["optiontype"]))


def _same_contract(candidate, obs, opt):
    try:
        return (
            _stamp(candidate.get("timestamp_ist")) == obs["timestamps"]
            and candidate.get("underlying_symbol") == obs["underlying"]
            and float(candidate.get("strike")) == opt["strike"]
            and candidate.get("option_type") == opt["optiontype"]
            and _expiry_day(candidate.get("expiry_date")) == opt["expirydate"].tz_convert("Asia/Kolkata").date()
            and str(candidate.get("trading_date")) == obs["timestamps"].tz_convert("Asia/Kolkata").date().isoformat()
            and str(candidate.get("version")) == str(obs["source_version"])
            and isinstance(candidate.get("option_symbol"), str) and bool(candidate["option_symbol"])
        )
    except (ValueError, TypeError, OverflowError):
        return False


def _linked_calculation(candidate, obs, opt):
    if not _same_contract(candidate, obs, opt):
        raise ValueError("derived_contract_mismatch")
    if candidate.get("calculation_method") != "OPENALGO_BLACK76":
        raise ValueError("unsupported_stored_model")
    available = _stamp(candidate.get("ingestion_time"))
    if available is None or available < obs["timestamps"]:
        raise ValueError("invalid_derived_availability")
    if str(obs["source_version"]) == "2":
        receipts = candidate.get("derived_receipts", [])
        if not isinstance(receipts, list) or len(receipts) != 1:
            raise ValueError("missing_or_ambiguous_derived_receipt")
        receipt = receipts[0]
        if (receipt.get("kind") != "DERIVED"
                or receipt.get("parent_observation_id") != obs["source_receipt_id"]
                or receipt.get("symbol") != candidate["option_symbol"]
                or _stamp(receipt.get("received_at")) != available
                or receipt.get("timestamp_basis") != "CALCULATION_RECEIPT"
                or receipt.get("freshness") != "DERIVED_UNVERIFIED_INPUT_TIME"
                or candidate.get("data_status") != "PARTIAL"
                or not isinstance(receipt.get("observation_id"), str)
                or re.fullmatch(r"[0-9a-f]{64}", receipt["observation_id"]) is None):
            raise ValueError("derived_receipt_provenance_mismatch")
        started = _stamp(receipt.get("request_started_at"))
        if started is None or not obs["timestamps"] <= started <= available:
            raise ValueError("derived_request_time_mismatch")
        if _stamp(receipt.get("scheduled_at")) != obs.get("scheduled_at"):
            raise ValueError("derived_schedule_mismatch")
        return available, receipt
    # Legacy arrival is conservatively taken from the stored ingestion time.
    if str(obs["source_version"]) == "1":
        if candidate.get("data_status") not in {"VALID", "PARTIAL"} or candidate.get("error_class"):
            raise ValueError("legacy_calculation_unavailable:" + str(candidate.get("data_status")))
        return available, {}
    raise ValueError("unsupported_derived_version")


def enrich_tables(cleaned, frame, source_context=None):
    """Enrich accepted rows only, retaining IDs calculated from original inputs."""
    from .cleaner import OBSERVATION_DTYPES, OPTION_DTYPES, _frame

    observations = cleaned.observations.to_dict("records")
    options_by_id = {row["observation_id"]: row for row in cleaned.options.to_dict("records")}
    source_rows = frame.to_dict("records")
    trusted_postgres = bool(source_context and source_context.get("source") == "postgresql"
                            and source_context.get("schema_version") == 2
                            and source_context.get("source_read_only") is True)
    contract_symbols = {}
    for obs in observations:
        opt = options_by_id[obs["observation_id"]]
        raw = source_rows[int(obs["source_row"])]
        stamp, expiry = obs["timestamps"], opt["expirydate"]
        readable = stamp.tz_convert("Asia/Kolkata").isoformat()
        obs.update(timestamps_ist=readable, trading_date=stamp.tz_convert("Asia/Kolkata").date().isoformat(),
                   supplied_iv=obs["iv"], iv_source="unavailable" if _missing(obs["iv"]) else "supplied",
                   iv_unit="decimal", iv_available_at=None if _missing(obs["iv"]) else stamp)
        opt.update(timestamps_ist=readable, expiry_at_ist=expiry.tz_convert("Asia/Kolkata").isoformat(),
                   expiry_date_local=expiry.tz_convert("Asia/Kolkata").date().isoformat(),
                   contract_key=_contract_key({**obs, **opt}), derivation_reason=opt["derivation_status"])
        if not trusted_postgres:
            continue
        issues = []
        source_option = _json(raw.get("source_option_json"), {})
        source_market = _json(raw.get("source_market_json"), [])
        calculations = _json(raw.get("source_greeks_json"), [])
        opt["raw_option_fields_json"] = raw.get("source_option_json")
        opt["stored_calculation_fields_json"] = raw.get("source_greeks_json")
        obs["raw_market_fields_json"] = raw.get("source_market_json")
        obs["raw_iv"] = _number(raw.get("source_iv"), "raw_iv", issues)
        obs["raw_iv_unit"] = "unverified" if obs["raw_iv"] is not None else "unavailable"
        obs["source_latency_ms"] = _number(source_option.get("source_latency_ms"), "source_latency_ms", issues, integer=True)
        obs["source_ingestion_time"] = _stamp(raw.get("source_ingestion_time"))
        receipts = _json(raw.get("source_receipt_evidence_json"), [])
        if len(receipts) == 1:
            obs["scheduled_at"] = _stamp(receipts[0].get("scheduled_at"))
            obs["request_started_at"] = _stamp(receipts[0].get("request_started_at"))
        if len(source_market) == 1:
            for name in ("spot_bid", "spot_ask", "spot_open", "spot_high", "spot_low", "spot_prev_close", "spot_volume", "vix", "vix_prev_close"):
                obs[name] = _number(source_market[0].get(name), name, issues, integer=name == "spot_volume")
        for name in ("bid", "ask", "bid_qty", "ask_qty", "lotsize", "tick_size"):
            opt[name] = _number(source_option.get(name), name, issues, integer=name in {"bid_qty", "ask_qty", "lotsize"})
        opt["moneyness"] = source_option.get("moneyness")
        opt.update(derivation_status="missing_stored_derived", derivation_reason="No matching stored calculation")
        if len(calculations) > 1:
            opt.update(derivation_status="ambiguous_stored_derived", derivation_reason="Multiple stored calculations match this observation")
        elif calculations:
            calculation = calculations[0]
            opt["calculation_data_status"] = calculation.get("data_status")
            opt["calculation_error_class"] = calculation.get("error_class")
            try:
                available, receipt = _linked_calculation(calculation, obs, opt)
                if not _missing(calculation.get("option_ltp")) and float(calculation["option_ltp"]) != opt["ltp"]:
                    raise ValueError("derived_raw_option_price_mismatch")
                if not _missing(calculation.get("underlying_ltp")) and float(calculation["underlying_ltp"]) != obs["spot"]:
                    raise ValueError("derived_raw_spot_mismatch")
            except (ValueError, TypeError, OverflowError) as exc:
                opt.update(derivation_status="invalid_stored_derived", derivation_reason=str(exc))
            else:
                legacy = str(obs["source_version"]) == "1"
                opt.update(symbol=calculation["option_symbol"], contract_identity_source="stored_calculation",
                           derived_receipt_id=receipt.get("observation_id"),
                           derived_parent_receipt_id=receipt.get("parent_observation_id"),
                           derived_received_at=available, greeks_available_at=available,
                           derived_freshness=receipt.get("freshness", "UNVERIFIED_LEGACY_CALCULATION"),
                           derived_source_version=int(calculation["version"]),
                           calculation_model=calculation["calculation_method"],
                           calculation_lag_ms=(available - stamp).total_seconds() * 1000.0)
                obs["symbol"] = opt["symbol"]
                contract_symbols.setdefault(opt["contract_key"], set()).add(opt["symbol"])
                # This collector's validated NIFTY option route is explicitly NFO.
                if obs["underlying"] == "NIFTY":
                    obs["exchange"] = opt["exchange"] = "NFO"
                    opt["exchange_source"] = "collector_option_route"
                iv = _number(calculation.get("implied_volatility"), "derived_iv", issues, positive=True)
                if iv is not None:
                    obs.update(iv=iv / 100.0, iv_source="OPENALGO_BLACK76", iv_available_at=available)
                for name in ("delta", "gamma", "theta", "vega", "rho"):
                    opt[name] = _number(calculation.get(name), name, issues, signed=name in {"delta", "theta", "rho"})
                for target, original in (("calculation_option_ltp", "calculation_option_ltp"),
                                         ("calculation_underlying_price", "calculation_spot_ltp"),
                                         ("calculation_forward_price", "forward_price")):
                    opt[target] = _number(calculation.get(original), target, issues, positive=True)
                rate = _number(calculation.get("interest_rate"), "interest_rate", issues, signed=True)
                opt["calculation_interest_rate"] = None if rate is None else rate / 100.0
                opt["calculation_underlying_kind"] = "explicit_forward" if opt["calculation_forward_price"] is not None else "unverified_spot_or_forward"
                flags = ["UNVERIFIED_INPUT_TIME"]
                if rate == 0:
                    flags.append("ZERO_INTEREST_RATE_ASSUMPTION")
                if opt["calculation_forward_price"] is None:
                    flags.append("UNDERLYING_BASIS_UNVERIFIED")
                if legacy:
                    flags.append("LEGACY_RECEIPT_UNAVAILABLE")
                opt["model_risk_flags"] = ";".join(flags)
                complete = iv is not None and all(opt[name] is not None for name in ("delta", "gamma", "theta", "vega"))
                opt.update(greeks_source="OPENALGO_BLACK76", derivation_status="stored_derived" if complete else "stored_partial",
                           derivation_reason="Linked stored calculation; availability time preserved" if complete else "Stored calculation has missing or invalid metrics")
        obs["enrichment_issues"] = ";".join(issues) if issues else None

    # Contract identity is static: an unambiguous symbol from the same contract
    # may label quotes whose analytics request failed, without copying analytics.
    for obs in observations:
        opt = options_by_id[obs["observation_id"]]
        symbols = contract_symbols.get(opt["contract_key"], set())
        if _missing(opt["symbol"]) and len(symbols) == 1:
            obs["symbol"] = opt["symbol"] = next(iter(symbols))
            opt["contract_identity_source"] = "stored_contract_mapping"
            if obs["underlying"] == "NIFTY":
                obs["exchange"] = opt["exchange"] = "NFO"
                opt["exchange_source"] = "collector_option_route"
    cleaned.observations = _frame(observations, OBSERVATION_DTYPES)
    cleaned.options = _frame([options_by_id[row["observation_id"]] for row in observations], OPTION_DTYPES)
    return feature_completeness(cleaned.observations, cleaned.options)


def feature_completeness(observations, options):
    fields = {name: int(observations[name].notna().sum()) for name in ("spot", "iv", "volume", "symbol", "exchange")}
    fields.update({name: int(options[name].notna().sum()) for name in ("ltp", "oi", "bid", "ask", "delta", "gamma", "theta", "vega", "rho")})
    complete = observations["iv"].notna() & options[["delta", "gamma", "theta", "vega"]].notna().all(axis=1)
    return {"rows": len(observations), "non_null_counts": fields,
            "iv_and_greeks_complete_rows": int(complete.sum()),
            "iv_and_greeks_missing_rows": int((~complete).sum()),
            "analytics_coverage": "COMPLETE" if len(observations) and complete.all() else "PARTIAL",
            "derivation_status_counts": {str(k): int(v) for k, v in options["derivation_status"].value_counts().items()},
            "note": "Coverage is separate from cleaning integrity. Model assumptions and unverified provider timing remain visible; availability timestamps are required for historical features."}

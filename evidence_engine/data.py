"""Verified local inputs and conservative receipt-time event/feature samples.

All time comparisons use application receipts, not claimed exchange timestamps.
Missing future coverage remains UNKNOWN and never becomes a negative outcome.
"""

from __future__ import annotations

from collections import Counter
import hashlib
from io import BytesIO
import json
import math
from pathlib import Path

import numpy as np
import pandas as pd


IST = "Asia/Kolkata"
OPTION_FEATURES = (
    "past_midpoint_return_pct", "past_oi_change_pct", "past_volume_increment",
    "iv_decimal", "spread_pct", "delta", "gamma", "theta", "vega",
)
FEATURE_COLUMNS = ["past_spot_change"] + [
    f"{side}_{name}" for side in ("CE", "PE") for name in OPTION_FEATURES
]
SAMPLE_COLUMNS = [
    "sample_id", "underlying", "trading_date", "anchor_at", "condition_at",
    "start_at", "end_at", "start_spot", "end_spot", "point_change", "label",
    "outcome_status", "outcome_reason", "start_observation_ids", "end_observation_ids",
    "feature_lineage_json", *FEATURE_COLUMNS,
]
DEFAULTS = {
    "horizon_seconds": 300, "anchor_seconds": 60, "lookback_seconds": 60,
    "quote_max_age_seconds": 5, "max_gap_seconds": 10, "min_move": 50, "max_move": 80,
}


def _json(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _source_quality(frame: pd.DataFrame) -> dict:
    counts = {}
    for name in ("timestamp_source", "data_status", "source_freshness", "derivation_status"):
        counts[name] = frame[name].fillna("MISSING").astype(str).value_counts().to_dict() if name in frame else {}
    issues = Counter()
    for name in ("enrichment_issues", "model_risk_flags"):
        if name in frame:
            for value in frame[name].dropna().astype(str):
                issues.update(part for part in value.split(";") if part)
    return {"status_counts": counts, "issue_counts": dict(issues)}


def _aware(series: pd.Series, name: str, *, nullable: bool = True) -> pd.Series:
    """Reject naive input instead of having pandas silently interpret it as UTC."""
    if not nullable and series.isna().any():
        raise ValueError(f"{name} contains missing timestamps")
    if isinstance(series.dtype, pd.DatetimeTZDtype):
        return series.dt.tz_convert("UTC").astype("datetime64[ns, UTC]")
    values = []
    for value in series:
        if pd.isna(value):
            values.append(pd.NaT)
            continue
        stamp = pd.Timestamp(value)
        if stamp.tzinfo is None:
            raise ValueError(f"{name} requires timezone-aware timestamps")
        values.append(stamp.tz_convert("UTC"))
    return pd.Series(values, index=series.index, dtype="datetime64[ns, UTC]")


def _validate_time_and_identity(frame: pd.DataFrame) -> pd.DataFrame:
    required = {"observation_id", "timestamps", "underlying", "trading_date", "spot"}
    if missing := required - set(frame.columns):
        raise ValueError(f"Missing required input columns: {sorted(missing)}")
    frame = frame.copy()
    if frame["observation_id"].isna().any() or not frame["observation_id"].is_unique:
        raise ValueError("observation_id must be non-null and globally unique")
    if frame[["underlying", "trading_date"]].isna().any().any():
        raise ValueError("underlying and trading_date must be present")
    # A receipt with no usable spot cannot establish price continuity. Reject
    # invalid source prices before grouping receipts or constructing segments;
    # validation must not fill, drop, or replace the stored values.
    for value in frame["spot"]:
        if isinstance(value, (bool, np.bool_, complex, np.complexfloating)):
            raise ValueError("spot must contain only finite positive numeric values")
        try:
            number = float(value)
        except (TypeError, ValueError, OverflowError):
            raise ValueError("spot must contain only finite positive numeric values") from None
        if not math.isfinite(number) or number <= 0:
            raise ValueError("spot must contain only finite positive numeric values")
    frame["timestamps"] = _aware(frame["timestamps"], "timestamps", nullable=False)
    for name in ("iv_available_at", "greeks_available_at", "provider_timestamp",
                 "source_ingestion_time", "derived_received_at", "expirydate",
                 "scheduled_at", "request_started_at"):
        if name in frame:
            frame[name] = _aware(frame[name], name)
    frame["trading_date"] = frame["trading_date"].astype(str)
    local_days = frame["timestamps"].dt.tz_convert(IST).dt.strftime("%Y-%m-%d")
    if not frame["trading_date"].eq(local_days).all():
        raise ValueError("trading_date differs from the IST raw receipt date")
    # CE and PE carry the same underlying observation. A disagreement is not
    # resolved by row order or by averaging two conflicting prices.
    if frame.groupby(["underlying", "timestamps"])["spot"].nunique().gt(1).any():
        raise ValueError("Conflicting spot values at a shared raw timestamp")
    for name in ("iv_available_at", "greeks_available_at"):
        if name in frame:
            valid = frame[name].notna()
            if frame.loc[valid, name].lt(frame.loc[valid, "timestamps"]).any():
                raise ValueError(f"{name} precedes its source raw receipt")
    return frame


def load_verified(registry_path: Path) -> tuple[pd.DataFrame, dict]:
    """Read only bytes named by a local verification registry and recheck hashes.

    The registry is provenance evidence of a prior verification; this function
    performs no live S3 or PostgreSQL calls and does not assert current S3 state.
    """
    registry_path = Path(registry_path).resolve()
    registry_bytes = registry_path.read_bytes()
    registry = json.loads(registry_bytes)
    if registry.get("status") != "VERIFIED" or not registry.get("verified"):
        raise ValueError("Input registry must contain VERIFIED artifacts")
    frames, sources = [], []
    for entry in registry["verified"]:
        tables = {}
        run_dir = Path(entry["run_dir"])
        if not run_dir.is_absolute():
            run_dir = registry_path.parent / run_dir
        for stem in ("observations", "options"):
            filename = stem + ".parquet"
            path = (run_dir / filename).resolve()
            expected = entry["artifact_checks"][filename]
            payload = path.read_bytes()
            digest = hashlib.sha256(payload).hexdigest()
            if expected.get("verified") is False:
                raise ValueError(f"Artifact was not verified: {path}")
            if digest != expected["sha256"] or len(payload) != expected["size_bytes"]:
                raise ValueError(f"Artifact hash/size mismatch: {path}")
            # Parse the exact bytes hashed above, avoiding a second mutable read.
            tables[stem] = pd.read_parquet(BytesIO(payload))
            sources.append({
                "path": str(path), "sha256": digest, "size_bytes": len(payload),
                "date": entry.get("date"), "commit_uri": entry.get("commit_uri"),
                "manifest_uri": entry.get("manifest_uri"),
            })
        obs, opt = tables["observations"], tables["options"]
        for name, table in tables.items():
            if "observation_id" not in table or table["observation_id"].isna().any() or not table["observation_id"].is_unique:
                raise ValueError(f"{name} observation_id must be non-null and unique")
        if set(obs["observation_id"]) != set(opt["observation_id"]):
            raise ValueError("Observation and option ID sets differ")
        if len(obs) != entry["rows_per_table"]:
            raise ValueError("Table row count differs from registry")
        if entry.get("date") and "trading_date" in obs and not obs["trading_date"].astype(str).eq(entry["date"]).all():
            raise ValueError("Table trading_date differs from registry date")
        # Validate common identity fields rather than leaving ambiguous _x/_y
        # columns or quietly choosing one table's value.
        left, right = obs.set_index("observation_id"), opt.set_index("observation_id")
        right = right.reindex(left.index)
        shared = set(left.columns) & set(right.columns)
        for name in shared:
            a, b = left[name], right[name]
            if name == "timestamps":
                a, b = _aware(a, name, nullable=False), _aware(b, name, nullable=False)
            equal = (a.eq(b) | (a.isna() & b.isna())).fillna(False)
            if not equal.all():
                raise ValueError(f"Conflicting shared column: {name}")
        joined = obs.merge(opt.drop(columns=sorted(shared)), on="observation_id", validate="one_to_one")
        frames.append(joined)
    all_data = _validate_time_and_identity(pd.concat(frames, ignore_index=True))
    if "source_version" not in all_data:
        raise ValueError("Missing source_version provenance")
    eligible = all_data["source_version"].astype("string").eq("2").fillna(False)
    data = all_data.loc[eligible].copy()
    excluded = all_data.loc[~eligible, "source_version"].fillna("MISSING").astype(str).value_counts().to_dict()
    content_identity = sorted((s["sha256"], s["size_bytes"]) for s in sources)
    dataset_id = hashlib.sha256(_json({"artifacts": content_identity, "source_version": "2"}).encode()).hexdigest()
    manifest = {
        "dataset_id": dataset_id, "source_registry": str(registry_path),
        "source_registry_sha256": hashlib.sha256(registry_bytes).hexdigest(),
        "source_verification_at_ist": registry.get("checked_at_ist"),
        "source_refs": sources, "all_rows": len(all_data), "version_2_rows": len(data),
        "excluded_legacy_rows": int((~eligible).sum()), "excluded_source_versions": excluded,
        "trading_dates": sorted(data["trading_date"].unique().tolist()),
        "timestamp_basis": "APPLICATION_RECEIPT; provider event time remains unverified",
        "verification_scope": "Local artifact hashes and sizes rechecked; no live remote verification",
        "source_quality": _source_quality(data),
    }
    data.attrs["dataset_id"] = dataset_id
    return data, manifest


def classify_move(change: float, min_move: float = 50, max_move: float = 80,
                  *, detector_version: str = "spot-endpoint-band-v1") -> str:
    if detector_version not in ("spot-endpoint-band-v1", "spot-endpoint-momentum-v2"):
        raise ValueError("Unsupported event detector")
    if not math.isfinite(float(change)):
        return "UNKNOWN"
    magnitude = abs(change)
    if detector_version == "spot-endpoint-momentum-v2":
        if magnitude >= min_move:
            return "UP_MOMENTUM" if change > 0 else "DOWN_MOMENTUM"
        return "OTHER"
    if min_move <= magnitude <= max_move:
        return "UP_50_80" if change > 0 else "DOWN_50_80"
    if magnitude > max_move:
        return "UP_OVER_80" if change > 0 else "DOWN_OVER_80"
    return "OTHER"


def _number(value) -> float:
    if pd.isna(value):
        return float("nan")
    result = float(value)
    return result if math.isfinite(result) else float("nan")


def _pct(new, old) -> float:
    new, old = _number(new), _number(old)
    return 100 * (new / old - 1) if old > 0 else float("nan")


def _asof(frame: pd.DataFrame, ns: np.ndarray, clock: pd.Timestamp, age: float):
    index = int(np.searchsorted(ns, clock.value, side="right")) - 1
    if index < 0 or clock.value - ns[index] > age * 1e9:
        return None
    return frame.iloc[index]


def _ids(row) -> list[str]:
    return row["observation_ids"] if "observation_ids" in row else [str(row["observation_id"])]


def _lineage(*rows, available_at=None) -> dict:
    return {
        "observation_ids": sorted({item for row in rows for item in _ids(row)}),
        "max_available_at": (available_at if available_at is not None else max(row["timestamps"] for row in rows)).isoformat(),
        "source_timestamps": sorted({row["timestamps"].isoformat() for row in rows}),
        "contract_keys": sorted({str(row["contract_key"]) for row in rows if "contract_key" in row}),
    }


def build_samples(frame: pd.DataFrame, config: dict) -> tuple[pd.DataFrame, dict]:
    """Build underlying spot outcomes and strictly earlier option features.

    Anchors include the last five session minutes: their missing/out-of-session
    endpoints are retained as UNKNOWN. Partial histories produce null features,
    never fabricated inputs, and do not filter events using future availability.
    """
    policy = {key: config.get(key, default) for key, default in DEFAULTS.items()}
    for key, value in policy.items():
        if not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
            raise ValueError(f"{key} must be a positive finite number")
    detector = config.get("detector_version", "spot-endpoint-band-v1")
    if detector not in ("spot-endpoint-band-v1", "spot-endpoint-momentum-v2"):
        raise ValueError("Unsupported event detector")
    if detector == "spot-endpoint-band-v1" and policy["min_move"] > policy["max_move"]:
        raise ValueError("min_move must be <= max_move")
    # Keep the original named band readable; new momentum targets have no cap.
    if detector == "spot-endpoint-band-v1" and (policy["min_move"], policy["max_move"]) != (50, 80):
        raise ValueError("The named event labels require min_move=50 and max_move=80")
    data = _validate_time_and_identity(frame)
    target_underlying = config.get("underlying", "NIFTY")
    if not isinstance(target_underlying, str) or not target_underlying.strip():
        raise ValueError("underlying must name one target instrument")
    excluded_underlyings = data.loc[data["underlying"].ne(target_underlying), "underlying"].value_counts().to_dict()
    data = data.loc[data["underlying"].eq(target_underlying)].copy()
    if "source_version" in data and not data["source_version"].astype("string").eq("2").all():
        raise ValueError("Sampling requires source_version 2 receipt provenance")
    required = {"optiontype", "contract_key"}
    if missing := required - set(data.columns):
        raise ValueError(f"Missing option identity: {sorted(missing)}")
    if data[list(required)].isna().any().any() or not data["optiontype"].isin(["CE", "PE"]).all():
        raise ValueError("Option contract identity and CE/PE side are required")
    for name in ("bid", "ask", "oi", "volume", "iv", "delta", "gamma", "theta", "vega"):
        if name not in data:
            data[name] = np.nan
    for name in ("iv_available_at", "greeks_available_at"):
        if name not in data:
            data[name] = pd.Series(pd.NaT, index=data.index, dtype="datetime64[ns, UTC]")
    dataset_id = frame.attrs.get("dataset_id", "unspecified_dataset")
    records, group_counts = [], []
    reasons = Counter()
    for (underlying, day), group in data.groupby(["underlying", "trading_date"], sort=True):
        session_start = pd.Timestamp(f"{day} 09:15:00", tz=IST).tz_convert("UTC")
        session_end = pd.Timestamp(f"{day} 15:30:00", tz=IST).tz_convert("UTC")
        in_session = group["timestamps"].between(session_start, session_end, inclusive="left")
        group = group.loc[in_session].sort_values(["timestamps", "observation_id"])
        counts = {"underlying": str(underlying), "trading_date": day,
                  "out_of_session_rows": int((~in_session).sum()), "clock_candidates": 0,
                  "invalid_start": 0, "selected": 0}
        if group.empty:
            group_counts.append(counts)
            continue
        spots = group.groupby("timestamps", as_index=False, sort=True).agg(
            spot=("spot", "first"), observation_ids=("observation_id", lambda s: sorted(s.astype(str))))
        spots["segment"] = spots["timestamps"].diff().dt.total_seconds().gt(policy["max_gap_seconds"]).cumsum()
        spot_ns = spots["timestamps"].astype("int64").to_numpy()
        side_data = {}
        for side, quotes in group.groupby("optiontype"):
            quotes = quotes.copy().reset_index(drop=True)
            if quotes["timestamps"].duplicated().any():
                raise ValueError(f"Ambiguous multiple {side} contracts/quotes at a raw timestamp")
            gaps = quotes["timestamps"].diff().dt.total_seconds()
            quotes["segment"] = (gaps.gt(policy["max_gap_seconds"]) | quotes["contract_key"].ne(quotes["contract_key"].shift())).cumsum()
            midpoint = (quotes["bid"] + quotes["ask"]) / 2
            quotes["midpoint"] = midpoint.where(midpoint.gt(0) & quotes["bid"].ge(0) & quotes["ask"].ge(quotes["bid"]))
            calculations = {}
            for segment, segment_quotes in quotes.groupby("segment"):
                for availability in ("iv_available_at", "greeks_available_at"):
                    available = segment_quotes.loc[segment_quotes[availability].notna()].sort_values([availability, "timestamps"])
                    calculations[(segment, availability)] = (available, available[availability].astype("int64").to_numpy())
            side_data[side] = (quotes, quotes["timestamps"].astype("int64").to_numpy(), calculations)
        grid = pd.date_range(session_start, session_end, freq=pd.Timedelta(seconds=policy["anchor_seconds"]), inclusive="left")
        counts["clock_candidates"] = len(grid)
        for anchor in grid:
            start = _asof(spots, spot_ns, anchor, policy["quote_max_age_seconds"])
            if start is None or not math.isfinite(_number(start["spot"])):
                counts["invalid_start"] += 1
                continue
            condition = start["timestamps"] - pd.Timedelta(1, unit="ns")
            end_clock = anchor + pd.Timedelta(seconds=policy["horizon_seconds"])
            end = _asof(spots, spot_ns, end_clock, policy["quote_max_age_seconds"]) if end_clock < session_end else None
            reason = "complete"
            if end_clock >= session_end:
                reason = "horizon_outside_session"
            elif end is None or not math.isfinite(_number(end["spot"])):
                reason = "missing_or_old_endpoint"
            elif abs((end["timestamps"] - start["timestamps"]).total_seconds() - policy["horizon_seconds"]) > policy["quote_max_age_seconds"]:
                reason = "receipt_interval_outside_tolerance"
            elif end["segment"] != start["segment"]:
                reason = "gap_in_outcome_window"
            complete = reason == "complete"
            change = _number(end["spot"]) - _number(start["spot"]) if complete else np.nan
            identity_policy = {**policy, "detector_version": detector} if detector != "spot-endpoint-band-v1" else policy
            key = _json([dataset_id, str(underlying), anchor.isoformat(), identity_policy])
            row = {
                "sample_id": hashlib.sha256(key.encode()).hexdigest(), "underlying": str(underlying),
                "trading_date": day, "anchor_at": anchor, "condition_at": condition,
                "start_at": start["timestamps"], "end_at": end["timestamps"] if end is not None else pd.NaT,
                "start_spot": _number(start["spot"]), "end_spot": _number(end["spot"]) if end is not None else np.nan,
                "point_change": change, "label": classify_move(change, policy["min_move"], policy["max_move"], detector_version=detector),
                "outcome_status": "COMPLETE" if complete else "UNKNOWN", "outcome_reason": reason,
                "start_observation_ids": _json(_ids(start)), "end_observation_ids": _json(_ids(end)) if end is not None else "[]",
                **{name: np.nan for name in FEATURE_COLUMNS},
            }
            lineage = {}

            def feature(name, value, *source_rows, available_at=None):
                numeric = _number(value)
                if math.isfinite(numeric):
                    row[name] = numeric
                    lineage[name] = _lineage(*source_rows, available_at=available_at)

            now = _asof(spots, spot_ns, condition, policy["quote_max_age_seconds"])
            past_clock = condition - pd.Timedelta(seconds=policy["lookback_seconds"])
            past = _asof(spots, spot_ns, past_clock, policy["quote_max_age_seconds"])
            if now is not None and past is not None and now["segment"] == past["segment"]:
                feature("past_spot_change", _number(now["spot"]) - _number(past["spot"]), past, now)
            for side, (quotes, quote_ns, calculations) in side_data.items():
                current = _asof(quotes, quote_ns, condition, policy["quote_max_age_seconds"])
                previous = _asof(quotes, quote_ns, past_clock, policy["quote_max_age_seconds"])
                if current is None:
                    continue
                if previous is not None and current["segment"] == previous["segment"]:
                    feature(f"{side}_past_midpoint_return_pct", _pct(current["midpoint"], previous["midpoint"]), previous, current)
                    feature(f"{side}_past_oi_change_pct", _pct(current["oi"], previous["oi"]), previous, current)
                    old_volume, new_volume = previous["volume"], current["volume"]
                    if pd.notna(new_volume) and pd.notna(old_volume) and new_volume >= old_volume:
                        # Subtract source integers before converting to float.
                        feature(f"{side}_past_volume_increment", current["volume"] - previous["volume"], previous, current)
                mid = _number(current["midpoint"])
                if mid > 0:
                    feature(f"{side}_spread_pct", 100 * (_number(current["ask"]) - _number(current["bid"])) / mid, current)
                for availability, metrics in (("iv_available_at", [("iv_decimal", "iv")]),
                                              ("greeks_available_at", [(name, name) for name in ("delta", "gamma", "theta", "vega")])):
                    available, available_ns = calculations[(current["segment"], availability)]
                    index = int(np.searchsorted(available_ns, condition.value, side="right")) - 1
                    if index < 0:
                        continue
                    calculated = available.iloc[index]
                    if condition.value - calculated[availability].value > 10_000_000_000 or condition.value - calculated["timestamps"].value > 15_000_000_000:
                        continue
                    for feature_name, source_name in metrics:
                        feature(f"{side}_{feature_name}", calculated[source_name], calculated, available_at=calculated[availability])
            row["feature_lineage_json"] = _json(lineage)
            records.append(row)
            counts["selected"] += 1
            reasons[reason] += 1
        group_counts.append(counts)
    samples = pd.DataFrame(records, columns=SAMPLE_COLUMNS)
    summary = {
        "sample_count": len(samples), "label_counts": samples["label"].value_counts().to_dict(),
        "outcome_reason_counts": dict(reasons), "group_counts": group_counts,
        "source_quality": _source_quality(data),
        "excluded_underlyings": excluded_underlyings,
        "feature_non_null_counts": {name: int(samples[name].notna().sum()) for name in FEATURE_COLUMNS},
        "policies": {
            **policy, "underlying": target_underlying, "timezone": IST, "session": "09:15:00 inclusive; 15:30:00 exclusive",
            "detector_version": detector,
            "event_definition": ("absolute endpoint move >= min_move; no upper bound" if detector == "spot-endpoint-momentum-v2" else "absolute endpoint move between min_move and max_move inclusive"),
            "condition": "actual start receipt minus 1 nanosecond; all features available by condition",
            "spot_deduplication": "one underlying value per raw receipt; conflicting prices rejected",
            "analytics_max_age_seconds": 10, "analytics_source_quote_max_age_seconds": 15,
            "missing_outcomes": "retained as UNKNOWN", "missing_features": "null, never filled",
            "contract_policy": "past/current/derived within continuous same-contract segments",
            "timestamp_basis": "application receipt time; provider event time remains unverified",
            "overlap": "one-minute anchors have overlapping five-minute outcome windows",
        },
    }
    return samples, summary

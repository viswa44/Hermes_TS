"""Synthetic adversarial coverage for receipt-time lineage and event sampling."""

import hashlib
import json

import numpy as np
import pandas as pd
import pytest

from evidence_engine.data import FEATURE_COLUMNS, build_samples, classify_move, load_verified


START = pd.Timestamp("2026-09-14T09:15:00+05:30")


def quotes(seconds=480):
    rows = []
    for second in range(0, seconds + 1, 5):
        stamp = START + pd.Timedelta(seconds=second)
        for side in ("CE", "PE"):
            rows.append({
                "observation_id": f"{side}-{second}", "timestamps": stamp,
                "underlying": "NIFTY", "trading_date": "2026-09-14", "spot": 25000 + second / 5,
                "source_version": "2", "optiontype": side, "contract_key": f"NIFTY|2026-09-15|25000|{side}",
                "bid": 99 + second / 100, "ask": 101 + second / 100,
                "oi": 1000 + second, "volume": 10000 + second,
                "iv": .2, "delta": .5 if side == "CE" else -.5,
                "gamma": .01, "theta": -2., "vega": 3.,
                "iv_available_at": stamp + pd.Timedelta(seconds=1),
                "greeks_available_at": stamp + pd.Timedelta(seconds=1),
            })
    return pd.DataFrame(rows)


def selected(samples, minute="09:17:00"):
    anchor = pd.Timestamp(f"2026-09-14T{minute}+05:30")
    return samples.loc[samples["anchor_at"].eq(anchor)].iloc[0]


def registry_for(tmp_path, frame=None, mutate_options=None):
    frame = quotes(30) if frame is None else frame
    observation_names = ["observation_id", "timestamps", "underlying", "trading_date", "spot",
                         "source_version", "volume", "iv", "iv_available_at"]
    option_names = [name for name in frame if name not in observation_names or name in ("observation_id", "timestamps", "underlying")]
    obs, opt = frame[observation_names].copy(), frame[option_names].copy()
    if mutate_options is not None:
        opt = mutate_options(opt)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    checks = {}
    for name, table in (("observations", obs), ("options", opt)):
        path = run_dir / f"{name}.parquet"
        table.to_parquet(path, index=False)
        payload = path.read_bytes()
        checks[path.name] = {"sha256": hashlib.sha256(payload).hexdigest(), "size_bytes": len(payload), "verified": True}
    registry = {"status": "VERIFIED", "verified": [{
        "run_dir": str(run_dir), "date": "2026-09-14", "rows_per_table": len(obs), "artifact_checks": checks,
    }]}
    path = tmp_path / "registry.json"
    path.write_text(json.dumps(registry))
    return path


@pytest.mark.parametrize("change,label", [
    (49.99, "OTHER"), (50, "UP_50_80"), (80, "UP_50_80"), (80.01, "UP_OVER_80"),
    (-49.99, "OTHER"), (-50, "DOWN_50_80"), (-80, "DOWN_50_80"), (-80.01, "DOWN_OVER_80"),
    (0, "OTHER"), (float("nan"), "UNKNOWN"),
])
def test_inclusive_endpoint_boundaries(change, label):
    assert classify_move(change) == label


@pytest.mark.parametrize("change,label", [
    (49.99, "OTHER"), (50, "UP_MOMENTUM"), (80, "UP_MOMENTUM"), (130, "UP_MOMENTUM"),
    (-49.99, "OTHER"), (-50, "DOWN_MOMENTUM"), (-80, "DOWN_MOMENTUM"), (-130, "DOWN_MOMENTUM"),
    (float("nan"), "UNKNOWN"),
])
def test_momentum_has_inclusive_minimum_and_no_upper_cap(change, label):
    assert classify_move(change, detector_version="spot-endpoint-momentum-v2") == label


def test_momentum_samples_include_large_moves_with_distinct_versioned_identity():
    frame = quotes()
    frame["spot"] = 25000 + 2 * (frame["spot"] - 25000)
    legacy, _ = build_samples(frame, {})
    current, summary = build_samples(frame, {"detector_version": "spot-endpoint-momentum-v2"})
    assert selected(legacy)["label"] == "UP_OVER_80"
    assert selected(current)["label"] == "UP_MOMENTUM"
    assert selected(current)["point_change"] == 120
    assert selected(current)["sample_id"] != selected(legacy)["sample_id"]
    assert summary["policies"]["event_definition"].endswith("no upper bound")
    assert selected(current, "09:20:00")["label"] == "UNKNOWN"


def test_loader_rechecks_bytes_and_merges_identity_once(tmp_path):
    path = registry_for(tmp_path)
    data, manifest = load_verified(path)
    assert len(data) == 14
    assert len(data.columns) == len(set(data.columns))
    assert not any(name.endswith(("_x", "_y")) for name in data)
    assert len(manifest["source_refs"]) == 2
    assert data.attrs["dataset_id"] == manifest["dataset_id"]
    assert manifest["excluded_legacy_rows"] == 0
    artifact = tmp_path / "run" / "observations.parquet"
    artifact.write_bytes(artifact.read_bytes() + b"tampered")
    with pytest.raises(ValueError, match="hash/size mismatch"):
        load_verified(path)


def test_loader_requires_exact_key_sets(tmp_path):
    def different_id(opt):
        opt.loc[0, "observation_id"] = "different"
        return opt
    with pytest.raises(ValueError, match="ID sets differ"):
        load_verified(registry_for(tmp_path, mutate_options=different_id))


def test_loader_checks_shared_values(tmp_path):
    def different_underlying(opt):
        opt.loc[0, "underlying"] = "BANKNIFTY"
        return opt
    with pytest.raises(ValueError, match="Conflicting shared column"):
        load_verified(registry_for(tmp_path, mutate_options=different_underlying))


def test_loader_rejects_duplicate_ids_across_runs(tmp_path):
    path = registry_for(tmp_path)
    registry = json.loads(path.read_text())
    registry["verified"].append(registry["verified"][0])
    path.write_text(json.dumps(registry))
    with pytest.raises(ValueError, match="globally unique"):
        load_verified(path)


def test_loader_records_legacy_exclusions(tmp_path):
    frame = quotes(30)
    frame.loc[:1, "source_version"] = "1"
    data, manifest = load_verified(registry_for(tmp_path, frame))
    assert len(data) == len(frame) - 2
    assert manifest["excluded_legacy_rows"] == 2
    assert manifest["excluded_source_versions"] == {"1": 2}


def test_naive_raw_timestamps_are_rejected(tmp_path):
    frame = quotes(30)
    frame["timestamps"] = frame["timestamps"].dt.tz_localize(None)
    with pytest.raises(ValueError, match="timezone-aware"):
        load_verified(registry_for(tmp_path, frame))


def test_conflicting_simultaneous_spot_is_rejected():
    frame = quotes()
    frame.loc[0, "spot"] += 1
    with pytest.raises(ValueError, match="Conflicting spot"):
        build_samples(frame, {})


@pytest.mark.parametrize("value", [
    None, pd.NA, np.nan, np.inf, -np.inf, 0, -1, True, False, "invalid", 1 + 2j,
])
def test_invalid_interior_spot_cannot_establish_outcome_continuity(value):
    frame = quotes()
    frame["spot"] = frame["spot"].astype(object)
    interior = frame["timestamps"].between(
        START + pd.Timedelta(seconds=240), START + pd.Timedelta(seconds=295))
    frame.loc[interior, "spot"] = value
    # The 09:17 -> 09:22 endpoints are valid, but invalid interior receipts
    # cannot bridge a minute without usable spot observations.
    with pytest.raises(ValueError, match="spot must contain only finite positive numeric values"):
        build_samples(frame, {})


@pytest.mark.parametrize("value", [np.nan, np.inf, 0, -1])
def test_loader_rejects_invalid_spot_even_when_artifact_hashes_match(tmp_path, value):
    frame = quotes(30)
    frame.loc[frame["timestamps"].eq(START + pd.Timedelta(seconds=15)), "spot"] = value
    with pytest.raises(ValueError, match="spot must contain only finite positive numeric values"):
        load_verified(registry_for(tmp_path, frame))


def test_spot_is_not_double_counted_and_lineage_is_strictly_pre_event():
    samples, summary = build_samples(quotes(), {})
    row = selected(samples)
    assert row["label"] == "UP_50_80"
    assert row["point_change"] == 60
    assert len(json.loads(row["start_observation_ids"])) == 2
    assert row["condition_at"] < row["start_at"]
    assert samples["anchor_at"].is_unique
    assert row["past_spot_change"] == 12
    assert summary["sample_count"] == 9
    source = quotes().set_index("observation_id")
    lineage = json.loads(row["feature_lineage_json"])
    assert set(lineage) == set(FEATURE_COLUMNS)
    for item in lineage.values():
        assert pd.Timestamp(item["max_available_at"]) <= row["condition_at"]
        for observation_id in item["observation_ids"]:
            assert source.loc[observation_id, "timestamps"] <= row["condition_at"]


def test_missing_horizons_remain_unknown_instead_of_negative():
    samples, _ = build_samples(quotes(), {})
    row = selected(samples, "09:20:00")
    assert row["label"] == "UNKNOWN"
    assert row["outcome_reason"] == "missing_or_old_endpoint"
    assert pd.isna(row["point_change"])
    assert pd.notna(row["past_spot_change"])


def test_interior_gap_invalidates_outcome_even_with_both_endpoints():
    frame = quotes()
    frame = frame.loc[~frame["timestamps"].between(START + pd.Timedelta(seconds=250), START + pd.Timedelta(seconds=265))]
    samples, _ = build_samples(frame, {})
    row = selected(samples)
    assert pd.notna(row["end_at"])
    assert row["label"] == "UNKNOWN"
    assert row["outcome_reason"] == "gap_in_outcome_window"


def test_late_analytics_cannot_leak_into_pre_event_features():
    frame = quotes()
    delayed = frame["timestamps"].ge(START + pd.Timedelta(seconds=110))
    frame.loc[delayed, "iv_available_at"] += pd.Timedelta(seconds=40)
    frame.loc[delayed, "greeks_available_at"] += pd.Timedelta(seconds=40)
    frame.loc[delayed, "iv"] = 999
    frame.loc[delayed, "delta"] = 999
    samples, _ = build_samples(frame, {})
    row = selected(samples)
    # The last pre-delay quote is now too old by calculation availability;
    # never copy analytics back from a later receipt to fill it.
    assert pd.isna(row["CE_iv_decimal"])
    assert pd.isna(row["PE_delta"])
    assert "CE_iv_decimal" not in json.loads(row["feature_lineage_json"])


def test_no_future_source_mutation_can_change_features():
    original, _ = build_samples(quotes(), {})
    frame = quotes()
    future = frame["timestamps"].ge(START + pd.Timedelta(seconds=120))
    for name in ("spot", "bid", "ask", "oi", "volume", "iv", "delta", "gamma", "theta", "vega"):
        frame.loc[future, name] = frame.loc[future, name] * 10
    changed, _ = build_samples(frame, {})
    pd.testing.assert_series_equal(selected(original)[FEATURE_COLUMNS], selected(changed)[FEATURE_COLUMNS])
    assert selected(original)["feature_lineage_json"] == selected(changed)["feature_lineage_json"]


def test_contract_switch_prevents_cross_contract_past_and_analytics_features():
    frame = quotes()
    switched = frame["optiontype"].eq("CE") & frame["timestamps"].ge(START + pd.Timedelta(seconds=115))
    frame.loc[switched, "contract_key"] = "NIFTY|2026-09-15|25050|CE"
    frame.loc[switched, "iv_available_at"] += pd.Timedelta(seconds=40)
    frame.loc[switched, "greeks_available_at"] += pd.Timedelta(seconds=40)
    samples, _ = build_samples(frame, {})
    row = selected(samples)
    assert pd.isna(row["CE_past_midpoint_return_pct"])
    assert pd.isna(row["CE_past_oi_change_pct"])
    assert pd.isna(row["CE_past_volume_increment"])
    assert pd.isna(row["CE_iv_decimal"])
    assert pd.isna(row["CE_delta"])
    assert pd.notna(row["CE_spread_pct"])
    assert pd.notna(row["PE_past_midpoint_return_pct"])


def test_missing_values_and_volume_counter_decrease_stay_null():
    frame = quotes()
    frame.loc[frame["timestamps"].eq(START + pd.Timedelta(seconds=115)), "volume"] = 0
    frame["oi"] = np.nan
    samples, _ = build_samples(frame, {})
    row = selected(samples)
    assert pd.isna(row["CE_past_volume_increment"])
    assert pd.isna(row["CE_past_oi_change_pct"])
    assert pd.notna(row["CE_past_midpoint_return_pct"])


def test_timezone_date_mismatch_and_impossible_availability_rejected():
    frame = quotes()
    frame.loc[0, "trading_date"] = "2026-09-15"
    with pytest.raises(ValueError, match="IST raw receipt date"):
        build_samples(frame, {})
    frame = quotes()
    frame.loc[0, "iv_available_at"] = START - pd.Timedelta(seconds=1)
    with pytest.raises(ValueError, match="precedes its source"):
        build_samples(frame, {})


def test_empty_samples_keep_required_columns():
    samples, summary = build_samples(quotes().iloc[:0], {})
    assert samples.empty
    assert {"sample_id", "condition_at", "label", "feature_lineage_json", *FEATURE_COLUMNS} <= set(samples)
    assert summary["sample_count"] == 0


def test_irregular_receipt_is_not_given_extra_freshness_tolerance():
    frame = quotes()
    older = frame["timestamps"].eq(START + pd.Timedelta(seconds=115))
    for name in ("timestamps", "iv_available_at", "greeks_available_at"):
        frame.loc[older, name] -= pd.Timedelta(seconds=1)
    samples, _ = build_samples(frame, {})
    row = selected(samples)
    assert row["label"] == "UP_50_80"
    assert row[FEATURE_COLUMNS].isna().all()


def test_source_status_issues_are_reported():
    frame = quotes()
    frame["data_status"] = "PARTIAL"
    frame["source_freshness"] = "UNVERIFIED_PROVIDER_TIME"
    frame["enrichment_issues"] = "iv_unavailable"
    _, summary = build_samples(frame, {})
    assert summary["source_quality"]["status_counts"]["data_status"] == {"PARTIAL": len(frame)}
    assert summary["source_quality"]["issue_counts"] == {"iv_unavailable": len(frame)}


def test_large_integer_volume_decrease_is_not_rounded_into_valid_increment():
    frame = quotes()
    frame["volume"] = 2**60 + 100
    frame.loc[frame["timestamps"].eq(START + pd.Timedelta(seconds=115)), "volume"] = 2**60 + 99
    samples, _ = build_samples(frame, {})
    assert pd.isna(selected(samples)["CE_past_volume_increment"])


def test_default_target_does_not_pool_other_underlyings():
    original = quotes()
    other = original.copy()
    other["underlying"] = "BANKNIFTY"
    other["observation_id"] = "other-" + other["observation_id"]
    other["contract_key"] = other["contract_key"].str.replace("NIFTY", "BANKNIFTY")
    samples, summary = build_samples(pd.concat([original, other], ignore_index=True), {})
    assert set(samples["underlying"]) == {"NIFTY"}
    assert summary["excluded_underlyings"] == {"BANKNIFTY": len(other)}
    alternate, _ = build_samples(other, {"underlying": "BANKNIFTY"})
    assert set(alternate["underlying"]) == {"BANKNIFTY"}


def test_session_close_is_exclusive_and_close_horizon_is_unknown():
    frame = quotes(seconds=360)
    # Six minutes 15:24-15:30, including two input rows at the excluded close.
    shift = pd.Timedelta(hours=6, minutes=9)
    for name in ("timestamps", "iv_available_at", "greeks_available_at"):
        frame[name] += shift
    samples, summary = build_samples(frame, {})
    assert selected(samples, "15:24:00")["outcome_status"] == "COMPLETE"
    assert selected(samples, "15:25:00")["outcome_reason"] == "horizon_outside_session"
    assert samples["anchor_at"].max() == pd.Timestamp("2026-09-14T15:29:00+05:30")
    assert summary["group_counts"][0]["out_of_session_rows"] == 2

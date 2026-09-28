"""Adversarial integrity checks using deterministic local source observations."""
from copy import deepcopy
import json

import pandas as pd
import pytest

from evidence_engine.config import ResearchConfig
from evidence_engine.data import build_samples
from evidence_engine.research import audit, discover, scan


@pytest.fixture(scope="module")
def evidence():
    config = ResearchConfig(min_support=2, max_conditions=2,
                            detector_version="spot-endpoint-band-v1",
                            discovery_version="single-feature-training-quantiles-v1").to_dict()
    rows = []
    for day in ("2026-09-17", "2026-09-18"):
        origin = pd.Timestamp(day + "T09:15:00+05:30")
        for second in range(0, 1501, 5):
            stamp = origin + pd.Timedelta(seconds=second)
            for side in ("CE", "PE"):
                rows.append({
                    "observation_id": f"{day}:{second}:{side}", "timestamps": stamp,
                    "underlying": "NIFTY", "trading_date": day,
                    "spot": 25000 + min(second, 600) * 0.2,
                    "source_version": "2", "optiontype": side, "contract_key": f"fixed-{side}",
                    "bid": 100 + second * 0.01, "ask": 102 + second * 0.01,
                    "oi": 1000 + second, "volume": 100 + second,
                    "iv": 0.2, "delta": 0.5 if side == "CE" else -0.5,
                    "gamma": 0.002, "theta": -3.0, "vega": 2.0,
                    "iv_available_at": stamp + pd.Timedelta(seconds=2),
                    "greeks_available_at": stamp + pd.Timedelta(seconds=2),
                })
    raw = pd.DataFrame(rows)
    raw.attrs["dataset_id"] = "test-source-snapshot"
    samples, _ = build_samples(raw, config)
    discovery = discover(samples, raw.attrs["dataset_id"], config)
    assert discovery["conditions"], "Fixture must discover actual hypotheses"
    occurrences, statistics = scan(samples, discovery, config)
    return raw, samples, discovery, occurrences, statistics, config


def inspect(evidence, **replace):
    names = ("raw_frame", "samples", "discovery", "occurrences", "statistics", "config")
    inputs = dict(zip(names, evidence))
    inputs.update(replace)
    return audit(**inputs)


def test_source_backed_evidence_passes(evidence):
    report = inspect(evidence)
    assert report["status"] == "PASS", report
    assert report["evidence_status"] == "EXPLORATORY"


@pytest.mark.parametrize("artifact", ["discovery", "statistics"])
@pytest.mark.parametrize("field", ["run_id", "executed_at"])
def test_run_metadata_cannot_be_borrowed_from_another_execution(evidence, artifact, field):
    discovery, statistics = deepcopy(evidence[2]), deepcopy(evidence[4])
    metadata = {"run_id": "expected-run", "executed_at": "2026-09-20T12:00:00+00:00"}
    discovery.update(metadata)
    statistics.update(metadata)
    payload = discovery if artifact == "discovery" else statistics
    payload[field] = "different-execution"
    report = inspect(evidence, discovery=discovery, statistics=statistics, run_metadata=metadata)
    assert report["status"] == "FAIL"
    assert not next(check["passed"] for check in report["checks"]
                    if check["check"] == "artifacts_bound_to_run")


def test_forged_future_lineage_fails(evidence):
    raw, original, *_ = evidence
    samples = original.copy(deep=True)
    index = samples.loc[samples["past_spot_change"].notna()].index[0]
    lineage = json.loads(samples.at[index, "feature_lineage_json"])
    future = raw.loc[raw["timestamps"] > samples.at[index, "start_at"]].iloc[0]
    lineage["past_spot_change"]["observation_ids"] = [future["observation_id"]]
    # Keep the claimed availability in the past; actual source time must win.
    samples.at[index, "feature_lineage_json"] = json.dumps(lineage)
    report = inspect(evidence, samples=samples)
    assert report["status"] == "FAIL"
    assert any(not row["passed"] for row in report["checks"]
               if row["check"] == "actual_feature_availability_before_condition")


@pytest.mark.parametrize("field,value", [
    ("point_change", -5000.0), ("success", False), ("nonoverlapping", False),
    ("partition", "evaluation"), ("target", "DOWN_50_80"),
    ("condition_at", pd.Timestamp("2030-01-01T00:00:00Z")),
])
def test_occurrence_field_tampering_fails(evidence, field, value):
    occurrences = evidence[3].copy(deep=True)
    index = occurrences.loc[occurrences["success"].eq(True) & occurrences["nonoverlapping"] &
                            occurrences["partition"].eq("discovery")].index[0]
    occurrences.at[index, field] = value
    report = inspect(evidence, occurrences=occurrences)
    assert report["status"] == "FAIL"
    assert not next(row["passed"] for row in report["checks"]
                    if row["check"] == "exact_occurrence_set_and_values")


@pytest.mark.parametrize("mutation", ["missing", "duplicate", "baseline", "rate", "boolean_count"])
def test_summary_tampering_fails(evidence, mutation):
    statistics = deepcopy(evidence[4])
    if mutation == "missing":
        statistics["summaries"] = []
    elif mutation == "duplicate":
        statistics["summaries"].append(deepcopy(statistics["summaries"][0]))
    elif mutation == "baseline":
        statistics["summaries"][0]["baseline"]["matches"] += 1
    elif mutation == "rate":
        statistics["summaries"][0]["event_rate"] = -1
    else:
        statistics["summaries"][0]["matches"] = True
    assert inspect(evidence, statistics=statistics)["status"] == "FAIL"


def test_extra_unrecognized_condition_occurrence_fails(evidence):
    occurrences = evidence[3].copy(deep=True)
    extra = occurrences.iloc[[0]].copy()
    extra["condition_id"] = "unregistered-condition"
    assert inspect(evidence, occurrences=pd.concat([occurrences, extra], ignore_index=True))["status"] == "FAIL"


def test_nonchronological_partition_fails(evidence):
    discovery = deepcopy(evidence[2])
    split = discovery["partition"]
    split["discovery_dates"], split["evaluation_dates"] = split["evaluation_dates"], split["discovery_dates"]
    assert inspect(evidence, discovery=discovery)["status"] == "FAIL"


def test_holdout_values_cannot_change_discovered_predicates(evidence):
    _, samples, original, _, _, config = evidence
    altered = samples.copy(deep=True)
    mask = altered["trading_date"].isin(original["partition"]["evaluation_dates"])
    altered.loc[mask, "past_spot_change"] = 1_000_000
    altered.loc[mask, "label"] = "DOWN_50_80"
    rebuilt = discover(altered, original["dataset_id"], config)
    assert rebuilt["conditions"] == original["conditions"]
    assert rebuilt["search_log"] == original["search_log"]


def test_spacing_reserves_receipt_uncertainty(evidence):
    _, samples, discovery, _, _, config = evidence
    _, stats = scan(samples, discovery, config)
    assert stats["summaries"]
    occurrences, _ = scan(samples, discovery, config)
    selected = occurrences.loc[occurrences["nonoverlapping"]]
    for _, rows in selected.groupby(["condition_id", "trading_date"]):
        spacing = rows.sort_values("anchor_at")["anchor_at"].diff().dropna().dt.total_seconds()
        assert spacing.ge(config["horizon_seconds"] + config["quote_max_age_seconds"]).all()


def test_feature_value_tampering_fails_independent_arithmetic(evidence):
    samples = evidence[1].copy(deep=True)
    index = samples.loc[samples["CE_iv_decimal"].notna()].index[0]
    samples.at[index, "CE_iv_decimal"] = 0.999
    report = inspect(evidence, samples=samples)
    assert report["status"] == "FAIL"
    assert not next(row["passed"] for row in report["checks"]
                    if row["check"] == "independent_source_feature_arithmetic")


def test_late_real_derived_availability_fails(evidence):
    raw = evidence[0].copy(deep=True)
    samples = evidence[1]
    row = samples.loc[samples["CE_iv_decimal"].notna()].iloc[0]
    source_id = json.loads(row["feature_lineage_json"])["CE_iv_decimal"]["observation_ids"][0]
    raw.loc[raw["observation_id"].eq(source_id), "iv_available_at"] = row["start_at"] + pd.Timedelta(seconds=30)
    report = inspect(evidence, raw_frame=raw)
    assert report["status"] == "FAIL"
    assert not next(row["passed"] for row in report["checks"]
                    if row["check"] == "actual_feature_availability_before_condition")


@pytest.mark.parametrize("invent_zero_change", [False, True])
def test_unknown_occurrences_survive_parquet_without_inventing_outcomes(evidence, tmp_path, invent_zero_change):
    raw, _, original, _, _, config = evidence
    # A gap in evaluation must retain selected matches with missing outcomes.
    raw = raw.loc[~(raw["trading_date"].eq("2026-09-18") &
                    raw["timestamps"].dt.minute.eq(20))].copy()
    samples, _ = build_samples(raw, config)
    discovery = discover(samples, original["dataset_id"], config)
    occurrences, statistics = scan(samples, discovery, config)
    unknown = occurrences["label"].eq("UNKNOWN")
    assert unknown.any()
    assert occurrences.loc[unknown, "point_change"].isna().all()
    occurrences.to_parquet(tmp_path / "occurrences.parquet", index=False)
    restored = pd.read_parquet(tmp_path / "occurrences.parquet")
    if invent_zero_change:
        restored.loc[unknown, "point_change"] = 0.0
    report = audit(samples, discovery, restored, statistics, raw, config)
    exact = next(check["passed"] for check in report["checks"]
                 if check["check"] == "exact_occurrence_set_and_values")
    assert exact is (not invent_zero_change)
    assert (report["status"] == "FAIL") is invent_zero_change

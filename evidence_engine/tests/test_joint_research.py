"""Joint CE/PE/IV discovery, chronological evaluation, and adversarial audit."""
from copy import deepcopy
from itertools import product
import json

import numpy as np
import pandas as pd
import pytest

from evidence_engine.config import ResearchConfig
from evidence_engine.data import build_samples
from evidence_engine.research import (
    FEATURES, JOINT_DISCOVERY, LEGACY_DISCOVERY, audit, discover, evaluate, rule_hash, scan,
)


def interaction_samples():
    """Parity creates a real four-way interaction with zero marginal lift."""
    rows = []
    for day in ("2026-09-17", "2026-09-18"):
        for index, bits in enumerate(list(product((0, 1), repeat=4)) * 4):
            stamp = pd.Timestamp(day + "T09:15:00+05:30") + pd.Timedelta(minutes=index)
            success = sum(bits) % 2 == 0
            rows.append({
                **dict.fromkeys(FEATURES, np.nan),
                "CE_past_midpoint_return_pct": float(bits[0]),
                "PE_past_midpoint_return_pct": float(bits[1]),
                "CE_iv_decimal": 0.1 + 0.2 * bits[2],
                "PE_iv_decimal": 0.1 + 0.2 * bits[3],
                "sample_id": f"{day}-{index}", "underlying": "NIFTY", "trading_date": day,
                "anchor_at": stamp, "start_at": stamp, "condition_at": stamp - pd.Timedelta(1, "ns"),
                "end_at": stamp + pd.Timedelta(minutes=5), "point_change": 120 if success else -120,
                "label": "UP_MOMENTUM" if success else "DOWN_MOMENTUM", "feature_lineage_json": "{}",
            })
    return pd.DataFrame(rows)


def test_joint_discovery_finds_interaction_even_when_all_single_predicates_have_no_lift():
    samples = interaction_samples()
    config = ResearchConfig(min_support=2, max_conditions=6).to_dict()
    legacy = discover(samples, "xor", {**config, "discovery_version": LEGACY_DISCOVERY})
    assert legacy["conditions"] == []
    joint = discover(samples, "xor", config)
    assert joint["conditions"]
    assert 0 < joint["tested_predicates"] == len(joint["search_log"]) <= 288
    assert all(item["base_rate"] == 0.5 for item in joint["search_log"])
    for rule in joint["conditions"]:
        assert rule["combination"] == "ALL"
        assert set(rule).isdisjoint({"feature", "operator", "threshold"})
        assert [term["feature"] for term in rule["predicates"]] == [
            "CE_past_midpoint_return_pct", "PE_past_midpoint_return_pct", "CE_iv_decimal", "PE_iv_decimal",
        ]
        _, matched = evaluate(rule, samples)
        assert samples.loc[matched, "label"].eq(rule["target"]).all()


@pytest.mark.parametrize("missing", ["CE_past_midpoint_return_pct", "PE_past_midpoint_return_pct", "CE_iv_decimal", "PE_iv_decimal"])
def test_missing_any_joint_input_is_unknown_and_excluded_from_feature_baseline(missing):
    samples = interaction_samples()
    config = ResearchConfig(min_support=2, max_conditions=1).to_dict()
    discovery = discover(samples, "xor", config)
    rule = discovery["conditions"][0]
    available, matched = evaluate(rule, samples)
    index = samples.loc[matched & samples["trading_date"].eq("2026-09-18")].index[0]
    samples.at[index, missing] = np.nan
    available, matched = evaluate(rule, samples)
    assert not available.loc[index] and not matched.loc[index]
    occurrences, statistics = scan(samples, discovery, config)
    heldout = next(item for item in statistics["summaries"] if item["partition"] == "evaluation")
    assert heldout["all_anchors"] == 64
    assert heldout["feature_available"] == 63
    assert heldout["feature_unknown"] == 1
    assert heldout["baseline"]["matches"] == 63
    assert samples.at[index, "sample_id"] not in set(occurrences["sample_id"])


def test_holdout_cannot_change_joint_thresholds_search_or_selection():
    config = ResearchConfig(min_support=2).to_dict()
    samples = interaction_samples()
    original = discover(samples, "xor", config)
    heldout = samples["trading_date"].eq("2026-09-18")
    samples.loc[heldout, FEATURES] = 1e9
    samples.loc[heldout, "label"] = "OTHER"
    rebuilt = discover(samples, "xor", config)
    assert original == rebuilt


@pytest.mark.parametrize("missing", ["CE_iv_decimal", "PE_iv_decimal", "CE_past_midpoint_return_pct", "PE_past_midpoint_return_pct"])
def test_no_candidates_when_a_required_input_has_no_training_information(missing):
    samples = interaction_samples()
    samples.loc[samples["trading_date"].eq("2026-09-17"), missing] = np.nan
    discovery = discover(samples, "xor", ResearchConfig().to_dict())
    assert discovery["conditions"] == []
    assert discovery["tested_predicates"] == 0


def test_joint_occurrences_record_every_used_feature_value_and_unknown_outcomes():
    samples = interaction_samples()
    config = ResearchConfig(min_support=2, max_conditions=1).to_dict()
    discovery = discover(samples, "xor", config)
    rule = discovery["conditions"][0]
    _, matched = evaluate(rule, samples)
    index = samples.loc[matched & samples["trading_date"].eq("2026-09-18")].index[0]
    samples.at[index, "label"] = "UNKNOWN"
    samples.at[index, "point_change"] = np.nan
    occurrences, statistics = scan(samples, discovery, config)
    row = occurrences.loc[occurrences["sample_id"].eq(samples.at[index, "sample_id"])].iloc[0]
    assert row["feature"] == "COMBINED" and pd.isna(row["feature_value"])
    assert json.loads(row["feature_values_json"]) == {
        term["feature"]: samples.at[index, term["feature"]] for term in rule["predicates"]
    }
    assert pd.isna(row["success"])
    summary = next(item for item in statistics["summaries"] if item["partition"] == "evaluation")
    assert summary["unknown_outcomes"] == 1
    assert summary["known_outcomes"] == summary["matches"] - 1


@pytest.fixture(scope="module")
def joint_evidence():
    config = ResearchConfig(min_support=2, max_conditions=3).to_dict()
    rows = []
    for day in ("2026-09-17", "2026-09-18"):
        origin = pd.Timestamp(day + "T09:15:00+05:30")
        for second in range(0, 1501, 5):
            stamp = origin + pd.Timedelta(seconds=second)
            for side in ("CE", "PE"):
                midpoint = 200 + (1 if side == "CE" else -1) * min(second, 600) * .05
                rows.append({
                    "observation_id": f"{day}:{second}:{side}", "timestamps": stamp,
                    "underlying": "NIFTY", "trading_date": day,
                    "spot": 25000 + min(second, 600) * .4,
                    "source_version": "2", "optiontype": side, "contract_key": f"fixed-{side}",
                    "bid": midpoint - 1, "ask": midpoint + 1,
                    "oi": 1000 + second, "volume": 100 + second * second,
                    "iv": .15 + second * .00003,
                    "delta": .5 if side == "CE" else -.5, "gamma": .002, "theta": -3., "vega": 2.,
                    "iv_available_at": stamp + pd.Timedelta(seconds=2),
                    "greeks_available_at": stamp + pd.Timedelta(seconds=2),
                })
    raw = pd.DataFrame(rows)
    raw.attrs["dataset_id"] = "source-backed-joint"
    samples, _ = build_samples(raw, config)
    discovery = discover(samples, raw.attrs["dataset_id"], config)
    assert discovery["conditions"]
    occurrences, statistics = scan(samples, discovery, config)
    return dict(raw_frame=raw, samples=samples, discovery=discovery,
                occurrences=occurrences, statistics=statistics, config=config)


def test_source_backed_joint_momentum_passes_independent_audit(joint_evidence, tmp_path):
    args = dict(joint_evidence)
    assert args["samples"]["point_change"].gt(80).any()
    assert args["samples"].loc[args["samples"]["point_change"].gt(80), "label"].eq("UP_MOMENTUM").all()
    args["occurrences"].to_parquet(tmp_path / "joint.parquet", index=False)
    args["occurrences"] = pd.read_parquet(tmp_path / "joint.parquet")
    report = audit(**args)
    assert report["status"] == "PASS", report


@pytest.mark.parametrize("tamper", ["drop_iv", "replace_put_with_call", "any_instead_of_all", "threshold"])
def test_joint_condition_tampering_fails_even_with_recomputed_rule_hash(joint_evidence, tamper):
    args = dict(joint_evidence)
    args["discovery"] = deepcopy(args["discovery"])
    rule = args["discovery"]["conditions"][0]
    if tamper == "drop_iv":
        rule["predicates"].pop()
    elif tamper == "replace_put_with_call":
        rule["predicates"][1]["feature"] = rule["predicates"][0]["feature"]
    elif tamper == "any_instead_of_all":
        rule["combination"] = "ANY"
    else:
        rule["predicates"][0]["threshold"] += 1
    rule["condition_id"] = rule_hash(rule)
    assert audit(**args)["status"] == "FAIL"


@pytest.mark.parametrize("tamper", ["values", "missing_value", "single_feature", "baseline"])
def test_joint_values_and_denominators_are_independently_audited(joint_evidence, tamper):
    args = dict(joint_evidence)
    args["occurrences"] = args["occurrences"].copy(deep=True)
    args["statistics"] = deepcopy(args["statistics"])
    if tamper in ("values", "missing_value"):
        values = json.loads(args["occurrences"].iloc[0]["feature_values_json"])
        if tamper == "values":
            values["PE_iv_decimal"] += .01
        else:
            values.pop("PE_iv_decimal")
        args["occurrences"].loc[0, "feature_values_json"] = json.dumps(values)
    elif tamper == "single_feature":
        args["occurrences"].loc[0, "feature"] = "CE_iv_decimal"
    else:
        args["statistics"]["summaries"][0]["baseline"]["known_outcomes"] += 1
    assert audit(**args)["status"] == "FAIL"


def test_joint_put_iv_cannot_use_future_real_availability(joint_evidence):
    args = dict(joint_evidence)
    args["raw_frame"] = args["raw_frame"].copy(deep=True)
    sample = args["samples"].loc[args["samples"]["PE_iv_decimal"].notna()].iloc[0]
    source_id = json.loads(sample["feature_lineage_json"])["PE_iv_decimal"]["observation_ids"][0]
    args["raw_frame"].loc[args["raw_frame"]["observation_id"].eq(source_id), "iv_available_at"] = sample["start_at"] + pd.Timedelta(seconds=30)
    report = audit(**args)
    assert report["status"] == "FAIL"
    assert not next(item["passed"] for item in report["checks"] if item["check"] == "actual_feature_availability_before_condition")


def test_versions_are_explicit_and_momentum_has_no_upper_cap():
    config = ResearchConfig(min_move=100)
    assert config.detector_version == "spot-endpoint-momentum-v2"
    assert config.discovery_version == JOINT_DISCOVERY
    with pytest.raises(ValueError):
        ResearchConfig(discovery_version="unrecognized")
    with pytest.raises(ValueError):
        ResearchConfig(detector_version="unrecognized")

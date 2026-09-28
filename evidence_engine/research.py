"""Deterministic hypothesis discovery, exhaustive matching, and evidence audit.

Versioned joint rules combine call observations, put observations and both IV
regimes. Legacy single-feature research remains reproducible. These are research
hypotheses, not executable trading instructions.
"""
from __future__ import annotations

import hashlib
import math
from itertools import product

import pandas as pd

from .storage import canonical_json


FEATURES = ["past_spot_change"] + [
    f"{side}_{feature}" for side in ("CE", "PE") for feature in (
        "past_midpoint_return_pct", "past_oi_change_pct", "past_volume_increment",
        "iv_decimal", "spread_pct", "delta", "gamma", "theta", "vega")
]
TARGETS = ("UP_50_80", "DOWN_50_80")
LEGACY_DISCOVERY = "single-feature-training-quantiles-v1"
JOINT_DISCOVERY = "joint-ce-pe-iv-training-v2"
JOINT_FAMILIES = ("past_midpoint_return_pct", "past_oi_change_pct", "past_volume_increment")


def targets(config):
    version = config.get("detector_version", "spot-endpoint-band-v1")
    if version == "spot-endpoint-band-v1":
        return TARGETS
    if version == "spot-endpoint-momentum-v2":
        return ("UP_MOMENTUM", "DOWN_MOMENTUM")
    raise ValueError("Unsupported event detector version")


def is_joint(rule):
    return rule.get("discovery_version") == JOINT_DISCOVERY


def predicates(rule):
    """Validate the bounded versioned predicate language, without evaluating it."""
    if is_joint(rule):
        terms = rule.get("predicates")
        if rule.get("combination") != "ALL" or not isinstance(terms, list) or len(terms) != 4:
            raise ValueError("Joint rules require four ALL predicates")
        if any(not isinstance(term, dict) or set(term) != {"feature", "operator", "threshold"} for term in terms):
            raise ValueError("Invalid joint predicate schema")
        names = [term["feature"] for term in terms]
        if len(set(names)) != 4 or names[2:] != ["CE_iv_decimal", "PE_iv_decimal"]:
            raise ValueError("Joint rules require both call and put IV")
        if not any(names[:2] == ["CE_" + family, "PE_" + family] for family in JOINT_FAMILIES):
            raise ValueError("Joint rules require paired call and put observations")
    else:
        if rule.get("discovery_version", LEGACY_DISCOVERY) != LEGACY_DISCOVERY:
            raise ValueError("Unsupported discovery version")
        terms = [{key: rule[key] for key in ("feature", "operator", "threshold")}]
    for term in terms:
        if term["feature"] not in FEATURES or term["operator"] not in (">=", "<="):
            raise ValueError("Unsupported condition predicate")
        threshold = term["threshold"]
        if isinstance(threshold, bool) or not isinstance(threshold, (int, float)) or not math.isfinite(threshold):
            raise ValueError("Condition threshold must be finite")
    if rule["target"] not in targets(rule) or rule["null_policy"] != "UNKNOWN":
        raise ValueError("Invalid condition target or null policy")
    return terms


def feature_evidence(rule, row):
    if is_joint(rule):
        values = {term["feature"]: float(row[term["feature"]]) for term in rule["predicates"]}
        return {"feature": "COMBINED", "feature_value": None,
                "feature_values_json": canonical_json(values).decode().strip()}
    return {"feature": rule["feature"], "feature_value": float(row[rule["feature"]])}



def rule_hash(rule):
    return hashlib.sha256(canonical_json({k: v for k, v in rule.items() if k != "condition_id"})).hexdigest()


def partition(samples, config):
    """Split whole sessions so five-minute training labels cannot enter holdout."""
    days = sorted(samples["trading_date"].astype(str).unique())
    if len(days) < 2:
        return {"discovery_dates": days, "evaluation_dates": [], "status": "INSUFFICIENT_DATA"}
    if config["discovery_end"]:
        train = [day for day in days if day <= config["discovery_end"]]
    else:
        count = min(len(days) - 1, max(1, int(len(days) * config["discovery_fraction"])))
        train = days[:count]
    test = [day for day in days if day not in train]
    return {"discovery_dates": train, "evaluation_dates": test,
            "status": "READY" if train and test else "INSUFFICIENT_DATA",
            "policy": "chronological_whole_sessions; same-session horizons; overnight boundary embargo"}


def evaluate(rule, samples):
    terms = predicates(rule)
    available = pd.Series(True, index=samples.index)
    matched = pd.Series(True, index=samples.index)
    for term in terms:
        values = pd.to_numeric(samples[term["feature"]], errors="coerce")
        available &= values.notna() & values.abs().lt(float("inf"))
        matched &= values.ge(term["threshold"]) if term["operator"] == ">=" else values.le(term["threshold"])
    return available, (available & matched).fillna(False)


def candidate_predicates(train, config):
    """Enumerate every candidate without any marginal feature ranking.

    Joint bound: 3 paired observation families x 3 quantile levels x 16
    inequality combinations = at most 144 conjunctions (288 target hypotheses).
    Both IV medians and the paired thresholds use only complete discovery rows.
    """
    version = config["discovery_version"]
    if version == LEGACY_DISCOVERY:
        for feature in FEATURES:
            values = pd.to_numeric(train[feature], errors="coerce").dropna()
            values = values.loc[values.abs().lt(float("inf"))]
            if values.nunique() < 2:
                continue
            for threshold in sorted(set(float(values.quantile(q)) for q in (0.25, 0.5, 0.75))):
                for operator in (">=", "<="):
                    yield {"feature": feature, "operator": operator, "threshold": threshold}
        return
    if version != JOINT_DISCOVERY:
        raise ValueError("Unsupported discovery version")
    for family in JOINT_FAMILIES:
        names = ["CE_" + family, "PE_" + family, "CE_iv_decimal", "PE_iv_decimal"]
        values = train[names].apply(pd.to_numeric, errors="coerce")
        values = values.loc[(values.notna() & values.abs().lt(float("inf"))).all(axis=1)]
        # Constant inputs cannot establish a joint observation/IV regime.
        if values.empty or any(values[name].nunique() < 2 for name in names):
            continue
        threshold_sets = sorted({(float(values[names[0]].quantile(q)), float(values[names[1]].quantile(q)),
                                  float(values[names[2]].median()), float(values[names[3]].median()))
                                 for q in (0.25, 0.5, 0.75)})
        for thresholds in threshold_sets:
            for operators in product((">=", "<="), repeat=4):
                yield {"combination": "ALL", "predicates": [
                    {"feature": name, "operator": operator, "threshold": threshold}
                    for name, operator, threshold in zip(names, operators, thresholds)]}


def discover(samples, dataset_id, config):
    split = partition(samples, config)
    train = samples.loc[samples["trading_date"].astype(str).isin(split["discovery_dates"])]
    train = train.loc[train["label"].ne("UNKNOWN")]
    attempts, candidates = [], []
    if split["status"] == "READY":
        for expression in candidate_predicates(train, config):
            for target in targets(config):
                rule = {"dataset_id": dataset_id, **expression, "target": target, "null_policy": "UNKNOWN",
                        "lookback_seconds": config["lookback_seconds"], "research_config": dict(config),
                        "discovery_dates": split["discovery_dates"], "detector_version": config["detector_version"],
                        "discovery_version": config["discovery_version"]}
                rule["condition_id"] = rule_hash(rule)
                available, matched = evaluate(rule, train)
                n = int(matched.sum())
                successes = int((matched & train["label"].eq(target)).sum())
                baseline = float(train.loc[available, "label"].eq(target).mean()) if available.any() else None
                rate = successes / n if n else None
                lift = rate / baseline if baseline is not None and baseline > 0 and rate is not None else None
                accepted = n >= config["min_support"] and successes > 0 and lift is not None and lift > 1
                attempts.append({"condition_id": rule["condition_id"], **expression, "target": target,
                                 "support": n, "successes": successes, "base_rate": baseline,
                                 "rate": rate, "lift": lift, "eligible_for_selection": accepted})
                if accepted:
                    candidates.append((lift, successes, n, rule))
    candidates.sort(key=lambda v: (-v[0], -v[1], -v[2], v[3]["condition_id"]))
    selected = [candidate[3] for candidate in candidates[:config["max_conditions"]]]
    discovery_matches = []
    for rule in selected:
        _, matched = evaluate(rule, train)
        positives = train.loc[matched & train["label"].eq(rule["target"])]
        for row in positives.to_dict("records"):
            discovery_matches.append({"condition_id": rule["condition_id"], "sample_id": row["sample_id"],
                                      "condition_at": str(row["condition_at"]), "event_start_at": str(row["start_at"]),
                                      **feature_evidence(rule, row), "feature_lineage_json": row["feature_lineage_json"]})
    mode = "joint CE/PE observations and both IV regimes" if config["discovery_version"] == JOINT_DISCOVERY else "single-feature"
    return {"dataset_id": dataset_id, "partition": split, "conditions": selected,
            "search_log": attempts, "tested_predicates": len(attempts), "discovery_matches": discovery_matches,
            "status": "CANDIDATES_FROZEN" if selected else "INSUFFICIENT_DATA",
            "interpretation": f"Exploratory {mode} hypotheses selected on discovery sessions only; no significance claim."}


def _stats(frame, target):
    known = frame["label"].ne("UNKNOWN")
    n = int(known.sum())
    successes = int((known & frame["label"].eq(target)).sum())
    return {"matches": len(frame), "successes": successes, "failures": n - successes,
            "unknown_outcomes": int((~known).sum()), "known_outcomes": n,
            "event_rate": successes / n if n else None}


def scan(samples, discovery, config):
    """Record every match, whether positive, negative, or not yet labelable."""
    rows, summaries = [], []
    train_days = discovery["partition"]["discovery_dates"]
    for rule in discovery["conditions"]:
        if rule_hash(rule) != rule["condition_id"]:
            raise ValueError("Frozen condition was modified")
        available, matched = evaluate(rule, samples)
        hits = samples.loc[matched].sort_values(["underlying", "trading_date", "anchor_at"])
        last_end = {}
        independent_ids = []
        for row in hits.to_dict("records"):
            key = (row["underlying"], str(row["trading_date"]))
            # Choose spacing from clock anchors before inspecting outcome validity.
            start = pd.Timestamp(row["anchor_at"])
            independent = key not in last_end or start >= last_end[key]
            if independent:
                # The next actual start can precede its anchor by max quote age.
                # Reserve that uncertainty without looking at future outcomes.
                last_end[key] = start + pd.Timedelta(seconds=config["horizon_seconds"] + config["quote_max_age_seconds"])
                independent_ids.append(row["sample_id"])
            rows.append({"condition_id": rule["condition_id"], "sample_id": row["sample_id"],
                         "partition": "discovery" if str(row["trading_date"]) in train_days else "evaluation",
                         "trading_date": str(row["trading_date"]), "condition_at": row["condition_at"],
                         "anchor_at": row["anchor_at"], "start_at": row["start_at"], "end_at": row["end_at"],
                         **feature_evidence(rule, row),
                         "label": row["label"], "point_change": row["point_change"],
                         "target": rule["target"], "success": None if row["label"] == "UNKNOWN" else row["label"] == rule["target"],
                         "nonoverlapping": independent, "feature_lineage_json": row["feature_lineage_json"]})
        for name, days in (("discovery", train_days), ("evaluation", discovery["partition"]["evaluation_dates"])):
            day_mask = samples["trading_date"].astype(str).isin(days)
            selected = hits.loc[hits["trading_date"].astype(str).isin(days)]
            baseline = samples.loc[available & day_mask]
            summaries.append({"condition_id": rule["condition_id"], "partition": name,
                              "all_anchors": int(day_mask.sum()), "feature_available": int((available & day_mask).sum()),
                              "feature_unknown": int((~available & day_mask).sum()),
                              "nonmatches": int((available & ~matched & day_mask).sum()),
                              **_stats(selected, rule["target"]),
                              "nonoverlapping": _stats(selected.loc[selected["sample_id"].isin(independent_ids)], rule["target"]),
                              "baseline": _stats(baseline, rule["target"])})
    columns = ["condition_id", "sample_id", "partition", "trading_date", "condition_at", "anchor_at", "start_at",
               "end_at", "feature", "feature_value", "label", "point_change", "target", "success",
               "nonoverlapping", "feature_lineage_json"]
    if config["discovery_version"] == JOINT_DISCOVERY:
        columns.append("feature_values_json")
    return pd.DataFrame(rows, columns=columns), {"summaries": summaries,
        "interpretation": "All matches retained. Nonoverlapping counts reduce repeated windows; sessions may still be dependent."}


def audit(samples, discovery, occurrences, statistics, raw_frame, config, run_metadata=None):
    """Fail-closed source checks plus explicitly identified producer replays.

    Source feature arithmetic, availability, contract continuity, event arithmetic,
    predicates, spacing and statistics are calculated independently here. Sampler
    replay additionally checks completeness and exact as-of source selection.
    """
    from bisect import bisect_right
    import json
    from .data import SAMPLE_COLUMNS, build_samples

    findings = []

    def check(name, passed, detail=""):
        findings.append({"check": name, "passed": bool(passed), "detail": detail})

    def result(enough=False):
        failed = any(not item["passed"] for item in findings)
        return {"status": "FAIL" if failed else "PASS" if enough else "INSUFFICIENT_DATA",
                "checks": findings,
                "evidence_status": "EXPLORATORY" if enough and not failed else "INSUFFICIENT_DATA",
                "interpretation": "PASS certifies implemented integrity checks, not predictive power or statistical significance.",
                "limitations": ["Receipt timestamps do not verify provider event time.",
                                "Sampler/discovery replay shares producer code; source arithmetic and availability checks are independent.",
                                "The versioned hypothesis search tests multiple candidates; no significance inference is made.",
                                "Nonoverlapping outcomes may still share features and session-level dependence."]}

    def equivalent(actual, expected):
        """Type-aware recursive comparison; booleans cannot masquerade as counts."""
        if isinstance(expected, dict):
            return isinstance(actual, dict) and set(actual) == set(expected) and all(
                equivalent(actual[key], value) for key, value in expected.items())
        if isinstance(expected, list):
            return isinstance(actual, list) and len(actual) == len(expected) and all(
                equivalent(a, b) for a, b in zip(actual, expected))
        if expected is None:
            return actual is None or actual is pd.NA or (isinstance(actual, float) and math.isnan(actual))
        if isinstance(expected, bool):
            return isinstance(actual, (bool, __import__("numpy").bool_)) and bool(actual) == expected
        if isinstance(expected, pd.Timestamp):
            return pd.notna(actual) and pd.Timestamp(actual) == expected
        # UNKNOWN outcomes intentionally retain NaN point changes. Compare
        # missing scalars before numeric tolerance; isclose(NaN, NaN) is false.
        if pd.isna(expected):
            return bool(pd.isna(actual))
        if isinstance(expected, (int, float)):
            if isinstance(actual, (bool, __import__("numpy").bool_)):
                return False
            try:
                return math.isclose(float(actual), float(expected), rel_tol=1e-12, abs_tol=1e-12)
            except (TypeError, ValueError):
                return False
        return actual == expected

    occurrence_columns = {"condition_id", "sample_id", "partition", "trading_date", "condition_at",
                          "anchor_at", "start_at", "end_at", "feature", "feature_value", "label",
                          "point_change", "target", "success", "nonoverlapping", "feature_lineage_json"}
    if config["discovery_version"] == JOINT_DISCOVERY:
        occurrence_columns.add("feature_values_json")
    schema_ok = (set(samples.columns) == set(SAMPLE_COLUMNS) and
                 set(occurrences.columns) == occurrence_columns and
                 {"dataset_id", "partition", "conditions"}.issubset(discovery) and
                 isinstance(statistics.get("summaries"), list))
    check("audit_input_schema", schema_ok)
    if not schema_ok:
        return result()
    if run_metadata is not None:
        metadata_ok = (set(run_metadata) == {"run_id", "executed_at"} and all(
            isinstance(value, str) and value and
            discovery.get(key) == value and statistics.get(key) == value
            for key, value in run_metadata.items()))
        check("artifacts_bound_to_run", metadata_ok)
        if not metadata_ok:
            return result()
    unique_ok = (samples["sample_id"].notna().all() and samples["sample_id"].is_unique and
                 raw_frame["observation_id"].notna().all() and
                 raw_frame["observation_id"].astype(str).is_unique)
    check("unique_source_and_sample_ids", unique_ok)
    if not unique_ok:
        return result()
    try:
        expected_partition = partition(samples, config)
        check("chronological_exhaustive_sessions", equivalent(discovery["partition"], expected_partition))
        train = expected_partition["discovery_dates"]
        holdout = expected_partition["evaluation_dates"]
        conditions = discovery["conditions"]
        rule_ids = [rule["condition_id"] for rule in conditions]
        rule_keys = {"dataset_id", "feature", "operator", "threshold", "target", "null_policy",
                     "lookback_seconds", "research_config", "discovery_dates", "detector_version",
                     "discovery_version", "condition_id"}
        if config["discovery_version"] == JOINT_DISCOVERY:
            rule_keys = (rule_keys - {"feature", "operator", "threshold"}) | {"combination", "predicates"}
        rules_ok = len(rule_ids) == len(set(rule_ids))
        for rule in conditions:
            evaluate(rule, samples)  # Validate the restricted predicate schema.
            rules_ok &= (set(rule) == rule_keys and rule_hash(rule) == rule["condition_id"] and
                         rule["dataset_id"] == discovery["dataset_id"] and
                         rule["discovery_dates"] == train and rule["research_config"] == config and
                         rule["lookback_seconds"] == config["lookback_seconds"] and
                         rule["detector_version"] == config["detector_version"] and
                         rule["discovery_version"] == config["discovery_version"])
        check("conditions_frozen_and_bound_to_config", rules_ok)
        expected_discovery = discover(samples, discovery["dataset_id"], config)
        if run_metadata is not None:
            expected_discovery.update(run_metadata)
        check("discovery_replay_consistency", equivalent(discovery, expected_discovery))
    except (KeyError, TypeError, ValueError, OverflowError) as exc:
        check("condition_and_partition_validation", False, type(exc).__name__)
        return result()

    # Rebuild the exact candidate population, including unknown outcomes and null
    # features. This detects dropped rows and altered as-of source selection.
    try:
        replay_raw = raw_frame.copy()
        replay_raw.attrs["dataset_id"] = discovery["dataset_id"]
        replay, _ = build_samples(replay_raw, config)
        pd.testing.assert_frame_equal(
            samples.sort_values("sample_id").reset_index(drop=True)[SAMPLE_COLUMNS],
            replay.sort_values("sample_id").reset_index(drop=True)[SAMPLE_COLUMNS],
            check_dtype=False, check_exact=False, rtol=1e-12, atol=1e-12)
        check("sampler_replay_consistency", True)
    except (AssertionError, KeyError, TypeError, ValueError, OverflowError) as exc:
        check("sampler_replay_consistency", False, type(exc).__name__)

    # Resolve lineage against source rows, never against declared availability.
    try:
        source = raw_frame.copy()
        source["timestamps"] = pd.to_datetime(source["timestamps"], utc=True)
        source["observation_id"] = source["observation_id"].astype(str)
        source_rows = source.to_dict("records")
        source_by_id = {row["observation_id"]: row for row in source_rows}
        spot_rows, sides = {}, {}
        for row in source_rows:
            stamp = row["timestamps"]
            local = stamp.tz_convert("Asia/Kolkata")
            if not ("09:15:00" <= local.strftime("%H:%M:%S") <= "15:30:00"):
                continue
            key = (str(row["underlying"]), str(row["trading_date"]))
            spot_rows.setdefault(key, {}).setdefault(stamp, []).append(row)
            sides.setdefault((*key, row["optiontype"]), []).append(row)
        spot_segments, side_segments, side_times = {}, {}, {}
        for key, groups in spot_rows.items():
            prior, segment = None, 0
            for stamp in sorted(groups):
                if prior is None or (stamp - prior).total_seconds() > config["max_gap_seconds"]:
                    segment += 1
                spot_segments[(*key, stamp)] = segment
                prior = stamp
        for key, rows in sides.items():
            rows.sort(key=lambda row: row["timestamps"])
            prior, segment = None, 0
            for row in rows:
                if (prior is None or row["contract_key"] != prior["contract_key"] or
                        (row["timestamps"] - prior["timestamps"]).total_seconds() > config["max_gap_seconds"]):
                    segment += 1
                side_segments[row["observation_id"]] = segment
                prior = row
            side_times[key] = [row["timestamps"] for row in rows]
        timing_ok = lineage_ok = feature_values_ok = endpoint_ok = True
        for row in samples.to_dict("records"):
            condition, start, anchor = (pd.Timestamp(row[name]) for name in ("condition_at", "start_at", "anchor_at"))
            key = (str(row["underlying"]), str(row["trading_date"]))
            timing_ok &= condition < start and 0 <= (anchor - start).total_seconds() <= config["quote_max_age_seconds"]
            lineage = json.loads(row["feature_lineage_json"])
            finite_features = {name for name in FEATURES if pd.notna(row[name]) and math.isfinite(float(row[name]))}
            lineage_ok &= set(lineage) == finite_features
            for name in finite_features:
                item = lineage.get(name, {})
                ids = item.get("observation_ids", [])
                valid_ids = (bool(ids) and len(ids) == len(set(ids)) and all(i in source_by_id for i in ids))
                lineage_ok &= valid_ids
                if not valid_ids:
                    continue
                refs = sorted([source_by_id[i] for i in ids], key=lambda ref: ref["timestamps"])
                dates = {str(ref["trading_date"]) for ref in refs}
                lineage_ok &= dates == {key[1]} and {str(ref["underlying"]) for ref in refs} == {key[0]}
                stamps = sorted({ref["timestamps"] for ref in refs})
                actual_available = max(stamps)
                reconstructed = float("nan")
                if name == "past_spot_change":
                    lineage_ok &= len(stamps) == 2
                    if len(stamps) == 2:
                        old, new = stamps
                        timing_ok &= (0 <= (condition - new).total_seconds() <= config["quote_max_age_seconds"] and
                                      0 <= (condition - pd.Timedelta(seconds=config["lookback_seconds"]) - old).total_seconds() <= config["quote_max_age_seconds"])
                        lineage_ok &= spot_segments.get((*key, old)) == spot_segments.get((*key, new))
                        old_values = {float(ref["spot"]) for ref in refs if ref["timestamps"] == old}
                        new_values = {float(ref["spot"]) for ref in refs if ref["timestamps"] == new}
                        lineage_ok &= len(old_values) == len(new_values) == 1
                        if len(old_values) == len(new_values) == 1:
                            reconstructed = next(iter(new_values)) - next(iter(old_values))
                else:
                    side, metric = name.split("_", 1)
                    side_key = (*key, side)
                    lineage_ok &= {ref["optiontype"] for ref in refs} == {side}
                    lineage_ok &= len({ref["contract_key"] for ref in refs}) == 1
                    index = bisect_right(side_times.get(side_key, []), condition) - 1
                    if index < 0:
                        lineage_ok = False
                        continue
                    current = sides[side_key][index]
                    timing_ok &= 0 <= (condition - current["timestamps"]).total_seconds() <= config["quote_max_age_seconds"]
                    lineage_ok &= all(side_segments.get(ref["observation_id"]) == side_segments.get(current["observation_id"])
                                      for ref in refs)
                    if metric.startswith("past_"):
                        lineage_ok &= len(refs) == 2 and len(stamps) == 2
                        if len(refs) == 2 and len(stamps) == 2:
                            old, new = refs
                            timing_ok &= (0 <= (condition - new["timestamps"]).total_seconds() <= config["quote_max_age_seconds"] and
                                          0 <= (condition - pd.Timedelta(seconds=config["lookback_seconds"]) - old["timestamps"]).total_seconds() <= config["quote_max_age_seconds"])
                            if metric == "past_midpoint_return_pct":
                                a, b = (float(old["bid"]) + float(old["ask"])) / 2, (float(new["bid"]) + float(new["ask"])) / 2
                                lineage_ok &= all(float(ref["ask"]) >= float(ref["bid"]) >= 0 for ref in refs)
                                reconstructed = 100 * (b / a - 1) if a > 0 and b > 0 else float("nan")
                            elif metric == "past_oi_change_pct":
                                a, b = float(old["oi"]), float(new["oi"])
                                reconstructed = 100 * (b / a - 1) if a > 0 else float("nan")
                            elif metric == "past_volume_increment":
                                reconstructed = float(new["volume"] - old["volume"]) if new["volume"] >= old["volume"] else float("nan")
                    elif len(refs) == 1:
                        ref = refs[0]
                        if metric == "spread_pct":
                            bid, ask = float(ref["bid"]), float(ref["ask"])
                            mid = (bid + ask) / 2
                            reconstructed = 100 * (ask - bid) / mid if mid > 0 and ask >= bid >= 0 else float("nan")
                            timing_ok &= 0 <= (condition - ref["timestamps"]).total_seconds() <= config["quote_max_age_seconds"]
                        else:
                            availability = "iv_available_at" if metric == "iv_decimal" else "greeks_available_at"
                            actual_available = pd.Timestamp(ref.get(availability))
                            timing_ok &= (pd.notna(actual_available) and actual_available >= ref["timestamps"] and
                                          0 <= (condition - actual_available).total_seconds() <= 10 and
                                          0 <= (condition - ref["timestamps"]).total_seconds() <= 15)
                            reconstructed = float(ref["iv" if metric == "iv_decimal" else metric])
                    else:
                        lineage_ok = False
                timing_ok &= all(stamp <= condition for stamp in stamps)
                timing_ok &= pd.notna(actual_available) and actual_available <= condition
                lineage_ok &= pd.Timestamp(item.get("max_available_at")) == actual_available
                lineage_ok &= item.get("source_timestamps") == sorted(stamp.isoformat() for stamp in stamps)
                expected_contracts = [] if name == "past_spot_change" else sorted({str(ref["contract_key"]) for ref in refs})
                lineage_ok &= item.get("contract_keys") == expected_contracts
                feature_values_ok &= math.isfinite(reconstructed) and equivalent(row[name], reconstructed)
            # Independent source arithmetic, receipt freshness and interior continuity.
            if row["label"] != "UNKNOWN":
                end = pd.Timestamp(row["end_at"])
                groups = spot_rows.get(key, {})
                begin, finish = groups.get(start, []), groups.get(end, [])
                endpoint_ok &= bool(begin) and bool(finish)
                if begin and finish:
                    s_values, e_values = {float(ref["spot"]) for ref in begin}, {float(ref["spot"]) for ref in finish}
                    endpoint_ok &= len(s_values) == len(e_values) == 1
                    change = next(iter(e_values)) - next(iter(s_values))
                    if config["detector_version"] == "spot-endpoint-momentum-v2":
                        expected_label = ("UP_MOMENTUM" if change >= config["min_move"] else
                                          "DOWN_MOMENTUM" if change <= -config["min_move"] else "OTHER")
                    else:
                        expected_label = ("UP_50_80" if 50 <= change <= 80 else "DOWN_50_80" if -80 <= change <= -50 else
                                          "UP_OVER_80" if change > 80 else "DOWN_OVER_80" if change < -80 else "OTHER")
                    endpoint_ok &= equivalent(row["point_change"], change) and row["label"] == expected_label
                    endpoint_ok &= (json.loads(row["start_observation_ids"]) == sorted(ref["observation_id"] for ref in begin) and
                                    json.loads(row["end_observation_ids"]) == sorted(ref["observation_id"] for ref in finish))
                    endpoint_ok &= spot_segments.get((*key, start)) == spot_segments.get((*key, end))
                    endpoint_ok &= (0 <= (anchor + pd.Timedelta(seconds=config["horizon_seconds"]) - end).total_seconds() <= config["quote_max_age_seconds"] and
                                    abs((end - start).total_seconds() - config["horizon_seconds"]) <= config["quote_max_age_seconds"])
        check("actual_feature_availability_before_condition", timing_ok)
        check("source_lineage_and_contract_continuity", lineage_ok)
        check("independent_source_feature_arithmetic", feature_values_ok)
        check("independent_source_event_checks", endpoint_ok)
    except (KeyError, TypeError, ValueError, OverflowError, IndexError, ZeroDivisionError) as exc:
        check("independent_source_verification", False, type(exc).__name__)

    # Independently calculate all matches, all occurrence fields, spacing and all
    # denominator/baseline statistics. No empty or partial summary can pass.
    try:
        expected_rows, expected_summaries = [], []

        def summarize(rows, target):
            known = [row for row in rows if row["label"] != "UNKNOWN"]
            successes = sum(row["label"] == target for row in known)
            return {"matches": len(rows), "successes": successes, "failures": len(known) - successes,
                    "unknown_outcomes": len(rows) - len(known), "known_outcomes": len(known),
                    "event_rate": successes / len(known) if known else None}

        records = samples.sort_values(["underlying", "trading_date", "anchor_at"]).to_dict("records")
        for rule in conditions:
            available_rows, matched_rows, chosen_ids, next_anchor = [], [], set(), {}
            for row in records:
                # Independent scalar conjunction evaluation: never call the producer's evaluate().
                terms = rule["predicates"] if config["discovery_version"] == JOINT_DISCOVERY else [rule]
                values = {term["feature"]: row[term["feature"]] for term in terms}
                if any(pd.isna(value) or not math.isfinite(float(value)) for value in values.values()):
                    continue
                available_rows.append(row)
                if not all(values[term["feature"]] >= term["threshold"] if term["operator"] == ">="
                           else values[term["feature"]] <= term["threshold"] for term in terms):
                    continue
                matched_rows.append(row)
                key = (row["underlying"], str(row["trading_date"]))
                anchor = pd.Timestamp(row["anchor_at"])
                chosen = key not in next_anchor or anchor >= next_anchor[key]
                if chosen:
                    next_anchor[key] = anchor + pd.Timedelta(seconds=config["horizon_seconds"] + config["quote_max_age_seconds"])
                    chosen_ids.add(row["sample_id"])
                evidence_values = ({"feature": "COMBINED", "feature_value": None,
                                    "feature_values_json": json.dumps({name: float(value) for name, value in values.items()},
                                        sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)}
                                   if config["discovery_version"] == JOINT_DISCOVERY else
                                   {"feature": rule["feature"], "feature_value": float(values[rule["feature"]])})
                expected_rows.append({"condition_id": rule["condition_id"], "sample_id": row["sample_id"],
                    "partition": "discovery" if str(row["trading_date"]) in train else "evaluation",
                    "trading_date": str(row["trading_date"]), "condition_at": row["condition_at"],
                    "anchor_at": row["anchor_at"], "start_at": row["start_at"], "end_at": row["end_at"],
                    **evidence_values, "label": row["label"],
                    "point_change": row["point_change"], "target": rule["target"],
                    "success": None if row["label"] == "UNKNOWN" else row["label"] == rule["target"],
                    "nonoverlapping": chosen, "feature_lineage_json": row["feature_lineage_json"]})
            for label, days in (("discovery", train), ("evaluation", holdout)):
                all_rows = [row for row in records if str(row["trading_date"]) in days]
                present = [row for row in available_rows if str(row["trading_date"]) in days]
                matched = [row for row in matched_rows if str(row["trading_date"]) in days]
                expected_summaries.append({"condition_id": rule["condition_id"], "partition": label,
                    "all_anchors": len(all_rows), "feature_available": len(present),
                    "feature_unknown": len(all_rows) - len(present), "nonmatches": len(present) - len(matched),
                    **summarize(matched, rule["target"]),
                    "nonoverlapping": summarize([row for row in matched if row["sample_id"] in chosen_ids], rule["target"]),
                    "baseline": summarize(present, rule["target"])})
        row_key = lambda row: (row["condition_id"], row["sample_id"])
        summary_key = lambda row: (row["condition_id"], row["partition"])
        check("exact_occurrence_set_and_values", equivalent(
            sorted(occurrences.to_dict("records"), key=row_key), sorted(expected_rows, key=row_key)))
        check("exact_summary_set_and_denominators", equivalent(
            sorted(statistics["summaries"], key=summary_key), sorted(expected_summaries, key=summary_key)))
        enough = bool(conditions and holdout and any(
            row["partition"] == "evaluation" and row["label"] != "UNKNOWN" for row in expected_rows))
    except (KeyError, TypeError, ValueError, OverflowError) as exc:
        check("occurrence_and_statistics_verification", False, type(exc).__name__)
        enough = False
    return result(enough)

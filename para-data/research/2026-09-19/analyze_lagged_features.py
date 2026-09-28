"""Exploratory receipt-time associations; this does not fit a trading model."""
from pathlib import Path
import hashlib
import json

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[3]
OUT = Path(__file__).resolve().parent
REGISTRY = ROOT / "data_cleaning_agent/runtime/enrichment_verification_2026-09-19.json"
FEATURES = [
    "past_spot_return_pct", "past_midpoint_return_pct", "past_oi_change_pct", "past_volume_increment",
    "iv_decimal", "past_iv_change_pp", "delta", "gamma",
    "theta_daily_pct_midpoint", "vega_pct_midpoint_per_iv_pp",
    "spread_pct", "spot_moneyness_pct", "days_to_expiry",
]


def number(value):
    return float(value) if pd.notna(value) else np.nan


def pct_change(new, old):
    new, old = number(new), number(old)
    return 100 * (new / old - 1) if old > 0 else np.nan


def load_data():
    registry = json.loads(REGISTRY.read_text())
    frames, verified = [], []
    for entry in registry["verified"]:
        tables = {}
        for stem in ("observations", "options"):
            filename = stem + ".parquet"
            path = Path(entry["run_dir"]) / filename
            expected = entry["artifact_checks"][filename]
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            assert digest == expected["sha256"]
            assert path.stat().st_size == expected["size_bytes"]
            verified.append({"path": str(path), "sha256": digest})
            tables[stem] = pd.read_parquet(path)
        obs, opt = tables["observations"], tables["options"]
        frame = obs[["observation_id", "timestamps", "trading_date", "spot", "iv",
                     "iv_available_at", "source_version", "volume"]].merge(
            opt[["observation_id", "contract_key", "optiontype", "bid", "ask", "oi",
                 "strike", "expirydate", "delta", "gamma", "theta", "vega",
                 "greeks_available_at"]], on="observation_id", validate="one_to_one")
        assert len(frame) == len(obs) == len(opt) == entry["rows_per_table"]
        frames.append(frame)
    all_data = pd.concat(frames, ignore_index=True)
    assert all_data["observation_id"].is_unique
    # Legacy timestamps have no matching v2 receipt provenance. Keep the lagged
    # exercise within v2; the independent contemporaneous exercise includes all days.
    data = all_data.loc[all_data["source_version"].eq("2")].copy()
    data["midpoint"] = (data["bid"] + data["ask"]) / 2
    assert data["midpoint"].gt(0).all() and data["ask"].ge(data["bid"]).all()
    return registry, all_data, data, verified


def sample(data):
    records, filter_counts = [], []
    for (day, side), quotes in data.groupby(["trading_date", "optiontype"], sort=True):
        quotes = quotes.sort_values("timestamps").reset_index(drop=True)
        ns = quotes["timestamps"].astype("int64").to_numpy()
        gaps = quotes["timestamps"].diff().dt.total_seconds()
        quotes["segment"] = (gaps.gt(10) | quotes["contract_key"].ne(
            quotes["contract_key"].shift())).cumsum()
        calculations = {}
        for segment, q in quotes.groupby("segment"):
            valid = q.loc[q["iv_available_at"].notna() & q["greeks_available_at"].notna()].copy()
            valid["available_at"] = valid[["iv_available_at", "greeks_available_at"]].max(axis=1)
            valid = valid.sort_values(["available_at", "timestamps"])
            assert valid["available_at"].ge(valid["timestamps"]).all()
            calculations[segment] = (valid, valid["available_at"].astype("int64").to_numpy())

        def calculation_asof(row):
            valid, available = calculations[row["segment"]]
            i = int(np.searchsorted(available, row["timestamps"].value, side="right") - 1)
            if i < 0:
                return None
            result = valid.iloc[i]
            if (row["timestamps"] - result["available_at"]).total_seconds() > 10:
                return None
            # Bound the source quote age too, even if a late calculation just arrived.
            if (row["timestamps"] - result["timestamps"]).total_seconds() > 15:
                return None
            assert result["contract_key"] == row["contract_key"]
            assert result["available_at"] <= row["timestamps"]
            return result

        grid = pd.date_range(quotes["timestamps"].min().ceil("min"),
                             quotes["timestamps"].max().floor("min"), freq="min")
        counts = {"date": day, "optiontype": side, "clock_candidates": len(grid),
                  "missing_or_old_quote": 0, "segment_change": 0, "selected": 0}
        last_future = None
        for clock in grid:
            clocks = [clock - pd.Timedelta(minutes=1), clock, clock + pd.Timedelta(minutes=1)]
            idx = [int(np.searchsorted(ns, c.value, side="right") - 1) for c in clocks]
            if any(i < 0 or not 0 <= (c.value - ns[i]) / 1e9 <= 5 for c, i in zip(clocks, idx)):
                counts["missing_or_old_quote"] += 1
                continue
            past, now, future = [quotes.iloc[i] for i in idx]
            if not past["segment"] == now["segment"] == future["segment"]:
                counts["segment_change"] += 1
                continue
            assert last_future is None or now["timestamps"] >= last_future
            last_future = future["timestamps"]
            seconds = (future["timestamps"] - now["timestamps"]).total_seconds()
            assert 55 <= seconds <= 65
            current_calc, past_calc = calculation_asof(now), calculation_asof(past)

            def greek(name):
                return number(current_calc[name]) if current_calc is not None else np.nan

            iv_change = (100 * (number(current_calc["iv"]) - number(past_calc["iv"]))
                         if current_calc is not None and past_calc is not None else np.nan)
            records.append({
                "date": day, "optiontype": side, "contract_key": now["contract_key"],
                "clock": clock.isoformat(), "past_at": past["timestamps"].isoformat(),
                "decision_receipt_at": now["timestamps"].isoformat(),
                "future_at": future["timestamps"].isoformat(), "future_seconds": seconds,
                "past_id": past["observation_id"], "current_id": now["observation_id"],
                "future_id": future["observation_id"],
                "calculation_id": current_calc["observation_id"] if current_calc is not None else None,
                "calculation_available_at": current_calc["available_at"].isoformat() if current_calc is not None else None,
                "past_calculation_available_at": past_calc["available_at"].isoformat() if past_calc is not None else None,
                "past_spot_return_pct": pct_change(now["spot"], past["spot"]),
                "past_midpoint_return_pct": pct_change(now["midpoint"], past["midpoint"]),
                "past_oi_change_pct": pct_change(now["oi"], past["oi"]),
                "iv_decimal": greek("iv"), "past_iv_change_pp": iv_change,
                "delta": greek("delta"), "gamma": greek("gamma"),
                "theta_daily_pct_midpoint": 100 * greek("theta") / number(now["midpoint"]),
                "vega_pct_midpoint_per_iv_pp": 100 * greek("vega") / number(now["midpoint"]),
                "spread_pct": 100 * number(now["ask"] - now["bid"]) / number(now["midpoint"]),
                "spot_moneyness_pct": 100 * (number(now["spot"]) / number(now["strike"]) - 1),
                "days_to_expiry": (now["expirydate"] - now["timestamps"]).total_seconds() / 86400,
                "past_volume_increment": number(now["volume"] - past["volume"])
                    if now["volume"] >= past["volume"] else np.nan,
                "volume_counter_decreased": bool(now["volume"] < past["volume"]),
                "future_midpoint_return_pct": pct_change(future["midpoint"], now["midpoint"]),
            })
            counts["selected"] += 1
        filter_counts.append(counts)
    return pd.DataFrame(records), filter_counts


def correlations(samples):
    rows = []
    for fields in (["date", "optiontype"], ["optiontype"]):
        for key, frame in samples.groupby(fields):
            labels = dict(zip(fields, key if isinstance(key, tuple) else (key,)))
            for feature in FEATURES:
                pair = frame[[feature, "future_midpoint_return_pct"]].dropna()
                varying = len(pair) >= 3 and pair.nunique().min() > 1
                rows.append({"date": labels.get("date", "POOLED"), "optiontype": labels["optiontype"],
                    "feature": feature, "n": len(pair),
                    "pearson": float(pair.corr().iloc[0, 1]) if varying else np.nan,
                    "spearman": float(pair.rank().corr().iloc[0, 1]) if varying else np.nan})
    return pd.DataFrame(rows)


def main():
    registry, all_data, data, hashes = load_data()
    samples, filters = sample(data)
    stats = correlations(samples)
    samples.to_csv(OUT / "lagged_one_minute_samples.csv", index=False)
    stats.to_csv(OUT / "lagged_feature_correlations.csv", index=False)
    report = {
        "source_registry": str(REGISTRY), "s3_verification_at_ist": registry["checked_at_ist"],
        "all_rows": len(all_data), "version_2_rows": len(data), "verified_parquet_files": hashes,
        "dates": sorted(samples["date"].unique().tolist()),
        "sample_rows": len(samples), "counts_by_side": samples.groupby("optiontype").size().to_dict(),
        "counts_by_day_side": samples.groupby(["date", "optiontype"]).size().rename("n").reset_index().to_dict("records"),
        "complete_feature_rows": int(samples[FEATURES].notna().all(axis=1).sum()),
        "reported_volume_decreases": int(samples["volume_counter_decreased"].sum()),
        "filter_counts": filters,
        "method": [
            "One selected committed revision per day; both Parquet hashes and sizes rechecked locally.",
            "One-to-one observation_id join; lagged exercise uses v2 application receipt provenance only.",
            "Exact minute clock; past, present and future use last raw quote at or before clock-60s, clock and clock+60s, each no older than five seconds.",
            "All three quotes must be in one continuous same-contract segment; break on date, contract switch or successive receipt gap above ten seconds.",
            "Model values selected as of the present or past raw quote receipt, using max(iv_available_at, greeks_available_at), maximum calculation receipt age ten seconds and source quote age fifteen seconds. No backward filling.",
            "Future target is percentage bid/ask midpoint change over about one minute; future intervals do not overlap within day and side.",
            "Volume increment is the nonnegative difference of the provider total-volume counter within the same day and contract, retaining source units; a decrease makes the feature unavailable.",
            "Pairwise complete Pearson and Spearman correlations separately for calls and puts, both pooled and by day; no model fitting, hypothesis tests, holdout evaluation or claims of prediction.",
        ],
        "limitations": [
            "Only seven v2 trading days, some incomplete; ATM-only data and segment filters select surviving contracts.",
            "Requiring a future quote in the same segment conditions sample inclusion on future availability. This is a descriptive usable-window filter, not an implementable live eligibility rule.",
            "Application receipts do not prove provider event freshness, simultaneity of market inputs, database persistence time or executable fills.",
            "Consecutive intervals remain dependent and CE/PE share underlying data. These sample counts are not independent experiments.",
            "Many features examined on the same small dataset; these correlations are exploratory and cannot select a proven signal.",
            "Pooled feature-level associations may reflect differences between days. Ratio features sharing the current midpoint with the target can also have mechanical associations.",
            "IV and Greeks are model estimates from prices; values use stored Black-76 with unverified spot/forward basis and zero-rate assumption.",
            "No net-of-spread, brokerage or slippage performance test. Model-feature subsets have missing rows.",
            "Volume is a source counter, not an independent event feed; differences do not identify buyer/seller initiation or volume units beyond the provider definition.",
        ],
    }
    (OUT / "lagged_relationship_report.json").write_text(json.dumps(report, indent=2) + "\n")
    print(stats.loc[stats["date"].eq("POOLED")].to_string(index=False))
    print(json.dumps({k: report[k] for k in ("all_rows", "version_2_rows", "dates", "counts_by_side", "complete_feature_rows", "reported_volume_decreases")}, indent=2))


if __name__ == "__main__":
    main()

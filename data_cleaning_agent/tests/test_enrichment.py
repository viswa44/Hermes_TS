"""Analytics linkage, time availability, ID stability and readable exports."""

import json

import pandas as pd
import pytest

from data_cleaning_agent.agent.cleaning_agent import DataCleaningAgent
from data_cleaning_agent.config.settings import Settings
from data_cleaning_agent.models.cleaning_plan import CleaningPlan
from data_cleaning_agent.tools.cleaner import clean_data
from data_cleaning_agent.tools.enrichment import enrich_tables
from data_cleaning_agent.tools.validator import validate_tables


STAMP = "2026-09-18T03:45:05.902530+00:00"
AVAILABLE = "2026-09-18T03:45:07.647454+00:00"
SCHEDULED = "2026-09-18T03:45:05+00:00"
RAW_ID = "ee831d3ac974850caac8b3753958a4c2070a5e005f5f98d028f1c93c81f102ad"
CONTEXT = {"source": "postgresql", "schema_version": 2, "source_read_only": True, "integrity_passed": True}


def fixture_row():
    receipt = {
        "kind": "DERIVED", "parent_observation_id": RAW_ID, "observation_id": "a" * 64,
        "symbol": "NIFTY22SEP2623350CE", "received_at": AVAILABLE,
        "request_started_at": "2026-09-18T03:45:06+00:00", "scheduled_at": SCHEDULED,
        "timestamp_basis": "CALCULATION_RECEIPT", "freshness": "DERIVED_UNVERIFIED_INPUT_TIME",
    }
    calculation = {
        "timestamp_ist": STAMP, "underlying_symbol": "NIFTY", "option_symbol": receipt["symbol"],
        "strike": 23350, "option_type": "CE", "expiry_date": "22SEP26", "trading_date": "2026-09-18",
        "version": 2, "data_status": "PARTIAL", "calculation_method": "OPENALGO_BLACK76",
        "ingestion_time": AVAILABLE, "option_ltp": 112.65, "underlying_ltp": 23344.95,
        "calculation_option_ltp": 112.90, "calculation_spot_ltp": 23363.10,
        "implied_volatility": 10.55, "delta": .5219, "gamma": .001496,
        "theta": -12.4511, "vega": 10.0545, "rho": -.013178,
        "interest_rate": 0, "forward_price": None, "derived_receipts": [receipt],
    }
    return {
        "timestamps": STAMP, "underlying": "NIFTY", "symbol": None, "exchange": None,
        "spot": 23344.95, "iv": None, "volume": 370825, "oi": 3605160, "ltp": 112.65,
        "strike": 23350, "optiontype": "CE", "expirydate": "22SEP26", "data_status": "PARTIAL",
        "timestamp_source": "APPLICATION_RECEIPT", "provider_timestamp": None, "source_version": 2,
        "source_receipt_id": RAW_ID, "source_freshness": "UNVERIFIED_PROVIDER_TIME",
        "source_ingestion_time": STAMP, "source_iv": None,
        "source_option_json": json.dumps({"bid": 112.60, "ask": 112.70, "source_latency_ms": 903, "bid_qty": 9007199254740993}),
        "source_market_json": json.dumps([{"spot_open": 23320.0, "vix": 12.5}]),
        "source_greeks_json": json.dumps([calculation]),
        "source_receipt_evidence_json": json.dumps([{"scheduled_at": SCHEDULED, "request_started_at": SCHEDULED}]),
    }


def normalize(rows):
    frame = pd.DataFrame(rows, dtype=object)
    cleaned = clean_data(frame, CleaningPlan(column_mapping={}), Settings(_env_file=None))
    return frame, cleaned


def enrich(rows, context=CONTEXT):
    frame, cleaned = normalize(rows)
    summary = enrich_tables(cleaned, frame, context)
    return cleaned, summary


def test_exact_sample_restores_available_values_without_reidentifying_or_backdating():
    row = fixture_row()
    frame, cleaned = normalize([row])
    old_id = cleaned.observations.iloc[0].observation_id
    assert old_id == "f4fe7f492df9c48c2d2a24f2e067ccde9a51b2642d068697e0153bd3e4c344d8"
    summary = enrich_tables(cleaned, frame, CONTEXT)
    obs, opt = cleaned.observations.iloc[0], cleaned.options.iloc[0]
    assert obs.observation_id == opt.observation_id == old_id
    assert obs.iv == pytest.approx(.1055)
    assert opt.delta == .5219 and opt.theta == -12.4511
    assert opt.rho == -.013178
    assert opt.ltp == 112.65 and opt.calculation_option_ltp == 112.9
    assert obs.spot == 23344.95 and opt.calculation_underlying_price == 23363.1
    assert obs.iv_available_at == opt.greeks_available_at == pd.Timestamp(AVAILABLE)
    assert opt.calculation_lag_ms == pytest.approx(1744.924)
    assert obs.timestamps == pd.Timestamp(STAMP)
    assert obs.timestamps_ist == "2026-09-18T09:15:05.902530+05:30"
    assert opt.expiry_at_ist == "2026-09-22T15:30:00+05:30"
    assert opt.bid_qty == 9007199254740993
    assert obs.symbol == opt.symbol == "NIFTY22SEP2623350CE"
    assert opt.exchange_source == "collector_option_route"
    assert pd.isna(obs.provider_timestamp)
    assert opt.calculation_underlying_kind == "unverified_spot_or_forward"
    assert "ZERO_INTEREST_RATE_ASSUMPTION" in opt.model_risk_flags
    assert summary["iv_and_greeks_complete_rows"] == 1
    assert validate_tables(cleaned.observations, cleaned.options).passed


@pytest.mark.parametrize("field,value", [
    ("option_type", "PE"), ("strike", 23400), ("version", 1),
    ("expiry_date", "29SEP26"), ("underlying_symbol", "BANKNIFTY"),
    ("option_ltp", 113.0), ("underlying_ltp", 23345.0),
    ("ingestion_time", "2026-09-18T03:45:04+00:00"),
])
def test_wrong_contract_parent_values_or_availability_never_becomes_features(field, value):
    row = fixture_row()
    candidate = json.loads(row["source_greeks_json"])[0]
    candidate[field] = value
    row["source_greeks_json"] = json.dumps([candidate])
    cleaned, summary = enrich([row])
    assert cleaned.options.iloc[0].derivation_status == "invalid_stored_derived"
    assert pd.isna(cleaned.options.iloc[0].delta)
    assert pd.isna(cleaned.observations.iloc[0].iv)
    assert summary["analytics_coverage"] == "PARTIAL"


@pytest.mark.parametrize("field,value", [
    ("parent_observation_id", "b" * 64), ("observation_id", "z" * 64),
    ("symbol", "NIFTY22SEP2623350PE"), ("freshness", "VERIFIED"),
    ("scheduled_at", "2026-09-18T03:45:00+00:00"),
    ("request_started_at", "2026-09-18T03:45:01+00:00"),
    ("received_at", "2026-09-18T03:45:09+00:00"),
])
def test_wrong_derived_receipt_is_not_imported(field, value):
    row = fixture_row()
    candidate = json.loads(row["source_greeks_json"])[0]
    candidate["derived_receipts"][0][field] = value
    row["source_greeks_json"] = json.dumps([candidate])
    cleaned, _ = enrich([row])
    assert cleaned.options.iloc[0].derivation_status == "invalid_stored_derived"
    assert pd.isna(cleaned.observations.iloc[0].iv)


def test_ambiguous_candidates_are_not_arbitrarily_selected():
    row = fixture_row()
    candidates = json.loads(row["source_greeks_json"])
    row["source_greeks_json"] = json.dumps(candidates + candidates)
    cleaned, _ = enrich([row])
    assert cleaned.options.iloc[0].derivation_status == "ambiguous_stored_derived"
    assert pd.isna(cleaned.observations.iloc[0].iv)


def test_static_symbol_mapping_does_not_forward_fill_analytics():
    first, missing = fixture_row(), fixture_row()
    missing.update(timestamps="2026-09-18T03:45:10+00:00", source_greeks_json="[]", source_receipt_id="c" * 64)
    cleaned, summary = enrich([first, missing])
    assert cleaned.observations.iloc[1].symbol == cleaned.observations.iloc[0].symbol
    assert pd.isna(cleaned.observations.iloc[1].iv)
    assert pd.isna(cleaned.options.iloc[1].delta)
    assert cleaned.options.iloc[1].derivation_status == "missing_stored_derived"
    assert summary["iv_and_greeks_missing_rows"] == 1


@pytest.mark.parametrize("status,expected", [("VALID", "stored_derived"), ("MISSING", "invalid_stored_derived"), ("REJECTED", "invalid_stored_derived")])
def test_legacy_status_is_preserved_and_failed_calculations_stay_missing(status, expected):
    row = fixture_row()
    row.update(source_version=1, source_receipt_id=None, timestamp_source="UNVERIFIED_LEGACY_TIME", source_freshness=None)
    candidate = json.loads(row["source_greeks_json"])[0]
    candidate.update(version=1, data_status=status, derived_receipts=[])
    row["source_greeks_json"] = json.dumps([candidate])
    cleaned, _ = enrich([row])
    assert cleaned.options.iloc[0].derivation_status == expected
    if status == "VALID":
        assert "LEGACY_RECEIPT_UNAVAILABLE" in cleaned.options.iloc[0].model_risk_flags
        assert pd.isna(cleaned.options.iloc[0].derived_receipt_id)
        assert validate_tables(cleaned.observations, cleaned.options).passed
    else:
        assert pd.isna(cleaned.options.iloc[0].delta)


def test_missing_one_metric_preserves_other_valid_calculated_metrics():
    row = fixture_row()
    candidate = json.loads(row["source_greeks_json"])[0]
    candidate["vega"] = None
    row["source_greeks_json"] = json.dumps([candidate])
    cleaned, _ = enrich([row])
    assert cleaned.options.iloc[0].derivation_status == "stored_partial"
    assert cleaned.observations.iloc[0].iv == pytest.approx(.1055)
    assert cleaned.options.iloc[0].delta == .5219
    assert pd.isna(cleaned.options.iloc[0].vega)
    assert validate_tables(cleaned.observations, cleaned.options).passed


def test_generic_input_does_not_claim_database_provenance():
    cleaned, _ = enrich([fixture_row()], None)
    assert pd.isna(cleaned.observations.iloc[0].iv)
    assert cleaned.options.iloc[0].derivation_status == "disabled"


def test_validation_rejects_backdated_availability_and_wrong_parent():
    cleaned, _ = enrich([fixture_row()])
    cleaned.options.loc[0, "greeks_available_at"] = pd.Timestamp(STAMP)
    assert not validate_tables(cleaned.observations, cleaned.options).passed
    cleaned, _ = enrich([fixture_row()])
    cleaned.options.loc[0, "derived_parent_receipt_id"] = "b" * 64
    assert not validate_tables(cleaned.observations, cleaned.options).passed


@pytest.mark.parametrize("field,value", [("derived_source_version", 1),
                                        ("calculation_model", "OTHER_MODEL"), ("calculation_lag_ms", 0)])
def test_validator_checks_model_version_and_lag(field, value):
    cleaned, _ = enrich([fixture_row()])
    cleaned.options.loc[0, field] = value
    assert not validate_tables(cleaned.observations, cleaned.options).passed


def test_pipeline_writes_enriched_parquet_and_readable_csv_with_counts(tmp_path):
    source = tmp_path / "source.jsonl"
    source.write_text(json.dumps(fixture_row()) + "\n")
    result = DataCleaningAgent(Settings(_env_file=None), planner="deterministic").run(source, tmp_path / "runs", source_context=CONTEXT)
    assert result.passed, (result.run_dir / "quality_report.json").read_text()
    obs = pd.read_parquet(result.run_dir / "observations.parquet")
    opt = pd.read_parquet(result.run_dir / "options.parquet")
    assert obs.iloc[0].iv == pytest.approx(.1055)
    assert opt.iloc[0].delta == .5219
    csv = pd.read_csv(result.run_dir / "observations.csv")
    assert csv.iloc[0].timestamps_ist == "2026-09-18T09:15:05.902530+05:30"
    manifest = json.loads((result.run_dir / "manifest.json").read_text())
    assert manifest["schema_version"] == 2
    assert manifest["feature_completeness"]["iv_and_greeks_complete_rows"] == 1
    assert {"observations.csv", "options.csv"}.issubset(manifest["artifacts"])

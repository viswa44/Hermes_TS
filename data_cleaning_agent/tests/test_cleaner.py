"""Regression coverage for preservation, quarantine and financial assumptions."""

from datetime import timedelta
from types import SimpleNamespace

import pandas as pd
import pytest

from data_cleaning_agent.config.settings import Settings
from data_cleaning_agent.models.cleaning_plan import CleaningPlan
from data_cleaning_agent.tools.cleaner import clean_data


def row(**changes):
    return {
        "timestamps": "2026-09-12T10:00:00+05:30", "spot": 24000,
        "iv": 0.2, "volume": 150, "oi": 1000, "ltp": 123.5,
        "strike": 24000, "optiontype": "CE", "expirydate": "2026-09-15",
        "underlying": "NIFTY", "exchange": "NFO", **changes,
    }


def run(rows, **settings):
    return clean_data(pd.DataFrame(rows), CleaningPlan(column_mapping={}),
                      Settings(_env_file=None, **settings))


def test_normalizes_without_mutating_input_and_preserves_provenance():
    source = pd.DataFrame([row(optiontype=" call ", iv="20", oi=" 1000 ",
                               underlying=" NIFTY ", timestamp_source="UNVERIFIED_PROVIDER_TIME",
                               provider_timestamp="2026-09-12T04:29:55Z", data_status="PARTIAL")])
    original = source.copy(deep=True)
    result = clean_data(source, CleaningPlan(column_mapping={}), Settings(_env_file=None, iv_unit="percent"))
    pd.testing.assert_frame_equal(source, original)
    assert result.quarantine.empty
    observed, option = result.observations.iloc[0], result.options.iloc[0]
    assert observed.iv == 0.2
    assert observed.underlying == "NIFTY"
    assert observed.timestamp_source == "UNVERIFIED_PROVIDER_TIME"
    assert observed.data_status == "PARTIAL"
    assert observed.provider_timestamp == pd.Timestamp("2026-09-12T04:29:55Z")
    assert option.optiontype == "CE"
    assert option.expirydate == pd.Timestamp("2026-09-15T10:00:00Z")
    assert option.daystoexpiry == pytest.approx(3 + 5.5 / 24)
    assert observed.observation_id == option.observation_id


def test_optional_absence_remains_null_and_raw_greeks_are_not_reused():
    result = run([row(oi=None, ltp=None, iv=None, volume=None,
                      delta=0.4, gamma=0.001, theta=-2, vega=12)])
    assert result.quarantine.empty
    assert result.observations[["iv", "volume"]].isna().all(axis=None)
    assert result.options[["oi", "ltp", "delta", "gamma", "theta", "vega"]].isna().all(axis=None)
    assert result.options.iloc[0].derivation_status == "disabled"
    assert result.observations.iloc[0].timestamp_source == "unspecified"
    assert result.observations.iloc[0].data_status == "unspecified"


@pytest.mark.parametrize("field,value", [
    ("spot", 0), ("strike", -1), ("ltp", -0.1), ("oi", -1), ("oi", 0.5),
    ("volume", "many"), ("iv", "NaN"), ("iv", float("inf")),
    ("spot", float("-inf")), ("volume", 2**63), ("oi", True),
    ("iv", 0), ("ltp", {"last": 100}), ("optiontype", "FUT"),
    ("timestamps", "09/12/2026 10:00:00"), ("expirydate", "15/09/2026"),
])
def test_malformed_supplied_values_quarantine_entire_row(field, value):
    result = run([row(**{field: value})])
    assert result.observations.empty and result.options.empty
    assert len(result.quarantine) == 1
    assert field in result.quarantine.iloc[0].reason


def test_empty_and_all_quarantined_frames_keep_parquet_compatible_types():
    for result in (run([]), run([row(spot=None)])):
        assert str(result.observations["timestamps"].dtype) == "datetime64[ns, UTC]"
        assert str(result.options["oi"].dtype) == "Int64"
        assert str(result.options["delta"].dtype) == "Float64"
        assert str(result.observations["provider_timestamp"].dtype) == "datetime64[ns, UTC]"


def test_exact_deduplication_and_conflicting_natural_key_quarantine():
    exact = run([row(), row()])
    assert exact.duplicates_removed == 1
    assert len(exact.options) == 1 and exact.quarantine.empty
    conflict = run([row(oi=100), row(oi=200)])
    assert conflict.duplicates_removed == 0
    assert conflict.options.empty
    assert len(conflict.quarantine) == 2
    assert conflict.quarantine.reason.str.contains("natural_key").all()
    formatted = run([row(optiontype="CE"), row(optiontype="call")])
    assert len(formatted.quarantine) == 2


def test_conflicting_invalid_optional_value_also_quarantines_valid_counterpart():
    result = run([row(oi=100), row(oi="invalid")])
    assert result.options.empty and result.observations.empty
    assert len(result.quarantine) == 2
    assert "natural_key" in result.quarantine.iloc[0].reason


def test_nullable_large_integer_is_preserved_without_float_inference():
    source = pd.DataFrame([row(oi=2**53 + 1), row(oi=None, strike=24100)], dtype=object)
    result = clean_data(source, CleaningPlan(column_mapping={}), Settings(_env_file=None))
    assert result.options.iloc[0].oi == 2**53 + 1
    assert pd.isna(result.options.iloc[1].oi)


def test_deduplicated_invalid_rows_reconcile_with_source_count():
    result = run([row(oi="invalid"), row(oi="invalid"), row(strike=24100)])
    assert len(result.observations) + len(result.quarantine) + result.duplicates_removed == 3
    assert result.duplicates_removed == 1


def test_duplicate_targets_and_existing_canonical_collision_are_rejected():
    with pytest.raises(ValueError, match="duplicate canonical targets"):
        clean_data(pd.DataFrame([row(a=1, b=1)]),
                   SimpleNamespace(column_mapping={"a": "spot", "b": "spot"}),
                   Settings(_env_file=None))
    with pytest.raises(ValueError, match="duplicate canonical targets"):
        clean_data(pd.DataFrame([row(price=1)]), CleaningPlan(column_mapping={"price": "spot"}),
                   Settings(_env_file=None))


def test_numeric_timestamp_requires_explicit_unit():
    timestamp = int(pd.Timestamp("2026-09-12T04:30:00Z").timestamp() * 1000)
    rejected = run([row(timestamps=timestamp)])
    assert "timestamp_unit" in rejected.quarantine.iloc[0].reason
    accepted = run([row(timestamps=timestamp)], timestamp_unit="ms")
    assert accepted.observations.iloc[0].timestamps == pd.Timestamp("2026-09-12T04:30:00Z")


def test_naive_timestamp_uses_explicit_timezone_and_ambiguous_dst_is_rejected():
    accepted = run([row(timestamps="2026-09-12 10:00:00")])
    assert accepted.observations.iloc[0].timestamps == pd.Timestamp("2026-09-12T04:30:00Z")
    ambiguous = run([row(timestamps="2026-11-01T01:30:00", expirydate="2026-11-03")],
                    input_timezone="America/New_York")
    assert len(ambiguous.quarantine) == 1
    nonexistent = run([row(timestamps="2026-03-08T02:30:00")], input_timezone="America/New_York")
    assert len(nonexistent.quarantine) == 1


@pytest.mark.parametrize("expiry", ["15-Sep-2026", "15SEP26", "2026-09-15"])
def test_explicit_named_and_iso_expiry_dates(expiry):
    result = run([row(expirydate=expiry)])
    assert result.options.iloc[0].expirydate == pd.Timestamp("2026-09-15T10:00:00Z")


def test_expired_rows_rejected_and_expiry_instant_has_no_greeks():
    expired = run([row(timestamps="2026-09-15T15:30:01+05:30")])
    assert expired.options.empty
    result = run([row(timestamps="2026-09-15T15:30:00+05:30")],
                 derive_greeks=True, risk_free_rate=0.05, dividend_yield=0.0)
    assert result.options.iloc[0].daystoexpiry == 0
    assert result.options.iloc[0].derivation_status == "nonpositive_time"
    assert result.options[["delta", "theta", "gamma", "vega"]].isna().all(axis=None)


def test_european_black_scholes_known_fixture_and_units():
    start = pd.Timestamp("2026-01-01T00:00:00Z")
    expiry = start + timedelta(days=0.3846 * 365)
    result = run([row(timestamps=start, expirydate=expiry, spot=49, strike=50, iv=0.2)],
                 derive_greeks=True, risk_free_rate=0.05, dividend_yield=0.0)
    option = result.options.iloc[0]
    assert option.delta == pytest.approx(0.521601633972, rel=1e-10)
    assert option.gamma == pytest.approx(0.0655453772525, rel=1e-10)
    assert option.theta == pytest.approx(-4.30538996455 / 365, rel=1e-10)
    assert option.vega == pytest.approx(0.121052427542, rel=1e-10)
    assert option.greeks_source == "black_scholes_european"
    assert option.derivation_status == "derived"


def test_call_put_delta_parity_with_nonzero_dividend():
    import math
    rows = [row(optiontype="CE"), row(optiontype="PE")]
    result = run(rows, derive_greeks=True, risk_free_rate=0.05, dividend_yield=0.03)
    call, put = result.options.iloc[0], result.options.iloc[1]
    years = call.daystoexpiry / 365
    assert call.delta - put.delta == pytest.approx(math.exp(-0.03 * years))
    assert call.gamma == pytest.approx(put.gamma)
    assert call.vega == pytest.approx(put.vega)


def test_missing_iv_never_uses_ltp_to_infer_volatility():
    result = run([row(iv=None, ltp=150)], derive_greeks=True,
                 risk_free_rate=0.05, dividend_yield=0.0)
    assert pd.isna(result.observations.iloc[0].iv)
    assert result.options.iloc[0].derivation_status == "missing_iv"
    assert pd.isna(result.options.iloc[0].delta)


def test_observation_id_is_deterministic_and_does_not_depend_on_row_position():
    first = run([row(), row(strike=24100)])
    second = run([row(strike=24100), row()])
    assert first.options.iloc[0].observation_id == second.options.iloc[1].observation_id
    assert first.observations.iloc[0].source_row == 0
    assert second.observations.iloc[1].source_row == 1

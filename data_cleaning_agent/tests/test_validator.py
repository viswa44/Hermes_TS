import pandas as pd
import pytest

from data_cleaning_agent.config.settings import Settings
from data_cleaning_agent.models.cleaning_plan import CleaningPlan
from data_cleaning_agent.tools.cleaner import clean_data
from data_cleaning_agent.tools.validator import validate_tables


def cleaned(derive=False):
    return clean_data(
        pd.DataFrame([{
            "timestamps": "2026-09-12T10:00:00+05:30", "spot": 24000,
            "strike": 24000, "optiontype": "CE", "expirydate": "2026-09-15",
            "iv": 0.2,
        }]), CleaningPlan(column_mapping={}), Settings(_env_file=None, derive_greeks=derive,
                                                      risk_free_rate=0.05, dividend_yield=0.0),
    )


def test_valid_linked_tables_and_empty_typed_tables_pass():
    result = cleaned()
    assert validate_tables(result.observations, result.options).to_dict() == {"passed": True, "errors": []}
    assert validate_tables(result.observations.iloc[:0], result.options.iloc[:0]).passed


def test_foreign_key_missing_or_mismatched_fails():
    result = cleaned()
    result.options.loc[0, "observation_id"] = "a" * 64
    validation = validate_tables(result.observations, result.options)
    assert not validation.passed
    assert any("foreign keys" in error for error in validation.errors)


@pytest.mark.parametrize("table,column,value", [
    ("observations", "spot", float("inf")), ("options", "ltp", -1),
    ("options", "delta", float("-inf")), ("observations", "iv", 0),
])
def test_invalid_numeric_values_fail(table, column, value):
    result = cleaned()
    getattr(result, table).loc[0, column] = value
    validation = validate_tables(result.observations, result.options)
    assert not validation.passed
    assert any(column in error for error in validation.errors)


def test_missing_required_field_and_duplicate_id_fail():
    result = cleaned()
    result.observations.loc[0, "spot"] = pd.NA
    assert not validate_tables(result.observations, result.options).passed
    result = cleaned()
    duplicate = pd.concat([result.options, result.options], ignore_index=True)
    validation = validate_tables(result.observations, duplicate)
    assert not validation.passed
    assert any("duplicate observation_id" in error for error in validation.errors)


def test_time_disagreement_between_tables_and_wrong_dte_fail():
    result = cleaned()
    result.options["timestamps"] = result.options["timestamps"] + pd.Timedelta(seconds=60)
    validation = validate_tables(result.observations, result.options)
    assert not validation.passed
    assert any("mismatched timestamps" in error for error in validation.errors)
    assert any("daystoexpiry" in error for error in validation.errors)


def test_unlabelled_or_partial_derived_greeks_fail():
    result = cleaned(True)
    assert validate_tables(result.observations, result.options).passed
    result.options.loc[0, "theta"] = pd.NA
    assert not validate_tables(result.observations, result.options).passed
    result = cleaned(True)
    result.options.loc[0, "derivation_status"] = "disabled"
    assert not validate_tables(result.observations, result.options).passed


def test_derived_greeks_require_supplied_iv():
    result = cleaned(True)
    result.observations.loc[0, "iv"] = pd.NA
    validation = validate_tables(result.observations, result.options)
    assert not validation.passed
    assert any("supplied IV" in error for error in validation.errors)


def test_naive_timestamps_and_missing_columns_fail():
    result = cleaned()
    result.observations["timestamps"] = result.observations["timestamps"].dt.tz_localize(None)
    validation = validate_tables(result.observations, result.options)
    assert not validation.passed
    assert any("UTC" in error for error in validation.errors)
    result = cleaned()
    validation = validate_tables(result.observations.drop(columns=["spot"]), result.options)
    assert not validation.passed
    assert any("missing columns" in error for error in validation.errors)

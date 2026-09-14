import pandas as pd
import pytest

from ..tools.profiler import profile_data
from ..tools.schema_detector import detect_schema, validate_plan
from ..models.cleaning_plan import CleaningPlan


def test_profiler_reports_counts_without_raw_values():
    frame = pd.DataFrame({'spot': [12345, 12345, None], 'symbol': ['private', 'private', None]})
    profile = profile_data(frame)
    assert profile['row_count'] == 3
    assert profile['exact_duplicate_count'] == 1
    assert profile['columns']['spot']['null_count'] == 1
    assert 'private' not in str(profile)
    assert '12345' not in str(profile)


def test_schema_spelling_and_conflicts():
    schema = detect_schema([' timestamp ', 'Spot Price', 'strike', 'Option Type', 'expiry_date', 'delta'])
    assert not schema.missing_required
    assert schema.column_mapping[' timestamp '] == 'timestamps'
    assert schema.ignored_derived_columns == ['delta']
    with pytest.raises(ValueError, match='Ambiguous'):
        detect_schema(['oi', 'open_interest'])


def test_plan_cannot_reassign_financial_fields():
    schema = detect_schema(['timestamps', 'spot', 'strike', 'optiontype', 'expirydate', 'oi'])
    mapping = dict(schema.column_mapping)
    mapping['oi'] = 'ltp'
    with pytest.raises(ValueError, match='exactly'):
        validate_plan(CleaningPlan(column_mapping=mapping), schema)


def test_plan_refuses_unsafe_actions():
    with pytest.raises(ValueError):
        CleaningPlan(column_mapping={}, null_policy='zero', python_code='unsafe')

"""Value-free profiling for the planner; actual records remain local."""

import json

import pandas as pd


def profile_data(frame: pd.DataFrame) -> dict:
    if not frame.columns.is_unique:
        raise ValueError('Duplicate input column names')
    # Serializing handles unhashable nested values without evaluating them.
    row_keys = [json.dumps(row, sort_keys=True, default=str, allow_nan=True) for row in frame.to_dict('records')]
    return {
        'row_count': len(frame),
        'column_count': len(frame.columns),
        'exact_duplicate_count': len(row_keys) - len(set(row_keys)),
        'columns': {
            str(name): {'dtype': str(frame[name].dtype), 'null_count': int(frame[name].isna().sum())}
            for name in frame.columns
        },
    }

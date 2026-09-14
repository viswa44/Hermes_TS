"""Daily source contracts: read-only SQL, linkage, provenance and precision."""

from datetime import date
from decimal import Decimal
import hashlib
import json
from types import SimpleNamespace

import pandas as pd
import pytest

from data_cleaning_agent.agent.cleaning_agent import load_data
from data_cleaning_agent.config.settings import Settings
from data_cleaning_agent.models.cleaning_plan import CleaningPlan
from data_cleaning_agent.tools.cleaner import clean_data
from data_cleaning_agent.tools.postgres_tool import available_trading_dates, export_trading_day, session_bounds


DAY = date(2026, 9, 11)
START, END = session_bounds(DAY)


def source_row(**changes):
    return {
        "timestamp_ist": START, "underlying": "NIFTY", "strike": 24000.0,
        "option_type": "CE", "expiry_date": "15SEP26", "trading_date": DAY,
        "ltp": 100.0, "volume": 150, "oi": 1000, "source_iv": None,
        "data_status": "PARTIAL", "source_version": 2, "source_ingestion_time": START,
        "market_match_count": 1, "source_spot": 24020.0, "market_trading_date": DAY,
        "market_version": 2, "market_data_status": "PARTIAL", "market_evidence": '{"spot":24020}',
        "receipt_match_count": 1, "receipt_id": "receipt-1",
        "timestamp_basis": "APPLICATION_RECEIPT", "freshness": "UNVERIFIED_PROVIDER_TIME",
        "receipt_evidence": '[{"observation_id":"receipt-1"}]', **changes,
    }


class FakeCursor:
    def __init__(self, connection, name=None):
        self.connection = connection
        self.name = name

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, query, params):
        self.connection.calls.append((query, params, self.name))
        if "AS all_day_option_rows" in query:
            rows = [{"all_day_option_rows": len(self.connection.rows) + self.connection.outside,
                     "out_of_window_count": self.connection.outside}]
        elif "AS orphan_market_rows" in query:
            rows = [{"orphan_market_rows": self.connection.orphans}]
        elif "SELECT symbol, scheduled_at, received_at" in query:
            rows = self.connection.receipts
        elif "SELECT DISTINCT" in query:
            rows = [{"trading_date": DAY}]
        else:
            assert "LEFT JOIN LATERAL" in query
            rows = self.connection.rows
        self.description = [SimpleNamespace(name=key) for key in rows[0]] if rows else []
        self.rows = [tuple(row.values()) for row in rows]

    def fetchmany(self, size):
        self.connection.batch_sizes.append(size)
        batch, self.rows = self.rows[:size], self.rows[size:]
        return batch


class FakeConnection:
    def __init__(self, rows, *, outside=0, orphans=0, receipts=None):
        self.rows, self.outside, self.orphans = rows, outside, orphans
        self.receipts = receipts if receipts is not None else [
            {"symbol": "NIFTY", "scheduled_at": START, "received_at": START}]
        self.calls, self.batch_sizes = [], []

    def cursor(self, name=None):
        return FakeCursor(self, name)


def test_export_preserves_integers_and_raw_provenance_without_using_derived_fields(tmp_path):
    connection = FakeConnection([source_row(oi=2**53 + 1, source_iv=0.27),
                                 source_row(oi=None, option_type="PE")])
    output = export_trading_day(connection, DAY, tmp_path / "source.jsonl", batch_size=1)
    lines = [json.loads(line) for line in output.path.read_text().splitlines()]
    assert lines[0]["oi"] == 2**53 + 1
    assert lines[1]["oi"] is None
    assert lines[0]["iv"] is None and lines[0]["source_iv"] == 0.27
    assert lines[0]["timestamp_source"] == "APPLICATION_RECEIPT"
    assert lines[0]["source_freshness"] == "UNVERIFIED_PROVIDER_TIME"
    assert lines[0]["provider_timestamp"] is None
    assert lines[0]["source_receipt_id"] == "receipt-1"
    assert lines[0]["symbol"] is None and lines[0]["underlying"] == "NIFTY"
    assert not {"delta", "gamma", "theta", "vega"} & lines[0].keys()
    assert output.source_sha256 == hashlib.sha256(output.path.read_bytes()).hexdigest()
    assert output.metadata["integrity_passed"]
    assert output.row_count == 2
    assert output.path.stat().st_mode & 0o777 == 0o600
    assert connection.calls[-1][1] == (START, END, 100001)
    assert all("option_greeks_snapshot" not in query for query, _, _ in connection.calls)
    assert all(query.lstrip().startswith("SELECT") for query, _, _ in connection.calls)
    frame = load_data(output.path, Settings(_env_file=None))
    result = clean_data(frame, CleaningPlan(column_mapping={}), Settings(_env_file=None))
    assert result.options.iloc[0].oi == 2**53 + 1
    assert pd.isna(result.options.iloc[1].oi)


@pytest.mark.parametrize("changes,issue", [
    ({"market_match_count": 0}, "missing_market_snapshot"),
    ({"market_match_count": 2}, "ambiguous_market_snapshot"),
    ({"receipt_match_count": 0}, "missing_raw_receipt"),
    ({"receipt_match_count": 2}, "ambiguous_raw_receipt"),
    ({"timestamp_basis": "PROVIDER_EVENT"}, "raw_receipt_provenance_mismatch"),
    ({"market_version": 1}, "market_provenance_mismatch"),
    ({"data_status": "VALID"}, "option_provenance_mismatch"),
    ({"trading_date": date(2026, 9, 10)}, "option_trading_date_mismatch"),
    ({"market_trading_date": date(2026, 9, 10)}, "market_trading_date_mismatch"),
    ({"source_version": 3}, "unsupported_source_version"),
])
def test_source_anomalies_retain_row_evidence_and_force_quarantine(tmp_path, changes, issue):
    output = export_trading_day(FakeConnection([source_row(**changes)]), DAY, tmp_path / "source.jsonl")
    record = json.loads(output.path.read_text())
    assert record["source_spot"] == 24020
    assert record["spot"] is None
    assert issue in record["source_integrity_issues"]
    assert output.metadata["anomaly_counts"][issue] == 1
    assert not output.metadata["integrity_passed"]
    result = clean_data(load_data(output.path, Settings(_env_file=None)),
                        CleaningPlan(column_mapping={}), Settings(_env_file=None))
    assert result.options.empty and len(result.quarantine) == 1


def test_legacy_is_retained_with_explicit_unverified_label(tmp_path):
    output = export_trading_day(FakeConnection([source_row(source_version=1, market_version=1,
        data_status="VALID", market_data_status="VALID", receipt_match_count=0,
        timestamp_basis=None, freshness=None, receipt_id=None)], receipts=[]), DAY, tmp_path / "source.jsonl")
    record = json.loads(output.path.read_text())
    assert record["timestamp_source"] == "UNVERIFIED_LEGACY_TIME"
    assert record["spot"] == 24020
    assert record["data_status"] == "VALID"
    assert output.metadata["legacy_rows"] == 1
    assert output.metadata["raw_receipt_coverage"]["NIFTY"]["missing_slots"] == 4500
    assert output.metadata["raw_receipt_coverage"]["NIFTY"]["raw_receipt_count"] == 0


def test_coverage_counts_missing_intervals_and_outside_window_without_faking_completeness(tmp_path):
    output = export_trading_day(FakeConnection([source_row()], outside=3, orphans=2),
                                DAY, tmp_path / "source.jsonl")
    coverage = output.metadata["raw_receipt_coverage"]["NIFTY"]
    assert coverage["expected_slots"] == 4500
    assert coverage["captured_distinct_slots"] == 1
    assert coverage["missing_slots"] == 4499
    assert coverage["missing_intervals"] == [{"start_inclusive": "2026-09-11T09:15:05+05:30",
        "end_exclusive": "2026-09-11T15:30:00+05:30", "slots": 4499}]
    assert output.metadata["out_of_window_count"] == 3
    assert output.metadata["all_day_option_rows"] == 4
    assert output.metadata["orphan_market_rows"] == 2
    assert not output.metadata["integrity_passed"]


def test_available_dates_uses_actual_local_receipt_day_and_excludes_weekends():
    connection = FakeConnection([])
    assert available_trading_dates(connection, date(2026, 9, 1), DAY) == [DAY]
    query, params, name = connection.calls[0]
    assert "ISODOW" in query and "Asia/Kolkata" in query
    assert params[0].isoformat() == "2026-09-01T00:00:00+05:30"
    assert params[1].isoformat() == "2026-09-12T00:00:00+05:30"
    assert params[2:] == (START.time(), END.time())
    assert name is None


@pytest.mark.parametrize("kwargs,error", [({"max_rows": 1}, "MAX_ROWS"), ({"max_bytes": 1}, "byte limit")])
def test_limits_never_publish_truncated_source(tmp_path, kwargs, error):
    destination = tmp_path / "source.jsonl"
    with pytest.raises(ValueError, match=error):
        export_trading_day(FakeConnection([source_row(), source_row(option_type="PE")]),
                           DAY, destination, **kwargs)
    assert not destination.exists()
    assert list(tmp_path.iterdir()) == []


def test_snapshot_never_overwrites_existing_source(tmp_path):
    destination = tmp_path / "source.jsonl"
    destination.write_text("original")
    with pytest.raises(FileExistsError):
        export_trading_day(FakeConnection([]), DAY, destination)
    assert destination.read_text() == "original"


def test_decimal_and_nonfinite_values_survive_for_explicit_validation(tmp_path):
    output = export_trading_day(FakeConnection([source_row(oi=Decimal("9007199254740993"),
        volume=float("inf"))]), DAY, tmp_path / "source.jsonl")
    row = json.loads(output.path.read_text())
    assert row["oi"] == "9007199254740993"
    assert row["volume"] == "inf"


def test_weekend_export_and_invalid_bounds_rejected_before_database_access(tmp_path):
    connection = FakeConnection([])
    with pytest.raises(ValueError, match="weekday"):
        export_trading_day(connection, date(2026, 9, 12), tmp_path / "source.jsonl")
    with pytest.raises(ValueError, match="start_date"):
        available_trading_dates(connection, DAY, date(2026, 9, 1))
    assert not connection.calls

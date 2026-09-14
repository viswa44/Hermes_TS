"""Read-only monitoring boundaries; isolated files, HTTP and database doubles."""
from datetime import date, datetime, timezone
import http.client
import json
from pathlib import Path
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from operations_dashboard.server import DashboardServer, DashboardStore, json_safe, parse_day


DAY = date(2026, 9, 11)
RUN = "44a24d8e-773b-4772-a0e2-6c4906efaeb9"


def put(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value))


@pytest.fixture
def export(tmp_path):
    import pyarrow as pa
    import pyarrow.parquet as pq
    runtime = tmp_path / "data_cleaning_agent/runtime"
    run = tmp_path / "data_cleaning_agent/output/postgres" / DAY.isoformat() / RUN
    put(runtime / "progress.json", {"days": {DAY.isoformat(): {
        "status": "PUBLISHED", "revision": "revision-a", "rows": 12,
        "manifest_uri": f"s3://test-bucket/cleaned/{DAY}/{RUN}/manifest.json",
        "private_setting": "do-not-expose"}}})
    put(run / "manifest.json", {"run_id": RUN, "source_context": {
        "trading_date": DAY.isoformat(), "revision": "revision-a", "raw_receipt_coverage": {
            "NIFTY": {"captured_distinct_slots": 6, "expected_slots": 7, "missing_slots": 1}}},
        "settings": {"derive_greeks": False, "api_key": "do-not-expose"},
        "table_counts": {"observations.parquet": 12, "options.parquet": 12}})
    put(run / "quality_report.json", {"passed": True, "quarantined_rows": 0, "accepted_rows": 12})
    for name in ("observations", "options"):
        pq.write_table(pa.table({"observation_id": [str(i) for i in range(12)],
                               "timestamps": ["2026-09-11T09:15:00+05:30"] * 12,
                               "private_setting": ["do-not-expose"] * 12}), run / f"{name}.parquet")
    return tmp_path, run


def test_saved_revision_preview_and_quality_do_not_hide_coverage(export):
    root, _ = export
    store = DashboardStore(root, database_enabled=False)
    result = store.status(DAY)
    assert result["selected"]["quality"]["passed"] is True
    assert result["selected"]["coverage"][0]["missing_slots"] == 1
    assert result["selected"]["greeks_enabled"] is False
    assert "do-not-expose" not in json.dumps(result)
    preview = store.samples(DAY, limit=3)
    assert preview["status"] == "AVAILABLE"
    for table in preview["tables"].values():
        assert len(table["rows"]) == 3
        assert table["total_rows"] == 12
        assert "private_setting" not in table["columns"]
    assert store.database_summary(DAY)["status"] == "DISABLED"


def test_mismatched_revision_never_shows_unpublished_export(export):
    root, run = export
    path = run / "manifest.json"
    manifest = json.loads(path.read_text())
    manifest["source_context"]["revision"] = "different-revision"
    put(path, manifest)
    store = DashboardStore(root)
    assert store.status(DAY)["selected"]["local_evidence"] is False
    assert store.samples(DAY)["status"] == "UNAVAILABLE"


def test_symlink_and_bad_json_cannot_escape_workspace(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    secret = tmp_path / "outside.json"
    put(secret, {"private": "secret"})
    (root / "link.json").symlink_to(secret)
    store = DashboardStore(root)
    assert store.read_json(root / "link.json") == {}
    assert store.read_json(secret) == {}
    (root / "bad.json").write_text("{broken")
    assert store.read_json(root / "bad.json") == {}


def test_missing_calendar_blocks_and_gate_history_is_exposed_without_private_fields(tmp_path):
    put(tmp_path / "market_calendar/runtime/gate-cleaner.json", {
        "status": "CLOSED", "allowed": False, "checked_at": "2026-09-14T10:15:00Z", "key": "secret"})
    result = DashboardStore(tmp_path).status(now=datetime(2026, 9, 15, tzinfo=timezone.utc))
    assert result["calendar"]["today"]["status"] == "UNKNOWN"
    assert result["calendar"]["today"]["allowed"] is False
    assert result["gates"]["cleaner"]["status"] == "CLOSED"
    assert "key" not in result["gates"]["cleaner"]


def test_database_uses_read_only_transaction_and_ist_bound_parameters(tmp_path, monkeypatch):
    import psycopg
    from data_cleaning_agent.config import settings
    config = SimpleNamespace(postgres_host="localhost", postgres_port=5432,
        postgres_database="test", postgres_user="test", postgres_connect_timeout=5,
        postgres_password=None)
    monkeypatch.setattr(settings, "Settings", lambda: config)
    cursor = MagicMock()
    cursor.__enter__.return_value = cursor
    cursor.fetchone.side_effect = [("on",), (12, None, None, 4, 12), (6,)]
    connection = MagicMock()
    connection.__enter__.return_value = connection
    connection.cursor.return_value = cursor
    connect = MagicMock(return_value=connection)
    monkeypatch.setattr(psycopg, "connect", connect)
    store = DashboardStore(tmp_path)
    result = store.database_summary(DAY)
    assert result["status"] == "CONNECTED" and result["read_only"]
    assert result["option_rows"] == 12
    assert connection.read_only is True
    assert "default_transaction_read_only=on" in connect.call_args.kwargs["options"]
    calls = cursor.execute.call_args_list
    assert calls[0].args == ("SHOW transaction_read_only",)
    for call in calls[1:]:
        sql, (start, end) = call.args
        assert sql.strip().startswith("SELECT")
        assert "2026-09-11" not in sql
        assert start.isoformat() == "2026-09-11T00:00:00+05:30"
        assert end.isoformat() == "2026-09-12T00:00:00+05:30"
    assert store.database_summary(DAY)["cached"] is True
    connect.assert_called_once()


def test_database_error_does_not_expose_dsn(tmp_path, monkeypatch):
    import psycopg
    monkeypatch.setattr(psycopg, "connect", MagicMock(side_effect=RuntimeError("password=secret")))
    result = DashboardStore(tmp_path).database_summary(DAY)
    assert result["status"] == "UNAVAILABLE"
    assert "secret" not in json.dumps(result)


@pytest.fixture
def server(tmp_path):
    service = DashboardServer(("127.0.0.1", 0), DashboardStore(tmp_path, database_enabled=False))
    thread = threading.Thread(target=service.serve_forever, daemon=True)
    thread.start()
    yield service
    service.shutdown()
    service.server_close()
    thread.join(timeout=2)


def request(server, path, *, method="GET", headers=None):
    connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)
    try:
        connection.request(method, path, headers=headers or {})
        response = connection.getresponse()
        return response.status, dict(response.getheaders()), response.read()
    finally:
        connection.close()


@pytest.mark.parametrize("path", ["/", "/style.css", "/app.js", "/health", "/api/status", "/api/day?date=2026-09-11", "/api/database?date=2026-09-11"])
def test_read_only_routes_work(server, path):
    status, headers, _ = request(server, path)
    assert status == 200
    assert headers["Cache-Control"] == "no-store"
    assert "frame-ancestors 'none'" in headers["Content-Security-Policy"]


@pytest.mark.parametrize("path", ["/api/day?date=../../.env", "/api/day?date=2026-09-31", "/api/day?date=2026-09-11&limit=100", "/api/status?date=2026-09-11&date=2026-09-12", "/api/database", "/api/status?file=.env"])
def test_invalid_requests_rejected(server, path):
    assert request(server, path)[0] == 400


def test_external_origin_writes_and_arbitrary_files_rejected(server):
    assert request(server, "/health", headers={"Host": "attacker.example"})[0] == 403
    assert request(server, "/health", headers={"Origin": "https://attacker.example"})[0] == 403
    assert request(server, "/api/status", method="POST")[0] == 405
    assert request(server, "/../data_cleaning_agent/.env")[0] == 404


def test_nonlocal_binding_is_refused(tmp_path):
    with pytest.raises(ValueError):
        DashboardServer(("0.0.0.0", 0), DashboardStore(tmp_path))


def test_strict_dates_and_nonfinite_json():
    assert parse_day("2026-09-11") == DAY
    with pytest.raises(ValueError):
        parse_day("20260911")
    assert json_safe({"iv": float("nan"), "gamma": float("inf")}) == {"iv": None, "gamma": None}

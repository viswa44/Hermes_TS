"""Production entrypoint guards, with isolated synthetic calendar evidence only.

No test fetches exchange data, reads credentials, connects to PostgreSQL, or
publishes to AWS. Calendar parsing itself is covered by the calendar unit tests.
"""
from __future__ import annotations

import asyncio
from argparse import Namespace
from contextlib import contextmanager
from datetime import date, datetime
import hashlib
import json
from pathlib import Path
import socket
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from market_calendar import gateway, run_guarded


HOLIDAY = datetime.fromisoformat("2026-09-14T10:00:00+05:30")
OPEN_DAY = datetime.fromisoformat("2026-09-15T10:00:00+05:30")
CIRCULAR = "https://nsearchives.nseindia.com/content/circulars/FAOP71777.pdf"


@pytest.fixture(autouse=True)
def isolated_calendar(tmp_path, monkeypatch):
    folder = tmp_path / "calendar"
    folder.mkdir()
    monkeypatch.setenv("MARKET_CALENDAR_CACHE_DIR", str(folder))
    monkeypatch.setenv("HERMES_RUNTIME_DIR", str(tmp_path / "hermes"))
    # Any accidentally reached network boundary is a test failure.
    network = Mock(side_effect=AssertionError("Network access is forbidden in gate tests"))
    monkeypatch.setattr(socket.socket, "connect", network)
    monkeypatch.setattr(socket.socket, "connect_ex", network)
    yield folder
    network.assert_not_called()


@pytest.fixture
def fresh_calendar(isolated_calendar):
    """Complete cache shape with synthetic archives, never production evidence."""
    folder = isolated_calendar
    (folder / "sources").mkdir()
    holidays = [
        "2026-01-26", "2026-03-03", "2026-03-26", "2026-03-31",
        "2026-04-03", "2026-04-14", "2026-05-01", "2026-05-28",
        "2026-06-26", "2026-09-14", "2026-10-02", "2026-10-20",
        "2026-11-10", "2026-11-24", "2026-12-25",
    ]
    sources = []
    for kind, name, url in (
        ("annual_circular", "fixture.pdf", CIRCULAR),
        ("holiday_api", "fixture.json", "https://www.nseindia.com/api/holiday-master?type=trading"),
    ):
        body = ("Synthetic test evidence: " + kind).encode()
        (folder / "sources" / name).write_bytes(body)
        sources.append({"kind": kind, "url": url, "archive_path": "sources/" + name,
                        "sha256": hashlib.sha256(body).hexdigest()})
    payload = {
        "schema_version": 1, "calendar_year": 2026, "exchange": "NSE", "segment": "FO",
        "coverage": {"start": "2026-01-01", "end": "2026-12-31", "annual_validated": True},
        "source_url": CIRCULAR, "circular_reference": "NSE/FAOP/71777",
        "fetched_at": "2026-09-14T00:00:00+00:00", "expires_at": "2026-09-16T00:00:00+00:00",
        "annual_holiday_dates": holidays, "sources": sources,
        "holidays": [{"date": day, "name": "Ganesh Chaturthi" if day == "2026-09-14" else "Fixture holiday",
                      "source_url": CIRCULAR} for day in holidays],
    }
    payload["content_sha256"] = gateway.digest_payload(payload)
    (folder / "calendar-2026.json").write_text(json.dumps(payload))
    assert gateway.decide_session(OPEN_DAY.date(), now=OPEN_DAY)["allowed"] is True
    return folder


@pytest.fixture(params=["holiday", "missing", "stale"])
def blocked_day(request, fresh_calendar):
    if request.param == "holiday":
        return HOLIDAY
    path = fresh_calendar / "calendar-2026.json"
    if request.param == "missing":
        path.unlink()
    else:
        payload = json.loads(path.read_text())
        payload["expires_at"] = "2026-09-14T12:00:00+00:00"
        payload["content_sha256"] = gateway.digest_payload(payload)
        path.write_text(json.dumps(payload))
    return OPEN_DAY


def pin_gate(monkeypatch, module, now):
    real_check = run_guarded.check_component

    def fixed_check(component, **kwargs):
        kwargs["now"] = now
        return real_check(component, **kwargs)

    check = Mock(side_effect=fixed_check)
    monkeypatch.setattr(module, "check_component", check)
    return check


def clock_at(now):
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return now.astimezone(tz) if tz is not None else now.replace(tzinfo=None)
    return Clock


@pytest.mark.parametrize("component", run_guarded.COMPONENTS)
def test_gateway_check_only_reports_closure_without_execution(component, blocked_day, monkeypatch, capsys):
    execute = Mock(side_effect=AssertionError("Must not execute a production command"))
    monkeypatch.setattr(run_guarded.os, "execv", execute)
    before = set(gateway.cache_directory().iterdir())
    code = run_guarded.main([component, "--check-only", "--at", blocked_day.isoformat()])
    result = json.loads(capsys.readouterr().out)
    assert code in (10, 11)
    assert result["allowed"] is False
    assert result["status"] == ("CLOSED" if blocked_day == HOLIDAY else "UNKNOWN")
    execute.assert_not_called()
    assert set(gateway.cache_directory().iterdir()) == before


@pytest.mark.parametrize("component", run_guarded.COMPONENTS)
def test_gateway_normal_dispatch_exits_before_command_or_credentials(component, blocked_day, monkeypatch):
    pin_gate(monkeypatch, run_guarded, blocked_day)
    command = Mock(side_effect=AssertionError("Must not resolve or launch downstream command"))
    execute = Mock(side_effect=AssertionError("Must not execute downstream command"))
    monkeypatch.setattr(run_guarded, "command_for", command)
    monkeypatch.setattr(run_guarded.os, "execv", execute)
    assert run_guarded.main([component]) == 0
    command.assert_not_called()
    execute.assert_not_called()
    saved = json.loads((gateway.cache_directory() / f"gate-{component}.json").read_text())
    assert saved["allowed"] is False


@pytest.mark.parametrize("component,hour,minute,allowed,status", [
    ("collector", 9, 14, False, "OUTSIDE_SESSION"),
    ("collector", 9, 15, True, "OPEN"),
    ("collector", 15, 30, False, "OUTSIDE_SESSION"),
    ("watchdog", 10, 0, True, "OPEN"),
    ("cleaner", 15, 44, False, "WAITING_FOR_CLEANING"),
    ("cleaner", 15, 45, True, "OPEN"),
])
def test_open_september_15_preserves_ist_component_windows(fresh_calendar, component, hour, minute, allowed, status):
    # Pass UTC to prove window selection uses IST rather than host clock time.
    from datetime import timezone
    now = OPEN_DAY.replace(hour=hour, minute=minute).astimezone(timezone.utc)
    result = run_guarded.check_component(component, now=now, record=False)
    assert (result["allowed"], result["status"]) == (allowed, status)


def test_simulation_timestamp_cannot_launch_live_job(monkeypatch):
    execute = Mock()
    monkeypatch.setattr(run_guarded.os, "execv", execute)
    with pytest.raises(SystemExit) as error:
        run_guarded.main(["collector", "--at", OPEN_DAY.isoformat()])
    assert error.value.code == 2
    execute.assert_not_called()


@pytest.mark.parametrize("mode", ["live", "replay", "history"])
def test_collector_async_cli_blocks_every_mode_before_provider_or_database(mode, blocked_day, monkeypatch):
    from hermes_v0.collector import recovery
    check = pin_gate(monkeypatch, recovery, blocked_day)
    constructors = {name: Mock(side_effect=AssertionError(name + " must not start"))
                    for name in ("StrictOpenAlgo", "RecoveryWriter", "Journal")}
    for name, mock in constructors.items():
        monkeypatch.setattr(recovery, name, mock)
    args = Namespace(status=False, replay=mode == "replay", history_date="2026-09-11" if mode == "history" else None)
    assert asyncio.run(recovery.main_async(args)) == 0
    check.assert_called_once()
    for mock in constructors.values():
        mock.assert_not_called()


def test_collector_main_blocks_before_lock(blocked_day, monkeypatch):
    from hermes_v0.collector import recovery
    pin_gate(monkeypatch, recovery, blocked_day)
    lock, downstream = Mock(), AsyncMock()
    monkeypatch.setattr(recovery, "ProcessCollectorLock", lock)
    monkeypatch.setattr(recovery, "main_async", downstream)
    monkeypatch.setattr(sys, "argv", ["recovery", "--replay"])
    assert recovery.main() == 0
    lock.assert_not_called()
    downstream.assert_not_called()


@pytest.mark.parametrize("method", ["run", "database_worker", "calculate", "finish_pending"])
def test_collector_runtime_rechecks_calendar_before_work(method, blocked_day, monkeypatch, tmp_path):
    from hermes_v0.collector import recovery
    pin_gate(monkeypatch, recovery, blocked_day)
    adapter, writer, journal = AsyncMock(), AsyncMock(), Mock()
    service = recovery.RecoveryService(adapter, writer, journal, status_path=tmp_path / "status.json")
    flush = AsyncMock()
    monkeypatch.setattr(service, "flush_once", flush)
    args = [Mock()] if method in ("run", "calculate") else []
    asyncio.run(asyncio.wait_for(getattr(service, method)(*args), timeout=1))
    adapter.start.assert_not_called()
    adapter.derived.assert_not_called()
    writer.start.assert_not_called()
    flush.assert_not_called()
    journal.put.assert_not_called()


@pytest.mark.parametrize("entrypoint", ["main", "check"])
def test_watchdog_blocks_before_lock_database_health_or_restart(entrypoint, blocked_day, monkeypatch, tmp_path):
    from hermes_v0.automation import watchdog
    pin_gate(monkeypatch, watchdog, blocked_day)
    monkeypatch.setattr(watchdog, "RUNTIME", tmp_path / "watchdog")
    lock, database, http, command = Mock(), AsyncMock(), Mock(), Mock()
    monkeypatch.setattr(watchdog, "ProcessCollectorLock", lock)
    monkeypatch.setattr(watchdog, "inspect_database", database)
    monkeypatch.setattr(watchdog.httpx, "AsyncClient", http)
    monkeypatch.setattr(watchdog.subprocess, "run", command)
    if entrypoint == "main":
        assert watchdog.main() == 0
    else:
        asyncio.run(watchdog.check())
    for mock in (lock, database, http, command):
        mock.assert_not_called()


@pytest.mark.parametrize("scheduled", [False, True])
def test_holiday_or_unknown_execution_day_blocks_historical_cleaning(scheduled, blocked_day, monkeypatch, tmp_path):
    from data_cleaning_agent import daily
    now = blocked_day.replace(hour=16)
    settings = SimpleNamespace(daily_runtime_dir=tmp_path / "cleaner", daily_ready_time="15:45:00")
    connection, keychain, s3, lock, agent = (Mock() for _ in range(5))
    for name, mock in (("_mistral_settings", keychain), ("_s3_client", s3),
                       ("single_job", lock), ("DataCleaningAgent", agent)):
        monkeypatch.setattr(daily, name, mock)
    result = daily.run_daily(settings, now=now, scheduled=scheduled, requested_date=date(2026, 9, 11),
                             planner="mistral", connection_factory=connection)
    assert result["status"] in {"SKIPPED_MARKET_HOLIDAY", "CALENDAR_UNAVAILABLE"}
    assert result["days"] == []
    for mock in (connection, keychain, s3, lock, agent):
        mock.assert_not_called()


def test_scheduled_cleaner_before_1545_does_not_catch_up(fresh_calendar, monkeypatch, tmp_path):
    from data_cleaning_agent import daily
    settings = SimpleNamespace(daily_runtime_dir=tmp_path / "cleaner", daily_ready_time="15:45:00")
    connection, keychain = Mock(), Mock()
    monkeypatch.setattr(daily, "_mistral_settings", keychain)
    result = daily.run_daily(settings, now=OPEN_DAY.replace(hour=15, minute=44), scheduled=True,
                             connection_factory=connection)
    assert result["status"] == "WAITING_FOR_CLEANING"
    connection.assert_not_called()
    keychain.assert_not_called()


def test_open_day_catchup_excludes_holiday_source_rows(fresh_calendar, monkeypatch, tmp_path):
    from data_cleaning_agent import daily
    from data_cleaning_agent.config.settings import Settings
    settings = Settings(_env_file=None, daily_runtime_dir=tmp_path / "cleaner", daily_output_dir=tmp_path / "output")
    dates = [date(2026, 9, 11), date(2026, 9, 14), date(2026, 9, 15)]
    exports = []

    @contextmanager
    def connection_factory(_):
        yield object()

    def export(_, trading_day, *args, **kwargs):
        exports.append(trading_day)
        return SimpleNamespace(row_count=0)

    monkeypatch.setattr(daily, "available_trading_dates", lambda *args: dates)
    monkeypatch.setattr(daily, "export_trading_day", export)
    result = daily.run_daily(settings, now=OPEN_DAY.replace(hour=15, minute=45), scheduled=True,
                             local_only=True, planner="deterministic", connection_factory=connection_factory)
    assert exports == [dates[0], dates[2]]
    assert {entry["trading_date"]: entry["status"] for entry in result["days"]} == {
        "2026-09-11": "NO_DATA", "2026-09-14": "SKIPPED_MARKET_HOLIDAY", "2026-09-15": "NO_DATA",
    }


def test_manual_file_cleaner_blocks_before_settings_mistral_or_upload(blocked_day, monkeypatch, tmp_path):
    from data_cleaning_agent import main
    pin_gate(monkeypatch, main, blocked_day)
    settings, agent, upload = Mock(), Mock(), Mock()
    monkeypatch.setattr(main, "Settings", settings)
    monkeypatch.setattr(main, "DataCleaningAgent", agent)
    monkeypatch.setattr(main, "publish_run", upload)
    expected_exit = 0 if gateway.decide_session(blocked_day.date(), now=blocked_day)["status"] == "CLOSED" else 2
    assert main.main([str(tmp_path / "input.csv"), "--upload"]) == expected_exit
    for mock in (settings, agent, upload):
        mock.assert_not_called()


@pytest.mark.parametrize("entrypoint", ["legacy_collector", "live_diagnostic"])
def test_legacy_public_entrypoints_cannot_bypass_gateway(entrypoint, blocked_day, monkeypatch):
    # These entrypoints deliberately import the gateway inside their function.
    pin_gate(monkeypatch, run_guarded, blocked_day)
    if entrypoint == "legacy_collector":
        from hermes_v0.collector import service as module
        call = module.run_collector()
    else:
        from hermes_v0.collector import b03_live_validation as module
        call = module._main_async(Namespace())
    adapter = Mock()
    monkeypatch.setattr(module, "OpenAlgoAdapter", adapter)
    asyncio.run(call)
    adapter.assert_not_called()


def test_collector_status_remains_available_without_calendar(monkeypatch, tmp_path, capsys):
    from hermes_v0.collector import recovery
    monkeypatch.setattr(recovery, "RUNTIME", tmp_path / "never-started")
    check = Mock(side_effect=AssertionError("Status must not require market permission"))
    monkeypatch.setattr(recovery, "check_component", check)
    assert asyncio.run(recovery.main_async(Namespace(status=True))) == 0
    assert json.loads(capsys.readouterr().out)["state"] == "NEVER_STARTED"
    check.assert_not_called()

"""Daily-run integration tests with isolated disk and no external services."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import date, datetime
import hashlib
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

from data_cleaning_agent import daily
from data_cleaning_agent.agent.cleaning_agent import DataCleaningAgent
from data_cleaning_agent.config.settings import Settings
from data_cleaning_agent.tools.postgres_tool import ExportResult


DAY = date(2026, 9, 11)
AFTER_CLOSE = datetime.fromisoformat('2026-09-11T16:00:00+05:30')


def forbidden(*args, **kwargs):
    raise AssertionError('An external service was unexpectedly invoked')


@pytest.fixture(autouse=True)
def no_external_services(monkeypatch, tmp_path):
    monkeypatch.setenv('MARKET_CALENDAR_CACHE_DIR', str(tmp_path / 'calendar'))
    monkeypatch.setattr(daily, 'decide_session', lambda day, **kwargs: {'allowed': True, 'status': 'OPEN', 'date': day.isoformat(), 'reason': 'Test calendar'})
    monkeypatch.setattr(daily, '_s3_client', forbidden)
    monkeypatch.setattr(daily.subprocess, 'run', forbidden)


@pytest.fixture
def settings(tmp_path):
    return Settings(
        _env_file=None, mistral_api_key=None, postgres_password=None,
        daily_planner='deterministic', daily_runtime_dir=tmp_path / 'runtime',
        daily_output_dir=tmp_path / 'output', daily_start_date=DAY,
        daily_ready_time='15:45:00', s3_bucket='heremesv0-cleaned-data',
        s3_prefix='cleaned', derive_greeks=False, max_quarantine_fraction=0,
    )


@pytest.mark.parametrize(('now', 'expected'), [
    ('2026-09-11T15:44:59+05:30', '2026-09-10'),
    ('2026-09-11T15:45:00+05:30', '2026-09-11'),
    ('2026-09-11T23:59:59+05:30', '2026-09-11'),
    ('2026-09-11T10:14:59+00:00', '2026-09-10'),
    ('2026-09-11T10:15:00+00:00', '2026-09-11'),
    ('2026-09-12T09:00:00+05:30', '2026-09-11'),
    ('2026-09-12T16:00:00+05:30', '2026-09-11'),
    ('2026-09-13T16:00:00+05:30', '2026-09-11'),
    ('2026-09-14T15:44:59+05:30', '2026-09-11'),
    ('2026-09-14T15:45:00+05:30', '2026-09-14'),
])
def test_completed_day_is_decided_in_ist(now, expected):
    assert daily.latest_completed_day(datetime.fromisoformat(now)) == date.fromisoformat(expected)


def test_completed_day_rejects_naive_time():
    with pytest.raises(ValueError, match='timezone-aware'):
        daily.latest_completed_day(datetime(2026, 9, 11, 16))


@pytest.mark.parametrize('now', [
    '2026-09-12T10:00:00+05:30', '2026-09-13T18:00:00+05:30',
])
def test_scheduled_weekends_skip_before_database_aws_or_keychain(settings, monkeypatch, now):
    monkeypatch.setattr(daily, '_mistral_settings', forbidden)
    monkeypatch.setattr(daily, 'read_daily_commit', forbidden)
    monkeypatch.setattr(daily, 'publish_daily_run', forbidden)
    result = daily.run_daily(
        settings, scheduled=True, now=datetime.fromisoformat(now),
        connection_factory=forbidden,
    )
    assert result['status'] == 'SKIPPED_WEEKEND'
    assert result['days'] == []
    assert json.loads((settings.daily_runtime_dir / 'last_status.json').read_text()) == result


def test_database_connection_is_read_only_repeatable_read_and_bounded(settings, monkeypatch):
    calls = []
    class Connection:
        read_only = False
        isolation_level = None
        closed = False

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.closed = True

    connection = Connection()
    def connect(**kwargs):
        calls.append(kwargs)
        return connection

    repeatable_read = object()
    monkeypatch.setitem(sys.modules, 'psycopg', SimpleNamespace(
        connect=connect, IsolationLevel=SimpleNamespace(REPEATABLE_READ=repeatable_read),
    ))
    with daily.connect_database(settings) as active:
        assert active is connection
        assert active.read_only is True
        assert active.isolation_level is repeatable_read
        assert not active.closed
    assert connection.closed
    assert len(calls) == 1
    assert calls[0]['options'] == (
        '-c default_transaction_read_only=on -c statement_timeout=120000 -c timezone=UTC'
    )
    assert calls[0]['application_name'] == 'hermes_daily_cleaner_read_only'
    assert calls[0]['connect_timeout'] == settings.postgres_connect_timeout
    assert calls[0]['host'] == settings.postgres_host
    assert calls[0]['dbname'] == settings.postgres_database
    assert 'password' not in calls[0]


@pytest.fixture
def harness(settings, monkeypatch):
    state = SimpleNamespace(
        connections=0, exports=0, cleanings=0, uploads=0, reads=0,
        commits={}, days=[DAY], fail_upload=False,
        metadata={'integrity_passed': True, 'row_count_reconciled': True},
        records=[{
            'timestamps': '2026-09-11T10:00:00+05:30', 'underlying': 'NIFTY',
            'spot': 25010, 'iv': None, 'volume': 1200, 'oi': 25000,
            'ltp': 180.5, 'strike': 25000, 'optiontype': 'CE',
            'expirydate': '2026-09-15', 'timestamp_source': 'APPLICATION_RECEIPT',
            'data_status': 'PARTIAL',
        }],
    )

    @contextmanager
    def connection_factory(configuration):
        state.connections += 1
        yield object()

    def export(connection, trading_day, output_path, **kwargs):
        state.exports += 1
        content = ''.join(json.dumps(row, sort_keys=True) + '\n' for row in state.records)
        output_path.write_text(content)
        return ExportResult(
            output_path, len(state.records), hashlib.sha256(content.encode()).hexdigest(),
            trading_day, dict(state.metadata),
        )

    class CountingAgent(DataCleaningAgent):
        def run(self, *args, **kwargs):
            state.cleanings += 1
            return super().run(*args, **kwargs)

    def read_commit(bucket, prefix, trading_day, revision, **kwargs):
        state.reads += 1
        return state.commits.get((trading_day, revision))

    def publish(run_dir, bucket, prefix, trading_day, revision, **kwargs):
        state.uploads += 1
        if state.fail_upload:
            state.fail_upload = False
            raise RuntimeError('credential-bearing-provider-error')
        manifest = json.loads((run_dir / 'manifest.json').read_text())
        assert manifest['status'] == 'PASS'
        assert manifest['source_context']['integrity_passed'] is True
        assert (run_dir / 'observations.parquet').is_file()
        assert (run_dir / 'options.parquet').is_file()
        result = {
            'commit_uri': f's3://{bucket}/{prefix}/{trading_day}/commits/{revision}.json',
            'manifest_uri': f's3://{bucket}/{prefix}/{trading_day}/{run_dir.name}/manifest.json',
            'source_sha256': manifest['source_sha256'], 'reused': False,
        }
        state.commits[(trading_day, revision)] = result
        return result

    monkeypatch.setattr(daily, 'available_trading_dates', lambda *args: state.days)
    monkeypatch.setattr(daily, 'export_trading_day', export)
    monkeypatch.setattr(daily, 'DataCleaningAgent', CountingAgent)
    monkeypatch.setattr(daily, 'read_daily_commit', read_commit)
    monkeypatch.setattr(daily, 'publish_daily_run', publish)
    monkeypatch.setattr(daily, 'pipeline_signature', lambda *args: 'a' * 64)
    state.connection_factory = connection_factory
    state.client = object()
    return state


def run(settings, harness, **kwargs):
    return daily.run_daily(
        settings, now=AFTER_CLOSE, connection_factory=harness.connection_factory,
        client=harness.client, **kwargs,
    )


def test_daily_success_persists_publication_and_remote_resume_skips_cleaning(settings, harness):
    first = run(settings, harness)
    assert first['status'] == 'COMPLETE'
    assert first['days'][0]['status'] == 'PUBLISHED'
    assert harness.cleanings == harness.uploads == 1
    progress = json.loads((settings.daily_runtime_dir / 'progress.json').read_text())
    assert progress['days'][DAY.isoformat()]['status'] == 'PUBLISHED'
    run_dir = Path(first['days'][0]['run_dir'])
    manifest = json.loads((run_dir / 'manifest.json').read_text())
    assert manifest['source_context']['source_read_only'] is True
    assert manifest['source_context']['source_isolation'] == 'REPEATABLE READ'

    # Losing local state after a committed upload must not create duplicate runs.
    (settings.daily_runtime_dir / 'progress.json').unlink()
    second = run(settings, harness)
    assert second['days'][0]['status'] == 'PUBLISHED'
    assert second['days'][0]['reused'] is True
    assert second['days'][0]['commit_uri'] == first['days'][0]['commit_uri']
    assert harness.exports == 2  # Re-read immutable source to verify the revision.
    assert harness.cleanings == harness.uploads == 1


def test_upload_failure_is_retry_pending_and_retries_successfully(settings, harness):
    harness.fail_upload = True
    first = run(settings, harness)
    assert first['status'] == 'ATTENTION_REQUIRED'
    assert first['days'][0]['status'] == 'RETRY_PENDING'
    assert first['days'][0]['error_type'] == 'RuntimeError'
    assert harness.commits == {}
    progress = json.loads((settings.daily_runtime_dir / 'progress.json').read_text())
    assert progress['days'][DAY.isoformat()]['status'] == 'RETRY_PENDING'
    assert 'credential-bearing-provider-error' not in json.dumps(first)

    second = run(settings, harness)
    assert second['days'][0]['status'] == 'PUBLISHED'
    assert second['status'] == 'COMPLETE'
    assert harness.uploads == 2


def test_source_integrity_failure_quarantines_entire_publication(settings, harness):
    harness.metadata['integrity_passed'] = False
    harness.metadata['orphan_market_rows'] = 1
    result = run(settings, harness)
    assert result['status'] == 'ATTENTION_REQUIRED'
    assert result['days'][0]['status'] == 'QUARANTINED'
    assert harness.uploads == 0
    run_dir = Path(result['days'][0]['run_dir'])
    report = json.loads((run_dir / 'quality_report.json').read_text())
    assert report['passed'] is False
    assert report['source_context']['integrity_passed'] is False
    assert not (run_dir / 'observations.parquet').exists()
    assert (run_dir / 'quarantine/observations.parquet').exists()
    again = run(settings, harness)
    assert again['days'][0]['status'] == 'QUARANTINED'
    assert again['days'][0]['reused'] is True
    assert harness.cleanings == 1


def test_integrity_metadata_change_invalidates_old_remote_publication(settings, harness):
    first = run(settings, harness)
    harness.metadata['integrity_passed'] = False
    harness.metadata['orphan_market_rows'] = 1
    second = run(settings, harness)
    assert second['days'][0]['status'] == 'QUARANTINED'
    assert second['days'][0]['revision'] != first['days'][0]['revision']
    assert harness.cleanings == 2
    assert harness.uploads == 1


def test_corrected_integrity_metadata_can_retry_old_quarantine(settings, harness):
    harness.metadata['integrity_passed'] = False
    first = run(settings, harness)
    harness.metadata['integrity_passed'] = True
    second = run(settings, harness)
    assert first['days'][0]['status'] == 'QUARANTINED'
    assert second['days'][0]['status'] == 'PUBLISHED'
    assert second['days'][0]['revision'] != first['days'][0]['revision']


def test_remote_commit_for_wrong_source_never_becomes_published(settings, harness):
    first = run(settings, harness)
    revision = first['days'][0]['revision']
    harness.commits[(DAY, revision)]['source_sha256'] = '0' * 64
    second = run(settings, harness)
    assert second['days'][0]['status'] == 'RETRY_PENDING'
    assert second['status'] == 'ATTENTION_REQUIRED'
    assert harness.uploads == 1


def test_local_only_creates_tables_without_aws_or_published_progress(settings, harness, monkeypatch):
    monkeypatch.setattr(daily, 'read_daily_commit', forbidden)
    monkeypatch.setattr(daily, 'publish_daily_run', forbidden)
    result = run(settings, harness, local_only=True)
    assert result['days'][0]['status'] == 'LOCAL_PASS'
    assert result['local_only'] is True
    assert not (settings.daily_runtime_dir / 'progress.json').exists()
    assert harness.uploads == 0


def test_changed_source_is_a_new_revision(settings, harness):
    first = run(settings, harness)
    harness.records[0]['oi'] = 25001
    second = run(settings, harness)
    assert second['days'][0]['status'] == 'PUBLISHED'
    assert second['days'][0]['revision'] != first['days'][0]['revision']
    assert harness.cleanings == harness.uploads == 2


def test_no_rows_does_not_clean_or_upload(settings, harness):
    harness.records = []
    result = run(settings, harness)
    assert result['days'][0]['status'] == 'NO_DATA'
    assert harness.cleanings == harness.uploads == harness.reads == 0


def test_lock_rejects_overlap_and_is_released(settings):
    with daily.single_job(settings.daily_runtime_dir):
        with pytest.raises(RuntimeError, match='owns the lock'):
            with daily.single_job(settings.daily_runtime_dir):
                pytest.fail('overlapping lock unexpectedly acquired')
    with daily.single_job(settings.daily_runtime_dir):
        pass


def test_mistral_missing_key_does_not_fall_back_or_open_database(settings, monkeypatch):
    calls = []
    def security(command, **kwargs):
        calls.append(command)
        return SimpleNamespace(returncode=44, stdout='', stderr='secret error text')

    monkeypatch.setattr(daily.subprocess, 'run', security)
    with pytest.raises(ValueError, match='Mistral key is unavailable'):
        daily.run_daily(
            settings, planner='mistral', now=AFTER_CLOSE,
            connection_factory=forbidden,
        )
    assert len(calls) == 1
    assert calls[0][0:2] == ['/usr/bin/security', 'find-generic-password']
    assert settings.mistral_api_key is None
    assert not (settings.daily_runtime_dir / 'progress.json').exists()


@pytest.mark.parametrize('requested', [date(2026, 9, 12), date(2026, 9, 14)])
def test_unfinished_or_weekend_manual_day_fails_before_database(settings, requested):
    with pytest.raises(ValueError, match='completed weekday'):
        daily.run_daily(settings, requested_date=requested, now=AFTER_CLOSE,
                        connection_factory=forbidden)


@pytest.mark.parametrize('requested_date', [None, DAY])
def test_explicit_rebuild_completed_runs_on_weekend_with_real_clock(settings, harness, monkeypatch, requested_date):
    now = datetime.fromisoformat('2026-09-12T10:00:00+05:30')
    calendar_calls = []

    def calendar(day, **kwargs):
        calendar_calls.append((day, kwargs['now']))
        return {'allowed': day == DAY, 'status': 'OPEN' if day == DAY else 'CLOSED'}

    monkeypatch.setattr(daily, 'decide_session', calendar)
    result = daily.run_daily(
        settings, rebuild_completed=True, requested_date=requested_date, now=now,
        connection_factory=harness.connection_factory, client=harness.client,
    )
    assert result['status'] == 'COMPLETE'
    assert result['rebuild_completed'] is True
    assert result['completed_through'] == DAY.isoformat()
    assert result['checked_at'] == now.isoformat()
    assert result['days'][0]['checked_at'] == now.isoformat()
    assert result['days'][0]['status'] == 'PUBLISHED'
    assert calendar_calls == [(DAY, now)]
    assert harness.exports == harness.cleanings == harness.uploads == 1
    manifest = json.loads((Path(result['days'][0]['run_dir']) / 'manifest.json').read_text())
    assert manifest['source_context']['source_read_only'] is True
    assert manifest['source_context']['source_isolation'] == 'REPEATABLE READ'


def test_rebuild_completed_does_not_change_immutable_revision_or_republish(settings, harness):
    original = run(settings, harness)
    rebuilt = daily.run_daily(
        settings, rebuild_completed=True, now=datetime.fromisoformat('2026-09-12T10:00:00+05:30'),
        connection_factory=harness.connection_factory, client=harness.client,
    )
    assert rebuilt['days'][0]['revision'] == original['days'][0]['revision']
    assert rebuilt['days'][0]['reused'] is True
    assert harness.exports == 2
    assert harness.cleanings == harness.uploads == 1


@pytest.mark.parametrize(('now', 'requested'), [
    ('2026-09-11T15:44:59+05:30', DAY),
    ('2026-09-12T10:00:00+05:30', date(2026, 9, 12)),
    ('2026-09-12T10:00:00+05:30', date(2026, 9, 14)),
])
def test_rebuild_completed_rejects_unfinished_current_or_future_day(settings, monkeypatch, now, requested):
    monkeypatch.setattr(daily, '_mistral_settings', forbidden)
    with pytest.raises(ValueError, match='completed weekday'):
        daily.run_daily(
            settings, rebuild_completed=True, requested_date=requested,
            now=datetime.fromisoformat(now), connection_factory=forbidden,
        )


def test_rebuild_completed_accepts_today_after_completed_cutoff(settings, harness):
    result = run(settings, harness, rebuild_completed=True, requested_date=DAY)
    assert result['days'][0]['status'] == 'PUBLISHED'
    assert result['completed_through'] == DAY.isoformat()


@pytest.mark.parametrize(('calendar_status', 'expected_status', 'summary_status'), [
    ('CLOSED', 'SKIPPED_MARKET_HOLIDAY', 'COMPLETE'),
    ('UNKNOWN', 'CALENDAR_UNAVAILABLE', 'ATTENTION_REQUIRED'),
])
def test_rebuild_completed_retains_source_calendar_gate(settings, harness, monkeypatch, calendar_status, expected_status, summary_status):
    now = datetime.fromisoformat('2026-09-12T10:00:00+05:30')
    calendar_calls = []

    def calendar(day, **kwargs):
        calendar_calls.append((day, kwargs['now']))
        return {'allowed': False, 'status': calendar_status}

    monkeypatch.setattr(daily, 'decide_session', calendar)
    result = daily.run_daily(
        settings, rebuild_completed=True, requested_date=DAY, now=now,
        connection_factory=harness.connection_factory, client=harness.client,
    )
    assert result['status'] == summary_status
    assert result['days'][0]['status'] == expected_status
    assert calendar_calls == [(DAY, now)]
    assert harness.exports == harness.cleanings == harness.reads == harness.uploads == 0


def test_rebuild_completed_forbids_scheduled_mode_before_side_effects(settings, monkeypatch):
    monkeypatch.setattr(daily, 'decide_session', forbidden)
    with pytest.raises(ValueError, match='scheduled mode'):
        daily.run_daily(settings, scheduled=True, rebuild_completed=True,
                        now=AFTER_CLOSE, connection_factory=forbidden)
    assert not settings.daily_runtime_dir.exists()


def test_rebuild_completed_respects_single_job_lock(settings, harness):
    with daily.single_job(settings.daily_runtime_dir):
        with pytest.raises(RuntimeError, match='owns the lock'):
            run(settings, harness, rebuild_completed=True)
    assert harness.connections == harness.exports == harness.uploads == 0


def test_cli_passes_rebuild_completed_explicitly(settings, monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(daily, 'Settings', lambda: settings)

    def runner(configuration, **kwargs):
        calls.append((configuration, kwargs))
        return {'status': 'COMPLETE', 'days': []}

    monkeypatch.setattr(daily, 'run_daily', runner)
    assert daily.main(['--rebuild-completed', '--date', DAY.isoformat(), '--local-only']) == 0
    assert calls == [(settings, {
        'scheduled': False, 'requested_date': DAY, 'rebuild_completed': True,
        'local_only': True, 'planner': None,
    })]
    assert json.loads(capsys.readouterr().out)['status'] == 'COMPLETE'


def test_cli_rebuild_completed_and_scheduled_are_mutually_exclusive(monkeypatch):
    monkeypatch.setattr(daily, 'run_daily', forbidden)
    with pytest.raises(SystemExit) as error:
        daily.main(['--rebuild-completed', '--scheduled'])
    assert error.value.code == 2

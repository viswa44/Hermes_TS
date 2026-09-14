"""Read completed PostgreSQL sessions and publish each source revision once."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import date, datetime, time, timedelta, timezone
import fcntl
import getpass
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
from zoneinfo import ZoneInfo

from pydantic import SecretStr
from market_calendar.gateway import decide_session
from market_calendar.run_guarded import record_gate

from .agent.cleaning_agent import DataCleaningAgent
from .config.settings import Settings
from .tools.daily_s3 import publish_daily_run, read_daily_commit
from .tools.postgres_tool import available_trading_dates, export_trading_day
from .tools.s3_tool import validate_bucket_name


IST = ZoneInfo('Asia/Kolkata')
PROJECT = Path(__file__).resolve().parent


def latest_completed_day(now: datetime, ready_time: str = '15:45:00') -> date:
    if now.tzinfo is None:
        raise ValueError('Scheduler requires timezone-aware time')
    local = now.astimezone(IST)
    result = local.date()
    if local.time() < time.fromisoformat(ready_time):
        result -= timedelta(days=1)
    while result.weekday() >= 5:
        result -= timedelta(days=1)
    return result


def _atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with tempfile.NamedTemporaryFile(mode='w', dir=path.parent, prefix='.' + path.name, delete=False) as stream:
        temporary = Path(stream.name)
        json.dump(value, stream, indent=2, sort_keys=True, allow_nan=False)
        stream.write('\n')
        stream.flush()
        import os
        os.fsync(stream.fileno())
    temporary.replace(path)


@contextmanager
def single_job(runtime_dir: Path):
    runtime_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (runtime_dir / 'daily.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError('Another daily cleaning process owns the lock') from None
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


@contextmanager
def connect_database(settings: Settings):
    import psycopg
    from psycopg import IsolationLevel
    kwargs = {
        'host': settings.postgres_host, 'port': settings.postgres_port,
        'dbname': settings.postgres_database, 'user': settings.postgres_user,
        'connect_timeout': settings.postgres_connect_timeout,
        'application_name': 'hermes_daily_cleaner_read_only',
        'options': '-c default_transaction_read_only=on -c statement_timeout=120000 -c timezone=UTC',
    }
    if settings.postgres_password:
        kwargs['password'] = settings.postgres_password.get_secret_value()
    with psycopg.connect(**kwargs) as connection:
        connection.read_only = True
        connection.isolation_level = IsolationLevel.REPEATABLE_READ
        yield connection


def pipeline_signature(settings: Settings, planner: str) -> str:
    keys = {
        'input_timezone', 'expiry_timezone', 'expiry_time', 'timestamp_unit',
        'iv_unit', 'derive_greeks', 'risk_free_rate', 'dividend_yield',
        'max_quarantine_fraction', 'mistral_model', 'postgres_host',
        'postgres_port', 'postgres_database',
    }
    digest = hashlib.sha256(json.dumps({'settings': settings.model_dump(mode='json', include=keys), 'planner': planner}, sort_keys=True).encode())
    paths = [PROJECT / 'daily.py']
    for folder in ('agent', 'tools', 'models', 'config'):
        paths.extend((PROJECT / folder).glob('*.py'))
    for path in sorted(paths):
        digest.update(str(path.relative_to(PROJECT)).encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


def _mistral_settings(settings: Settings, planner: str) -> Settings:
    if planner != 'mistral' or (settings.mistral_api_key and settings.mistral_api_key.get_secret_value().strip()):
        return settings
    result = subprocess.run(
        ['/usr/bin/security', 'find-generic-password', '-a', getpass.getuser(), '-s', settings.mistral_keychain_service, '-w'],
        capture_output=True, text=True, timeout=15, check=False,
    )
    if result.returncode != 0 or not result.stdout.strip():
        raise ValueError('Mistral key is unavailable in environment or configured Keychain item')
    return settings.model_copy(update={'mistral_api_key': SecretStr(result.stdout.strip())})


def _s3_client(settings: Settings):
    import boto3
    from botocore.config import Config
    return boto3.client('s3', region_name=settings.aws_region,
                        config=Config(connect_timeout=10, read_timeout=30, retries={'max_attempts': 2, 'mode': 'standard'}))


def run_daily(settings: Settings, *, scheduled: bool = False, requested_date: date | None = None,
              local_only: bool = False, planner: str | None = None, now: datetime | None = None,
              connection_factory=connect_database, client=None) -> dict:
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise ValueError('Scheduler requires timezone-aware time')
    local = now.astimezone(IST)
    runtime = settings.daily_runtime_dir.expanduser().resolve()
    if scheduled and local.weekday() >= 5:
        summary = {'status': 'SKIPPED_WEEKEND', 'checked_at': now.isoformat(), 'days': []}
        _atomic_json(runtime / 'last_status.json', summary)
        return summary
    market = decide_session(local.date(), now=now)
    record_gate('cleaner', dict(market, component='cleaner', checked_at=now.isoformat()))
    if market.get('allowed') is not True:
        summary = {'status': 'CALENDAR_UNAVAILABLE' if market['status'] == 'UNKNOWN' else 'SKIPPED_MARKET_HOLIDAY',
                   'checked_at': now.isoformat(), 'gateway': market, 'days': []}
        _atomic_json(runtime / 'last_status.json', summary)
        return summary
    if scheduled and local.time() < time.fromisoformat(settings.daily_ready_time):
        summary = {'status': 'WAITING_FOR_CLEANING', 'checked_at': now.isoformat(), 'gateway': market, 'days': []}
        _atomic_json(runtime / 'last_status.json', summary)
        return summary
    cutoff = latest_completed_day(now, settings.daily_ready_time)
    if requested_date and (requested_date > cutoff or requested_date.weekday() >= 5):
        raise ValueError('Requested day must be a completed weekday session')
    planner = planner or settings.daily_planner
    settings = _mistral_settings(settings, planner)
    if not local_only:
        validate_bucket_name(settings.s3_bucket)
    with single_job(runtime):
        state_path = runtime / 'progress.json'
        state = json.loads(state_path.read_text()) if state_path.exists() else {'version': 1, 'days': {}}
        if state.get('version') != 1 or not isinstance(state.get('days'), dict):
            raise ValueError('Invalid progress file; preserve it and inspect before retrying')
        beginning = requested_date or settings.daily_start_date or (cutoff - timedelta(days=settings.daily_lookback_days - 1))
        with connection_factory(settings) as connection:
            days = [requested_date] if requested_date else available_trading_dates(connection, beginning, cutoff)
        signature = pipeline_signature(settings, planner)
        entries = []
        exports = runtime / 'exports'
        exports.mkdir(exist_ok=True, mode=0o700)
        for trading_day in days:
            if trading_day.weekday() >= 5 or trading_day > cutoff:
                continue
            day_key = trading_day.isoformat()
            entry = {'trading_date': day_key, 'checked_at': now.isoformat()}
            source_market = decide_session(trading_day, now=now)
            if source_market.get('allowed') is not True:
                entry.update(status='CALENDAR_UNAVAILABLE' if source_market['status'] == 'UNKNOWN' else 'SKIPPED_MARKET_HOLIDAY', gateway=source_market)
                entries.append(entry)
                continue
            try:
                with tempfile.TemporaryDirectory(prefix=day_key + '-', dir=exports) as temporary:
                    with connection_factory(settings) as connection:
                        exported = export_trading_day(connection, trading_day, Path(temporary) / 'postgres.jsonl',
                                                      max_rows=settings.max_rows, max_bytes=settings.max_input_mb * 1024 * 1024)
                    if exported.row_count == 0:
                        entry.update(status='NO_DATA', rows=0)
                        entries.append(entry)
                        continue
                    metadata_digest = hashlib.sha256(json.dumps(exported.metadata, sort_keys=True, allow_nan=False).encode()).hexdigest()
                    revision = hashlib.sha256((exported.source_sha256 + metadata_digest + signature).encode()).hexdigest()
                    entry.update(revision=revision, source_sha256=exported.source_sha256, metadata_sha256=metadata_digest, rows=exported.row_count)
                    # Check the remote commit before cleaning. This also recovers a
                    # completed upload if the process died before saving local state.
                    if not local_only:
                        client = client or _s3_client(settings)
                        existing = read_daily_commit(settings.s3_bucket, settings.s3_prefix, trading_day, revision, client=client)
                        if existing is not None:
                            if existing.get('source_sha256') != exported.source_sha256 or exported.metadata.get('integrity_passed') is not True:
                                raise ValueError('Remote commit does not match current source integrity')
                            entry.update(status='PUBLISHED', reused=True, **{key: existing[key] for key in ('commit_uri', 'manifest_uri')})
                            state['days'][day_key] = entry
                            _atomic_json(state_path, state)
                            entries.append(entry)
                            continue
                    previous = state['days'].get(day_key, {})
                    if previous.get('revision') == revision and previous.get('status') == 'QUARANTINED':
                        entry.update(status='QUARANTINED', run_dir=previous.get('run_dir'), reused=True)
                        entries.append(entry)
                        continue
                    context = dict(exported.metadata)
                    context.update(trading_date=day_key, revision=revision,
                                   pipeline_signature=signature, metadata_sha256=metadata_digest, source_database=settings.postgres_database,
                                   source_read_only=True, source_isolation='REPEATABLE READ')
                    result = DataCleaningAgent(settings, planner=planner).run(exported.path, settings.daily_output_dir / day_key, source_context=context)
                    entry.update(run_dir=str(result.run_dir), accepted_rows=result.accepted_rows, quarantined_rows=result.quarantined_rows)
                    if not result.passed:
                        report = json.loads((result.run_dir / 'quality_report.json').read_text())
                        entry['status'] = 'RETRY_PENDING' if report.get('error_type') else 'QUARANTINED'
                        entry['error_type'] = report.get('error_type')
                    elif local_only:
                        entry['status'] = 'LOCAL_PASS'
                    else:
                        uploaded = publish_daily_run(result.run_dir, settings.s3_bucket, settings.s3_prefix,
                                                     trading_day, revision, region=settings.aws_region, client=client)
                        entry.update(status='PUBLISHED', **{key: uploaded[key] for key in ('commit_uri', 'manifest_uri', 'reused')})
                    if not local_only:
                        state['days'][day_key] = entry
                        _atomic_json(state_path, state)
            except Exception as exc:
                # Error type is sufficient for retries; SDK/DB text may contain secrets.
                entry.update(status='RETRY_PENDING', error_type=type(exc).__name__)
                if not local_only:
                    state['days'][day_key] = entry
                    _atomic_json(state_path, state)
            entries.append(entry)
        failed = any(item['status'] in {'QUARANTINED', 'RETRY_PENDING', 'CALENDAR_UNAVAILABLE'} for item in entries)
        summary = {
            'status': 'ATTENTION_REQUIRED' if failed else ('COMPLETE' if entries else 'NO_DATA'),
            'checked_at': now.isoformat(), 'completed_through': cutoff.isoformat(),
            'planner': planner, 'local_only': local_only, 'days': entries,
        }
        _atomic_json(runtime / 'last_status.json', summary)
        return summary


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description='PostgreSQL weekday cleaning and S3 publication')
    parser.add_argument('--scheduled', action='store_true', help='Weekday guard and completed-session catch-up')
    parser.add_argument('--date', type=date.fromisoformat, help='Manually process one completed weekday, YYYY-MM-DD')
    parser.add_argument('--local-only', action='store_true', help='Read PostgreSQL and clean without any AWS calls')
    parser.add_argument('--planner', choices=['deterministic', 'mistral'])
    parser.add_argument('--status', action='store_true', help='Read saved scheduler status without DB or AWS calls')
    args = parser.parse_args(argv)
    try:
        settings = Settings()
        if args.status:
            path = settings.daily_runtime_dir / 'last_status.json'
            print(path.read_text() if path.exists() else json.dumps({'status': 'NOT_RUN'}))
            return 0
        result = run_daily(settings, scheduled=args.scheduled, requested_date=args.date,
                           local_only=args.local_only, planner=args.planner)
        print(json.dumps(result, indent=2, sort_keys=True))
        return 2 if result['status'] in {'ATTENTION_REQUIRED', 'CALENDAR_UNAVAILABLE'} else 0
    except Exception as exc:
        result = {'status': 'ERROR', 'error_type': type(exc).__name__, 'checked_at': datetime.now(timezone.utc).isoformat()}
        if 'settings' in locals():
            _atomic_json(settings.daily_runtime_dir / 'last_status.json', result)
        print(json.dumps(result))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())

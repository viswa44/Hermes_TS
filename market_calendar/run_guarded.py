"""Small, credential-free entrypoint before any scheduled market job imports."""
from __future__ import annotations

import argparse
from datetime import datetime, time, timezone
import json
import os
from pathlib import Path
import tempfile

from .gateway import IST, cache_directory, decide_session

WORKSPACE = Path(__file__).resolve().parent.parent
COMPONENTS = ('collector', 'watchdog', 'cleaner')


def record_gate(component: str, decision: dict) -> None:
    """Keep current safe status, so periodic checks cannot fill logs."""
    if component not in COMPONENTS:
        raise ValueError('Unknown gateway component')
    folder = cache_directory()
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    with tempfile.NamedTemporaryFile(mode='w', dir=folder, prefix='.gate-', delete=False) as stream:
        temporary = Path(stream.name)
        json.dump(decision, stream, sort_keys=True, allow_nan=False)
        stream.write('\n')
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(folder / f'gate-{component}.json')


def check_component(component: str, *, now: datetime | None = None, enforce_time: bool = True,
                    record: bool = True) -> dict:
    if component not in COMPONENTS:
        raise ValueError('Unknown gateway component')
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise ValueError('Gateway clock must include a timezone')
    local = now.astimezone(IST)
    try:
        result = dict(decide_session(local.date(), now=now))
    except Exception as exc:
        result = {'allowed': False, 'status': 'UNKNOWN', 'reason': 'Calendar check failed: ' + type(exc).__name__, 'date': local.date().isoformat()}
    result.update(component=component, checked_at=now.isoformat())
    if result.get('allowed') is not True:
        result['allowed'] = False
    elif enforce_time:
        if component in ('collector', 'watchdog') and not time(9, 15) <= local.time() < time(15, 30):
            result.update(allowed=False, status='OUTSIDE_SESSION', reason='Collection window is 09:15–15:30 IST')
        elif component == 'cleaner' and local.time() < time(15, 45):
            result.update(allowed=False, status='WAITING_FOR_CLEANING', reason='Cleaning starts at 15:45 IST on open weekdays')
    if record:
        record_gate(component, result)
    return result


def command_for(component: str, *, workspace: Path = WORKSPACE) -> list[str]:
    if component == 'collector':
        return ['/bin/zsh', str(workspace / 'hermes_v0/automation/run_b04_option_metrics.zsh')]
    if component == 'watchdog':
        return [str(workspace / 'hermes_v0/.venv/bin/python'), '-m', 'hermes_v0.automation.watchdog']
    if component == 'cleaner':
        return [str(workspace / 'data_cleaning_agent/.venv/bin/python'), '-m', 'data_cleaning_agent.daily', '--scheduled']
    raise ValueError('Unknown gateway component')


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('component', choices=COMPONENTS)
    parser.add_argument('--check-only', action='store_true')
    parser.add_argument('--at', help='ISO timestamp, permitted only with --check-only')
    args = parser.parse_args(argv)
    if args.at and not args.check_only:
        parser.error('--at is only for read-only checks')
    result = check_component(args.component, now=datetime.fromisoformat(args.at) if args.at else None, record=not bool(args.at))
    if args.check_only:
        print(json.dumps(result, sort_keys=True))
        return 0 if result['allowed'] else 10 if result['status'] != 'UNKNOWN' else 11
    if not result['allowed']:
        return 0
    command = command_for(args.component)
    os.chdir(WORKSPACE)
    os.execv(command[0], command)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

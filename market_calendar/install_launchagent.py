"""Install the holiday gateway, calendar refresh and local dashboard schedules."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import plistlib
import shutil
import subprocess
import tempfile

WORKSPACE = Path(__file__).resolve().parent.parent
REFRESH_LABEL = 'com.openalgo.market-calendar'
DASHBOARD_LABEL = 'com.openalgo.operations-dashboard'


def definitions(workspace: Path = WORKSPACE) -> dict[str, dict]:
    workspace = Path(workspace).absolute()
    python = str(workspace / 'data_cleaning_agent/.venv/bin/python')
    common = {
        'WorkingDirectory': str(workspace),
        'EnvironmentVariables': {'PYTHONPATH': str(workspace), 'PYTHONUNBUFFERED': '1'},
        'ProcessType': 'Background', 'Umask': 0o077, 'ThrottleInterval': 60,
    }
    result = {}
    for component, label in (('collector', 'com.openalgo.hermes-v0-option-metrics'), ('watchdog', 'com.openalgo.hermes-v0-watchdog')):
        source = workspace / 'hermes_v0/automation' / (label + '.plist')
        config = plistlib.loads(source.read_bytes())
        config['ProgramArguments'] = [str(workspace / 'hermes_v0/.venv/bin/python'), '-m', 'market_calendar.run_guarded', component]
        config['WorkingDirectory'] = str(workspace)
        config.setdefault('EnvironmentVariables', {})['PYTHONPATH'] = str(workspace)
        result[label] = config
    result[REFRESH_LABEL] = dict(common, Label=REFRESH_LABEL,
        ProgramArguments=[python, '-m', 'market_calendar.refresh'],
        StartCalendarInterval={'Hour': 6, 'Minute': 0}, StartInterval=21600,
        RunAtLoad=True, KeepAlive=False,
        StandardOutPath=str(workspace / 'market_calendar/runtime/refresh.out.log'),
        StandardErrorPath=str(workspace / 'market_calendar/runtime/refresh.err.log'))
    result[DASHBOARD_LABEL] = dict(common, Label=DASHBOARD_LABEL,
        ProgramArguments=[python, '-m', 'operations_dashboard.server', '--port', '8765'],
        RunAtLoad=True, KeepAlive=True,
        StandardOutPath=str(workspace / 'operations_dashboard/runtime/server.out.log'),
        StandardErrorPath=str(workspace / 'operations_dashboard/runtime/server.err.log'))
    return result


def atomic_write(path: Path, data: bytes):
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix='.' + path.name, delete=False) as stream:
        temporary = Path(stream.name)
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


def install(*, workspace: Path = WORKSPACE, home: Path | None = None, runner=subprocess.run) -> list[Path]:
    launchctl = shutil.which('launchctl')
    if launchctl is None:
        raise RuntimeError('macOS launchctl is required')
    configs = definitions(workspace)
    directory = (home or Path.home()) / 'Library/LaunchAgents'
    directory.mkdir(parents=True, exist_ok=True)
    domain = f'gui/{os.getuid()}'
    installed = []
    def command(*args, required=True):
        value = runner([launchctl, *args], capture_output=True, text=True, check=False)
        if required and value.returncode:
            raise RuntimeError(f'launchctl {args[0]} failed ({value.returncode})')
        return value
    for label, config in configs.items():
        path = directory / (label + '.plist')
        if path.is_symlink():
            raise ValueError('Refusing a symlinked LaunchAgent')
        previous = path.read_bytes() if path.exists() else None
        if previous and plistlib.loads(previous).get('Label') != label:
            raise ValueError('Existing plist has a different label')
        for key in ('StandardOutPath', 'StandardErrorPath'):
            log = Path(config[key])
            if log.is_symlink() or log.parent.is_symlink():
                raise ValueError('Refusing symlinked logs')
            log.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            log.touch(mode=0o600, exist_ok=True)
        service = domain + '/' + label
        loaded = command('print', service, required=False).returncode == 0
        if previous:
            stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
            atomic_write(path.with_name(path.name + '.backup-' + stamp), previous)
        if loaded:
            command('bootout', service)
        try:
            atomic_write(path, plistlib.dumps(config, sort_keys=False))
            command('enable', service)
            command('bootstrap', domain, str(path))
        except Exception:
            if previous:
                atomic_write(path, previous)
                if loaded:
                    command('bootstrap', domain, str(path), required=False)
            else:
                path.unlink(missing_ok=True)
            raise
        installed.append(path)
    return installed


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--install', action='store_true')
    args = parser.parse_args(argv)
    if not args.install:
        print(json.dumps(definitions(), indent=2))
        return 0
    try:
        paths = install()
        print(json.dumps({'installed': [str(path) for path in paths], 'dashboard': 'http://127.0.0.1:8765'}))
        return 0
    except Exception as exc:
        print(json.dumps({'status': 'INSTALL_FAILED', 'error_type': type(exc).__name__}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())

"""Install the cleaning agent's own macOS LaunchAgent without storing secrets.

The calendar trigger follows the Mac's timezone. The daily runner separately
enforces Asia/Kolkata business days and its 15:45 cutoff on every invocation,
including login and half-hour catch-up triggers.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import os
from pathlib import Path
import plistlib
import shutil
import subprocess
import sys
import tempfile
from typing import Callable
from uuid import uuid4


LABEL = "com.openalgo.data-cleaning-agent"
PROJECT_DIR = Path(__file__).resolve().parents[1]


def _absolute_path(value: str | Path, name: str) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise ValueError(f"{name} must be an absolute path")
    # Keep the executable's .venv symlink: resolving it can bypass the venv.
    return Path(os.path.abspath(path))


def build_plist(
    project_dir: str | Path,
    python_executable: str | Path,
    home: str | Path,
) -> dict:
    """Build a credential-free schedule; do not create files or launch jobs."""
    project = _absolute_path(project_dir, "project_dir")
    python = _absolute_path(python_executable, "python_executable")
    _absolute_path(home, "home")
    workspace = project.parent
    return {
        "Label": LABEL,
        "ProgramArguments": [
            str(python), "-m", "market_calendar.run_guarded", "cleaner",
        ],
        "WorkingDirectory": str(workspace),
        "EnvironmentVariables": {
            "PYTHONPATH": str(workspace),
            "PYTHONUNBUFFERED": "1",
        },
        "StartCalendarInterval": [
            {"Weekday": weekday, "Hour": 15, "Minute": 45}
            for weekday in range(1, 6)
        ],
        "RunAtLoad": True,
        "StartInterval": 1800,
        "KeepAlive": False,
        "ProcessType": "Background",
        "ThrottleInterval": 60,
        "Umask": 0o077,
        "StandardOutPath": str(project / "runtime" / "weekday.out.log"),
        "StandardErrorPath": str(project / "runtime" / "weekday.err.log"),
    }


def _atomic_write(path: Path, payload: bytes) -> None:
    fd, temporary = tempfile.mkstemp(prefix=f".{LABEL}.", dir=path.parent)
    temporary_path = Path(temporary)
    try:
        with os.fdopen(fd, "wb") as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)


def _prepare_logs(project: Path) -> None:
    runtime = project / "runtime"
    if runtime.is_symlink():
        raise ValueError("runtime must be a real directory")
    runtime.mkdir(mode=0o700, parents=True, exist_ok=True)
    runtime.chmod(0o700)
    for name in ("weekday.out.log", "weekday.err.log"):
        path = runtime / name
        if path.is_symlink():
            raise ValueError("LaunchAgent log files must not be symlinks")
        flags = os.O_CREAT | os.O_APPEND | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(path, flags, 0o600)
        try:
            os.fchmod(fd, 0o600)
        finally:
            os.close(fd)


def install(
    project_dir: str | Path = PROJECT_DIR,
    python_executable: str | Path | None = None,
    home: str | Path | None = None,
    *,
    runner: Callable = subprocess.run,
) -> Path:
    """Install and load only this label in the current user's GUI domain.

    Existing settings for this label are backed up before replacement. Other
    launch agents are never inspected or changed. ``runner`` allows isolated
    tests without invoking launchctl or touching the actual user's home.
    """
    if sys.platform != "darwin":
        raise RuntimeError("LaunchAgent installation requires macOS")
    project = _absolute_path(project_dir, "project_dir")
    python = _absolute_path(python_executable or project / ".venv/bin/python", "python_executable")
    user_home = _absolute_path(home or Path.home(), "home")
    if not (project / "daily.py").is_file():
        raise ValueError("project_dir must contain data_cleaning_agent/daily.py")
    if not python.is_file() or not os.access(python, os.X_OK):
        raise ValueError("python_executable must be an existing executable")
    launchctl = shutil.which("launchctl")
    if launchctl is None:
        raise RuntimeError("launchctl was not found on PATH")

    configuration = build_plist(project, python, user_home)
    directory = user_home / "Library" / "LaunchAgents"
    path = directory / f"{LABEL}.plist"
    if path.is_symlink():
        raise ValueError("existing LaunchAgent plist must not be a symlink")
    old_payload = path.read_bytes() if path.exists() else None
    if old_payload is not None:
        try:
            old_configuration = plistlib.loads(old_payload)
        except Exception as exc:
            raise ValueError("existing LaunchAgent plist is invalid; left unchanged") from exc
        if not isinstance(old_configuration, dict) or old_configuration.get("Label") != LABEL:
            raise ValueError("existing plist belongs to another label; left unchanged")

    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    _prepare_logs(project)
    backup: Path | None = None
    if old_payload is not None:
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        backup = path.with_name(f"{path.name}.backup-{timestamp}-{uuid4().hex[:8]}")
        _atomic_write(backup, old_payload)

    domain = f"gui/{os.getuid()}"
    service = f"{domain}/{LABEL}"

    def command(*arguments: str, check: bool = True):
        result = runner(
            [launchctl, *arguments], check=False, capture_output=True, text=True,
        )
        if check and result.returncode:
            # Do not echo launchctl output: it can contain inherited env values.
            raise RuntimeError(
                f"launchctl {arguments[0]} failed with exit code {result.returncode}"
            )
        return result

    loaded = command("print", service, check=False).returncode == 0
    if loaded:
        command("bootout", service)
    try:
        _atomic_write(path, plistlib.dumps(configuration, sort_keys=False))
        command("enable", service)
        command("bootstrap", domain, str(path))
    except Exception as exc:
        # Restore the previous file and loaded service where possible. Never
        # remove the backup, which also supports manual recovery if needed.
        restored = False
        try:
            if old_payload is None:
                path.unlink(missing_ok=True)
            else:
                _atomic_write(path, old_payload)
                if loaded:
                    command("bootstrap", domain, str(path))
            restored = True
        except Exception:
            pass
        recovery = "Previous installation restored." if restored else "Automatic restoration failed."
        if backup is not None:
            recovery += f" Backup: {backup}"
        raise RuntimeError(f"LaunchAgent installation failed. {recovery}") from exc
    return path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--print", action="store_true", dest="print_plist", help="print the plist without changes")
    mode.add_argument("--install", action="store_true", help="install and start the per-user LaunchAgent")
    parser.add_argument("--project-dir", type=Path, default=PROJECT_DIR)
    parser.add_argument("--python", type=Path, dest="python_executable")
    parser.add_argument("--home", type=Path, default=Path.home())
    args = parser.parse_args(argv)
    python = args.python_executable or args.project_dir / ".venv/bin/python"
    try:
        if args.print_plist:
            configuration = build_plist(args.project_dir, python, args.home)
            sys.stdout.write(plistlib.dumps(configuration, sort_keys=False).decode("utf-8"))
        else:
            path = install(args.project_dir, python, args.home)
            print(f"Installed {path}")
            print("Calendar: Monday-Friday 15:45 Mac local time; shared gateway enforces open NSE days and the IST cutoff.")
            print("Catch-up checks: login and every 30 minutes. Check daily.py --status for publication results.")
    except (OSError, ValueError, RuntimeError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Install only the local evidence dashboard as a macOS user service."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import plistlib
import shutil
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
LABEL = "com.openalgo.evidence-dashboard"


def definition(root=ROOT):
    root = Path(root).resolve()
    return {
        "Label": LABEL,
        "ProgramArguments": [str(root / "evidence_engine/.venv/bin/python"), "-m", "evidence_dashboard.server", "--port", "8767"],
        "WorkingDirectory": str(root),
        "EnvironmentVariables": {"PYTHONPATH": str(root), "PYTHONUNBUFFERED": "1"},
        "RunAtLoad": True, "KeepAlive": True, "ThrottleInterval": 10, "Umask": 0o077,
        "ProcessType": "Background", "AbandonProcessGroup": True,
        "StandardOutPath": str(root / "evidence_dashboard/runtime/server.out.log"),
        "StandardErrorPath": str(root / "evidence_dashboard/runtime/server.err.log"),
    }


def install(root=ROOT):
    launchctl = shutil.which("launchctl")
    if not launchctl:
        raise RuntimeError("macOS launchctl is required")
    config = definition(root)
    if not Path(config["ProgramArguments"][0]).exists():
        raise ValueError("Install the evidence engine environment first")
    directory = Path.home() / "Library/LaunchAgents"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / (LABEL + ".plist")
    if path.is_symlink():
        raise ValueError("Refusing symlinked service configuration")
    previous = path.read_bytes() if path.exists() else None
    if previous and plistlib.loads(previous).get("Label") != LABEL:
        raise ValueError("Existing service has a different label")
    for key in ("StandardOutPath", "StandardErrorPath"):
        log = Path(config[key])
        if log.is_symlink() or log.parent.is_symlink():
            raise ValueError("Refusing symlinked logs")
        log.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        log.touch(mode=0o600, exist_ok=True)
    domain = f"gui/{os.getuid()}"
    service = domain + "/" + LABEL

    def call(*args, required=True):
        result = subprocess.run([launchctl, *args], capture_output=True, check=False)
        if required and result.returncode:
            raise RuntimeError(f"launchctl {args[0]} failed")
        return result.returncode == 0

    loaded = call("print", service, required=False)
    if loaded:
        call("bootout", service)
    fd, name = tempfile.mkstemp(prefix=".evidence-", dir=directory)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(plistlib.dumps(config))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
        call("enable", service)
        call("bootstrap", domain, str(path))
    except Exception:
        if previous:
            path.write_bytes(previous)
            if loaded:
                call("bootstrap", domain, str(path), required=False)
        else:
            path.unlink(missing_ok=True)
        raise
    finally:
        Path(name).unlink(missing_ok=True)
    return path


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--install", action="store_true")
    args = parser.parse_args(argv)
    if args.install:
        print(install())
        print("Evidence dashboard: http://127.0.0.1:8767")
    else:
        print(plistlib.dumps(definition()).decode())


if __name__ == "__main__":
    main()

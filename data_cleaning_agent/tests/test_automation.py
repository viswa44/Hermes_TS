"""Isolated launchd tests: no installed services or user files are changed."""

from __future__ import annotations

from pathlib import Path
import plistlib
from types import SimpleNamespace

import pytest

from data_cleaning_agent.automation import install_launchagent as automation


@pytest.fixture
def layout(tmp_path, monkeypatch):
    project = tmp_path / "workspace with spaces" / "data_cleaning_agent"
    project.mkdir(parents=True)
    (project / "daily.py").write_text("# test fixture")
    python = project / ".venv/bin/python"
    python.parent.mkdir(parents=True)
    python.write_text("#!/bin/sh\nexit 0\n")
    python.chmod(0o700)
    user_home = tmp_path / "isolated-user"
    user_home.mkdir()
    monkeypatch.setattr(automation.sys, "platform", "darwin")
    monkeypatch.setattr(automation.shutil, "which", lambda _: "/bin/launchctl")
    return project, python, user_home


class FakeLaunchctl:
    def __init__(self, *, loaded=False, fail_bootstrap=False):
        self.loaded = loaded
        self.fail_bootstrap = fail_bootstrap
        self.calls = []

    def __call__(self, command, **kwargs):
        assert kwargs == {"check": False, "capture_output": True, "text": True}
        self.calls.append(command)
        verb = command[1]
        if verb == "print":
            code = 0 if self.loaded else 113
        elif verb == "bootstrap":
            code = 5 if self.fail_bootstrap else 0
            self.fail_bootstrap = False
            self.loaded = code == 0
        elif verb == "bootout":
            code = 0
            self.loaded = False
        else:
            code = 0
        return SimpleNamespace(returncode=code, stdout="", stderr="sensitive failure detail")


def test_schedule_uses_weekdays_and_catchup_with_no_credentials(layout, monkeypatch):
    project, python, user_home = layout
    monkeypatch.setenv("MISTRAL_API_KEY", "not-for-the-plist")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "not-for-the-plist-either")
    configuration = automation.build_plist(project, python, user_home)
    assert configuration["Label"] == automation.LABEL
    assert configuration["StartCalendarInterval"] == [
        {"Weekday": day, "Hour": 15, "Minute": 45} for day in range(1, 6)
    ]
    assert configuration["StartInterval"] == 1800
    assert configuration["RunAtLoad"] is True
    assert configuration["KeepAlive"] is False
    assert configuration["Umask"] == 0o077
    assert configuration["ProgramArguments"] == [
        str(python), "-m", "market_calendar.run_guarded", "cleaner",
    ]
    assert configuration["WorkingDirectory"] == str(project.parent)
    assert configuration["EnvironmentVariables"] == {
        "PYTHONPATH": str(project.parent), "PYTHONUNBUFFERED": "1",
    }
    serialized = plistlib.dumps(configuration)
    assert b"not-for-the-plist" not in serialized
    assert plistlib.loads(serialized) == configuration
    assert not (user_home / "Library").exists()
    assert not (project / "runtime").exists()


@pytest.mark.parametrize("argument", ["project_dir", "python_executable", "home"])
def test_build_rejects_relative_paths(layout, argument):
    project, python, user_home = layout
    kwargs = dict(project_dir=project, python_executable=python, home=user_home)
    kwargs[argument] = Path("relative")
    with pytest.raises(ValueError, match="absolute path"):
        automation.build_plist(**kwargs)


def test_venv_symlink_is_not_resolved(layout):
    project, python, user_home = layout
    target = python.parent / "base-python"
    python.rename(target)
    python.symlink_to(target)
    configuration = automation.build_plist(project, python, user_home)
    assert configuration["ProgramArguments"][0] == str(python)
    assert configuration["ProgramArguments"][0] != str(target)


def test_new_install_prepares_restricted_logs_and_only_loads_own_label(layout):
    project, python, user_home = layout
    runner = FakeLaunchctl()
    path = automation.install(project, python, user_home, runner=runner)
    assert path == user_home / "Library/LaunchAgents" / f"{automation.LABEL}.plist"
    assert plistlib.loads(path.read_bytes()) == automation.build_plist(project, python, user_home)
    assert path.stat().st_mode & 0o777 == 0o600
    assert (project / "runtime").stat().st_mode & 0o777 == 0o700
    for name in ("weekday.out.log", "weekday.err.log"):
        assert (project / "runtime" / name).stat().st_mode & 0o777 == 0o600
    assert [call[1] for call in runner.calls] == ["print", "enable", "bootstrap"]
    assert runner.calls[-1][-1] == str(path)
    assert runner.calls[-1][-2] == f"gui/{automation.os.getuid()}"
    assert all("hermes-v0-option-metrics" not in " ".join(call) for call in runner.calls)


def test_update_backs_up_existing_plist_before_reloading_own_label(layout):
    project, python, user_home = layout
    runner = FakeLaunchctl()
    path = automation.install(project, python, user_home, runner=runner)
    old = path.read_bytes()
    runner.calls.clear()
    automation.install(project, python, user_home, runner=runner)
    backups = list(path.parent.glob(f"{path.name}.backup-*"))
    assert len(backups) == 1
    assert backups[0].read_bytes() == old
    assert backups[0].stat().st_mode & 0o777 == 0o600
    assert [call[1] for call in runner.calls] == ["print", "bootout", "enable", "bootstrap"]
    assert runner.calls[1][2] == f"gui/{automation.os.getuid()}/{automation.LABEL}"


def test_failed_update_restores_previous_file_and_service_without_secret_output(layout):
    project, python, user_home = layout
    path = automation.install(project, python, user_home, runner=FakeLaunchctl())
    old = path.read_bytes()
    runner = FakeLaunchctl(loaded=True, fail_bootstrap=True)
    with pytest.raises(RuntimeError, match="Previous installation restored") as failure:
        automation.install(project, python, user_home, runner=runner)
    assert "sensitive failure detail" not in str(failure.value)
    assert path.read_bytes() == old
    assert runner.loaded
    assert [call[1] for call in runner.calls].count("bootstrap") == 2


def test_failed_new_install_removes_unloaded_plist(layout):
    project, python, user_home = layout
    runner = FakeLaunchctl(fail_bootstrap=True)
    with pytest.raises(RuntimeError, match="installation failed"):
        automation.install(project, python, user_home, runner=runner)
    assert not (user_home / "Library/LaunchAgents" / f"{automation.LABEL}.plist").exists()
    assert not runner.loaded


def test_existing_unrelated_plist_is_left_unchanged(layout):
    project, python, user_home = layout
    directory = user_home / "Library/LaunchAgents"
    directory.mkdir(parents=True)
    path = directory / f"{automation.LABEL}.plist"
    payload = plistlib.dumps({"Label": "unrelated-agent"})
    path.write_bytes(payload)
    runner = FakeLaunchctl()
    with pytest.raises(ValueError, match="another label"):
        automation.install(project, python, user_home, runner=runner)
    assert path.read_bytes() == payload
    assert not runner.calls


@pytest.mark.parametrize("target", ["runtime", "plist", "stdout"])
def test_symlink_targets_are_not_modified(layout, target):
    project, python, user_home = layout
    elsewhere = user_home / "elsewhere"
    elsewhere.mkdir()
    if target == "runtime":
        (project / "runtime").symlink_to(elsewhere, target_is_directory=True)
    elif target == "plist":
        directory = user_home / "Library/LaunchAgents"
        directory.mkdir(parents=True)
        payload = elsewhere / "keep.plist"
        payload.write_bytes(plistlib.dumps({"Label": automation.LABEL}))
        (directory / f"{automation.LABEL}.plist").symlink_to(payload)
    else:
        (project / "runtime").mkdir()
        payload = elsewhere / "keep.log"
        payload.write_text("retain")
        (project / "runtime/weekday.out.log").symlink_to(payload)
    before = {path.name: path.read_bytes() for path in elsewhere.iterdir()}
    runner = FakeLaunchctl()
    with pytest.raises(ValueError):
        automation.install(project, python, user_home, runner=runner)
    assert {path.name: path.read_bytes() for path in elsewhere.iterdir()} == before
    assert not runner.calls


def test_print_mode_has_no_side_effects(layout, capsys):
    project, python, user_home = layout
    code = automation.main([
        "--print", "--project-dir", str(project), "--python", str(python),
        "--home", str(user_home),
    ])
    assert code == 0
    assert plistlib.loads(capsys.readouterr().out.encode())["Label"] == automation.LABEL
    assert not (user_home / "Library").exists()
    assert not (project / "runtime").exists()

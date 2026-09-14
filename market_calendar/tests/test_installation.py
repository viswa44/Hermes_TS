from pathlib import Path
import plistlib
from types import SimpleNamespace

import pytest

from market_calendar import install_launchagent as installer


@pytest.fixture
def workspace(tmp_path):
    source = tmp_path / "workspace/hermes_v0/automation"
    source.mkdir(parents=True)
    for label in ("com.openalgo.hermes-v0-option-metrics", "com.openalgo.hermes-v0-watchdog"):
        path = source / (label + ".plist")
        path.write_bytes(plistlib.dumps({"Label": label, "ProgramArguments": ["old"],
            "StartInterval": 20, "KeepAlive": False,
            "StandardOutPath": str(tmp_path / "logs" / (label + ".out")),
            "StandardErrorPath": str(tmp_path / "logs" / (label + ".err"))}))
    return tmp_path / "workspace"


def test_market_jobs_use_gateway_and_control_services_stay_available(workspace):
    configs = installer.definitions(workspace)
    for label, component in (("com.openalgo.hermes-v0-option-metrics", "collector"), ("com.openalgo.hermes-v0-watchdog", "watchdog")):
        assert configs[label]["ProgramArguments"][-2:] == ["market_calendar.run_guarded", component]
        assert configs[label]["StartInterval"] == 20
    refresh = configs[installer.REFRESH_LABEL]
    assert refresh["StartCalendarInterval"] == {"Hour": 6, "Minute": 0}
    assert refresh["StartInterval"] == 21600
    assert "market_calendar.refresh" in refresh["ProgramArguments"]
    assert configs[installer.DASHBOARD_LABEL]["KeepAlive"] is True


def test_bootstrap_failure_restores_previous_plist(workspace, tmp_path, monkeypatch):
    monkeypatch.setattr(installer.shutil, "which", lambda _: "/test/launchctl")
    directory = tmp_path / "home/Library/LaunchAgents"
    directory.mkdir(parents=True)
    label = "com.openalgo.hermes-v0-option-metrics"
    path = directory / (label + ".plist")
    before = plistlib.dumps({"Label": label, "ProgramArguments": ["original"]})
    path.write_bytes(before)
    calls = []
    def run(argv, **kwargs):
        calls.append(argv)
        return SimpleNamespace(returncode=1 if argv[1] == "bootstrap" and sum(c[1] == "bootstrap" for c in calls) == 1 else 0)
    with pytest.raises(RuntimeError):
        installer.install(workspace=workspace, home=tmp_path / "home", runner=run)
    assert path.read_bytes() == before
    assert len(list(directory.glob("*.backup-*"))) == 1
    assert [c[1] for c in calls] == ["print", "bootout", "enable", "bootstrap", "bootstrap"]

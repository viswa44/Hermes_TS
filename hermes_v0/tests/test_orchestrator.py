from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
CONTROL = ROOT / "Agent_Control"


def command(control_dir: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(CONTROL / "orchestrator.py"), "--control-dir", str(control_dir), *args],
        text=True, capture_output=True, check=False,
    )


def fixture(tmp_path: Path) -> Path:
    control = tmp_path / "Agent_Control"
    control.mkdir()
    shutil.copy(CONTROL / "orchestrator.py", control)
    shutil.copy(CONTROL / "handoff.py", control)
    shutil.copy(CONTROL / "event_logger.py", control)
    (control / "state.yaml").write_text(yaml.safe_dump({"project": {"status": "READY", "current_task": "B01", "active_agent": None, "attempt": 1, "max_attempts": 3}, "agents": {"BUILD-001": {"status": "READY"}, "QA-001": {"status": "READY"}}}))
    (control / "queue.yaml").write_text(yaml.safe_dump({"tasks": [
        {"id": "B01", "name": "Connectivity", "build_agent": "BUILD-001", "qa_agent": "QA-001", "qa_task": "Q01", "status": "READY", "max_attempts": 3},
        {"id": "B02", "name": "Index fetch", "build_agent": "BUILD-001", "qa_agent": "QA-001", "qa_task": "Q02", "status": "PENDING", "max_attempts": 3},
        {"id": "B03", "name": "Next task", "build_agent": "BUILD-001", "qa_agent": "QA-001", "qa_task": "Q03", "status": "PENDING", "max_attempts": 3},
    ]}))
    return control


def complete(control: Path, task_id: str, agent: str, result: str) -> subprocess.CompletedProcess[str]:
    report = control.parent / f"{task_id}-{agent}-{result}.md"
    report.write_text(f"# {agent}\n\nResult: {result}\n\nDefects: none\n")
    return command(control, "complete", task_id, agent, result, "--report", str(report))


def test_pass_automatically_starts_next_task(tmp_path: Path) -> None:
    control = fixture(tmp_path)
    assert command(control, "run", "B01").returncode == 0
    assert complete(control, "B01", "BUILD-001", "HANDOFF_READY").returncode == 0
    assert complete(control, "B01", "QA-001", "PASS").returncode == 0
    queue = yaml.safe_load((control / "queue.yaml").read_text())
    state = yaml.safe_load((control / "state.yaml").read_text())
    assert [task["status"] for task in queue["tasks"]] == ["APPROVED", "IN_PROGRESS", "PENDING"]
    assert state["project"]["current_task"] == "B02"
    assert state["project"]["active_agent"] == "BUILD-001"
    assert state["project"]["attempt"] == 1
    assert state["agents"]["BUILD-001"]["status"] == "ACTIVE"
    assert (control / "logs" / "B02" / "BUILD-001" / "B02_attempt_1.md").is_file()


def test_each_completed_task_repeats_the_build_qa_loop(tmp_path: Path) -> None:
    control = fixture(tmp_path)
    assert command(control, "run", "B01").returncode == 0
    assert complete(control, "B01", "BUILD-001", "HANDOFF_READY").returncode == 0
    assert complete(control, "B01", "QA-001", "PASS").returncode == 0
    assert complete(control, "B02", "BUILD-001", "HANDOFF_READY").returncode == 0
    assert complete(control, "B02", "QA-001", "PASS").returncode == 0

    queue = yaml.safe_load((control / "queue.yaml").read_text())
    state = yaml.safe_load((control / "state.yaml").read_text())
    assert [task["status"] for task in queue["tasks"]] == ["APPROVED", "APPROVED", "IN_PROGRESS"]
    assert state["project"]["current_task"] == "B03"
    assert state["project"]["active_agent"] == "BUILD-001"
    assert (control / "logs" / "B03" / "BUILD-001" / "B03_attempt_1.md").is_file()


def test_explicit_rework_restarts_a_blocked_task_at_next_attempt(tmp_path: Path) -> None:
    control = fixture(tmp_path)
    assert command(control, "run", "B01").returncode == 0
    assert complete(control, "B01", "BUILD-001", "HANDOFF_READY").returncode == 0
    assert complete(control, "B01", "QA-001", "PASS").returncode == 0
    assert complete(control, "B02", "BUILD-001", "HANDOFF_READY").returncode == 0
    assert complete(control, "B02", "QA-001", "BLOCKED").returncode == 0

    assert command(control, "rework", "B02").returncode == 0
    state = yaml.safe_load((control / "state.yaml").read_text())
    queue = yaml.safe_load((control / "queue.yaml").read_text())
    assert state["project"]["attempt"] == 2
    assert state["project"]["active_agent"] == "BUILD-001"
    assert queue["tasks"][1]["status"] == "IN_PROGRESS"
    assert (control / "logs" / "B02" / "BUILD-001" / "B02_attempt_2.md").is_file()


def test_qa_failure_reworks_then_blocks_at_cap(tmp_path: Path) -> None:
    control = fixture(tmp_path)
    assert command(control, "run", "B01").returncode == 0
    for attempt in range(1, 4):
        assert complete(control, "B01", "BUILD-001", "HANDOFF_READY").returncode == 0
        result = complete(control, "B01", "QA-001", "FAIL")
        assert result.returncode == 0
        if attempt < 3:
            assert "B01-FIX" in (control / "logs" / "B01" / "BUILD-001" / f"B01_attempt_{attempt + 1}.md").read_text()
    state = yaml.safe_load((control / "state.yaml").read_text())
    queue = yaml.safe_load((control / "queue.yaml").read_text())
    assert state["project"]["status"] == "BLOCKED"
    assert queue["tasks"][0]["status"] == "BLOCKED"


def test_resume_deferred_task_defers_current_work_without_two_active_agents(tmp_path: Path) -> None:
    control = fixture(tmp_path)
    state = yaml.safe_load((control / "state.yaml").read_text())
    queue = yaml.safe_load((control / "queue.yaml").read_text())
    state["project"].update(current_task="B02", active_agent="BUILD-001", attempt=1)
    state["agents"]["BUILD-001"]["status"] = "ACTIVE"
    queue["tasks"][0]["status"] = "DEFERRED"
    queue["tasks"][1]["status"] = "IN_PROGRESS"
    (control / "state.yaml").write_text(yaml.safe_dump(state))
    (control / "queue.yaml").write_text(yaml.safe_dump(queue))

    resumed = command(control, "resume", "B01")

    assert resumed.returncode == 0, resumed.stderr
    state = yaml.safe_load((control / "state.yaml").read_text())
    queue = yaml.safe_load((control / "queue.yaml").read_text())
    assert [task["status"] for task in queue["tasks"]] == ["IN_PROGRESS", "DEFERRED", "PENDING"]
    assert state["project"]["current_task"] == "B01"
    assert state["project"]["active_agent"] == "BUILD-001"
    assert state["agents"]["BUILD-001"]["status"] == "ACTIVE"
    assert (control / "logs" / "B01" / "BUILD-001" / "B01_attempt_1.md").is_file()


def test_pass_after_resume_returns_to_interrupted_task_before_pending_work(tmp_path: Path) -> None:
    control = fixture(tmp_path)
    state = yaml.safe_load((control / "state.yaml").read_text())
    queue = yaml.safe_load((control / "queue.yaml").read_text())
    state["project"].update(current_task="B02", active_agent="BUILD-001", attempt=1)
    state["agents"]["BUILD-001"]["status"] = "ACTIVE"
    queue["tasks"][0]["status"] = "DEFERRED"
    queue["tasks"][1]["status"] = "IN_PROGRESS"
    (control / "state.yaml").write_text(yaml.safe_dump(state))
    (control / "queue.yaml").write_text(yaml.safe_dump(queue))

    assert command(control, "resume", "B01").returncode == 0
    assert complete(control, "B01", "BUILD-001", "HANDOFF_READY").returncode == 0
    assert complete(control, "B01", "QA-001", "PASS").returncode == 0

    state = yaml.safe_load((control / "state.yaml").read_text())
    queue = yaml.safe_load((control / "queue.yaml").read_text())
    assert [task["status"] for task in queue["tasks"]] == ["APPROVED", "IN_PROGRESS", "PENDING"]
    assert state["project"]["current_task"] == "B02"
    assert state["project"]["active_agent"] == "BUILD-001"

"""Prompt construction and handoff-artifact creation for the controller."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def report_path(control_dir: Path, task: dict[str, Any], agent_id: str, attempt: int) -> Path:
    name = task["qa_task"] if agent_id == task["qa_agent"] else task["id"]
    # A task may have an explicitly resumed validation phase (B03B).  Keep its
    # artifact distinct from the earlier phase instead of overwriting evidence.
    if phase := task.get("handoff_phase"):
        name = f"{name}_{phase}"
    return control_dir / "logs" / task["id"] / agent_id / f"{name}_attempt_{attempt}.md"


def write_handoff(control_dir: Path, task: dict[str, Any], agent_id: str, attempt: int) -> Path:
    """Write the exact bounded prompt for the next agent; never execute it."""
    is_qa = agent_id == task["qa_agent"]
    artifact = report_path(control_dir, task, agent_id, attempt)
    artifact.parent.mkdir(parents=True, exist_ok=True)
    previous_qa = report_path(control_dir, task, task["qa_agent"], attempt - 1)
    latest_build = report_path(control_dir, task, task["build_agent"], attempt)
    requirement = task.get("requirement", task["name"])
    if is_qa:
        body = f"""You are {agent_id}.

TASK: {task['qa_task']} attempt {attempt}
Target: {task['build_agent']} / {task['id']}
Requirement: {requirement}

Read the latest BUILD report: {latest_build}
Read previous QA defects when present: {previous_qa}

Retest all previously failed cases and critical {task['id']} regression cases.
Do not fix anything. Do not begin another task.

Return exactly one terminal status: PASS, FAIL, or BLOCKED.
Include defects, tests run, observed results, and unresolved blockers.
"""
    else:
        fix_only = attempt > 1
        title = f"{task['id']}-FIX-{attempt:02d}" if fix_only else task["id"]
        scope = (f"Fix ONLY defects in {previous_qa}. Do not expand scope." if fix_only
                 else "Implement only this task. Do not begin the next task.")
        body = f"""You are {agent_id}.

TASK: {title}
Requirement: {requirement}

{scope}

After work, run the {task['id']} tests and report files modified, tests run, and unresolved defects.
Return exactly one terminal status: HANDOFF_READY or BLOCKED.
"""
    artifact.write_text(
        f"# Controller handoff\n\nCreated: {datetime.now(UTC).isoformat(timespec='seconds')}\n\n{body}\nSave your final report to: {artifact}\n",
        encoding="utf-8",
    )
    return artifact

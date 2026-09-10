"""Controlled BUILD -> QA orchestration for Hermes V0 tasks.

This program intentionally creates handoff artifacts rather than launching agents.
An external agent runner must submit its terminal result with ``complete``.
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path
from typing import Any

import yaml

try:  # Supports both ``python orchestrator.py`` and ``python -m Agent_Control.orchestrator``.
    from .event_logger import log_event
    from .handoff import report_path, write_handoff
except ImportError:  # pragma: no cover - exercised by direct CLI invocation.
    from event_logger import log_event
    from handoff import report_path, write_handoff


CONTROL_DIR = Path(__file__).resolve().parent
BUILD_RESULTS = {"HANDOFF_READY", "BLOCKED"}
QA_RESULTS = {"PASS", "FAIL", "BLOCKED"}


def load_yaml(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as stream:
        return yaml.safe_load(stream) or {}


def save_yaml(path: Path, data: dict[str, Any]) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as stream:
        yaml.safe_dump(data, stream, sort_keys=False)
    tmp.replace(path)


def task_by_id(queue: dict[str, Any], task_id: str) -> dict[str, Any]:
    for task in queue.get("tasks", []):
        if task.get("id") == task_id:
            return task
    raise ValueError(f"Unknown task: {task_id}")


def dispatch(control_dir: Path, state: dict[str, Any], queue: dict[str, Any], task: dict[str, Any], agent: str) -> Path:
    attempt = int(state["project"]["attempt"])
    artifact = write_handoff(control_dir, task, agent, attempt)
    project = state["project"]
    project.update(current_task=task["id"], active_agent=agent,
                   status="REWORK" if agent == task["build_agent"] and attempt > 1 else "READY")
    state.setdefault("agents", {}).setdefault(agent, {})["status"] = "ACTIVE"
    save_yaml(control_dir / "state.yaml", state)
    # The dispatcher is also used by the automatic QA-PASS transition.  Persist
    # the queue here so the next task cannot be left READY after it is handed off.
    save_yaml(control_dir / "queue.yaml", queue)
    log_event(control_dir, "START", task_id=task["id"], agent_id=agent, attempt=attempt, handoff=str(artifact))
    return artifact


def run(control_dir: Path, task_id: str) -> Path:
    state, queue = load_yaml(control_dir / "state.yaml"), load_yaml(control_dir / "queue.yaml")
    task = task_by_id(queue, task_id)
    project = state["project"]
    if task.get("status") not in {"READY", "REWORK"}:
        raise ValueError(f"{task_id} is {task.get('status')}, not READY/REWORK")
    if project.get("active_agent"):
        raise ValueError(f"{project['active_agent']} is already active")
    task["status"] = "IN_PROGRESS"
    project.update(current_task=task_id, attempt=int(project.get("attempt", 1)),
                   max_attempts=int(task.get("max_attempts", 3)))
    save_yaml(control_dir / "queue.yaml", queue)
    return dispatch(control_dir, state, queue, task, task["build_agent"])


def rework(control_dir: Path, task_id: str) -> Path:
    """Return a blocked BUILD/QA task to its next controlled build attempt."""
    state, queue = load_yaml(control_dir / "state.yaml"), load_yaml(control_dir / "queue.yaml")
    task = task_by_id(queue, task_id)
    project = state["project"]
    if task.get("status") != "BLOCKED":
        raise ValueError(f"{task_id} is {task.get('status')}, not BLOCKED")
    if project.get("active_agent"):
        raise ValueError(f"{project['active_agent']} is already active")
    next_attempt = int(project.get("attempt", 1)) + 1
    if next_attempt > int(task.get("max_attempts", 3)):
        raise ValueError(f"{task_id} has exhausted its {task.get('max_attempts', 3)} attempts")
    # A rework is explicit and auditable: no blocked task is silently resumed.
    task["status"] = "REWORK"
    project.update(status="READY", current_task=task_id, attempt=next_attempt,
                   max_attempts=int(task.get("max_attempts", 3)))
    state.setdefault("agents", {}).setdefault(task["build_agent"], {})["status"] = "READY"
    log_event(control_dir, "REWORK", task_id=task_id, attempt=next_attempt,
              reason="explicit rework request")
    save_yaml(control_dir / "state.yaml", state)
    save_yaml(control_dir / "queue.yaml", queue)
    return run(control_dir, task_id)


def defer(control_dir: Path, task_id: str) -> Path:
    """Defer the active task and dispatch the next configured BUILD task."""
    state, queue = load_yaml(control_dir / "state.yaml"), load_yaml(control_dir / "queue.yaml")
    task = task_by_id(queue, task_id)
    project = state["project"]
    if project.get("current_task") != task_id or task.get("status") != "IN_PROGRESS":
        raise ValueError(f"{task_id} is not the active IN_PROGRESS task")
    active_agent = project.get("active_agent")
    if active_agent:
        state.setdefault("agents", {}).setdefault(active_agent, {})["status"] = "DEFERRED"
    task["status"] = "DEFERRED"
    project.update(status="READY", active_agent=None)
    next_task = next(
        (item for item in queue["tasks"] if item.get("status") == "PENDING"
         and item.get("build_agent") and item.get("qa_agent") and item.get("qa_task")),
        None,
    )
    if not next_task:
        save_yaml(control_dir / "state.yaml", state)
        save_yaml(control_dir / "queue.yaml", queue)
        log_event(control_dir, "DEFERRED", task_id=task_id, reason="explicit deferral request")
        return project["status"]
    next_task["status"] = "IN_PROGRESS"
    project.update(current_task=next_task["id"], attempt=1,
                   max_attempts=int(next_task.get("max_attempts", 3)))
    log_event(control_dir, "DEFERRED", task_id=task_id, reason="explicit deferral request")
    log_event(control_dir, "READY", task_id=next_task["id"], reason=f"{task_id} deferred")
    save_yaml(control_dir / "state.yaml", state)
    save_yaml(control_dir / "queue.yaml", queue)
    return str(dispatch(control_dir, state, queue, next_task, next_task["build_agent"]))


def resume(control_dir: Path, task_id: str) -> Path:
    """Prioritize an explicitly deferred task without creating two active agents.

    This is intentionally distinct from ``run`` and ``rework``: it preserves a
    deferred task's current attempt and records the displaced active task as
    DEFERRED before it hands BUILD-001 the resumed task.
    """
    state, queue = load_yaml(control_dir / "state.yaml"), load_yaml(control_dir / "queue.yaml")
    target = task_by_id(queue, task_id)
    project = state["project"]
    if target.get("status") != "DEFERRED":
        raise ValueError(f"{task_id} is {target.get('status')}, not DEFERRED")
    current_id = project.get("current_task")
    if not current_id:
        raise ValueError("No current task is available to defer before resuming")
    current = task_by_id(queue, current_id)
    if current_id == task_id or current.get("status") != "IN_PROGRESS":
        raise ValueError(f"{current_id} is not an active IN_PROGRESS task")

    active_agent = project.get("active_agent")
    if active_agent:
        state.setdefault("agents", {}).setdefault(active_agent, {})["status"] = "DEFERRED"
    current["status"] = "DEFERRED"
    # Preserve the interrupted work's position in the controlled sequence.
    # After B03B passes Q03, it must return to B04 rather than skip to B05.
    current["resume_after"] = task_id
    target["status"] = "IN_PROGRESS"
    project.update(
        status="READY",
        current_task=task_id,
        active_agent=None,
        attempt=int(project.get("attempt", 1)),
        max_attempts=int(target.get("max_attempts", 3)),
    )
    log_event(control_dir, "DEFERRED", task_id=current_id, reason=f"explicit resume of {task_id}")
    log_event(control_dir, "RESUMED", task_id=task_id, reason=f"explicit priority over {current_id}")
    save_yaml(control_dir / "state.yaml", state)
    save_yaml(control_dir / "queue.yaml", queue)
    return dispatch(control_dir, state, queue, target, target["build_agent"])


def complete(control_dir: Path, task_id: str, agent: str, result: str, report: Path | None) -> str:
    state, queue = load_yaml(control_dir / "state.yaml"), load_yaml(control_dir / "queue.yaml")
    task = task_by_id(queue, task_id)
    project = state["project"]
    if project.get("current_task") != task_id or project.get("active_agent") != agent:
        raise ValueError("Completion does not match the active task and agent")
    allowed = BUILD_RESULTS if agent == task["build_agent"] else QA_RESULTS if agent == task["qa_agent"] else set()
    if result not in allowed:
        raise ValueError(f"{agent} cannot submit {result}")
    if report is None:
        raise ValueError("A terminal result requires --report for auditability and the next handoff")
    if not report.is_file():
        raise ValueError(f"Report does not exist: {report}")
    destination = report_path(control_dir, task, agent, int(project["attempt"]))
    if report.resolve() != destination.resolve():
        # Preserve the controller handoff at its standard destination when a
        # caller supplies a separate report, but never overwrite a completed
        # report that is already saved at that required path.
        write_handoff(control_dir, task, agent, int(project["attempt"]))
        shutil.copyfile(report, destination)
    project["active_agent"] = None
    state["agents"][agent]["status"] = result
    log_event(control_dir, result, task_id=task_id, agent_id=agent, attempt=project["attempt"], report=str(report) if report else None)
    if result == "BLOCKED":
        task["status"] = "BLOCKED"
        project["status"] = "BLOCKED"
    elif agent == task["build_agent"]:  # HANDOFF_READY
        save_yaml(control_dir / "state.yaml", state)
        save_yaml(control_dir / "queue.yaml", queue)
        return str(dispatch(control_dir, state, queue, task, task["qa_agent"]))
    elif result == "PASS":
        task["status"] = "APPROVED"
        project["status"] = "APPROVED"
        # Only tasks governed by this BUILD -> QA controller may be dispatched.
        # First resume work explicitly displaced by this task (B04 after B03B),
        # then keep unrelated roadmap entries pending while continuing B tasks.
        next_task = next(
            (
                item for item in queue["tasks"]
                if item.get("status") == "DEFERRED"
                and item.get("resume_after") == task_id
                and item.get("build_agent")
                and item.get("qa_agent")
                and item.get("qa_task")
            ),
            None,
        )
        if next_task is None:
            next_task = next(
                (
                    item for item in queue["tasks"]
                    if item.get("status") == "PENDING"
                    and item.get("build_agent")
                    and item.get("qa_agent")
                    and item.get("qa_task")
                ),
                None,
            )
        if next_task:
            # A QA PASS approves the completed task and immediately begins the
            # next configured build task.  Each task starts at attempt one.
            next_task["status"] = "IN_PROGRESS"
            next_task.pop("resume_after", None)
            project.update(
                current_task=next_task["id"],
                attempt=1,
                max_attempts=int(next_task.get("max_attempts", 3)),
            )
            log_event(control_dir, "READY", task_id=next_task["id"], reason=f"{task_id} QA PASS")
            save_yaml(control_dir / "state.yaml", state)
            save_yaml(control_dir / "queue.yaml", queue)
            return str(dispatch(control_dir, state, queue, next_task, next_task["build_agent"]))
    else:  # QA FAIL
        if int(project["attempt"]) >= int(task.get("max_attempts", 3)):
            task["status"] = "BLOCKED"
            project["status"] = "BLOCKED"
            log_event(control_dir, "BLOCKED", task_id=task_id, reason="maximum QA attempts reached")
        else:
            project["attempt"] = int(project["attempt"]) + 1
            task["status"] = "REWORK"
            save_yaml(control_dir / "state.yaml", state)
            save_yaml(control_dir / "queue.yaml", queue)
            return str(dispatch(control_dir, state, queue, task, task["build_agent"]))
    save_yaml(control_dir / "state.yaml", state)
    save_yaml(control_dir / "queue.yaml", queue)
    return project["status"]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--control-dir", type=Path, default=CONTROL_DIR)
    sub = parser.add_subparsers(dest="command", required=True)
    run_parser = sub.add_parser("run"); run_parser.add_argument("task_id")
    done = sub.add_parser("complete")
    done.add_argument("task_id"); done.add_argument("agent_id"); done.add_argument("result")
    done.add_argument("--report", type=Path)
    rework_parser = sub.add_parser("rework")
    rework_parser.add_argument("task_id")
    defer_parser = sub.add_parser("defer")
    defer_parser.add_argument("task_id")
    resume_parser = sub.add_parser("resume")
    resume_parser.add_argument("task_id")
    sub.add_parser("status")
    args = parser.parse_args()
    try:
        if args.command == "run": output = run(args.control_dir, args.task_id)
        elif args.command == "rework": output = rework(args.control_dir, args.task_id)
        elif args.command == "defer": output = defer(args.control_dir, args.task_id)
        elif args.command == "resume": output = resume(args.control_dir, args.task_id)
        elif args.command == "complete": output = complete(args.control_dir, args.task_id, args.agent_id, args.result, args.report)
        else: output = yaml.safe_dump({"state": load_yaml(args.control_dir / "state.yaml"), "queue": load_yaml(args.control_dir / "queue.yaml")}, sort_keys=False)
        print(output)
        return 0
    except ValueError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

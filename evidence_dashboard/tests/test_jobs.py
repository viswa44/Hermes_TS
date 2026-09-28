"""Dashboard controls exercised with temporary evidence and inert child workers."""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest

from evidence_dashboard.jobs import JobConflict, JobController, atomic_json, read_json
from evidence_dashboard import worker
from evidence_engine import __main__ as engine_cli
from evidence_dashboard.tests.test_sources import write_pinned_source, write_export


@pytest.fixture
def controller(tmp_path):
    result = JobController(tmp_path)
    write_pinned_source(tmp_path)
    return result


@pytest.mark.parametrize("options", [
    [], {"source_registry": "/tmp/unapproved.json"}, {"min_support": True},
    {"min_support": 0}, {"min_support": 100001}, {"max_conditions": 25},
    {"publish_to_s3": "true"}, {"discovery_end": "2026-02-30"},
    {"discovery_end": "2026-09-14"}, {"discovery_end": "2026-09-17"},
    {"discovery_end": "2026-09-16T00:00:00"},
])
def test_start_rejects_invalid_controls_before_launch(controller, monkeypatch, options):
    def unexpected(*args, **kwargs):
        raise AssertionError("Invalid options reached process launch")
    monkeypatch.setattr(controller, "_launch", unexpected)
    with pytest.raises(ValueError):
        controller.start(options)


def test_start_requires_available_verified_registry(controller):
    controller.source.unlink()
    with pytest.raises(ValueError, match="source registry"):
        controller.start({})


def test_real_child_inherits_lock_across_parent_close_and_controller_restart(controller, tmp_path):
    """A real process keeps the flock; it never imports or runs the engine."""
    ready, release = tmp_path / "child-ready", tmp_path / "child-release"
    processes = []
    script = (
        "import os, pathlib, sys, time; "
        "os.fstat(int(sys.argv[1])); pathlib.Path(sys.argv[2]).touch(); "
        "\nwhile not pathlib.Path(sys.argv[3]).exists(): time.sleep(0.02)\n"
    )

    def inert_popen(command, **kwargs):
        assert command[1:3] == ["-m", "evidence_dashboard.worker"]
        assert kwargs["start_new_session"] is True
        inherited = kwargs["pass_fds"]
        assert len(inherited) == 1
        assert command[-1] == str(inherited[0])
        process = subprocess.Popen(
            [sys.executable, "-c", script, str(inherited[0]), str(ready), str(release)],
            **kwargs,
        )
        processes.append(process)
        return process

    controller.popen = inert_popen
    try:
        started = controller.start({})
        # The child need not have run any Python to retain the inherited lock.
        with pytest.raises(JobConflict, match="already active"):
            controller.start({})
        restarted = JobController(tmp_path)
        with pytest.raises(JobConflict, match="already active"):
            restarted.start({})
        deadline = time.monotonic() + 5
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert ready.exists()
        assert restarted.active()["run_id"] == started["run_id"]
        assert restarted.active()["status"] == "RUNNING"
        release.touch()
        assert processes[0].wait(timeout=5) == 0
        assert restarted.active()["status"] == "INTERRUPTED"
        assert restarted._busy() is False
    finally:
        release.touch()
        for process in processes:
            if process.poll() is None:
                process.terminate()
            process.wait(timeout=5)


def test_spawn_failure_persists_failure_and_releases_lock(controller):
    def fail(*args, **kwargs):
        raise OSError("sensitive simulated startup error")
    controller.popen = fail
    with pytest.raises(RuntimeError, match="could not start"):
        controller.start({})
    active = controller.active()
    assert active["status"] == "FAILED"
    assert "sensitive" not in json.dumps(active)
    assert controller._busy() is False


def saved_request(controller, monkeypatch, *, run_id="saved-run", cutoff="2026-09-16", publish=False):
    monkeypatch.setattr(engine_cli, "engine_hashes", lambda: {"graph.py": "fixed"})
    directory = controller.output / "runs" / run_id
    atomic_json(directory / "request.json", {
        "run_id": run_id, "engine_sources": {"graph.py": "fixed"},
        "config": {"discovery_end": cutoff, "min_support": 7, "max_conditions": 4},
    })
    atomic_json(controller.runtime / "jobs" / f"{run_id}.json", {
        "run_id": run_id, "status": "FAILED", "publish_to_s3": publish,
    })
    launches = []
    def record(run_id, options, *, resume):
        result = {"run_id": run_id, "options": options, "resume": resume}
        launches.append(result)
        return result
    monkeypatch.setattr(controller, "_launch", record)
    return directory, launches


def test_resume_preserves_saved_config_and_publication_choice(controller, monkeypatch):
    _, launches = saved_request(controller, monkeypatch, publish=True)
    result = controller.resume("saved-run")
    assert result == launches[0]
    assert result["resume"] is True
    assert result["options"] == {
        "discovery_end": "2026-09-16", "min_support": 7,
        "max_conditions": 4, "publish_to_s3": True,
    }


def test_resume_does_not_require_current_source_registry(controller, monkeypatch):
    saved_request(controller, monkeypatch)
    controller.source.unlink()
    result = controller.resume("saved-run")
    assert result["resume"] is True
    assert result["options"]["discovery_end"] == "2026-09-16"


def test_resume_can_finish_sealed_run_missing_final_registry_index(controller, monkeypatch):
    directory, _ = saved_request(controller, monkeypatch)
    atomic_json(directory / "manifest.json", {"run_id": "saved-run", "status": "REGISTERED", "qa_status": "PASS"})
    result = controller.resume("saved-run")
    assert result["resume"] is True


def test_resume_rejects_completed_registered_local_run(controller, monkeypatch):
    from evidence_engine.storage import digest_file
    directory, launches = saved_request(controller, monkeypatch)
    atomic_json(directory / "manifest.json", {"run_id": "saved-run", "status": "REGISTERED", "qa_status": "PASS"})
    atomic_json(controller.output / "registry/saved-run.json", {
        "run_id": "saved-run", "run_dir": str(directory), "qa_status": "PASS",
        "manifest_sha256": digest_file(directory / "manifest.json"),
    })
    with pytest.raises(ValueError, match="finished"):
        controller.resume("saved-run")
    assert launches == []


def test_resume_rejects_changed_engine_before_launch(controller, monkeypatch):
    _, launches = saved_request(controller, monkeypatch)
    monkeypatch.setattr(engine_cli, "engine_hashes", lambda: {"graph.py": "changed"})
    with pytest.raises(ValueError, match="engine changed"):
        controller.resume("saved-run")
    assert launches == []


@pytest.mark.parametrize("run_id", ["../escape", "a/b", "a\\b", "", "/absolute", "a" * 102])
def test_resume_rejects_unsafe_run_names(controller, run_id):
    with pytest.raises(ValueError, match="identifier"):
        controller.resume(run_id)


@pytest.mark.parametrize("component", ["evidence_dashboard/runtime", "evidence_engine/output"])
def test_controller_rejects_symlink_runtime_or_run_directory(controller, tmp_path, component):
    target = tmp_path / "outside"
    target.mkdir()
    path = tmp_path / component
    path.parent.mkdir(exist_ok=True, parents=True)
    path.symlink_to(target, target_is_directory=True)
    with pytest.raises(ValueError, match="regular local"):
        if component.endswith("runtime"):
            controller.start({})
        else:
            controller.resume("saved-run")


@pytest.mark.parametrize("outcome", ["success", "insufficient", "insufficient_publication", "exception"])
def test_worker_persists_terminal_state_and_releases_lock(controller, monkeypatch, outcome):
    controller._safe_runtime()
    run_id = "worker-fixture"
    job_path = controller.runtime / "jobs" / f"{run_id}.json"
    atomic_json(job_path, {
        "run_id": run_id, "status": "QUEUED", "min_support": 8, "max_conditions": 3,
        "discovery_end": "2026-09-16", "resume": True, "publish_to_s3": True,
    })
    captured = []
    def inert_main(args):
        captured.extend(args)
        if outcome == "exception":
            raise RuntimeError("private-token-should-not-appear")
        atomic_json(controller.output / "runs" / run_id / "manifest.json", {
            "status": "REGISTERED" if outcome == "success" else "REJECTED",
            "qa_status": "PASS" if outcome == "success" else "INSUFFICIENT_DATA",
        })
        if outcome == "insufficient_publication":
            # This is the real CLI's publication gate after a completed run.
            raise ValueError("S3 evidence publication requires integrity PASS")
        return 0 if outcome == "success" else 2
    monkeypatch.setattr(engine_cli, "main", inert_main)
    fd = os.open(controller.runtime / "runner.lock", os.O_CREAT | os.O_RDWR, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    code = worker.execute(controller.root, run_id, fd)
    assert code == {"success": 0, "insufficient": 2, "insufficient_publication": 2, "exception": 1}[outcome]
    assert captured[captured.index("--min-support") + 1] == "8"
    assert captured[captured.index("--max-conditions") + 1] == "3"
    assert captured[captured.index("--s3-bucket") + 1] == "heremesv0-cleaned-data"
    assert "--resume" in captured
    state = read_json(job_path)
    assert state["status"] == {"success": "SUCCEEDED", "insufficient": "INSUFFICIENT_DATA",
                              "insufficient_publication": "INSUFFICIENT_DATA", "exception": "FAILED"}[outcome]
    assert state["finished_at"]
    assert "private-token" not in json.dumps(state)
    assert controller._busy() is False
    with pytest.raises(OSError):
        os.fstat(fd)


def inert_process(*args, **kwargs):
    class Completed:
        def wait(self):
            return 0
    return Completed()


def test_each_new_job_freezes_latest_source_without_changing_earlier_snapshot(controller):
    controller.popen = inert_process
    historical = controller.source.read_bytes()
    first = controller.start({})
    original_path = Path(first["job"]["source_registry"])
    original_bytes = original_path.read_bytes()
    assert original_path.parent == controller.runtime / "sources"
    assert hashlib.sha256(original_bytes).hexdigest() == first["job"]["source_registry_sha256"]
    assert json.loads(original_bytes)["catalog"]["latest_date"] == "2026-09-17"
    write_export(controller.root, "2026-09-21")
    second = controller.start({})
    newer_path = Path(second["job"]["source_registry"])
    assert newer_path != original_path
    assert json.loads(newer_path.read_bytes())["catalog"]["latest_date"] == "2026-09-21"
    assert original_path.read_bytes() == original_bytes
    assert controller.source.read_bytes() == historical


def test_resume_keeps_original_source_reference_when_latest_catalog_changes(controller, monkeypatch):
    controller.popen = inert_process
    monkeypatch.setattr(engine_cli, "engine_hashes", lambda: {"graph.py": "fixed"})
    started = controller.start({})
    run_id = started["run_id"]
    source_path = Path(started["job"]["source_registry"])
    snapshot = source_path.read_bytes()
    directory = controller.output / "runs" / run_id
    atomic_json(directory / "request.json", {
        "run_id": run_id, "engine_sources": {"graph.py": "fixed"},
        "config": {"discovery_end": None, "min_support": 5, "max_conditions": 6},
    })
    atomic_json(directory / "source_registry.json", json.loads(snapshot))
    write_export(controller.root, "2026-09-22")
    monkeypatch.setattr(controller.catalog, "snapshot", lambda: pytest.fail("Resume consulted a different source catalog"))
    resumed = controller.resume(run_id)
    assert resumed["job"]["resume"] is True
    assert resumed["job"]["source_registry"] == str(source_path)
    assert resumed["job"]["source_registry_sha256"] == started["job"]["source_registry_sha256"]
    assert source_path.read_bytes() == snapshot
    assert read_json(directory / "source_registry.json")["catalog"]["latest_date"] == "2026-09-17"


@pytest.mark.parametrize("tampered", [False, True])
def test_fresh_worker_uses_and_checks_frozen_job_source(controller, monkeypatch, tampered):
    controller.popen = inert_process
    started = controller.start({})
    run_id = started["run_id"]
    source_path = Path(started["job"]["source_registry"])
    write_export(controller.root, "2026-09-22")
    if tampered:
        source_path.write_bytes(source_path.read_bytes() + b" ")
    captured = []
    def inert_main(args):
        captured.extend(args)
        snapshot_path = Path(args[args.index("--source-registry") + 1])
        assert snapshot_path == source_path
        assert read_json(snapshot_path)["catalog"]["latest_date"] == "2026-09-17"
        atomic_json(controller.output / "runs" / run_id / "manifest.json", {"status": "REGISTERED", "qa_status": "PASS"})
        return 0
    monkeypatch.setattr(engine_cli, "main", inert_main)
    fd = os.open(controller.runtime / "runner.lock", os.O_RDWR)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    code = worker.execute(controller.root, run_id, fd)
    assert code == (1 if tampered else 0)
    assert bool(captured) is (not tampered)
    assert controller._busy() is False

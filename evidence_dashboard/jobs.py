"""One durable research worker at a time, independently of HTTP requests."""
from __future__ import annotations

from datetime import date, datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import threading
from uuid import uuid4

from .sources import SourceCatalog

RUN_ID = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,100}\Z")
STAGES = [
    ("aligned.parquet", "align_timestamps", "Aligning observations"),
    ("samples.parquet", "detect_events", "Detecting five-minute events"),
    ("agent1/conditions.json", "agent1_discover", "Discovering conditions"),
    ("agent2/statistics.json", "agent2_scan", "Scanning historical matches"),
    ("agent3/qa_report.json", "agent3_evidence_qa", "Checking evidence"),
    ("manifest.json", "register_evidence", "Recording final evidence"),
]


class JobConflict(ValueError):
    pass


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def atomic_json(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix=".job-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream, allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def read_json(path):
    try:
        if path.is_symlink() or path.stat().st_size > 1_000_000:
            return {}
        value = json.loads(path.read_text())
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


class JobController:
    def __init__(self, root: Path, *, python=None, popen=None):
        self.root = Path(root).resolve()
        self.runtime = self.root / "evidence_dashboard/runtime"
        self.output = self.root / "evidence_engine/output"
        self.source = self.root / "data_cleaning_agent/runtime/enrichment_verification_2026-09-19.json"
        self.catalog = SourceCatalog(self.root)
        self.python = str(python or sys.executable)
        self.popen = popen or subprocess.Popen
        self._mutex = threading.Lock()

    def _safe_runtime(self):
        for path in (self.runtime.parent, self.runtime, self.runtime / "jobs", self.runtime / "logs", self.runtime / "sources",
                     self.runtime / "runner.lock", self.runtime / "current.json"):
            if path.is_symlink():
                raise ValueError("Dashboard runtime must use regular local paths.")
        self.runtime.mkdir(parents=True, exist_ok=True, mode=0o700)
        (self.runtime / "jobs").mkdir(exist_ok=True, mode=0o700)
        (self.runtime / "logs").mkdir(exist_ok=True, mode=0o700)
        (self.runtime / "sources").mkdir(exist_ok=True, mode=0o700)

    def _run_path(self, run_id):
        if not isinstance(run_id, str) or not RUN_ID.fullmatch(run_id):
            raise ValueError("Invalid run identifier.")
        path = self.output / "runs" / run_id
        if any(p.is_symlink() for p in (self.output, self.output / "runs", path)):
            raise ValueError("Run must use a regular local directory.")
        return path

    def _busy(self):
        path = self.runtime / "runner.lock"
        if not path.exists() or path.is_symlink():
            return False
        fd = os.open(path, os.O_RDWR | os.O_NOFOLLOW)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
            fcntl.flock(fd, fcntl.LOCK_UN)
            return False
        finally:
            os.close(fd)

    def active(self):
        current = read_json(self.runtime / "current.json")
        run_id = current.get("run_id")
        if not isinstance(run_id, str) or not RUN_ID.fullmatch(run_id):
            return None
        job = read_json(self.runtime / "jobs" / (run_id + ".json"))
        if not job:
            return None
        public = {k: job.get(k) for k in ("run_id", "status", "started_at", "finished_at", "error", "publish_to_s3")}
        if job.get("status") in ("QUEUED", "RUNNING"):
            if not self._busy():
                public.update(status="INTERRUPTED", stage="interrupted", message="Run interrupted. Resume it or start a new run.")
            else:
                public.update(status="RUNNING", stage="starting", message="Starting research")
                root = self._run_path(run_id)
                for artifact, stage, message in STAGES:
                    if not (root / artifact).is_file():
                        public.update(stage=stage, message=message)
                        break
                else:
                    public.update(stage="publishing" if job.get("publish_to_s3") else "finishing",
                                  message="Publishing evidence to S3" if job.get("publish_to_s3") else "Finishing research")
        else:
            public.update(stage="complete", message=job.get("message", "Research finished"))
        public["resumable"] = False
        if public["status"] in ("INTERRUPTED", "FAILED"):
            root = self._run_path(run_id)
            request, manifest = read_json(root / "request.json"), read_json(root / "manifest.json")
            from evidence_engine.__main__ import engine_hashes
            public["resumable"] = (request.get("engine_sources") == engine_hashes() and
                                    (not manifest or (manifest.get("status") == "REGISTERED" and
                                     (job.get("publish_to_s3") or not (self.output / "registry" / (run_id + ".json")).exists()))))
        return public

    def _options(self, value, *, check_source=True, registry=None):
        allowed = {"discovery_end", "min_support", "max_conditions", "publish_to_s3"}
        if not isinstance(value, dict) or set(value) - allowed:
            raise ValueError("Use only the research settings shown in the form.")
        result = {"discovery_end": value.get("discovery_end") or None,
                  "min_support": value.get("min_support", 5), "max_conditions": value.get("max_conditions", 6),
                  "publish_to_s3": value.get("publish_to_s3", False)}
        for key, high in (("min_support", 100000), ("max_conditions", 24)):
            if type(result[key]) is not int or not 1 <= result[key] <= high:
                raise ValueError(f"{key} must be an integer from 1 to {high}.")
        if type(result["publish_to_s3"]) is not bool:
            raise ValueError("publish_to_s3 must be true or false.")
        if result["discovery_end"] is not None:
            cutoff = result["discovery_end"]
            if not isinstance(cutoff, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", cutoff):
                raise ValueError("Discovery cutoff must use YYYY-MM-DD.")
            date.fromisoformat(cutoff)
            if check_source:
                registry = registry if registry is not None else self.catalog.snapshot()
                days = sorted(entry.get("date", "") for entry in registry.get("verified", []))
                if not any(day <= cutoff for day in days) or not any(day > cutoff for day in days):
                    raise ValueError("Choose a cutoff leaving sessions for both discovery and evaluation.")
        return result

    def start(self, options):
        options = self._options(options, check_source=False)
        registry = self.catalog.snapshot()
        if registry.get("status") != "VERIFIED" or not registry.get("verified"):
            raise ValueError("The latest local source registry is unavailable or has failed verification. Review the source issues.")
        options = self._options(options, registry=registry)
        run_id = "research-" + datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-") + uuid4().hex[:8]
        return self._launch(run_id, options, resume=False, source_snapshot=registry)

    def resume(self, run_id):
        root = self._run_path(run_id)
        request = read_json(root / "request.json")
        if request.get("run_id") != run_id:
            raise ValueError("This run has no saved request to resume.")
        from evidence_engine.__main__ import engine_hashes
        if request.get("engine_sources") != engine_hashes():
            raise ValueError("The engine changed since this run. Start a new research run.")
        manifest = read_json(root / "manifest.json")
        previous = read_json(self.runtime / "jobs" / (run_id + ".json"))
        if manifest:
            from evidence_engine.storage import digest_file
            index = read_json(self.output / "registry" / (run_id + ".json"))
            registered = (index.get("run_id") == run_id and index.get("qa_status") == "PASS" and
                          index.get("manifest_sha256") == digest_file(root / "manifest.json"))
            if manifest.get("status") != "REGISTERED" or (registered and not previous.get("publish_to_s3")):
                raise ValueError("This run has finished. Start a new run to research again.")
        options = {k: request.get("config", {}).get(k) for k in ("discovery_end", "min_support", "max_conditions")}
        options["publish_to_s3"] = previous.get("publish_to_s3", False)
        return self._launch(run_id, self._options(options, check_source=False), resume=True)

    def _launch(self, run_id, options, *, resume, source_snapshot=None):
        with self._mutex:
            self._safe_runtime()
            fd = os.open(self.runtime / "runner.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            try:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    raise JobConflict("A research run is already active.") from None
                job = {"run_id": run_id, "status": "QUEUED", "started_at": utc_now(), "finished_at": None,
                       "resume": resume, **options, "error": None}
                if resume:
                    previous = read_json(self.runtime / "jobs" / (run_id + ".json"))
                    # The engine resumes its original source_registry.json; keep
                    # the original dashboard registry reference for auditability.
                    for name in ("source_registry", "source_registry_sha256", "source_summary"):
                        if name in previous:
                            job[name] = previous[name]
                else:
                    if not source_snapshot or source_snapshot.get("status") != "VERIFIED":
                        raise ValueError("A verified source snapshot is required for a new research job.")
                    from evidence_engine.storage import write_json
                    source_path = self.runtime / "sources" / (run_id + ".json")
                    digest = write_json(source_path, source_snapshot)
                    job.update(source_registry=str(source_path), source_registry_sha256=digest,
                               source_summary=source_snapshot.get("catalog", {}))
                atomic_json(self.runtime / "jobs" / (run_id + ".json"), job)
                atomic_json(self.runtime / "current.json", {"run_id": run_id})
                command = [self.python, "-m", "evidence_dashboard.worker", "--workspace", str(self.root),
                           "--run-id", run_id, "--lock-fd", str(fd)]
                log_path = self.runtime / "logs" / (run_id + ".log")
                log_fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
                try:
                    with os.fdopen(log_fd, "ab") as log:
                        process = self.popen(command, cwd=self.root, stdin=subprocess.DEVNULL, stdout=log,
                                             stderr=subprocess.STDOUT, start_new_session=True, pass_fds=(fd,))
                    # The worker owns the same lock descriptor and writes its PID.
                    threading.Thread(target=process.wait, daemon=True).start()
                except Exception:
                    job.update(status="FAILED", finished_at=utc_now(), error="Research worker could not start.")
                    atomic_json(self.runtime / "jobs" / (run_id + ".json"), job)
                    raise RuntimeError("Research worker could not start.") from None
                return {"run_id": run_id, "job": {**job, "status": "RUNNING", "stage": "starting", "message": "Starting research"}}
            finally:
                # Closing, rather than unlocking, keeps the child-owned lock held.
                os.close(fd)

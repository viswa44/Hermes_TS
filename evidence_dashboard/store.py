"""Read-only, integrity-checked views of local evidence-engine artifacts.

The dashboard has no provider, database, or S3 client. A registered run is trusted
only after its index binds the sealed manifest and every declared file passes a
local SHA-256 check. File signatures invalidate both integrity and frame caches.
"""
from __future__ import annotations

from collections import OrderedDict
from datetime import date as calendar_date
import hashlib
import json
import os
from pathlib import Path
import re
import stat
from threading import RLock
from typing import Any

import pandas as pd
import pyarrow.parquet as pq


WORKSPACE = Path(__file__).resolve().parents[1]
RUN_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,100}\Z")
DIGEST = re.compile(r"[0-9a-f]{64}\Z")
REQUIRED_ARTIFACTS = {
    "request.json", "source_registry.json", "aligned.parquet", "dataset.json",
    "samples.parquet", "events.parquet", "alignment.json", "agent1/conditions.json",
    "agent2/occurrences.parquet", "agent2/statistics.json", "agent3/qa_report.json",
    "registry.json",
}
TABLE_FILES = {
    "observations": "aligned.parquet", "samples": "samples.parquet",
    "events": "events.parquet", "occurrences": "agent2/occurrences.parquet",
}
OBSERVATION_COLUMNS = [
    "timestamps", "trading_date", "underlying", "symbol", "optiontype", "strike",
    "expiry_date_local", "spot", "ltp", "iv", "iv_unit", "oi", "volume", "bid", "ask",
    "delta", "gamma", "theta", "vega", "rho", "iv_available_at", "greeks_available_at",
    "iv_source", "greeks_source", "derivation_status", "derivation_reason", "contract_key",
    "observation_id", "timestamp_source", "provider_timestamp", "source_freshness",
    "data_status", "model_risk_flags", "enrichment_issues", "source_version",
]
SAMPLE_COLUMNS = [
    "sample_id", "trading_date", "underlying", "anchor_at", "condition_at", "start_at",
    "end_at", "start_spot", "end_spot", "point_change", "label", "outcome_status",
    "outcome_reason", "start_observation_ids", "end_observation_ids", "past_spot_change",
] + [f"{side}_{name}" for side in ("CE", "PE") for name in (
    "past_midpoint_return_pct", "past_oi_change_pct", "past_volume_increment", "iv_decimal",
    "spread_pct", "delta", "gamma", "theta", "vega",
)]
OCCURRENCE_COLUMNS = [
    "condition_id", "sample_id", "partition", "trading_date", "condition_at", "anchor_at",
    "start_at", "end_at", "feature", "feature_value", "label", "point_change", "target",
    "success", "nonoverlapping", "feature_values_json",
]
STAGES = [
    ("align_timestamps", "Align observations", ("aligned.parquet", "dataset.json")),
    ("detect_events", "Detect five-minute events", ("samples.parquet", "events.parquet", "alignment.json")),
    ("agent1_discover", "Discover conditions", ("agent1/conditions.json",)),
    ("agent2_scan", "Scan occurrences", ("agent2/occurrences.parquet", "agent2/statistics.json")),
    ("agent3_evidence_qa", "Check evidence integrity", ("agent3/qa_report.json",)),
    ("register_evidence", "Register evidence", ("registry.json", "manifest.json")),
]


class IntegrityError(ValueError):
    """A local artifact cannot be trusted or safely opened."""


class EvidenceStore:
    """Serve a fixed workspace's evidence, never a request-supplied filesystem path."""

    def __init__(self, root: Path | str = WORKSPACE):
        self.root = Path(root).absolute()
        if self.root.is_symlink():
            raise ValueError("Workspace root must not be a symlink")
        self.root = self.root.resolve()
        self.output = self.root / "evidence_engine/output"
        self.runs = self.output / "runs"
        self.source_registry = self.root / "data_cleaning_agent/runtime/enrichment_verification_2026-09-19.json"
        self._lock = RLock()
        self._integrity_cache: OrderedDict[str, tuple[Any, dict]] = OrderedDict()
        self._frames: OrderedDict[tuple[str, str], tuple[Any, pd.DataFrame, int]] = OrderedDict()
        self._frame_bytes = 0

    def _safe(self, path: Path, *, regular: bool = False) -> Path:
        """Reject symlinks in every component, including links within the workspace."""
        try:
            relative = path.relative_to(self.root)
        except ValueError as exc:
            raise IntegrityError("Artifact path is outside the workspace") from exc
        if any(part in (".", "..") for part in relative.parts):
            raise IntegrityError("Artifact path contains traversal")
        cursor = self.root
        for part in relative.parts:
            cursor /= part
            try:
                info = cursor.lstat()
            except FileNotFoundError:
                continue
            if stat.S_ISLNK(info.st_mode):
                raise IntegrityError("Symlink artifacts are not permitted")
        if regular and path.exists() and not stat.S_ISREG(path.lstat().st_mode):
            raise IntegrityError("Artifact must be a regular file")
        return path

    def _run(self, run_id: str) -> Path:
        if not isinstance(run_id, str) or RUN_ID.fullmatch(run_id) is None:
            raise ValueError("run_id must be a safe identifier")
        return self._safe(self.runs / run_id)

    def _artifact(self, directory: Path, name: str) -> Path:
        if (not isinstance(name, str) or not name or "\\" in name or "\x00" in name
                or any(part in ("", ".", "..") for part in name.split("/"))
                or re.match(r"^[A-Za-z]:", name)):
            raise IntegrityError("Invalid artifact name")
        path = directory / name
        if not path.is_relative_to(directory):
            raise IntegrityError("Artifact escapes run directory")
        return self._safe(path, regular=True)

    def _signature(self, path: Path) -> tuple | None:
        self._safe(path, regular=True)
        try:
            value = path.stat()
            return self._stat_signature(value)
        except FileNotFoundError:
            return None

    @staticmethod
    def _stat_signature(value: os.stat_result) -> tuple:
        return (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns)

    def _signatures(self, paths: list[Path]) -> tuple:
        return tuple((str(path), self._signature(path)) for path in paths)

    def _open(self, path: Path):
        """Open beneath the workspace using descriptors; do not follow replaced parents."""
        self._safe(path, regular=True)
        parts = path.relative_to(self.root).parts
        if not parts:
            raise IntegrityError("Expected an artifact filename")
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        parent = os.open(self.root, flags | getattr(os, "O_DIRECTORY", 0))
        try:
            for part in parts[:-1]:
                next_parent = os.open(part, flags | getattr(os, "O_DIRECTORY", 0), dir_fd=parent)
                os.close(parent)
                parent = next_parent
            descriptor = os.open(parts[-1], flags, dir_fd=parent)
        finally:
            os.close(parent)
        return os.fdopen(descriptor, "rb")

    def _read(self, path: Path) -> bytes:
        with self._open(path) as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > 16 * 1024 * 1024:
                raise IntegrityError("JSON artifact is not a bounded regular file")
            payload = stream.read(16 * 1024 * 1024 + 1)
            if (len(payload) > 16 * 1024 * 1024
                    or self._stat_signature(info) != self._stat_signature(os.fstat(stream.fileno()))):
                raise IntegrityError("JSON artifact changed during read")
            return payload

    @staticmethod
    def _decode_json(payload: bytes) -> dict:
        try:
            value = json.loads(payload, parse_constant=lambda _: (_ for _ in ()).throw(ValueError("Nonfinite JSON")))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise IntegrityError("Artifact contains invalid JSON") from exc
        if not isinstance(value, dict):
            raise IntegrityError("JSON artifact must contain an object")
        return value

    def _json(self, path: Path, expected: str | None = None) -> dict:
        payload = self._read(path)
        if expected is not None and hashlib.sha256(payload).hexdigest() != expected:
            raise IntegrityError("JSON artifact changed after integrity verification")
        return self._decode_json(payload)

    def _optional_json(self, path: Path) -> dict:
        try:
            return self._json(path)
        except (OSError, ValueError):
            return {}

    def _digest(self, path: Path) -> str:
        digest = hashlib.sha256()
        with self._open(path) as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > 512 * 1024 * 1024:
                raise IntegrityError("Artifact is not a bounded regular file")
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
            if self._stat_signature(info) != self._stat_signature(os.fstat(stream.fileno())):
                raise IntegrityError("Artifact changed while hashing")
        return digest.hexdigest()

    def _inspect(self, run_id: str) -> dict:
        directory = self._run(run_id)
        manifest_path = directory / "manifest.json"
        index_path = self.output / "registry" / f"{run_id}.json"
        result = {"status": "INCOMPLETE", "qa_status": "PENDING", "trusted": False,
                  "integrity": {"status": "INCOMPLETE", "reason": "This run has no sealed manifest yet."},
                  "manifest": {}, "documents": {}, "signatures": {}}
        if not directory.exists():
            result.update(status="NOT_FOUND")
            result["integrity"] = {"status": "MISSING", "reason": "The selected run is not available locally."}
            return result
        try:
            manifest_signature = self._signature(manifest_path)
            if manifest_signature is None:
                return result
            manifest_payload = self._read(manifest_path)
            manifest_digest = hashlib.sha256(manifest_payload).hexdigest()
            manifest = self._decode_json(manifest_payload)
            result["manifest"] = manifest
            if manifest.get("run_id") != run_id:
                raise IntegrityError("Manifest run identity does not match its directory")
            artifacts = manifest.get("artifacts")
            if not isinstance(artifacts, dict) or not artifacts or len(artifacts) > 128:
                raise IntegrityError("Manifest must declare a bounded nonempty artifact mapping")
            paths = [manifest_path, index_path]
            for name, digest in artifacts.items():
                if not isinstance(digest, str) or not DIGEST.fullmatch(digest):
                    raise IntegrityError("Manifest contains an invalid SHA-256 digest")
                paths.append(self._artifact(directory, name))
            signatures = self._signatures(paths)
            if dict(signatures)[str(manifest_path)] != manifest_signature:
                raise IntegrityError("Manifest changed while opening run")
            cached = self._integrity_cache.get(run_id)
            if cached and cached[0] == signatures:
                self._integrity_cache.move_to_end(run_id)
                return cached[1]
            index = self._optional_json(index_path)
            if index:
                if (index.get("run_id") != run_id or index.get("qa_status") != "PASS"
                        or index.get("manifest_sha256") != manifest_digest
                        or index.get("run_dir") != str(directory)):
                    raise IntegrityError("Registry index does not bind this manifest and run identity")
            documents = {}
            for name, expected in artifacts.items():
                path = self._artifact(directory, name)
                if self._digest(path) != expected:
                    raise IntegrityError(f"Artifact checksum mismatch: {name}")
                if name.endswith(".json"):
                    documents[name] = self._json(path, expected)
            if self._signatures(paths) != signatures:
                raise IntegrityError("Artifacts changed during integrity verification; retry after the writer finishes")
            result.update(documents=documents, signatures=dict(signatures), manifest_digest=manifest_digest)
            qa = documents.get("agent3/qa_report.json", {})
            declared_qa = manifest.get("qa_status", "PENDING")
            if declared_qa != "PASS":
                result.update(status="FAILED", qa_status=declared_qa)
                result["integrity"] = {"status": "UNREGISTERED", "reason": "This run did not pass evidence QA and is not registered."}
            elif not index:
                result.update(status="UNREGISTERED", qa_status="UNVERIFIED")
                result["integrity"] = {"status": "UNREGISTERED", "reason": "No valid registry index binds this sealed run."}
            else:
                if not REQUIRED_ARTIFACTS.issubset(artifacts):
                    raise IntegrityError("Registered manifest is missing required evidence artifacts")
                registry = documents["registry.json"]
                if (manifest.get("status") != "REGISTERED" or qa.get("status") != "PASS"
                        or qa.get("run_id") != run_id or not qa.get("checks")
                        or not all(isinstance(check, dict) and check.get("passed") is True for check in qa["checks"])
                        or registry.get("run_id") != run_id or registry.get("integrity_status") != "PASS"
                        or registry.get("conditions_sha256") != artifacts["agent1/conditions.json"]
                        or registry.get("qa_sha256") != artifacts["agent3/qa_report.json"]):
                    raise IntegrityError("Registered manifest, QA report, or evidence registry disagree")
                result.update(status="REGISTERED", qa_status="PASS", trusted=True)
                result["integrity"] = {"status": "VERIFIED", "reason": "Registry, sealed manifest, and every declared artifact passed local SHA-256 checks."}
            self._integrity_cache[run_id] = (signatures, result)
            while len(self._integrity_cache) > 32:
                self._integrity_cache.popitem(last=False)
            return result
        except (OSError, ValueError, TypeError, KeyError) as exc:
            self._integrity_cache.pop(run_id, None)
            result.update(status="INVALID", qa_status="UNVERIFIED", trusted=False, documents={})
            reason = str(exc) if isinstance(exc, IntegrityError) else "A required artifact is missing, unreadable, or malformed."
            result["integrity"] = {"status": "FAILED", "reason": reason}
            return result

    def _source(self) -> dict:
        from .sources import SourceCatalog
        if not hasattr(self, "_catalog"):
            self._catalog = SourceCatalog(self.root)
        return self._catalog.summary()

    def status(self) -> dict:
        with self._lock:
            runs = []
            try:
                self._safe(self.runs)
                directories = list(self.runs.iterdir()) if self.runs.is_dir() else []
            except (OSError, ValueError):
                directories = []
            for directory in directories:
                if not RUN_ID.fullmatch(directory.name) or directory.is_symlink() or not directory.is_dir():
                    continue
                inspected = self._inspect(directory.name)
                request = self._optional_json(directory / "request.json")
                runs.append({"run_id": directory.name,
                             "executed_at": inspected["manifest"].get("executed_at", request.get("executed_at")),
                             "status": inspected["status"], "qa_status": inspected["qa_status"]})
            runs.sort(key=lambda item: (str(item["executed_at"] or ""), item["run_id"]), reverse=True)
            selected = next((item["run_id"] for item in runs if item["status"] == "REGISTERED"),
                            runs[0]["run_id"] if runs else None)
            return {"source": self._source(), "runs": runs, "selected_run_id": selected}

    def _pipeline(self, directory: Path, inspected: dict) -> list[dict]:
        result = []
        for identifier, label, files in STAGES:
            try:
                complete = all(self._signature(directory / name) is not None for name in files)
            except (OSError, ValueError):
                complete = False
            status = "completed" if complete else "pending"
            if identifier == "agent3_evidence_qa" and inspected["status"] in ("FAILED", "INVALID"):
                status = "failed"
            if identifier == "register_evidence" and not inspected["trusted"]:
                status = "failed" if inspected["status"] in ("FAILED", "INVALID") else "pending"
            result.append({"id": identifier, "label": label, "status": status})
        return result

    def _publication(self, run_id: str, inspected: dict) -> dict:
        value = {"published": False, "verified_at": None, "manifest_uri": None}
        if not inspected["trusted"]:
            return value
        expected = {**inspected["manifest"]["artifacts"], "manifest.json": inspected["manifest_digest"]}
        receipt = self._optional_json(self.output / "publication" / f"{run_id}.json")
        objects = receipt.get("objects", [])
        if (isinstance(objects, list) and len(objects) == len(expected)
                and all(isinstance(item, dict) for item in objects)
                and {item.get("file"): item.get("sha256") for item in objects} == expected
                and isinstance(receipt.get("bucket"), str) and isinstance(receipt.get("prefix"), str)):
            prefix = receipt["prefix"].rstrip("/")
            if all(item.get("key") == f"{prefix}/{item['file']}" for item in objects):
                value.update(published=True, manifest_uri=f"s3://{receipt['bucket']}/{prefix}/manifest.json")
        verification = self._optional_json(self.root / "evidence_engine/runtime" / f"{run_id}-s3-verification.json")
        checks = verification.get("s3_artifact_checks", [])
        if (value["published"] and verification.get("status") == "VERIFIED"
                and verification.get("run_id") == run_id
                and verification.get("manifest_sha256") == expected["manifest.json"]
                and verification.get("manifest_uri") == value["manifest_uri"]
                and isinstance(checks, list) and len(checks) == len(expected)
                and all(isinstance(check, dict) and check.get("verified") is True for check in checks)
                and {check.get("file"): check.get("sha256") for check in checks} == expected):
            value["verified_at"] = verification.get("checked_at")
        return value

    def detail(self, run_id: str) -> dict:
        with self._lock:
            directory = self._run(run_id)
            inspected = self._inspect(run_id)
            request = self._optional_json(directory / "request.json")
            documents = inspected["documents"] if inspected["trusted"] else {}
            dataset = documents.get("dataset.json", {})
            alignment = documents.get("alignment.json", {})
            discovery = documents.get("agent1/conditions.json", {})
            statistics = documents.get("agent2/statistics.json", {})
            qa = documents.get("agent3/qa_report.json", {})
            counts = alignment.get("label_counts", {})
            summaries = {(item["condition_id"], item["partition"]): item for item in statistics.get("summaries", [])}
            conditions = [{**rule, "discovery": summaries.get((rule["condition_id"], "discovery"), {}),
                           "evaluation": summaries.get((rule["condition_id"], "evaluation"), {})}
                          for rule in discovery.get("conditions", [])]
            config = inspected["manifest"].get("config", request.get("config", {}))
            momentum = config.get("detector_version") == "spot-endpoint-momentum-v2"
            labels = ("UP_MOMENTUM", "DOWN_MOMENTUM") if momentum else ("UP_50_80", "DOWN_50_80")
            source_dates = sorted({entry["date"] for entry in documents.get("source_registry.json", {}).get("verified", []) if entry.get("date")})
            if not source_dates:
                source_dates = sorted({entry["date"] for entry in dataset.get("source_refs", []) if entry.get("date")}) or dataset.get("trading_dates", [])
            horizon = config.get("horizon_seconds", 300) / 60
            threshold = config.get("min_move", 50)
            description = (f"At least {threshold:g} points up or down over {horizon:g} minutes; no upper cap" if momentum
                           else f"{threshold:g}\u2013{config.get('max_move', 80):g} point endpoint moves over {horizon:g} minutes")
            return {
                "run_id": run_id, "executed_at": inspected["manifest"].get("executed_at", request.get("executed_at")),
                "status": inspected["status"], "qa_status": inspected["qa_status"], "integrity": dict(inspected["integrity"]),
                "metrics": {"source_rows": dataset.get("all_rows"), "eligible_rows": dataset.get("version_2_rows"),
                            "excluded_rows": dataset.get("excluded_legacy_rows"), "sample_count": alignment.get("sample_count"),
                            "event_count": sum(counts.get(label, 0) for label in labels) if inspected["trusted"] else None,
                            "unknown_count": counts.get("UNKNOWN", 0) if inspected["trusted"] else None,
                            "condition_count": len(conditions) if inspected["trusted"] else None},
                "label_counts": counts, "partition": discovery.get("partition", {"discovery_dates": [], "evaluation_dates": []}),
                "conditions": conditions, "qa": {"status": inspected["qa_status"], "checks": qa.get("checks", [])},
                "pipeline": self._pipeline(directory, inspected), "publication": self._publication(run_id, inspected),
                "dates": dataset.get("trading_dates", []),
                "source": {"dates": source_dates, "rows": dataset.get("all_rows"), "sessions": len(source_dates),
                           "latest_date": max(source_dates) if source_dates else None},
                "research": {"mode": "joint" if config.get("discovery_version", "").startswith("joint-") else "single",
                             "target_description": description, "discovery_version": config.get("discovery_version"),
                             "detector_version": config.get("detector_version"),
                             "tested_predicates": discovery.get("tested_predicates"),
                             "interpretation": discovery.get("interpretation")},
            }

    def _frame(self, run_id: str, kind: str, inspected: dict) -> pd.DataFrame:
        path = self._artifact(self._run(run_id), TABLE_FILES[kind])
        signature = self._signature(path)
        if signature != inspected["signatures"].get(str(path)):
            raise IntegrityError("Artifact changed after integrity verification")
        key = (run_id, kind)
        cached = self._frames.get(key)
        if cached and cached[0] == signature:
            self._frames.move_to_end(key)
            return cached[1]
        columns = OBSERVATION_COLUMNS if kind == "observations" else OCCURRENCE_COLUMNS if kind == "occurrences" else SAMPLE_COLUMNS
        with self._open(path) as stream:
            if self._stat_signature(os.fstat(stream.fileno())) != signature:
                raise IntegrityError("Artifact changed before opening table")
            available = set(pq.read_schema(stream).names)
            selected = [column for column in columns if column in available]
            stream.seek(0)
            frame = pd.read_parquet(stream, columns=selected)
            if self._stat_signature(os.fstat(stream.fileno())) != signature:
                raise IntegrityError("Artifact changed while reading table")
        order = [column for column in ("trading_date", "timestamps" if kind == "observations" else "anchor_at", "observation_id" if kind == "observations" else "sample_id") if column in frame]
        if order:
            frame = frame.sort_values(order, kind="stable").reset_index(drop=True)
        if self._signature(path) != signature:
            raise IntegrityError("Artifact changed while loading table")
        size = int(frame.memory_usage(deep=True).sum())
        if cached:
            self._frame_bytes -= self._frames.pop(key)[2]
        if size <= 128 * 1024 * 1024:
            while self._frames and (len(self._frames) >= 6 or self._frame_bytes + size > 128 * 1024 * 1024):
                self._frame_bytes -= self._frames.popitem(last=False)[1][2]
            self._frames[key] = (signature, frame, size)
            self._frame_bytes += size
        return frame

    @staticmethod
    def _validate_date(value: str) -> None:
        if not isinstance(value, str):
            raise ValueError("date must be a YYYY-MM-DD string")
        if value:
            try:
                if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
                    raise ValueError
                calendar_date.fromisoformat(value)
            except ValueError as exc:
                raise ValueError("date must be a valid YYYY-MM-DD string") from exc

    @staticmethod
    def _records(frame: pd.DataFrame) -> list[dict]:
        # pandas handles NaT, numpy scalars/arrays, and NaN without invalid JSON.
        return json.loads(frame.to_json(orient="records", date_format="iso", date_unit="ns"))

    def table(self, run_id: str, kind: str = "observations", date: str = "", side: str = "",
              label: str = "", condition_id: str = "", q: str = "", offset: int = 0, limit: int = 25) -> dict:
        self._run(run_id)
        if kind not in TABLE_FILES:
            raise ValueError("kind must be observations, samples, events, or occurrences")
        self._validate_date(date)
        if side not in ("", "CE", "PE"):
            raise ValueError("side must be CE or PE")
        if (isinstance(offset, bool) or not isinstance(offset, int) or offset < 0
                or isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 250):
            raise ValueError("offset must be nonnegative and limit must be between 1 and 250")
        if not all(isinstance(value, str) and len(value) <= 200 for value in (label, condition_id, q)):
            raise ValueError("Text filters must be strings no longer than 200 characters")
        with self._lock:
            inspected = self._inspect(run_id)
            value = {"columns": [], "rows": [], "total": 0, "offset": offset, "limit": limit,
                     "integrity": dict(inspected["integrity"])}
            if not inspected["trusted"]:
                return value
            frame = self._frame(run_id, kind, inspected)
            for column, selected in (("trading_date", date), ("optiontype", side), ("label", label), ("condition_id", condition_id)):
                if selected:
                    frame = frame.loc[frame[column].astype(str).eq(selected)] if column in frame else frame.iloc[:0]
            if q:
                matches = pd.Series(False, index=frame.index)
                for column in frame.columns:
                    matches |= frame[column].astype(str).str.contains(q, regex=False, case=False, na=False)
                frame = frame.loc[matches]
            value.update(columns=list(frame.columns), rows=self._records(frame.iloc[offset:offset + limit]), total=len(frame))
            return value

    def series(self, run_id: str, date: str = "") -> dict:
        self._run(run_id)
        self._validate_date(date)
        with self._lock:
            inspected = self._inspect(run_id)
            value = {"date": date or None, "points": [], "events": [], "integrity": dict(inspected["integrity"])}
            if not inspected["trusted"]:
                return value
            frame = self._frame(run_id, "observations", inspected)
            if not {"trading_date", "timestamps", "spot"}.issubset(frame):
                return value
            date = date or max(frame["trading_date"].astype(str), default="")
            value["date"] = date or None
            points = frame.loc[frame["trading_date"].astype(str).eq(date), ["timestamps", "spot"]].drop_duplicates("timestamps").dropna()
            points = points.sort_values("timestamps").copy()
            # Preserve actual receipt gaps before decimation; spaced display
            # points must neither invent gaps nor draw lines over missing data.
            points["segment"] = pd.to_datetime(points["timestamps"], utc=True).diff().dt.total_seconds().gt(10).cumsum()
            if len(points) > 800:
                indexes = sorted({round(i * (len(points) - 1) / 799) for i in range(800)})
                points = points.iloc[indexes]
            value["points"] = self._records(points.rename(columns={"timestamps": "time"}))
            events = self._frame(run_id, "events", inspected)
            if {"trading_date", "start_at", "end_at", "label", "point_change"}.issubset(events):
                events = events.loc[events["trading_date"].astype(str).eq(date), ["start_at", "end_at", "label", "point_change"]]
                value["events"] = self._records(events.rename(columns={"start_at": "time", "end_at": "end_time"}))
            return value

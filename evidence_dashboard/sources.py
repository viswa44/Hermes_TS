"""Discover and verify the cleaner's current local revisions without network I/O.

The cleaner's progress record selects one revision per date. Its publication
record is historical context; this catalog verifies local bytes and makes no
fresh S3 or database claim. Every research job freezes its own registry snapshot.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime, timezone
import hashlib
from io import BytesIO
import json
import os
from pathlib import Path
import re
import stat
from threading import RLock
from uuid import UUID

import pandas as pd
import pyarrow.parquet as pq


HASH = re.compile(r"[0-9a-f]{64}\Z")
PUBLISHED_ARTIFACTS = {"observations.parquet", "options.parquet", "observations.csv", "options.csv", "quality_report.json"}
SCOPE = "LOCAL: current cleaner revisions and local artifact hashes; no live database or S3 verification."


class SourceCatalog:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.progress = self.root / "data_cleaning_agent/runtime/progress.json"
        self.historical = self.root / "data_cleaning_agent/runtime/enrichment_verification_2026-09-19.json"
        self.output = self.root / "data_cleaning_agent/output/postgres"
        self._lock = RLock()
        self._cache = None

    def _safe(self, path):
        path = Path(path)
        try:
            relative = path.relative_to(self.root)
        except ValueError as exc:
            raise ValueError("Source path is outside the workspace") from exc
        if any(part in (".", "..") for part in relative.parts):
            raise ValueError("Source path contains traversal")
        cursor = self.root
        for part in relative.parts:
            cursor /= part
            if cursor.is_symlink():
                raise ValueError("Source paths must not contain symlinks")
        return path

    @staticmethod
    def _stat(info):
        return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns

    def _signature(self, path):
        self._safe(path)
        try:
            info = path.stat()
        except FileNotFoundError:
            return None
        if not stat.S_ISREG(info.st_mode):
            raise ValueError("Source artifact must be a regular file")
        return self._stat(info)

    def _open(self, path):
        self._safe(path)
        parts = path.relative_to(self.root).parts
        if not parts:
            raise ValueError("Expected a source filename")
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        parent = os.open(self.root, flags | getattr(os, "O_DIRECTORY", 0))
        try:
            for part in parts[:-1]:
                following = os.open(part, flags | getattr(os, "O_DIRECTORY", 0), dir_fd=parent)
                os.close(parent)
                parent = following
            descriptor = os.open(parts[-1], flags, dir_fd=parent)
        finally:
            os.close(parent)
        return os.fdopen(descriptor, "rb")

    def _bytes(self, path, maximum=16 * 1024 * 1024):
        with self._open(path) as stream:
            before = os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode) or before.st_size > maximum:
                raise ValueError("Source artifact exceeds its size limit")
            result = stream.read(maximum + 1)
            if len(result) > maximum or self._stat(before) != self._stat(os.fstat(stream.fileno())):
                raise ValueError("Source artifact changed while reading")
            return result

    def _json(self, path):
        result = json.loads(self._bytes(path))
        if not isinstance(result, dict):
            raise ValueError("Source metadata must contain an object")
        return result

    @staticmethod
    def _day(value):
        if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            raise ValueError("Invalid source trading date")
        date.fromisoformat(value)
        return value

    @staticmethod
    def _hash(value):
        if not isinstance(value, str) or not HASH.fullmatch(value):
            raise ValueError("Source metadata contains an invalid SHA-256 hash")
        return value

    def _artifact(self, directory, name):
        if (not isinstance(name, str) or not name or "/" in name or "\\" in name
                or name in (".", "..") or "\x00" in name):
            raise ValueError("Source artifact name must be a local filename")
        return self._safe(directory / name)

    def _verify_file(self, path, record, *, capture=False):
        expected = self._hash(record.get("sha256"))
        size = record.get("size_bytes")
        if type(size) is not int or size < 0:
            raise ValueError("Invalid source artifact byte count")
        chunks, digest = [], hashlib.sha256()
        with self._open(path) as stream:
            before = os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode) or before.st_size != size or size > 512 * 1024 * 1024:
                raise ValueError(f"Source artifact size differs: {path.name}")
            while block := stream.read(1024 * 1024):
                digest.update(block)
                if capture:
                    chunks.append(block)
            if self._stat(before) != self._stat(os.fstat(stream.fileno())):
                raise ValueError("Source artifact changed while hashing")
        if digest.hexdigest() != expected:
            raise ValueError(f"Source artifact checksum differs: {path.name}")
        return b"".join(chunks) if capture else None

    @staticmethod
    def _validate_tables(payloads, day, row_count):
        if type(row_count) is not int or row_count <= 0:
            raise ValueError("Source table must contain a positive row count")
        schemas = {name: set(pq.read_schema(BytesIO(payload)).names) for name, payload in payloads.items()}
        shared = schemas["observations.parquet"] & schemas["options.parquet"]
        needed = shared | {"observation_id", "timestamps", "trading_date", "spot", "underlying", "source_version",
                           "iv_available_at", "greeks_available_at"}
        tables = {name: pd.read_parquet(BytesIO(payload), columns=sorted(needed & schemas[name]))
                  for name, payload in payloads.items()}
        obs, opt = tables["observations.parquet"], tables["options.parquet"]
        for table in (obs, opt):
            if (len(table) != row_count or "observation_id" not in table
                    or table.observation_id.isna().any() or not table.observation_id.is_unique):
                raise ValueError("Source table count or observation identity differs")
        if set(obs.observation_id) != set(opt.observation_id):
            raise ValueError("Observation and option ID sets differ")
        required = {"timestamps", "trading_date", "spot", "underlying", "source_version"}
        if not required.issubset(obs):
            raise ValueError("Source observation table lacks required provenance columns")
        if not isinstance(obs.timestamps.dtype, pd.DatetimeTZDtype) or obs.timestamps.isna().any():
            raise ValueError("Source receipts must contain timezone-aware timestamps")
        if not obs.trading_date.astype(str).eq(day).all() or not obs.timestamps.dt.tz_convert("Asia/Kolkata").dt.strftime("%Y-%m-%d").eq(day).all():
            raise ValueError("Source table contains observations from a different trading date")
        numeric = pd.to_numeric(obs.spot, errors="coerce")
        if not (numeric.notna() & numeric.gt(0) & numeric.lt(float("inf"))).all():
            raise ValueError("Source contains an invalid spot price")
        if obs.underlying.isna().any() or obs.groupby(["underlying", "timestamps"]).spot.nunique().gt(1).any():
            raise ValueError("Source contains conflicting underlying receipts")
        left, right = obs.set_index("observation_id"), opt.set_index("observation_id").reindex(obs.observation_id)
        for name in set(left) & set(right):
            if not (left[name].eq(right[name]) | (left[name].isna() & right[name].isna())).fillna(False).all():
                raise ValueError(f"Source tables disagree on shared column: {name}")
        for name, table in (("iv_available_at", left), ("greeks_available_at", right)):
            if name in table:
                available = table[name]
                present = available.notna()
                if present.any() and (not isinstance(available.dtype, pd.DatetimeTZDtype)
                                      or not available.loc[present].ge(left.loc[present, "timestamps"]).all()):
                    raise ValueError("Analytics availability precedes its source receipt")
        return {"ids": set(obs.observation_id), "eligible_rows": int(obs.source_version.astype(str).eq("2").sum())}

    def _candidate(self, day, entry):
        self._day(day)
        if not isinstance(entry, dict) or entry.get("trading_date") != day:
            raise ValueError("Progress entry trading date does not match its key")
        status = entry.get("status")
        if status not in ("PUBLISHED", "RETRY_PENDING", "LOCAL_PASS"):
            raise ValueError("Current cleaner revision has not passed cleaning")
        revision = self._hash(entry.get("revision"))
        self._hash(entry.get("source_sha256"))
        self._hash(entry.get("metadata_sha256"))
        uri = entry.get("manifest_uri")
        if status == "PUBLISHED":
            prefix = f"s3://heremesv0-cleaned-data/cleaned/{day}/"
            if not isinstance(uri, str) or not uri.startswith(prefix) or not uri.endswith("/manifest.json"):
                raise ValueError("Published source has an unexpected manifest location")
            run_id = uri[len(prefix):-len("/manifest.json")]
            if str(UUID(run_id)) != run_id:
                raise ValueError("Published source requires a canonical run UUID")
            if entry.get("commit_uri") != f"{prefix}commits/{revision}.json":
                raise ValueError("Published source commit does not match its revision")
            directory = self.output / day / run_id
            if entry.get("run_dir") and Path(entry["run_dir"]) != directory:
                raise ValueError("Published source run directory and manifest location disagree")
        else:
            directory = Path(entry.get("run_dir", ""))
            if not directory.is_absolute() or directory.parent != self.output / day or str(UUID(directory.name)) != directory.name:
                raise ValueError("Pending publication has no safe local cleaner run")
        self._safe(directory)
        manifest_signature = self._signature(directory / "manifest.json")
        manifest = self._json(directory / "manifest.json")
        context = manifest.get("source_context", {})
        if (manifest.get("schema_version") != 2 or manifest.get("run_id") != directory.name
                or manifest.get("status") != "PASS" or manifest.get("source_sha256") != entry["source_sha256"]
                or not isinstance(context, dict) or context.get("trading_date") != day
                or context.get("revision") != revision or context.get("metadata_sha256") != entry["metadata_sha256"]
                or context.get("integrity_passed") is not True or context.get("row_count_reconciled") is not True):
            raise ValueError("Current progress and PASS manifest identities or integrity gates disagree")
        counts = manifest.get("table_counts", {})
        rows = entry.get("rows")
        if type(rows) is not int or rows <= 0 or counts != {"observations.parquet": rows, "options.parquet": rows}:
            raise ValueError("Current progress and manifest table counts disagree")
        records = manifest.get("artifacts")
        if not isinstance(records, dict) or not PUBLISHED_ARTIFACTS.issubset(records) or len(records) > 64:
            raise ValueError("Cleaner manifest is missing required artifacts")
        if self._signature(directory / "manifest.json") != manifest_signature:
            raise ValueError("Cleaner manifest changed while selecting its revision")
        return {"day": day, "directory": directory, "manifest": manifest, "records": records,
                "entry": entry, "rows": rows, "manifest_path": directory / "manifest.json",
                "manifest_signature": manifest_signature}

    def _verified_candidate(self, candidate):
        payloads = {}
        checks = {}
        for name, record in candidate["records"].items():
            if not isinstance(record, dict):
                raise ValueError("Invalid source artifact metadata")
            payload = self._verify_file(self._artifact(candidate["directory"], name), record,
                                        capture=name in ("observations.parquet", "options.parquet", "quality_report.json"))
            if name in ("observations.parquet", "options.parquet"):
                payloads[name] = payload
            elif name == "quality_report.json":
                quality = json.loads(payload)
                if (not isinstance(quality, dict) or quality.get("passed") is not True
                        or quality.get("quarantined_rows") != 0 or quality.get("row_count_reconciled") is not True
                        or quality.get("accepted_rows") != candidate["rows"]):
                    raise ValueError("Source quality report did not pass the required row integrity gates")
            checks[name] = {"sha256": record["sha256"], "size_bytes": record["size_bytes"], "verified": True}
        validation = self._validate_tables(payloads, candidate["day"], candidate["rows"])
        entry = candidate["entry"]
        result = {"date": candidate["day"], "run_dir": str(candidate["directory"]),
                  "rows_per_table": candidate["rows"], "artifact_checks": checks,
                  "commit_uri": entry.get("commit_uri"), "manifest_uri": entry.get("manifest_uri"),
                  "revision": entry["revision"], "publication_status": entry["status"],
                  "verification_scope": "LOCAL", "eligible_rows": validation["eligible_rows"],
                  "local_manifest_sha256": hashlib.sha256(self._bytes(candidate["manifest_path"])).hexdigest()}
        return result, validation["ids"]

    def _fallback(self):
        registry = self._json(self.historical)
        if registry.get("status") != "VERIFIED" or not isinstance(registry.get("verified"), list) or not registry["verified"]:
            raise ValueError("Historical source registry is unavailable or unverified")
        candidates, paths = [], [self.historical]
        seen = set()
        for entry in registry["verified"]:
            day = self._day(entry.get("date"))
            if day in seen:
                raise ValueError("Historical source registry repeats a session")
            seen.add(day)
            directory = Path(entry["run_dir"])
            if not directory.is_absolute():
                directory = self.historical.parent / directory
            self._safe(directory)
            records = entry["artifact_checks"]
            if not isinstance(records, dict) or not {"observations.parquet", "options.parquet"}.issubset(records):
                raise ValueError("Historical source registry is missing table hashes")
            for name, record in records.items():
                if record.get("verified") is not True:
                    raise ValueError("Historical source artifact was not verified")
                paths.append(self._artifact(directory, name))
            candidates.append({"day": day, "entry": entry, "directory": directory, "records": records})
        return candidates, paths

    def snapshot(self):
        with self._lock:
            paths, candidates, issues = [self.progress], [], []
            mode, latest_candidate = "daily_progress", None
            try:
                progress_signature = self._signature(self.progress)
                if progress_signature is None:
                    mode = "historical_registry"
                    candidates, fallback_paths = self._fallback()
                    paths += fallback_paths
                    latest_candidate = max(item["day"] for item in candidates)
                else:
                    progress = self._json(self.progress)
                    if not isinstance(progress.get("days"), dict):
                        raise ValueError("Cleaner progress has no daily revision mapping")
                    for day, entry in sorted(progress["days"].items()):
                        if isinstance(entry, dict) and entry.get("status") in ("NO_DATA", "SKIPPED_MARKET_HOLIDAY", "SKIPPED_WEEKEND"):
                            continue
                        latest_candidate = max(latest_candidate or "", day)
                        try:
                            candidate = self._candidate(day, entry)
                            candidates.append(candidate)
                            paths.append(candidate["manifest_path"])
                            paths += [self._artifact(candidate["directory"], name) for name in candidate["records"]]
                        except (OSError, ValueError, KeyError, TypeError) as exc:
                            issues.append({"date": day, "reason": self._reason(exc)})
                signatures = tuple((str(path), self._signature(path)) for path in paths)
                if mode == "daily_progress" and any(
                        dict(signatures).get(str(item["manifest_path"])) != item["manifest_signature"] for item in candidates):
                    raise ValueError("Cleaner manifest changed while building the catalog")
                cache_key = (signatures, json.dumps(issues, sort_keys=True))
                if self._cache and self._cache[0] == cache_key:
                    return deepcopy(self._cache[1])
                verified, all_ids = [], set()
                for candidate in candidates:
                    try:
                        if mode == "historical_registry":
                            payloads = {}
                            for name, record in candidate["records"].items():
                                payload = self._verify_file(self._artifact(candidate["directory"], name), record,
                                                            capture=name in ("observations.parquet", "options.parquet"))
                                if name in ("observations.parquet", "options.parquet"):
                                    payloads[name] = payload
                            row_count = candidate["entry"]["rows_per_table"]
                            validation = self._validate_tables(payloads, candidate["day"], row_count)
                            entry, ids = {**candidate["entry"], "run_dir": str(candidate["directory"]),
                                          "verification_scope": "LOCAL", "eligible_rows": validation["eligible_rows"]}, validation["ids"]
                        else:
                            entry, ids = self._verified_candidate(candidate)
                        if all_ids & ids:
                            raise ValueError("Observation IDs repeat across source sessions")
                        all_ids.update(ids)
                        verified.append(entry)
                    except (OSError, ValueError, KeyError, TypeError) as exc:
                        issues.append({"date": candidate["day"], "reason": self._reason(exc)})
                if signatures != tuple((str(path), self._signature(path)) for path in paths):
                    raise ValueError("Source files changed while building the catalog; refresh after cleaning finishes")
                if progress_signature != self._signature(self.progress):
                    raise ValueError("Cleaner progress changed while building the catalog")
                result = self._result(verified, issues, mode, latest_candidate)
                self._cache = (cache_key, result)
                return deepcopy(result)
            except (OSError, ValueError, KeyError, TypeError) as exc:
                self._cache = None
                return self._result([], [{"date": None, "reason": self._reason(exc)}], mode, latest_candidate)

    @staticmethod
    def _reason(exc):
        if isinstance(exc, FileNotFoundError):
            return "A required local source registry or artifact is missing."
        if isinstance(exc, OSError):
            return "A required local source artifact cannot be safely read."
        if isinstance(exc, (KeyError, TypeError, json.JSONDecodeError)):
            return "Source metadata is incomplete or malformed."
        return str(exc)[:240]

    @staticmethod
    def _result(verified, issues, mode, latest_candidate):
        verified.sort(key=lambda item: item["date"])
        dates = [item["date"] for item in verified]
        rows = sum(item["rows_per_table"] for item in verified)
        available = bool(verified) and not issues
        checked = datetime.now(timezone.utc).isoformat()
        summary = {"available": available, "status": "VERIFIED" if available else "INVALID" if issues else "MISSING",
                   "rows": rows, "eligible_rows": sum(item.get("eligible_rows", 0) for item in verified),
                   "sessions": len(dates), "dates": dates, "latest_date": dates[-1] if dates else None,
                   "latest_candidate_date": latest_candidate, "verified_at": checked, "scope": SCOPE,
                   "issues": issues, "mode": mode}
        return {"status": summary["status"], "checked_at_ist": checked, "verification_scope": "LOCAL",
                "dates": len(dates), "rows_per_table_total": rows, "verified": verified,
                "catalog": summary}

    def summary(self):
        return self.snapshot()["catalog"]

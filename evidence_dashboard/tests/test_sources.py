"""Current cleaner revisions advance the catalog without changing application code."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from uuid import uuid4

import pandas as pd
import pytest

from evidence_dashboard.sources import SourceCatalog


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value))


def hash_record(path):
    return {"sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "size_bytes": path.stat().st_size}


def tables(directory, day, *, marker="a"):
    directory.mkdir(parents=True, exist_ok=True)
    timestamps = pd.to_datetime([f"{day}T03:45:05Z", f"{day}T03:45:10Z"])
    obs = pd.DataFrame({"observation_id": [f"{day}-{marker}-1", f"{day}-{marker}-2"],
                        "timestamps": timestamps, "trading_date": [day, day], "spot": [23000.0, 23001.0],
                        "underlying": ["NIFTY", "NIFTY"], "source_version": ["2", "2"],
                        "iv_available_at": timestamps + pd.Timedelta(seconds=2)})
    opt = pd.DataFrame({"observation_id": obs.observation_id,
                        "greeks_available_at": timestamps + pd.Timedelta(seconds=2), "optiontype": ["CE", "PE"]})
    obs.to_parquet(directory / "observations.parquet", index=False)
    opt.to_parquet(directory / "options.parquet", index=False)


def write_pinned_source(root, days=("2026-09-15", "2026-09-16", "2026-09-17")):
    root = Path(root).resolve()
    entries = []
    for day in days:
        directory = root / "fixtures" / day
        tables(directory, day)
        entries.append({"date": day, "run_dir": str(directory), "rows_per_table": 2,
                        "artifact_checks": {name: {**hash_record(directory / name), "verified": True}
                                            for name in ("observations.parquet", "options.parquet")}})
    path = root / "data_cleaning_agent/runtime/enrichment_verification_2026-09-19.json"
    write_json(path, {"status": "VERIFIED", "verified": entries})
    return path


def write_export(root, day="2026-09-21", *, marker="a", publication="PUBLISHED"):
    root = Path(root).resolve()
    run_id = str(uuid4())
    directory = root / "data_cleaning_agent/output/postgres" / day / run_id
    tables(directory, day, marker=marker)
    (directory / "observations.csv").write_text("observation_id\nfixture\n")
    (directory / "options.csv").write_text("observation_id\nfixture\n")
    write_json(directory / "quality_report.json", {"passed": True, "quarantined_rows": 0,
                                                   "row_count_reconciled": True, "accepted_rows": 2})
    source_hash = hashlib.sha256(f"source-{day}-{marker}".encode()).hexdigest()
    metadata_hash = hashlib.sha256(f"metadata-{day}-{marker}".encode()).hexdigest()
    revision = hashlib.sha256(f"revision-{day}-{marker}".encode()).hexdigest()
    manifest = {"schema_version": 2, "run_id": run_id, "status": "PASS", "source_sha256": source_hash,
                "source_context": {"trading_date": day, "revision": revision, "metadata_sha256": metadata_hash,
                                   "integrity_passed": True, "row_count_reconciled": True},
                "table_counts": {"observations.parquet": 2, "options.parquet": 2},
                "artifacts": {path.name: hash_record(path) for path in directory.iterdir()}}
    write_json(directory / "manifest.json", manifest)
    entry = {"trading_date": day, "status": publication, "revision": revision, "source_sha256": source_hash,
             "metadata_sha256": metadata_hash, "rows": 2,
             "manifest_uri": f"s3://heremesv0-cleaned-data/cleaned/{day}/{run_id}/manifest.json",
             "commit_uri": f"s3://heremesv0-cleaned-data/cleaned/{day}/commits/{revision}.json"}
    if publication != "PUBLISHED":
        entry["run_dir"] = str(directory)
    progress = root / "data_cleaning_agent/runtime/progress.json"
    state = json.loads(progress.read_text()) if progress.exists() else {"version": 1, "days": {}}
    state["days"][day] = entry
    write_json(progress, state)
    return directory, entry


def update_artifact(directory, name, data):
    path = directory / name
    write_json(path, data)
    manifest_path = directory / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["artifacts"][name] = hash_record(path)
    write_json(manifest_path, manifest)


def test_new_completed_session_appears_without_code_or_historical_registry_changes(tmp_path):
    historical = write_pinned_source(tmp_path)
    original = historical.read_bytes()
    write_export(tmp_path, "2026-09-21")
    catalog = SourceCatalog(tmp_path)
    assert catalog.summary()["dates"] == ["2026-09-21"]
    write_export(tmp_path, "2026-09-22")
    summary = catalog.summary()
    assert summary["available"] is True
    assert summary["dates"] == ["2026-09-21", "2026-09-22"]
    assert summary["rows"] == 4
    assert summary["latest_date"] == "2026-09-22"
    assert summary["mode"] == "daily_progress"
    assert historical.read_bytes() == original
    assert catalog.snapshot()["verification_scope"] == "LOCAL"


def test_published_progress_without_run_dir_resolves_fixed_uuid_directory(tmp_path):
    directory, entry = write_export(tmp_path)
    assert "run_dir" not in entry
    snapshot = SourceCatalog(tmp_path).snapshot()
    assert snapshot["status"] == "VERIFIED"
    assert snapshot["verified"][0]["run_dir"] == str(directory)


def test_new_revision_replaces_old_revision_without_duplicating_session(tmp_path):
    first, _ = write_export(tmp_path, marker="old")
    catalog = SourceCatalog(tmp_path)
    assert catalog.snapshot()["verified"][0]["run_dir"] == str(first)
    second, _ = write_export(tmp_path, marker="new")
    result = catalog.snapshot()
    assert result["dates"] == 1
    assert result["rows_per_table_total"] == 2
    assert result["verified"][0]["run_dir"] == str(second)


def test_tamper_with_restored_mtime_invalidates_cached_latest_source(tmp_path):
    directory, _ = write_export(tmp_path)
    catalog = SourceCatalog(tmp_path)
    assert catalog.summary()["available"]
    path = directory / "observations.csv"
    original = path.stat()
    path.write_bytes(path.read_bytes().replace(b"fixture", b"changed"))
    os.utime(path, ns=(original.st_atime_ns, original.st_mtime_ns))
    summary = catalog.summary()
    assert not summary["available"]
    assert summary["status"] == "INVALID"
    assert "checksum" in summary["issues"][0]["reason"]


def test_invalid_newer_entry_is_explicit_and_never_falls_back_to_old_registry(tmp_path):
    write_pinned_source(tmp_path)
    write_export(tmp_path, "2026-09-21")
    directory, _ = write_export(tmp_path, "2026-09-22")
    (directory / "options.parquet").unlink()
    result = SourceCatalog(tmp_path).snapshot()
    assert result["status"] == "INVALID"
    assert result["catalog"]["latest_candidate_date"] == "2026-09-22"
    assert result["catalog"]["latest_date"] == "2026-09-21"
    assert result["catalog"]["issues"][0]["date"] == "2026-09-22"
    assert [item["date"] for item in result["verified"]] == ["2026-09-21"]


def test_retry_publication_local_pass_is_eligible_with_explicit_local_scope(tmp_path):
    write_export(tmp_path, publication="RETRY_PENDING")
    result = SourceCatalog(tmp_path).snapshot()
    assert result["status"] == "VERIFIED"
    assert result["verified"][0]["publication_status"] == "RETRY_PENDING"
    assert result["verified"][0]["verification_scope"] == "LOCAL"


@pytest.mark.parametrize("field,value", [("revision", "0" * 64), ("source_sha256", "0" * 64),
                                           ("metadata_sha256", "0" * 64), ("trading_date", "2026-09-20")])
def test_progress_manifest_identity_mismatch_rejected(tmp_path, field, value):
    write_export(tmp_path)
    path = tmp_path / "data_cleaning_agent/runtime/progress.json"
    progress = json.loads(path.read_text())
    progress["days"]["2026-09-21"][field] = value
    write_json(path, progress)
    assert not SourceCatalog(tmp_path).summary()["available"]


def test_changed_quality_report_cannot_pass_even_with_updated_manifest_hash(tmp_path):
    directory, _ = write_export(tmp_path)
    update_artifact(directory, "quality_report.json", {"passed": False, "quarantined_rows": 0,
                                                      "row_count_reconciled": True, "accepted_rows": 2})
    result = SourceCatalog(tmp_path).summary()
    assert not result["available"]
    assert "quality report" in result["issues"][0]["reason"]


@pytest.mark.parametrize("corruption", ["ids", "availability", "trading_date"])
def test_semantically_invalid_parquet_rejected_after_checksum_verification(tmp_path, corruption):
    directory, _ = write_export(tmp_path)
    filename = "options.parquet" if corruption in ("ids", "availability") else "observations.parquet"
    path = directory / filename
    frame = pd.read_parquet(path)
    if corruption == "ids":
        frame.loc[0, "observation_id"] = "unlinked"
    elif corruption == "availability":
        frame["greeks_available_at"] -= pd.Timedelta(minutes=1)
    else:
        frame["trading_date"] = "2026-09-20"
    frame.to_parquet(path, index=False)
    manifest_path = directory / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["artifacts"][filename] = hash_record(path)
    write_json(manifest_path, manifest)
    assert not SourceCatalog(tmp_path).summary()["available"]


def test_unsafe_manifest_uri_and_symlink_artifacts_are_rejected(tmp_path):
    directory, _ = write_export(tmp_path)
    path = directory / "options.parquet"
    target = tmp_path / "elsewhere.parquet"
    path.rename(target)
    path.symlink_to(target)
    assert not SourceCatalog(tmp_path).summary()["available"]
    path.unlink()
    target.rename(path)
    progress_path = tmp_path / "data_cleaning_agent/runtime/progress.json"
    progress = json.loads(progress_path.read_text())
    progress["days"]["2026-09-21"]["manifest_uri"] = "s3://other-bucket/../../manifest.json"
    write_json(progress_path, progress)
    assert not SourceCatalog(tmp_path).summary()["available"]


def test_historical_fallback_only_when_progress_does_not_exist(tmp_path):
    write_pinned_source(tmp_path)
    catalog = SourceCatalog(tmp_path)
    assert catalog.summary()["available"]
    assert catalog.summary()["mode"] == "historical_registry"
    write_json(catalog.progress, {"days": {}})
    assert not catalog.summary()["available"]
    assert catalog.summary()["sessions"] == 0


def test_snapshot_is_detached_from_cache_and_engine_compatible(tmp_path):
    directory, _ = write_export(tmp_path)
    catalog = SourceCatalog(tmp_path)
    first = catalog.snapshot()
    first["verified"].clear()
    assert len(catalog.snapshot()["verified"]) == 1
    # The existing engine accepts these exact registry fields and table joins.
    from evidence_engine.data import load_verified
    path = tmp_path / "snapshot.json"
    write_json(path, catalog.snapshot())
    frame, metadata = load_verified(path)
    assert len(frame) == 2
    assert metadata["trading_dates"] == ["2026-09-21"]


def test_unchanged_catalog_reuses_expensive_checks(tmp_path, monkeypatch):
    write_export(tmp_path)
    catalog = SourceCatalog(tmp_path)
    assert catalog.summary()["available"]
    monkeypatch.setattr(catalog, "_verify_file", lambda *args, **kwargs: pytest.fail("Unchanged files rehashed"))
    assert catalog.summary()["available"]

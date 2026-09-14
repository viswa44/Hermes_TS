"""Offline publication tests; no AWS account or credential lookup is needed."""

from __future__ import annotations

import base64
import hashlib
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from data_cleaning_agent.tools.s3_tool import publish_run, validate_bucket_name


class FakeS3:
    def __init__(self, fail_at: int | None = None):
        self.calls: list[dict] = []
        self.objects: dict[tuple[str, str], bytes] = {}
        self.fail_at = fail_at

    def put_object(self, **kwargs):
        self.calls.append(kwargs)
        if self.fail_at == len(self.calls):
            raise RuntimeError("simulated upload failure")
        key = (kwargs["Bucket"], kwargs["Key"])
        if key in self.objects and kwargs.get("IfNoneMatch") == "*":
            raise RuntimeError("PreconditionFailed")
        self.objects[key] = kwargs["Body"]
        return {"ETag": '"fake"'}


@pytest.fixture
def run_dir(tmp_path: Path) -> Path:
    directory = tmp_path / str(uuid4())
    directory.mkdir()
    (directory / "observations.parquet").write_bytes(b"example observation artifact")
    (directory / "options.parquet").write_bytes(b"example option artifact")
    (directory / "quality_report.json").write_text(json.dumps({"passed": True}))
    (directory / "quarantine.jsonl").write_text('{"raw": "private rejected row"}\n')
    manifest = {
        "run_id": directory.name,
        "status": "PASS",
        "artifacts": {
            name: {"sha256": hashlib.sha256((directory / name).read_bytes()).hexdigest()}
            for name in ("observations.parquet", "options.parquet", "quality_report.json")
        },
    }
    (directory / "manifest.json").write_text(json.dumps(manifest))
    return directory


def test_publish_is_conditional_encrypted_and_manifest_last(run_dir):
    client = FakeS3()
    locations = publish_run(run_dir, "heremesv0-cleaned-data", client=client)

    assert list(locations) == [
        "observations.parquet", "options.parquet", "quality_report.json", "manifest.json"
    ]
    assert client.calls[-1]["Key"].endswith("/manifest.json")
    assert all("quarantine" not in call["Key"] for call in client.calls)
    for call in client.calls:
        assert call["IfNoneMatch"] == "*"
        assert call["ServerSideEncryption"] == "AES256"
        assert "ACL" not in call
        assert call["ContentLength"] == len(call["Body"])
        assert call["ChecksumSHA256"] == base64.b64encode(
            hashlib.sha256(call["Body"]).digest()
        ).decode("ascii")
    assert locations["manifest.json"] == (
        f"s3://heremesv0-cleaned-data/cleaned/{run_dir.name}/manifest.json"
    )


def test_existing_objects_are_never_overwritten(run_dir):
    client = FakeS3()
    publish_run(run_dir, "heremesv0-cleaned-data", client=client)
    before = dict(client.objects)
    with pytest.raises(RuntimeError, match="PreconditionFailed"):
        publish_run(run_dir, "heremesv0-cleaned-data", client=client)
    assert client.objects == before
    assert len(client.calls) == 5


def test_remote_manifest_scopes_local_evidence_without_uploading_it(run_dir):
    manifest_path = run_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    local_names = ("source.csv", "profile.json", "schema.json", "cleaning_plan.json", "quarantine.jsonl")
    for name in local_names:
        artifact = run_dir / name
        if not artifact.exists():
            artifact.write_text("local evidence")
        payload = artifact.read_bytes()
        manifest["artifacts"][name] = {
            "sha256": hashlib.sha256(payload).hexdigest(),
            "size_bytes": len(payload),
        }
    manifest["source_file"] = "source.csv"
    manifest_path.write_text(json.dumps(manifest))
    local_manifest_bytes = manifest_path.read_bytes()
    client = FakeS3()

    publish_run(run_dir, "heremesv0-cleaned-data", client=client)

    assert len(client.calls) == 4
    remote_manifest = json.loads(client.calls[-1]["Body"])
    assert set(remote_manifest["artifacts"]) == {
        "observations.parquet", "options.parquet", "quality_report.json"
    }
    assert remote_manifest["local_evidence"] == {
        "scope": "local_only",
        "artifacts": {name: manifest["artifacts"][name] for name in local_names},
    }
    assert remote_manifest["source_file"] == "source.csv"
    assert manifest_path.read_bytes() == local_manifest_bytes
    for name, record in remote_manifest["artifacts"].items():
        remote_bytes = client.objects[("heremesv0-cleaned-data", f"cleaned/{run_dir.name}/{name}")]
        assert record["sha256"] == hashlib.sha256(remote_bytes).hexdigest()
    assert all(call["Key"].split("/")[-1] not in local_names for call in client.calls)


@pytest.mark.parametrize("failed_put", [1, 2, 3])
def test_partial_failure_does_not_upload_manifest(run_dir, failed_put):
    client = FakeS3(fail_at=failed_put)
    with pytest.raises(RuntimeError, match="simulated upload failure"):
        publish_run(run_dir, "heremesv0-cleaned-data", client=client)
    assert all(not call["Key"].endswith("/manifest.json") for call in client.calls)


@pytest.mark.parametrize("passed", [False, "true", 1, None])
def test_quality_failure_never_constructs_aws_client(run_dir, monkeypatch, passed):
    def forbid_client(*args, **kwargs):
        raise AssertionError("AWS client must not be constructed for failed quality")

    monkeypatch.setitem(sys.modules, "boto3", SimpleNamespace(client=forbid_client))
    (run_dir / "quality_report.json").write_text(json.dumps({"passed": passed}))
    with pytest.raises(ValueError, match="passing quality report"):
        publish_run(run_dir, "heremesv0-cleaned-data")


@pytest.mark.parametrize("status", ["FAIL", "pass", True, None])
def test_manifest_failure_stops_before_aws(run_dir, status):
    manifest_path = run_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["status"] = status
    manifest_path.write_text(json.dumps(manifest))
    client = FakeS3()
    with pytest.raises(ValueError, match="passing quality report"):
        publish_run(run_dir, "heremesv0-cleaned-data", client=client)
    assert client.calls == []


@pytest.mark.parametrize("filename", ["observations.parquet", "options.parquet", "quality_report.json", "manifest.json"])
def test_all_required_files_checked_before_any_upload(run_dir, filename):
    (run_dir / filename).unlink()
    client = FakeS3()
    with pytest.raises(ValueError, match="required artifact"):
        publish_run(run_dir, "heremesv0-cleaned-data", client=client)
    assert client.calls == []


def test_tampered_artifact_is_not_uploaded(run_dir):
    (run_dir / "options.parquet").write_bytes(b"modified after quality check")
    client = FakeS3()
    with pytest.raises(ValueError, match="checksum"):
        publish_run(run_dir, "heremesv0-cleaned-data", client=client)
    assert client.calls == []


@pytest.mark.parametrize("prefix", ["", "/cleaned", "../cleaned", "a/../b", "a/./b", "a//b", "a/", "a\\b", "s3://bucket/key", "a\nkey", "x" * 901])
def test_unsafe_prefix_is_rejected_before_aws(run_dir, prefix):
    client = FakeS3()
    with pytest.raises(ValueError, match="prefix"):
        publish_run(run_dir, "heremesv0-cleaned-data", prefix=prefix, client=client)
    assert client.calls == []


def test_symlink_artifact_is_rejected(run_dir, tmp_path):
    target = tmp_path / "elsewhere.parquet"
    original = run_dir / "options.parquet"
    original.rename(target)
    original.symlink_to(target)
    client = FakeS3()
    with pytest.raises(ValueError, match="unsafe required artifact"):
        publish_run(run_dir, "heremesv0-cleaned-data", client=client)
    assert client.calls == []


def test_non_uuid_run_id_is_rejected(run_dir):
    renamed = run_dir.with_name("arbitrary-run")
    run_dir.rename(renamed)
    with pytest.raises(ValueError, match="UUID"):
        publish_run(renamed, "heremesv0-cleaned-data", client=FakeS3())


@pytest.mark.parametrize("bucket", [
    "heremesv0-cleaned_data", "UPPER-case", "ab", "a" * 64, "-bucket", "bucket-",
    "a..b", "192.168.1.1", "xn--reserved", "sthree-reserved", "amzn-s3-demo-bucket",
    "bucket-s3alias", "bucket--ol-s3", "bucket.mrap", "bucket--x-s3", "bucket--table-s3",
    "bucket-an", "s3://valid-bucket", "bucket/key", "valid-bucket\n", None,
])
def test_invalid_or_reserved_bucket_names(bucket):
    with pytest.raises(ValueError):
        validate_bucket_name(bucket)


@pytest.mark.parametrize("bucket", [
    "heremesv0-cleaned-data", "example.bucket", "a-b", "bucket-012345678901-ap-south-1-an"
])
def test_valid_bucket_names(bucket):
    assert validate_bucket_name(bucket) == bucket

"""Daily publication tests use memory-only S3 with real ClientError shapes."""

from __future__ import annotations

import base64
import hashlib
import io
import json
from datetime import date, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from botocore.exceptions import ClientError

from data_cleaning_agent.tools.daily_s3 import publish_daily_run, read_daily_commit


BUCKET = "heremesv0-cleaned-data"
PREFIX = "cleaned/postgres"
DAY = date(2026, 9, 11)
REVISION = "a" * 64
SOURCE_HASH = "b" * 64
COMMIT_KEY = f"{PREFIX}/{DAY}/commits/{REVISION}.json"


def aws_error(code: str, status: int = 500) -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": "Fake AWS error"}, "ResponseMetadata": {"HTTPStatusCode": status}}, "S3")


class MemoryS3:
    def __init__(self):
        self.objects = {}
        self.gets = []
        self.puts = []
        self.fail_get = None
        self.fail_put_at = None
        self.on_commit = None

    def get_object(self, **kwargs):
        self.gets.append(kwargs)
        if self.fail_get:
            raise self.fail_get
        key = kwargs["Key"]
        if key not in self.objects:
            raise aws_error("NoSuchKey", 404)
        return {"Body": io.BytesIO(self.objects[key])}

    def put_object(self, **kwargs):
        self.puts.append(kwargs)
        if self.fail_put_at == len(self.puts):
            raise aws_error("InternalError")
        key = kwargs["Key"]
        if key == COMMIT_KEY and self.on_commit:
            callback, self.on_commit = self.on_commit, None
            callback()
        if key in self.objects:
            assert kwargs["IfNoneMatch"] == "*"
            raise aws_error("PreconditionFailed", 412)
        self.objects[key] = kwargs["Body"]
        return {"ETag": '"fake"'}


def make_run(tmp_path: Path) -> Path:
    directory = tmp_path / str(uuid4())
    directory.mkdir()
    (directory / "observations.parquet").write_bytes(b"example observations")
    (directory / "options.parquet").write_bytes(b"example options")
    (directory / "quality_report.json").write_text(json.dumps({"passed": True, "accepted_rows": 3}))
    (directory / "source.csv").write_text("local raw evidence")
    manifest = {
        "schema_version": 1,
        "run_id": directory.name,
        "status": "PASS",
        "source_sha256": SOURCE_HASH,
        "source_context": {"trading_date": DAY.isoformat(), "revision": REVISION},
        "table_counts": {"observations.parquet": 3, "options.parquet": 3},
        "artifacts": {
            name: {"sha256": hashlib.sha256((directory / name).read_bytes()).hexdigest(), "size_bytes": (directory / name).stat().st_size}
            for name in ("observations.parquet", "options.parquet", "quality_report.json", "source.csv")
        },
    }
    (directory / "manifest.json").write_text(json.dumps(manifest))
    return directory


@pytest.fixture
def run_dir(tmp_path):
    return make_run(tmp_path)


def publish(run_dir, client):
    return publish_daily_run(run_dir, BUCKET, PREFIX, DAY, REVISION, client=client)


def read(client):
    return read_daily_commit(BUCKET, PREFIX, DAY, REVISION, client)


def test_daily_commit_is_final_encrypted_conditional_put(run_dir):
    client = MemoryS3()
    result = publish(run_dir, client)
    assert len(client.puts) == 5
    assert client.puts[-2]["Key"].endswith("/manifest.json")
    assert client.puts[-1]["Key"] == COMMIT_KEY
    assert result["reused"] is False
    assert result["commit_uri"] == f"s3://{BUCKET}/{COMMIT_KEY}"
    assert result["source_sha256"] == SOURCE_HASH
    assert result["table_counts"] == {"observations.parquet": 3, "options.parquet": 3}
    assert set(result["locations"]) == {"observations.parquet", "options.parquet", "quality_report.json", "manifest.json"}
    for call in client.puts:
        assert call["ServerSideEncryption"] == "AES256"
        assert call["IfNoneMatch"] == "*"
        assert call["ContentLength"] == len(call["Body"])
        assert call["ChecksumSHA256"] == base64.b64encode(hashlib.sha256(call["Body"]).digest()).decode("ascii")
    assert all("source.csv" not in call["Key"] for call in client.puts)


def test_remote_commit_survives_local_state_loss(run_dir, tmp_path):
    client = MemoryS3()
    first = publish(run_dir, client)
    original = dict(client.objects)
    second = read(client)
    assert second["reused"] is True
    assert second["locations"] == first["locations"]
    assert second["commit_uri"] == first["commit_uri"]
    # A fresh local run UUID for the same revision must reuse the remote winner.
    third = publish(make_run(tmp_path), client)
    assert third["run_id"] == run_dir.name
    assert third["reused"] is True
    assert len(client.puts) == 5
    assert client.objects == original


@pytest.mark.parametrize("code", ["NoSuchKey", "404"])
def test_only_missing_commit_returns_none(code):
    client = MemoryS3()
    client.fail_get = aws_error(code, 404)
    assert read(client) is None
    assert client.puts == []


@pytest.mark.parametrize("code,status", [("AccessDenied", 403), ("NoSuchBucket", 404), ("SlowDown", 503), ("InternalError", 500), ("AccessDenied", 404)])
def test_permissions_bucket_and_transient_errors_are_not_absence(run_dir, code, status):
    client = MemoryS3()
    client.fail_get = aws_error(code, status)
    with pytest.raises(ClientError):
        publish(run_dir, client)
    assert client.puts == []


@pytest.mark.parametrize("failed_put", [1, 2, 3, 4, 5])
def test_failed_artifact_manifest_or_commit_never_marks_complete(run_dir, failed_put):
    client = MemoryS3()
    client.fail_put_at = failed_put
    with pytest.raises(ClientError):
        publish(run_dir, client)
    assert COMMIT_KEY not in client.objects
    assert read(client) is None


def test_retry_after_incomplete_run_uses_new_uuid(run_dir, tmp_path):
    client = MemoryS3()
    client.fail_put_at = 5
    with pytest.raises(ClientError):
        publish(run_dir, client)
    client.fail_put_at = None
    new_run = make_run(tmp_path)
    result = publish(new_run, client)
    assert result["run_id"] == new_run.name
    assert result["reused"] is False
    assert read(client)["run_id"] == new_run.name


def test_concurrent_commit_returns_verified_winner(run_dir, tmp_path):
    client = MemoryS3()
    concurrent_run = make_run(tmp_path)
    client.on_commit = lambda: publish(concurrent_run, client)
    result = publish(run_dir, client)
    assert result["reused"] is True
    assert result["run_id"] == concurrent_run.name
    assert read(client)["run_id"] == concurrent_run.name
    assert len(client.objects) == 9  # Two immutable runs, one winning daily commit.


def test_collision_without_committed_winner_is_not_success(run_dir):
    client = MemoryS3()

    def unresolved_collision():
        raise aws_error("ConditionalRequestConflict", 409)

    client.on_commit = unresolved_collision
    with pytest.raises(ClientError):
        publish(run_dir, client)
    assert read(client) is None


def test_missing_remote_manifest_is_not_missing_commit(run_dir):
    client = MemoryS3()
    result = publish(run_dir, client)
    del client.objects[result["manifest_uri"].removeprefix(f"s3://{BUCKET}/")]
    with pytest.raises(ClientError):
        read(client)
    assert len(client.puts) == 5


def test_modified_manifest_fails_checksum(run_dir):
    client = MemoryS3()
    result = publish(run_dir, client)
    key = result["manifest_uri"].removeprefix(f"s3://{BUCKET}/")
    client.objects[key] += b" "
    with pytest.raises(ValueError, match="checksum"):
        read(client)


@pytest.mark.parametrize("field,value", [
    ("schema_version", True), ("schema_version", 2), ("trading_date", "2026-09-10"),
    ("revision", "c" * 64), ("source_sha256", "bad"), ("manifest_sha256", None),
    ("run_id", "../arbitrary"), ("manifest_uri", "s3://other-bucket/path"),
    ("locations", {}), ("artifacts", {}), ("table_counts", {"observations.parquet": 999}),
])
def test_invalid_commit_is_never_reused(run_dir, field, value):
    client = MemoryS3()
    publish(run_dir, client)
    commit = json.loads(client.objects[COMMIT_KEY])
    commit[field] = value
    client.objects[COMMIT_KEY] = json.dumps(commit).encode()
    with pytest.raises(ValueError):
        read(client)
    assert len(client.puts) == 5


@pytest.mark.parametrize("field,value", [
    ("status", "FAIL"), ("source_context", {}),
    ("source_context", {"trading_date": DAY.isoformat(), "revision": "c" * 64}),
    ("source_context", {"trading_date": "2026-09-10", "revision": REVISION}),
    ("source_sha256", "c" * 64), ("run_id", str(uuid4())),
])
def test_manifest_semantics_checked_even_with_matching_checksum(run_dir, field, value):
    client = MemoryS3()
    result = publish(run_dir, client)
    key = result["manifest_uri"].removeprefix(f"s3://{BUCKET}/")
    manifest = json.loads(client.objects[key])
    manifest[field] = value
    client.objects[key] = json.dumps(manifest).encode()
    commit = json.loads(client.objects[COMMIT_KEY])
    commit["manifest_sha256"] = hashlib.sha256(client.objects[key]).hexdigest()
    client.objects[COMMIT_KEY] = json.dumps(commit).encode()
    with pytest.raises(ValueError, match="does not match"):
        read(client)


@pytest.mark.parametrize("context", [None, {}, {"trading_date": "wrong", "revision": REVISION}])
def test_local_source_context_required_before_aws(run_dir, context):
    client = MemoryS3()
    path = run_dir / "manifest.json"
    manifest = json.loads(path.read_text())
    manifest["source_context"] = context
    path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="does not match"):
        publish(run_dir, client)
    assert client.gets == client.puts == []


@pytest.mark.parametrize("revision", ["", "a" * 63, "A" * 64, "z" * 64, None])
def test_bad_revision_rejected_without_aws(revision):
    client = MemoryS3()
    with pytest.raises(ValueError, match="revision"):
        read_daily_commit(BUCKET, PREFIX, DAY, revision, client)
    assert client.gets == []


def test_datetime_rejected_without_aws():
    client = MemoryS3()
    with pytest.raises(ValueError, match="calendar date"):
        read_daily_commit(BUCKET, PREFIX, datetime(2026, 9, 11), REVISION, client)
    assert client.gets == []


def test_same_revision_different_source_is_rejected(run_dir, tmp_path):
    client = MemoryS3()
    publish(run_dir, client)
    second = make_run(tmp_path)
    path = second / "manifest.json"
    manifest = json.loads(path.read_text())
    manifest["source_sha256"] = "c" * 64
    path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="different source"):
        publish(second, client)
    assert len(client.puts) == 5


def test_manifest_table_counts_can_be_absent(run_dir):
    path = run_dir / "manifest.json"
    manifest = json.loads(path.read_text())
    del manifest["table_counts"]
    path.write_text(json.dumps(manifest))
    client = MemoryS3()
    assert publish(run_dir, client)["table_counts"] == {}
    assert read(client)["table_counts"] == {}

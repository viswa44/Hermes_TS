"""Immutable, date-partitioned S3 commits for repeatable daily cleaning.

A source/pipeline revision is complete only after its conditional commit PUT.
Local state is an optimization: the remote commit and checksummed manifest are
the durable proof used when resuming after a crash or local state loss.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
from datetime import date
from pathlib import Path
from typing import Any
from uuid import UUID

from botocore.exceptions import ClientError

from .s3_tool import _ARTIFACTS, _validated_prefix, publish_run, validate_bucket_name


_HASH = re.compile(r"[0-9a-f]{64}")
_MAX_JSON_BYTES = 2 * 1024 * 1024
_MISSING = {"NoSuchKey", "404"}
_COLLISION = {"PreconditionFailed", "412", "ConditionalRequestConflict", "409"}


def _client(region: str | None = None) -> Any:
    import boto3
    from botocore.config import Config

    return boto3.client(
        "s3",
        region_name=region,
        config=Config(connect_timeout=5, read_timeout=30, retries={"mode": "standard", "total_max_attempts": 3}),
    )


def _identity(bucket: str, base_prefix: str, trading_date: date, revision: str) -> tuple[str, str, str]:
    bucket = validate_bucket_name(bucket)
    if type(trading_date) is not date:
        raise ValueError("trading_date must be a calendar date.")
    if not isinstance(revision, str) or not _HASH.fullmatch(revision):
        raise ValueError("revision must be a lowercase SHA-256 digest.")
    prefix = _validated_prefix(f"{_validated_prefix(base_prefix)}/{trading_date.isoformat()}")
    key = f"{prefix}/commits/{revision}.json"
    if len(key.encode("utf-8")) > 1024:
        raise ValueError("Daily S3 key is too long.")
    return bucket, prefix, key


def _decode(payload: bytes) -> dict[str, Any]:
    try:
        value = json.loads(payload)
    except (ValueError, UnicodeError) as exc:
        raise ValueError("S3 publication metadata must be valid JSON.") from exc
    if not isinstance(value, dict):
        raise ValueError("S3 publication metadata must be a JSON object.")
    return value


def _get_json(client: Any, bucket: str, key: str) -> tuple[dict[str, Any], bytes]:
    response = client.get_object(Bucket=bucket, Key=key)
    body = response["Body"]
    try:
        payload = body.read(_MAX_JSON_BYTES + 1)
    finally:
        body.close()
    if len(payload) > _MAX_JSON_BYTES:
        raise ValueError("S3 publication metadata exceeds the size limit.")
    return _decode(payload), payload


def _hash(value: Any, label: str) -> str:
    if not isinstance(value, str) or not _HASH.fullmatch(value):
        raise ValueError(f"Invalid {label} SHA-256 digest.")
    return value


def _run_id(value: Any) -> str:
    try:
        parsed = UUID(value)
    except (ValueError, AttributeError, TypeError) as exc:
        raise ValueError("Publication run_id must be a canonical lowercase UUID.") from exc
    if value not in (str(parsed), parsed.hex):
        raise ValueError("Publication run_id must be a canonical lowercase UUID.")
    return value


def _table_counts(manifest: dict[str, Any]) -> dict[str, int]:
    counts = manifest.get("table_counts", {})
    if not isinstance(counts, dict) or any(
        name not in {"observations.parquet", "options.parquet"}
        or type(count) is not int or count < 0
        for name, count in counts.items()
    ):
        raise ValueError("Invalid publication table counts.")
    return counts


def _validate_manifest(
    manifest: dict[str, Any], trading_date: date, revision: str, run_id: str, source_sha256: str,
) -> None:
    context = manifest.get("source_context")
    if (
        manifest.get("status") != "PASS"
        or manifest.get("run_id") != run_id
        or manifest.get("source_sha256") != source_sha256
        or not isinstance(context, dict)
        or context.get("trading_date") != trading_date.isoformat()
        or context.get("revision") != revision
    ):
        raise ValueError("Publication manifest does not match its source, date, revision, or PASS status.")
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, dict):
        raise ValueError("Publication manifest must include artifact metadata.")
    for name in _ARTIFACTS:
        record = artifacts.get(name)
        if not isinstance(record, dict):
            raise ValueError("Publication manifest is missing artifact metadata.")
        _hash(record.get("sha256"), "artifact")
        if "size_bytes" in record and (type(record["size_bytes"]) is not int or record["size_bytes"] < 0):
            raise ValueError("Invalid artifact size metadata.")
    _table_counts(manifest)


def read_daily_commit(
    bucket: str, base_prefix: str, trading_date: date, revision: str, client: Any = None,
    *, region: str | None = None,
) -> dict[str, Any] | None:
    """Return a verified existing daily publication, or None for a missing key.

    Permissions, missing buckets, malformed commits, missing manifests and AWS
    service failures propagate. They never mean that a revision is absent.
    Artifact checksums are validated as metadata here; readers still verify
    downloaded Parquet bytes against that metadata before consuming tables.
    """
    bucket, prefix, commit_key = _identity(bucket, base_prefix, trading_date, revision)
    if client is None:
        client = _client(region)
    try:
        commit, _ = _get_json(client, bucket, commit_key)
    except ClientError as exc:
        if str(exc.response.get("Error", {}).get("Code")) in _MISSING:
            return None
        raise
    if (
        type(commit.get("schema_version")) is not int
        or commit["schema_version"] != 1
        or commit.get("trading_date") != trading_date.isoformat()
        or commit.get("revision") != revision
    ):
        raise ValueError("Daily commit identity or schema is invalid.")
    run_id = _run_id(commit.get("run_id"))
    source_hash = _hash(commit.get("source_sha256"), "source")
    manifest_hash = _hash(commit.get("manifest_sha256"), "manifest")
    expected_locations = {
        name: f"s3://{bucket}/{prefix}/{run_id}/{name}"
        for name in (*_ARTIFACTS, "manifest.json")
    }
    if commit.get("locations") != expected_locations or commit.get("manifest_uri") != expected_locations["manifest.json"]:
        raise ValueError("Daily commit contains invalid object locations.")
    manifest, payload = _get_json(client, bucket, f"{prefix}/{run_id}/manifest.json")
    if hashlib.sha256(payload).hexdigest() != manifest_hash:
        raise ValueError("Daily commit manifest checksum does not match.")
    _validate_manifest(manifest, trading_date, revision, run_id, source_hash)
    if set(manifest["artifacts"]) != set(_ARTIFACTS) or commit.get("artifacts") != manifest["artifacts"]:
        raise ValueError("Daily commit artifact metadata does not match its manifest.")
    if "table_counts" not in commit or _table_counts(commit) != _table_counts(manifest):
        raise ValueError("Daily commit table counts do not match its manifest.")
    return {
        **commit,
        "commit_uri": f"s3://{bucket}/{commit_key}",
        "reused": True,
    }


def publish_daily_run(
    run_dir: Path, bucket: str, base_prefix: str, trading_date: date, revision: str,
    *, region: str | None = None, client: Any = None,
) -> dict[str, Any]:
    """Publish a PASS run and atomically commit its immutable daily revision.

    When another publisher wins the conditional commit race, its validated
    publication is returned. Partial runs are never returned as successful;
    retries use new UUID run directories to avoid modifying immutable objects.
    """
    bucket, prefix, commit_key = _identity(bucket, base_prefix, trading_date, revision)
    run_dir = Path(run_dir)
    manifest_path = run_dir / "manifest.json"
    if run_dir.is_symlink() or manifest_path.is_symlink() or not manifest_path.is_file():
        raise ValueError("Daily publication requires a safe local manifest.")
    local_manifest = _decode(manifest_path.read_bytes())
    source_hash = _hash(local_manifest.get("source_sha256"), "source")
    run_id = _run_id(run_dir.name)
    _validate_manifest(local_manifest, trading_date, revision, run_id, source_hash)
    if client is None:
        client = _client(region)
    existing = read_daily_commit(bucket, base_prefix, trading_date, revision, client)
    if existing is not None:
        if existing["source_sha256"] != source_hash:
            raise ValueError("Existing daily revision has a different source checksum.")
        return existing

    locations = publish_run(run_dir, bucket, prefix, region=region, client=client)
    remote_manifest, manifest_payload = _get_json(client, bucket, f"{prefix}/{run_id}/manifest.json")
    _validate_manifest(remote_manifest, trading_date, revision, run_id, source_hash)
    commit = {
        "schema_version": 1,
        "trading_date": trading_date.isoformat(),
        "revision": revision,
        "source_sha256": source_hash,
        "run_id": run_id,
        "manifest_uri": locations["manifest.json"],
        "manifest_sha256": hashlib.sha256(manifest_payload).hexdigest(),
        "locations": locations,
        "artifacts": remote_manifest["artifacts"],
        "table_counts": _table_counts(remote_manifest),
    }
    payload = (json.dumps(commit, indent=2, sort_keys=True, allow_nan=False) + "\n").encode("utf-8")
    try:
        client.put_object(
            Bucket=bucket,
            Key=commit_key,
            Body=payload,
            ContentLength=len(payload),
            ContentType="application/json",
            ServerSideEncryption="AES256",
            ChecksumSHA256=base64.b64encode(hashlib.sha256(payload).digest()).decode("ascii"),
            IfNoneMatch="*",
        )
    except ClientError as exc:
        if str(exc.response.get("Error", {}).get("Code")) not in _COLLISION:
            raise
        winner = read_daily_commit(bucket, base_prefix, trading_date, revision, client)
        if winner is None:
            raise
        if winner["source_sha256"] != source_hash:
            raise ValueError("Concurrent daily revision has a different source checksum.") from exc
        return winner
    return {**commit, "commit_uri": f"s3://{bucket}/{commit_key}", "reused": False}

"""Publish validated run artifacts to immutable S3 keys.

Only a successfully uploaded ``manifest.json`` commits a run. Consumers must
require that manifest and verify its artifact checksums before reading a run.
Quarantined input stays local and is never included in this publisher.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
from pathlib import Path
from typing import Any
from uuid import UUID


_ARTIFACTS = ("observations.parquet", "options.parquet", "quality_report.json")
_RESERVED_PREFIXES = ("xn--", "sthree-", "amzn-s3-demo-")
_RESERVED_SUFFIXES = ("-s3alias", "--ol-s3", ".mrap", "--x-s3", "--table-s3")


def validate_bucket_name(bucket: str) -> str:
    """Validate a general purpose S3 bucket name without making an AWS call.

    Account regional namespace names ending in ``-an`` must include an account
    ID and Region suffix. Bucket ownership/existence is established by AWS at
    upload time; this function only checks the name's syntax.
    """
    if not isinstance(bucket, str) or not re.fullmatch(
        r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]", bucket
    ):
        raise ValueError(
            "S3 bucket names must be 3-63 lowercase letters, digits, periods, "
            "or hyphens, and start/end with a letter or digit; underscores are invalid."
        )
    if (
        ".." in bucket
        or re.fullmatch(r"(?:[0-9]{1,3}\.){3}[0-9]{1,3}", bucket)
        or bucket.startswith(_RESERVED_PREFIXES)
        or bucket.endswith(_RESERVED_SUFFIXES)
    ):
        raise ValueError("Invalid or reserved S3 bucket name.")
    if bucket.endswith("-an") and not re.fullmatch(
        r"[a-z0-9][a-z0-9.-]*-[0-9]{12}-[a-z]{2,}(?:-[a-z]+)+-[0-9]+-an", bucket
    ):
        raise ValueError("The reserved -an suffix requires an account regional bucket name.")
    return bucket


def _validated_prefix(prefix: str) -> str:
    if not isinstance(prefix, str) or not prefix:
        raise ValueError("S3 prefix must be a nonempty relative path.")
    if len(prefix.encode("utf-8")) > 900 or any(
        segment in (".", "..")
        or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", segment)
        for segment in prefix.split("/")
    ):
        raise ValueError("S3 prefix contains an unsafe path segment or is too long.")
    return prefix


def _json_object(payload: bytes, filename: str) -> dict[str, Any]:
    try:
        value = json.loads(payload)
    except (ValueError, UnicodeError) as exc:
        raise ValueError(f"{filename} must contain valid JSON.") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{filename} must contain a JSON object.")
    return value


def publish_run(
    run_dir: Path,
    bucket: str,
    prefix: str = "cleaned",
    region: str | None = None,
    client: Any = None,
) -> dict[str, str]:
    """Publish one PASS run; return filename-to-``s3://`` locations.

    ``manifest.json`` must contain ``status: "PASS"`` and ``artifacts`` mapping
    each of the two Parquet files and the quality report to ``{"sha256": hex}``.
    The report's ``passed`` must be the JSON boolean ``true``. Every required
    artifact and checksum is checked before constructing an AWS client. The
    remote manifest lists only the three published artifacts under ``artifacts``;
    other local checksums move to ``local_evidence.artifacts`` with an explicit
    ``local_only`` scope. The original local manifest remains unchanged.

    Uploads use AES256 server-side encryption and ``IfNoneMatch="*"``. Any
    collision or failed upload propagates without writing the commit manifest.
    Uncommitted objects can remain after a failure; retry with a new run ID.
    No bucket creation, ACL change, deletion, or quarantine upload is performed.
    """
    bucket = validate_bucket_name(bucket)
    prefix = _validated_prefix(prefix)
    run_dir = Path(run_dir)
    if ".." in run_dir.parts or run_dir.is_symlink() or not run_dir.is_dir():
        raise ValueError("Run directory must be an existing directory without traversal or symlinks.")
    run_id = run_dir.name
    try:
        parsed_id = UUID(run_id)
    except (ValueError, AttributeError) as exc:
        raise ValueError("Run directory name must be a UUID.") from exc
    if run_id not in (str(parsed_id), parsed_id.hex):
        raise ValueError("Run directory name must be a canonical lowercase UUID.")

    # Snapshot all bytes before network mutations so the uploaded contents are
    # exactly those validated, even if the local files subsequently change.
    payloads: dict[str, bytes] = {}
    for filename in (*_ARTIFACTS, "manifest.json"):
        artifact = run_dir / filename
        if artifact.is_symlink() or not artifact.is_file():
            raise ValueError(f"Missing or unsafe required artifact: {filename}")
        payloads[filename] = artifact.read_bytes()

    quality = _json_object(payloads["quality_report.json"], "quality_report.json")
    manifest = _json_object(payloads["manifest.json"], "manifest.json")
    if quality.get("passed") is not True or manifest.get("status") != "PASS":
        raise ValueError("S3 publication requires a passing quality report and PASS manifest.")
    if "run_id" in manifest and manifest["run_id"] != run_id:
        raise ValueError("Manifest run_id does not match the run directory.")

    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, dict):
        raise ValueError("Manifest must include artifact SHA-256 checksums.")
    for filename in _ARTIFACTS:
        record = artifacts.get(filename)
        if not isinstance(record, dict) or record.get("sha256") != hashlib.sha256(
            payloads[filename]
        ).hexdigest():
            raise ValueError(f"Missing or mismatched manifest checksum: {filename}")

    remote_manifest = dict(manifest)
    remote_manifest["artifacts"] = {filename: artifacts[filename] for filename in _ARTIFACTS}
    local_artifacts = {name: record for name, record in artifacts.items() if name not in _ARTIFACTS}
    remote_manifest["local_evidence"] = {
        "scope": "local_only",
        "artifacts": local_artifacts,
    }
    payloads["manifest.json"] = (
        json.dumps(remote_manifest, indent=2, sort_keys=True, allow_nan=False) + "\n"
    ).encode("utf-8")

    if client is None:
        import boto3

        client = boto3.client("s3", region_name=region)

    locations: dict[str, str] = {}
    # Dict insertion order makes the manifest the final, conditional PUT.
    for filename, payload in payloads.items():
        key = f"{prefix}/{run_id}/{filename}"
        client.put_object(
            Bucket=bucket,
            Key=key,
            Body=payload,
            ContentLength=len(payload),
            ContentType=("application/json" if filename.endswith(".json") else "application/vnd.apache.parquet"),
            ServerSideEncryption="AES256",
            ChecksumSHA256=base64.b64encode(hashlib.sha256(payload).digest()).decode("ascii"),
            IfNoneMatch="*",
        )
        locations[filename] = f"s3://{bucket}/{key}"
    return locations

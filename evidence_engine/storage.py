"""Immutable evidence artifacts and explicitly invoked S3 publication.

Importing this module never contacts AWS. Local files are published with an
exclusive hard link after their contents have been flushed to disk.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import tempfile
from typing import Any


def canonical_json(value: Any) -> bytes:
    """Serialize JSON data deterministically; reject coercions and nonfinite numbers."""

    def validate(item: Any, ancestors: set[int]) -> None:
        if item is None or isinstance(item, (str, bool, int)):
            return
        if isinstance(item, float):
            if not math.isfinite(item):
                raise ValueError("JSON numbers must be finite")
            return
        if not isinstance(item, (list, dict)):
            raise TypeError(f"Unsupported JSON value type: {type(item).__name__}")
        identity = id(item)
        if identity in ancestors:
            raise ValueError("JSON data cannot contain cycles")
        ancestors.add(identity)
        try:
            if isinstance(item, dict):
                for key, child in item.items():
                    if not isinstance(key, str):
                        raise TypeError("JSON object keys must be strings")
                    validate(child, ancestors)
            else:
                for child in item:
                    validate(child, ancestors)
        finally:
            ancestors.remove(identity)

    validate(value, set())
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")


def _read_regular(path: Path) -> bytes:
    """Read a regular file without following a final symlink."""
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError as exc:
        if path.is_symlink():
            raise ValueError(f"Artifact must be a regular file: {path}") from exc
        raise
    with os.fdopen(descriptor, "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ValueError(f"Artifact must be a regular file: {path}")
        return stream.read()


def digest_file(path: Path) -> str:
    """Return the SHA-256 digest of a regular file's exact bytes."""
    return hashlib.sha256(_read_regular(Path(path))).hexdigest()


def _sync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _mkdir_durable(path: Path) -> None:
    missing: list[Path] = []
    cursor = path
    while not cursor.exists():
        missing.append(cursor)
        cursor = cursor.parent
    path.mkdir(parents=True, exist_ok=True)
    for directory in reversed(missing):
        _sync_directory(directory)
        _sync_directory(directory.parent)


def write_bytes(path: Path, content: bytes) -> str:
    """Durably create an immutable file, accepting retries with identical bytes.

    A conflicting existing file raises ValueError and is never overwritten.
    Each temporary file lives beside its destination so publication is atomic.
    """
    if not isinstance(content, bytes):
        raise TypeError("Artifact content must be bytes")
    path = Path(path)
    digest = hashlib.sha256(content).hexdigest()
    _mkdir_durable(path.parent)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            if _read_regular(path) != content:
                raise ValueError(f"Immutable artifact already contains different bytes: {path}")
        _sync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)
        _sync_directory(path.parent)
    return digest


def write_json(path: Path, value: Any) -> str:
    """Write canonical JSON using the immutable artifact protocol."""
    return write_bytes(path, canonical_json(value))


def _relative_name(name: str) -> str:
    if (
        not isinstance(name, str)
        or not name
        or "\\" in name
        or "\x00" in name
        or any(part in ("", ".", "..") for part in name.split("/"))
        or re.match(r"^[A-Za-z]:", name)
    ):
        raise ValueError("Artifact names must be safe relative paths")
    return name


def _artifact_path(root: Path, name: str) -> Path:
    name = _relative_name(name)
    resolved_root = Path(root).resolve(strict=True)
    path = (resolved_root / name).resolve(strict=True)
    if not path.is_relative_to(resolved_root):
        raise ValueError(f"Artifact path escapes its root: {name}")
    return path


def verify_artifacts(root: Path, manifest: dict[str, str]) -> dict[str, str]:
    """Verify every named digest, rejecting traversal and symlinks escaping root.

    Returns the verified name-to-digest mapping. Missing files raise
    FileNotFoundError; malformed manifests and digest mismatches raise ValueError.
    """
    if not isinstance(manifest, dict):
        raise ValueError("Artifact manifest must map filenames to SHA-256 digests")
    verified: dict[str, str] = {}
    for name, expected in manifest.items():
        if not isinstance(expected, str) or re.fullmatch(r"[0-9a-f]{64}", expected) is None:
            raise ValueError("Manifest SHA-256 digests must contain 64 lowercase hexadecimal characters")
        actual = digest_file(_artifact_path(Path(root), name))
        if actual != expected:
            raise ValueError(f"Artifact digest mismatch: {name}")
        verified[name] = actual
    return verified


def _is_precondition_conflict(error: Exception) -> bool:
    response = getattr(error, "response", {})
    if not isinstance(response, dict):
        return False
    return (
        response.get("Error", {}).get("Code")
        in {"PreconditionFailed", "412", "ConditionalRequestConflict"}
        or response.get("ResponseMetadata", {}).get("HTTPStatusCode") in {409, 412}
    )


def publish_s3(
    root: Path,
    bucket: str,
    prefix: str,
    files: list[str],
    client: Any = None,
    *,
    expected_artifacts: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Publish immutable artifacts, with manifest.json as the final commit object.

    ``files`` must include exactly ``manifest.json`` and its declared artifacts.
    The manifest must describe a registered integrity-PASS run. All local bytes
    are captured and checked against the manifest before the first remote write.
    ``expected_artifacts`` additionally binds all snapshots, including the
    manifest itself, to a previously verified checkpoint. Existing remote
    objects are accepted only when GET confirms identical content. AWS
    credentials are resolved by boto3's normal credential chain when no client
    is supplied; this function never prints credentials or requests.
    """
    if not isinstance(bucket, str) or not bucket.strip():
        raise ValueError("An S3 bucket is required")
    if not isinstance(prefix, str):
        raise ValueError("S3 prefix must be a string")
    normalized_prefix = prefix.rstrip("/")
    if normalized_prefix:
        _relative_name(normalized_prefix)
    if not isinstance(files, list) or not files:
        raise ValueError("An explicit artifact list including manifest.json is required")
    names = [_relative_name(name) for name in files]
    if len(names) != len(set(names)):
        raise ValueError("Artifact names must be unique")
    if "manifest.json" not in names:
        raise ValueError("Publication requires manifest.json")
    names = [name for name in names if name != "manifest.json"] + ["manifest.json"]
    snapshots = []
    for name in names:
        content = _read_regular(_artifact_path(Path(root), name))
        snapshots.append((name, content, hashlib.sha256(content).hexdigest()))
    captured = {name: (content, digest) for name, content, digest in snapshots}

    def check_digests(expected: dict[str, str], required_names: set[str]) -> None:
        if not isinstance(expected, dict) or set(expected) != required_names:
            raise ValueError("Publication artifact list must match the complete manifest")
        for name, digest in expected.items():
            _relative_name(name)
            if not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
                raise ValueError("Publication manifest contains an invalid SHA-256 digest")
            if captured[name][1] != digest:
                raise ValueError(f"Publication artifact digest mismatch: {name}")

    if expected_artifacts is not None:
        check_digests(expected_artifacts, set(names))
    try:
        manifest = json.loads(captured["manifest.json"][0])
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("Publication requires a valid JSON manifest") from exc
    if (
        not isinstance(manifest, dict)
        or manifest.get("status") != "REGISTERED"
        or manifest.get("qa_status") != "PASS"
    ):
        raise ValueError("Publication requires a registered integrity-PASS manifest")
    check_digests(manifest.get("artifacts"), set(names) - {"manifest.json"})
    if not manifest["artifacts"]:
        raise ValueError("Publication manifest must declare evidence artifacts")
    if client is None:
        import boto3

        client = boto3.client("s3")
    objects = []
    for name, content, digest in snapshots:
        key = f"{normalized_prefix}/{name}" if normalized_prefix else name
        status = "created"
        try:
            client.put_object(
                Bucket=bucket,
                Key=key,
                Body=content,
                Metadata={"sha256": digest},
                ServerSideEncryption="AES256",
                IfNoneMatch="*",
            )
        except Exception as exc:
            if not _is_precondition_conflict(exc):
                raise
            existing = client.get_object(Bucket=bucket, Key=key)
            body = existing["Body"]
            try:
                existing_bytes = body.read()
            finally:
                body.close()
            if hashlib.sha256(existing_bytes).hexdigest() != digest:
                raise ValueError(f"Immutable S3 artifact already contains different bytes: {key}") from exc
            status = "already_exists"
        objects.append({"file": name, "key": key, "sha256": digest, "status": status})
    return {"bucket": bucket, "prefix": normalized_prefix, "objects": objects}

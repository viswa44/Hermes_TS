"""Storage checks use temporary directories and an in-memory S3 fake only."""

from concurrent.futures import ThreadPoolExecutor
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest

from evidence_engine.storage import (
    canonical_json,
    digest_file,
    publish_s3,
    verify_artifacts,
    write_bytes,
    write_json,
)


class PreconditionFailed(Exception):
    response = {"Error": {"Code": "PreconditionFailed"}, "ResponseMetadata": {"HTTPStatusCode": 412}}


class FakeS3:
    def __init__(self):
        self.objects = {}
        self.puts = []
        self.gets = []
        self.fail_key = None

    def put_object(self, **request):
        self.puts.append(request)
        key = request["Key"]
        if key == self.fail_key:
            raise RuntimeError("simulated upload failure")
        if key in self.objects:
            raise PreconditionFailed()
        self.objects[key] = request["Body"]
        return {"ETag": "unused"}

    def get_object(self, **request):
        self.gets.append(request)
        return {"Body": io.BytesIO(self.objects[request["Key"]])}


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def test_canonical_json_orders_keys_and_preserves_unicode(self):
        first = {"z": [None, True, 1, 1.5], "a": "₹"}
        second = {"a": "₹", "z": [None, True, 1, 1.5]}
        self.assertEqual(canonical_json(first), canonical_json(second))
        self.assertEqual(canonical_json(first), '{"a":"₹","z":[null,true,1,1.5]}'.encode())

    def test_canonical_json_rejects_invalid_values_and_cycles(self):
        cyclic = []
        cyclic.append(cyclic)
        for invalid in [float("nan"), {"x": float("inf")}, {1: "x"}, (1, 2), {"x": object()}, cyclic]:
            with self.subTest(invalid_type=type(invalid).__name__):
                with self.assertRaises((TypeError, ValueError)):
                    canonical_json(invalid)

    def test_immutable_write_retries_and_rejects_conflicts(self):
        path = self.root / "nested" / "artifact.json"
        expected = hashlib.sha256(b'{"a":1}').hexdigest()
        self.assertEqual(write_json(path, {"a": 1}), expected)
        self.assertEqual(write_bytes(path, b'{"a":1}'), expected)
        self.assertEqual(digest_file(path), expected)
        with self.assertRaises(ValueError):
            write_json(path, {"a": 2})
        self.assertEqual(path.read_bytes(), b'{"a":1}')
        self.assertEqual(list(path.parent.iterdir()), [path])

    def test_competing_writers_cannot_overwrite_one_another(self):
        path = self.root / "concurrent.bin"

        def attempt(content):
            try:
                return write_bytes(path, content)
            except ValueError:
                return None

        with ThreadPoolExecutor(max_workers=8) as workers:
            results = list(workers.map(attempt, [b"a", b"b"] * 12))
        winning_hash = digest_file(path)
        self.assertTrue(any(result is None for result in results))
        self.assertTrue(all(result in (None, winning_hash) for result in results))
        self.assertIn(path.read_bytes(), (b"a", b"b"))
        self.assertEqual(list(self.root.iterdir()), [path])

    def test_existing_symlink_is_never_accepted_as_immutable_destination(self):
        original = self.root / "original"
        original.write_bytes(b"original")
        link = self.root / "link"
        link.symlink_to(original)
        with self.assertRaises(ValueError):
            write_bytes(link, b"original")
        self.assertEqual(original.read_bytes(), b"original")

    def test_manifest_verifies_exact_hashes(self):
        manifest = {"sub/artifact.json": write_json(self.root / "sub/artifact.json", {"ok": True})}
        self.assertEqual(verify_artifacts(self.root, manifest), manifest)
        with self.assertRaises(ValueError):
            verify_artifacts(self.root, {"sub/artifact.json": "0" * 64})
        with self.assertRaises(ValueError):
            verify_artifacts(self.root, {"sub/artifact.json": "invalid"})
        with self.assertRaises(FileNotFoundError):
            verify_artifacts(self.root, {"absent": "0" * 64})

    def test_manifest_rejects_unsafe_paths_and_escaping_symlinks(self):
        for name in ["../artifact", "/tmp/artifact", "sub/../artifact", "sub//artifact", "./artifact", "a\\b", "C:/x"]:
            with self.subTest(name=name):
                with self.assertRaises(ValueError):
                    verify_artifacts(self.root, {name: "0" * 64})
        with tempfile.TemporaryDirectory() as outside:
            destination = Path(outside) / "data"
            destination.write_bytes(b"x")
            (self.root / "escape").symlink_to(destination)
            with self.assertRaises(ValueError):
                verify_artifacts(self.root, {"escape": digest_file(destination)})

    def test_manifest_can_verify_symlink_contained_within_root(self):
        digest = write_bytes(self.root / "data", b"x")
        (self.root / "alias").symlink_to(self.root / "data")
        self.assertEqual(verify_artifacts(self.root, {"alias": digest}), {"alias": digest})

    def prepare_publication(self):
        digest = write_json(self.root / "nested" / "evidence.json", {"value": 5})
        write_json(self.root / "manifest.json", {
            "status": "REGISTERED", "qa_status": "PASS",
            "artifacts": {"nested/evidence.json": digest},
        })
        return ["manifest.json", "nested/evidence.json"]

    def test_s3_publication_is_immutable_encrypted_and_manifest_is_last(self):
        files = self.prepare_publication()
        client = FakeS3()
        receipt = publish_s3(self.root, "evidence-test", "runs/id/", files, client)
        self.assertEqual(receipt["prefix"], "runs/id")
        self.assertEqual([item["Key"] for item in client.puts], ["runs/id/nested/evidence.json", "runs/id/manifest.json"])
        for request in client.puts:
            self.assertEqual(request["IfNoneMatch"], "*")
            self.assertEqual(request["ServerSideEncryption"], "AES256")
            self.assertEqual(request["Metadata"]["sha256"], hashlib.sha256(request["Body"]).hexdigest())
        retry = publish_s3(self.root, "evidence-test", "runs/id", files, client)
        self.assertTrue(all(item["status"] == "already_exists" for item in retry["objects"]))
        self.assertEqual(len(client.gets), 2)

    def test_s3_conflicting_bytes_prevent_manifest_publication(self):
        files = self.prepare_publication()
        client = FakeS3()
        client.objects["runs/id/nested/evidence.json"] = b"different bytes"
        with self.assertRaises(ValueError):
            publish_s3(self.root, "evidence-test", "runs/id", files, client)
        self.assertNotIn("runs/id/manifest.json", client.objects)
        self.assertEqual(len(client.puts), 1)

    def test_s3_failed_upload_prevents_manifest_publication(self):
        files = self.prepare_publication()
        client = FakeS3()
        client.fail_key = "runs/id/nested/evidence.json"
        with self.assertRaisesRegex(RuntimeError, "simulated"):
            publish_s3(self.root, "evidence-test", "runs/id", files, client)
        self.assertNotIn("runs/id/manifest.json", client.objects)
        self.assertEqual(client.gets, [])

    def test_s3_preflights_entire_list_before_upload(self):
        self.prepare_publication()
        client = FakeS3()
        for files in [[], ["nested/evidence.json"], ["manifest.json", "manifest.json"], ["manifest.json", "../outside"]]:
            with self.subTest(files=files):
                with self.assertRaises(ValueError):
                    publish_s3(self.root, "evidence-test", "runs/id", files, client)
        with self.assertRaises(FileNotFoundError):
            publish_s3(self.root, "evidence-test", "runs/id", ["nested/evidence.json", "missing", "manifest.json"], client)
        self.assertEqual(client.puts, [])

    def test_s3_changed_local_bytes_fail_before_any_upload(self):
        files = self.prepare_publication()
        (self.root / "nested" / "evidence.json").write_bytes(b"changed after research")
        client = FakeS3()
        with self.assertRaisesRegex(ValueError, "digest mismatch"):
            publish_s3(self.root, "evidence-test", "runs/id", files, client)
        self.assertEqual(client.puts, [])

    def test_s3_requires_exact_manifest_dependency_closure(self):
        files = self.prepare_publication()
        write_bytes(self.root / "extra.json", b"not bound to manifest")
        client = FakeS3()
        for selection in [["manifest.json"], files + ["extra.json"]]:
            with self.subTest(selection=selection):
                with self.assertRaisesRegex(ValueError, "complete manifest"):
                    publish_s3(self.root, "evidence-test", "runs/id", selection, client)
        self.assertEqual(client.puts, [])

    def test_s3_requires_registered_pass_manifest_with_valid_digests(self):
        files = self.prepare_publication()
        original = json.loads((self.root / "manifest.json").read_text())
        invalid_manifests = [
            [],
            {**original, "status": "FAIL"},
            {**original, "qa_status": "FAIL"},
            {**original, "artifacts": {"nested/evidence.json": "invalid"}},
            {**original, "artifacts": None},
            {**original, "artifacts": {**original["artifacts"], "manifest.json": "0" * 64}},
        ]
        client = FakeS3()
        for manifest in invalid_manifests:
            with self.subTest(manifest=manifest):
                (self.root / "manifest.json").write_bytes(canonical_json(manifest))
                with self.assertRaises(ValueError):
                    publish_s3(self.root, "evidence-test", "runs/id", files, client)
        (self.root / "manifest.json").write_bytes(b"not JSON")
        with self.assertRaisesRegex(ValueError, "valid JSON manifest"):
            publish_s3(self.root, "evidence-test", "runs/id", files, client)
        (self.root / "manifest.json").write_bytes(canonical_json({**original, "artifacts": {}}))
        with self.assertRaisesRegex(ValueError, "declare evidence artifacts"):
            publish_s3(self.root, "evidence-test", "runs/id", ["manifest.json"], client)
        self.assertEqual(client.puts, [])

    def test_s3_checkpoint_digests_bind_manifest_itself(self):
        files = self.prepare_publication()
        expected = {name: digest_file(self.root / name) for name in files}
        client = FakeS3()
        publish_s3(self.root, "evidence-test", "runs/id", files, client,
                   expected_artifacts=expected)
        self.assertEqual(len(client.puts), 2)
        manifest = json.loads((self.root / "manifest.json").read_text())
        (self.root / "manifest.json").write_bytes(canonical_json({**manifest, "run_id": "changed"}))
        unchanged_puts = len(client.puts)
        with self.assertRaisesRegex(ValueError, "digest mismatch: manifest.json"):
            publish_s3(self.root, "evidence-test", "runs/id", files, client,
                       expected_artifacts=expected)
        self.assertEqual(len(client.puts), unchanged_puts)

    def test_s3_uploads_verified_snapshots_even_when_files_change_during_upload(self):
        files = self.prepare_publication()
        expected = {name: (self.root / name).read_bytes() for name in files}
        root = self.root

        class MutatingFakeS3(FakeS3):
            def put_object(self, **request):
                for name in files:
                    (root / name).write_bytes(b"changed during upload")
                return super().put_object(**request)

        client = MutatingFakeS3()
        publish_s3(self.root, "evidence-test", "runs/id", files, client)
        self.assertEqual(client.objects, {f"runs/id/{name}": content for name, content in expected.items()})


if __name__ == "__main__":
    unittest.main()

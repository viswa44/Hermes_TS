"""Real LangGraph/SQLite integration, with deterministic fault injection."""

from contextlib import contextmanager, redirect_stdout
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import pandas as pd
from langgraph.checkpoint.sqlite import SqliteSaver

from evidence_engine import __main__ as cli
from evidence_engine.config import ResearchConfig
from evidence_engine import graph as pipeline
from evidence_engine.storage import digest_file, verify_artifacts


class GraphIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.output = self.root / "output"
        self.source = self.root / "source_registry.json"
        self.source.write_text(json.dumps({"status": "VERIFIED", "verified": []}))
        self.config = ResearchConfig(min_support=1, max_conditions=2)
        self.calls = []

    @contextmanager
    def stages(self, status="PASS"):
        """Replace research computations; keep every graph node/write/checkpoint real."""
        def load(path):
            self.calls.append("align")
            return pd.DataFrame({"observation_id": ["source-row"], "spot": [22000.0]}), {"dataset_id": "test-dataset"}

        def samples(frame, config):
            self.calls.append("detect")
            return pd.DataFrame({"sample_id": ["sample-row"], "label": ["UP_50_80"]}), {"sample_count": 1}

        def discover(frame, dataset_id, config):
            self.calls.append("discover")
            return {"dataset_id": dataset_id, "conditions": [], "partition": {}}

        def scan(frame, discovery, config):
            self.calls.append("scan")
            return pd.DataFrame({"sample_id": ["sample-row"]}), {"summaries": []}

        def audit(*args):
            self.calls.append("audit")
            return {"status": status, "checks": []}

        with patch.multiple(pipeline, load_verified=load, build_samples=samples,
                            discover=discover, scan=scan, audit=audit):
            yield

    def execute(self, run_id, **kwargs):
        return cli.run(self.source, self.output, self.config, run_id=run_id, **kwargs)

    def test_sequential_graph_pass_registers_and_seals_verified_references(self):
        with self.stages():
            result = self.execute("passing")
        self.assertEqual(self.calls, ["align", "detect", "discover", "scan", "audit"])
        self.assertEqual(result["status"], "REGISTERED")
        root = Path(result["run_dir"])
        self.assertTrue((root / "registry.json").exists())
        self.assertFalse((root / "quarantine.json").exists())
        index = json.loads((self.output / "registry" / "passing.json").read_text())
        self.assertEqual(index["manifest_sha256"], digest_file(root / "manifest.json"))
        self.assertEqual(verify_artifacts(root, result["artifacts"]), result["artifacts"])
        manifest = json.loads((root / "manifest.json").read_text())
        self.assertEqual(manifest["qa_status"], "PASS")
        self.assertNotIn("manifest.json", manifest["artifacts"])
        self.assertIn("registry.json", manifest["artifacts"])
        self.assertFalse(any(isinstance(value, pd.DataFrame) for value in result.values()))

    def test_failed_and_insufficient_evidence_are_excluded_from_registry(self):
        for status in ("FAIL", "INSUFFICIENT_DATA"):
            with self.subTest(status=status), self.stages(status):
                result = self.execute(status.lower())
            root = Path(result["run_dir"])
            self.assertEqual(result["status"], status)
            self.assertFalse((root / "registry.json").exists())
            self.assertFalse((self.output / "registry" / f"{status.lower()}.json").exists())
            self.assertTrue((root / "quarantine.json").exists())
            manifest = json.loads((root / "manifest.json").read_text())
            self.assertNotIn("registry.json", manifest["artifacts"])
            self.assertEqual(manifest["qa_status"], status)

    def test_sqlite_resume_retries_write_that_succeeded_before_interruption(self):
        original_write = pipeline.write_json
        interrupted = False

        def interrupt_after_write(path, value):
            nonlocal interrupted
            digest = original_write(path, value)
            if Path(path).as_posix().endswith("agent1/conditions.json") and not interrupted:
                interrupted = True
                raise RuntimeError("interrupted after durable artifact write")
            return digest

        with self.stages():
            with patch.object(pipeline, "write_json", side_effect=interrupt_after_write):
                with self.assertRaisesRegex(RuntimeError, "after durable"):
                    self.execute("interrupted")
            root = self.output / "runs" / "interrupted"
            before = digest_file(root / "agent1/conditions.json")
            with SqliteSaver.from_conn_string(str(self.output / "checkpoints.sqlite3")) as saver:
                snapshot = pipeline.build_graph(saver).get_state({"configurable": {"thread_id": "interrupted"}})
                self.assertEqual(snapshot.next, ("agent1_discover",))
                self.assertNotIn("agent1/conditions.json", snapshot.values["artifacts"])
                self.assertIn("events.parquet", snapshot.values["artifacts"])
            self.calls.clear()
            result = self.execute("interrupted", resume=True)
        self.assertEqual(self.calls, ["discover", "scan", "audit"])
        self.assertEqual(result["status"], "REGISTERED")
        self.assertEqual(digest_file(root / "agent1/conditions.json"), before)
        self.assertEqual(result["artifacts"]["agent1/conditions.json"], before)
        self.assertEqual([path.name for path in (root / "agent1").iterdir()], ["conditions.json"])

    def test_completed_resume_uses_saved_config_and_performs_no_research(self):
        with self.stages():
            before = self.execute("complete")
            self.calls.clear()
            after = cli.run(self.source, self.output, ResearchConfig(min_support=99),
                            run_id="complete", resume=True)
        self.assertEqual(before, after)
        self.assertEqual(self.calls, [])

    def test_resume_rejects_mutated_source_request_or_derived_artifact(self):
        for name in ("source_registry.json", "request.json", "aligned.parquet"):
            with self.subTest(name=name), self.stages():
                run_id = "changed_" + name.replace(".", "_")
                result = self.execute(run_id)
                target = Path(result["run_dir"]) / name
                if name == "request.json":
                    request = json.loads(target.read_text())
                    request["executed_at"] = "2000-01-01T00:00:00+00:00"
                    target.write_text(json.dumps(request))
                else:
                    target.write_bytes(target.read_bytes() + b" ")
                self.calls.clear()
                with self.assertRaises(ValueError):
                    self.execute(run_id, resume=True)
                self.assertEqual(self.calls, [])

    def test_resume_rejects_changed_engine_code(self):
        with self.stages():
            self.execute("code_changed")
            changed = {**cli.engine_hashes(), "graph.py": "0" * 64}
            self.calls.clear()
            with patch.object(cli, "engine_hashes", return_value=changed):
                with self.assertRaisesRegex(ValueError, "Engine code changed"):
                    self.execute("code_changed", resume=True)
        self.assertEqual(self.calls, [])

    def test_cli_refuses_s3_publication_for_quarantined_evidence(self):
        with self.stages("INSUFFICIENT_DATA"), patch.object(cli, "publish_s3") as publish:
            with self.assertRaisesRegex(ValueError, "PASS"):
                cli.main(["--source-registry", str(self.source), "--output", str(self.output),
                          "--run-id", "quarantined", "--s3-bucket", "fake-bucket"])
        publish.assert_not_called()

    def test_cli_publication_receipt_is_identical_after_completed_resume(self):
        uploads = []

        def publish(root, bucket, prefix, files, *, expected_artifacts):
            self.assertEqual(verify_artifacts(root, expected_artifacts), expected_artifacts)
            status = "created" if not uploads else "already_exists"
            uploads.append(list(files))
            return {"bucket": bucket, "prefix": prefix,
                    "objects": [{"file": name, "key": f"{prefix}/{name}",
                                 "sha256": digest_file(root / name), "status": status}
                                for name in files]}

        arguments = ["--source-registry", str(self.source), "--output", str(self.output),
                     "--run-id", "published", "--s3-bucket", "fake-bucket"]
        with self.stages(), patch.object(cli, "publish_s3", side_effect=publish), redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(arguments), 0)
            receipt = self.output / "publication" / "published.json"
            before = receipt.read_bytes()
            self.calls.clear()
            self.assertEqual(cli.main(arguments + ["--resume"]), 0)
        self.assertEqual(receipt.read_bytes(), before)
        self.assertEqual(self.calls, [])
        self.assertEqual(len(uploads), 2)
        self.assertTrue(all("status" not in item for item in json.loads(before)["objects"]))


class RealDataPipelineTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def source_registry(self, days, joint=False):
        entries = []
        for day in days:
            directory = self.root / "input" / day
            directory.mkdir(parents=True)
            rows = []
            start = pd.Timestamp(f"{day} 09:15:00", tz="Asia/Kolkata")
            for seconds in range(0, 2401, 5):
                # Two directional bands separated by flat intervals, selected
                # before any model is fitted and repeated on held-out sessions.
                if seconds <= 600:
                    spot = 22000 + 0.2 * seconds
                elif seconds <= 1200:
                    spot = 22120
                elif seconds <= 1800:
                    spot = 22120 - 0.2 * (seconds - 1200)
                else:
                    spot = 22000
                for side in ("CE", "PE"):
                    rows.append({"observation_id": f"{day}-{seconds}-{side}",
                                 "timestamps": start + pd.Timedelta(seconds=seconds),
                                 "spot": spot, "underlying": "NIFTY", "trading_date": day,
                                 "source_version": "2", "optiontype": side,
                                 "contract_key": f"NIFTY-2026-09-29-22000-{side}"})
                    if joint:
                        sign = 1 if side == "CE" else -1
                        midpoint = 100 + sign * (spot - 22000) / 5
                        rows[-1].update(bid=midpoint - 1, ask=midpoint + 1,
                                        oi=10000 + sign * seconds, volume=20000 + seconds,
                                        iv=.18 + (seconds % 600) / 30000,
                                        iv_available_at=start + pd.Timedelta(seconds=seconds + 1),
                                        greeks_available_at=start + pd.Timedelta(seconds=seconds + 1))
            frame = pd.DataFrame(rows)
            frame[["observation_id", "timestamps", "spot"]].to_parquet(directory / "observations.parquet", index=False)
            frame.drop(columns=["timestamps", "spot"]).to_parquet(directory / "options.parquet", index=False)
            checks = {}
            for name in ("observations.parquet", "options.parquet"):
                payload = (directory / name).read_bytes()
                checks[name] = {"sha256": hashlib.sha256(payload).hexdigest(),
                                "size_bytes": len(payload), "verified": True}
            entries.append({"run_dir": str(directory.relative_to(self.root)), "date": day,
                            "rows_per_table": len(frame), "artifact_checks": checks})
        registry = self.root / "registry.json"
        registry.write_text(json.dumps({"status": "VERIFIED", "verified": entries}))
        return registry

    def test_real_parquet_discovery_scan_audit_and_relative_registry_paths(self):
        source = self.source_registry(["2026-09-16", "2026-09-17"])
        config = ResearchConfig(min_support=2, max_conditions=2, detector_version="spot-endpoint-band-v1",
                                discovery_version="single-feature-training-quantiles-v1")
        result = cli.run(source, self.root / "output", config, run_id="real")
        root = Path(result["run_dir"])
        report = json.loads((root / "agent3/qa_report.json").read_text())
        self.assertEqual(result["status"], "REGISTERED", report)
        self.assertTrue(all(check["passed"] for check in report["checks"]))
        discovery = json.loads((root / "agent1/conditions.json").read_text())
        request = json.loads((root / "request.json").read_text())
        self.assertEqual(request["original_source_registry_sha256"], digest_file(source))
        self.assertEqual(request["source_registry_sha256"], digest_file(root / "source_registry.json"))
        snapshot = json.loads((root / "source_registry.json").read_text())
        self.assertTrue(all(Path(entry["run_dir"]).is_absolute() for entry in snapshot["verified"]))
        self.assertTrue(discovery["conditions"])
        self.assertEqual(discovery["partition"]["discovery_dates"], ["2026-09-16"])
        self.assertEqual(discovery["partition"]["evaluation_dates"], ["2026-09-17"])
        occurrences = pd.read_parquet(root / "agent2/occurrences.parquet")
        self.assertEqual(set(occurrences["partition"]), {"discovery", "evaluation"})
        self.assertEqual(verify_artifacts(root, result["artifacts"]), result["artifacts"])

    def test_real_joint_momentum_graph_preserves_all_four_observations(self):
        source = self.source_registry(["2026-09-16", "2026-09-17"], joint=True)
        result = cli.run(source, self.root / "output", ResearchConfig(min_support=2, max_conditions=2), run_id="joint-real")
        root = Path(result["run_dir"])
        report = json.loads((root / "agent3/qa_report.json").read_text())
        self.assertEqual(result["status"], "REGISTERED", report)
        self.assertTrue(all(check["passed"] for check in report["checks"]))
        discovery = json.loads((root / "agent1/conditions.json").read_text())
        self.assertTrue(discovery["conditions"])
        for rule in discovery["conditions"]:
            self.assertEqual(rule["combination"], "ALL")
            features = {predicate["feature"] for predicate in rule["predicates"]}
            self.assertTrue({"CE_iv_decimal", "PE_iv_decimal"}.issubset(features))
            self.assertTrue(any(name.startswith("CE_past_") for name in features))
            self.assertTrue(any(name.startswith("PE_past_") for name in features))
            self.assertIn(rule["target"], ("UP_MOMENTUM", "DOWN_MOMENTUM"))
        occurrences = pd.read_parquet(root / "agent2/occurrences.parquet")
        self.assertEqual(set(occurrences["partition"]), {"discovery", "evaluation"})
        self.assertTrue(occurrences["feature_value"].isna().all())
        self.assertTrue(occurrences["feature_values_json"].map(lambda value: len(json.loads(value)) == 4).all())
        self.assertEqual(verify_artifacts(root, result["artifacts"]), result["artifacts"])

    def test_single_session_real_data_is_quarantined(self):
        source = self.source_registry(["2026-09-16"])
        output = self.root / "output"
        result = cli.run(source, output, ResearchConfig(min_support=1), run_id="one_session")
        self.assertEqual(result["status"], "INSUFFICIENT_DATA")
        self.assertFalse((output / "registry" / "one_session.json").exists())
        self.assertFalse((Path(result["run_dir"]) / "registry.json").exists())


if __name__ == "__main__":
    unittest.main()

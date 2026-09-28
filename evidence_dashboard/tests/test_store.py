"""Exercise the dashboard's trust boundary, filters, and incomplete states."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import pandas as pd

from evidence_dashboard.store import EvidenceStore, IntegrityError


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, allow_nan=False))


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.store = EvidenceStore(self.root)
        self.run_id = "test-run-01"
        self.directory = self.root / "evidence_engine/output/runs" / self.run_id

    def make_run(self, rows=8):
        self.directory.mkdir(parents=True)
        timestamps = pd.date_range("2026-09-17T09:15:00+05:30", periods=rows, freq="s")
        observations = pd.DataFrame({
            "observation_id": [f"obs-{i:05d}" for i in range(rows)],
            "timestamps": timestamps,
            "trading_date": ["2026-09-17"] * (rows // 2) + ["2026-09-18"] * (rows - rows // 2),
            "spot": [23000.0 + i for i in range(rows)],
            "optiontype": ["CE" if i % 2 == 0 else "PE" for i in range(rows)],
            "symbol": ["NIFTY_CE" if i % 2 == 0 else "NIFTY_PE" for i in range(rows)],
            "iv": [None] + [0.12] * (rows - 1), "oi": [None] + [1000] * (rows - 1),
            "ltp": [100.0] * rows, "volume": [12] * rows,
            "contract_key": ["NIFTY|2026-09-22|23000|CE" if i % 2 == 0 else "NIFTY|2026-09-22|23000|PE" for i in range(rows)],
            "iv_available_at": timestamps + pd.Timedelta(seconds=2),
            "greeks_available_at": timestamps + pd.Timedelta(seconds=3),
            "provider_timestamp": [pd.NaT] * rows,
            "raw_market_fields_json": ["hidden payload"] * rows,
        })
        samples = pd.DataFrame({
            "sample_id": ["sample-01", "sample-02"], "trading_date": ["2026-09-17", "2026-09-18"],
            "anchor_at": pd.to_datetime(["2026-09-17T04:00:00Z", "2026-09-18T04:00:00Z"]),
            "start_at": pd.to_datetime(["2026-09-17T04:00:00Z", "2026-09-18T04:00:00Z"]),
            "end_at": pd.to_datetime(["2026-09-17T04:05:00Z", None]),
            "start_spot": [23000.0, 23100.0], "end_spot": [23060.0, None],
            "label": ["UP_50_80", "UNKNOWN"], "point_change": [60.0, None],
            "start_observation_ids": [["obs-00000"], ["obs-00004"]],
            "outcome_reason": ["complete", "gap_in_outcome_window"],
        })
        occurrences = samples.assign(condition_id="condition-01", partition=["discovery", "evaluation"],
                                     feature="past_spot_change", feature_value=12.5, success=[True, None])
        observations.to_parquet(self.directory / "aligned.parquet", index=False)
        samples.to_parquet(self.directory / "samples.parquet", index=False)
        samples.iloc[:1].to_parquet(self.directory / "events.parquet", index=False)
        (self.directory / "agent2").mkdir()
        occurrences.to_parquet(self.directory / "agent2/occurrences.parquet", index=False)
        documents = {
            "request.json": {"run_id": self.run_id, "executed_at": "2026-09-20T11:00:00Z"},
            "source_registry.json": {"status": "VERIFIED"},
            "dataset.json": {"all_rows": rows + 2, "version_2_rows": rows, "excluded_legacy_rows": 2,
                             "trading_dates": ["2026-09-17", "2026-09-18"]},
            "alignment.json": {"sample_count": 2, "label_counts": {"UP_50_80": 1, "UNKNOWN": 1}},
            "agent1/conditions.json": {"conditions": [{"condition_id": "condition-01", "feature": "past_spot_change",
                                                       "operator": ">=", "threshold": 12.5, "target": "UP_50_80"}],
                                       "partition": {"discovery_dates": ["2026-09-17"], "evaluation_dates": ["2026-09-18"]}},
            "agent2/statistics.json": {"summaries": [{"condition_id": "condition-01", "partition": "discovery", "matches": 1},
                                                    {"condition_id": "condition-01", "partition": "evaluation", "matches": 1}]},
            "agent3/qa_report.json": {"status": "PASS", "run_id": self.run_id,
                                      "checks": [{"check": "fixture_integrity", "passed": True}]},
        }
        for name, value in documents.items():
            write_json(self.directory / name, value)
        write_json(self.directory / "registry.json", {"run_id": self.run_id, "integrity_status": "PASS",
                   "conditions_sha256": digest(self.directory / "agent1/conditions.json"),
                   "qa_sha256": digest(self.directory / "agent3/qa_report.json")})
        self.seal()
        return observations

    def seal(self, qa_status="PASS", registered=True):
        manifest = {"run_id": self.run_id, "executed_at": "2026-09-20T11:00:00Z", "qa_status": qa_status,
                    "status": "REGISTERED" if registered else "FAIL",
                    "artifacts": {str(path.relative_to(self.directory)): digest(path)
                                  for path in self.directory.rglob("*") if path.is_file() and path.name != "manifest.json"}}
        write_json(self.directory / "manifest.json", manifest)
        index = self.root / "evidence_engine/output/registry" / f"{self.run_id}.json"
        if registered:
            write_json(index, {"run_id": self.run_id, "run_dir": str(self.directory), "qa_status": "PASS",
                               "manifest_sha256": digest(self.directory / "manifest.json")})
        else:
            index.unlink(missing_ok=True)

    def test_empty_workspace_and_missing_run_are_useful_empty_states(self):
        status = self.store.status()
        self.assertEqual(status["runs"], [])
        self.assertIsNone(status["selected_run_id"])
        self.assertFalse(status["source"]["available"])
        self.assertEqual(self.store.detail("missing-run")["status"], "NOT_FOUND")
        self.assertEqual(self.store.table("missing-run")["rows"], [])
        self.assertEqual(self.store.series("missing-run")["points"], [])

    def test_registered_detail_joins_rules_to_correct_partition_summaries(self):
        self.make_run()
        detail = self.store.detail(self.run_id)
        self.assertEqual(detail["status"], "REGISTERED")
        self.assertEqual(detail["qa_status"], "PASS")
        self.assertEqual(detail["integrity"]["status"], "VERIFIED")
        self.assertEqual(detail["metrics"], {"source_rows": 10, "eligible_rows": 8, "excluded_rows": 2,
                                          "sample_count": 2, "event_count": 1, "unknown_count": 1, "condition_count": 1})
        self.assertEqual(detail["conditions"][0]["evaluation"]["partition"], "evaluation")
        self.assertTrue(all(stage["status"] == "completed" for stage in detail["pipeline"]))

    def test_pagination_date_side_and_literal_search(self):
        self.make_run()
        table = self.store.table(self.run_id, date="2026-09-18", side="CE", limit=1, offset=1)
        self.assertEqual(table["total"], 2)
        self.assertEqual(table["rows"][0]["observation_id"], "obs-00006")
        self.assertEqual(self.store.table(self.run_id, q="nifty_pe")["total"], 4)
        self.assertEqual(self.store.table(self.run_id, q=".*")["total"], 0)
        self.assertEqual(self.store.table(self.run_id, q="obs-00002")["total"], 1)
        self.assertEqual(self.store.table(self.run_id, offset=1000)["rows"], [])

    def test_joint_run_source_coverage_and_momentum_counts_are_run_specific(self):
        self.make_run()
        request = json.loads((self.directory / "request.json").read_text())
        request["config"] = {"detector_version": "spot-endpoint-momentum-v2",
                             "discovery_version": "joint-ce-pe-iv-training-v2"}
        write_json(self.directory / "request.json", request)
        write_json(self.directory / "source_registry.json", {"status": "VERIFIED", "verified": [
            {"date": "2026-09-17"}, {"date": "2026-09-18"}]})
        write_json(self.directory / "alignment.json", {"sample_count": 7,
                   "label_counts": {"UP_MOMENTUM": 2, "DOWN_MOMENTUM": 3, "OTHER": 1, "UNKNOWN": 1}})
        occurrences = pd.read_parquet(self.directory / "agent2/occurrences.parquet")
        values = {"CE_past_midpoint_return_pct": 1.2, "PE_past_midpoint_return_pct": -1.1,
                  "CE_iv_decimal": .2, "PE_iv_decimal": .21}
        occurrences["feature_values_json"] = json.dumps(values)
        occurrences["feature_value"] = None
        occurrences["feature"] = "COMBINED"
        occurrences.to_parquet(self.directory / "agent2/occurrences.parquet", index=False)
        self.seal()
        detail = self.store.detail(self.run_id)
        self.assertEqual(detail["metrics"]["event_count"], 5)
        self.assertEqual(detail["research"]["mode"], "joint")
        self.assertIn("no upper cap", detail["research"]["target_description"])
        self.assertEqual(detail["source"]["latest_date"], "2026-09-18")
        self.assertEqual(detail["source"]["sessions"], 2)
        table = self.store.table(self.run_id, kind="occurrences")
        self.assertEqual(json.loads(table["rows"][0]["feature_values_json"]), values)

    def test_missing_values_stay_null_and_availability_and_identity_survive(self):
        self.make_run()
        table = self.store.table(self.run_id, limit=1)
        row = table["rows"][0]
        self.assertIsNone(row["iv"])
        self.assertIsNone(row["oi"])
        self.assertIsNone(row["provider_timestamp"])
        self.assertIn("contract_key", table["columns"])
        self.assertGreater(pd.Timestamp(row["iv_available_at"]), pd.Timestamp(row["timestamps"]))
        self.assertNotIn("raw_market_fields_json", table["columns"])
        json.dumps(table, allow_nan=False)

    def test_sample_label_and_occurrence_condition_filters(self):
        self.make_run()
        samples = self.store.table(self.run_id, kind="samples", label="UNKNOWN")
        self.assertEqual(samples["total"], 1)
        self.assertIsNone(samples["rows"][0]["point_change"])
        self.assertEqual(samples["rows"][0]["start_observation_ids"], ["obs-00004"])
        self.assertEqual(self.store.table(self.run_id, kind="events")["total"], 1)
        occurrences = self.store.table(self.run_id, kind="occurrences", condition_id="condition-01", date="2026-09-18")
        self.assertEqual(occurrences["total"], 1)
        self.assertEqual(occurrences["rows"][0]["partition"], "evaluation")
        self.assertEqual(self.store.table(self.run_id, kind="occurrences", condition_id="missing")["total"], 0)

    def test_every_artifact_is_checked_before_trusted_results_are_served(self):
        self.make_run()
        self.store.table(self.run_id)  # Populate both caches before tampering with an unrelated artifact.
        path = self.directory / "agent2/statistics.json"
        original = path.stat()
        path.write_text(path.read_text().replace('"matches": 1', '"matches": 9'))
        os.utime(path, ns=(original.st_atime_ns, original.st_mtime_ns))
        detail = self.store.detail(self.run_id)
        self.assertEqual(detail["status"], "INVALID")
        self.assertEqual(detail["qa_status"], "UNVERIFIED")
        self.assertIsNone(detail["metrics"]["eligible_rows"])
        self.assertEqual(detail["conditions"], [])
        self.assertEqual(self.store.table(self.run_id)["rows"], [])
        self.assertEqual(self.store.series(self.run_id)["points"], [])

    def test_registry_digest_tamper_and_index_removal_invalidate_cached_pass(self):
        self.make_run()
        self.assertEqual(self.store.detail(self.run_id)["qa_status"], "PASS")
        index = self.root / "evidence_engine/output/registry" / f"{self.run_id}.json"
        entry = json.loads(index.read_text())
        entry["manifest_sha256"] = "0" * 64
        write_json(index, entry)
        self.assertEqual(self.store.detail(self.run_id)["status"], "INVALID")
        index.unlink()
        self.assertEqual(self.store.detail(self.run_id)["status"], "UNREGISTERED")
        self.assertEqual(self.store.table(self.run_id)["rows"], [])

    def test_missing_artifact_and_manifest_tamper_fail_closed(self):
        self.make_run()
        self.store.detail(self.run_id)
        (self.directory / "events.parquet").unlink()
        self.assertEqual(self.store.detail(self.run_id)["status"], "INVALID")
        self.assertEqual(self.store.table(self.run_id)["rows"], [])
        manifest = json.loads((self.directory / "manifest.json").read_text())
        manifest["run_id"] = "another-run"
        write_json(self.directory / "manifest.json", manifest)
        self.assertIn("identity", self.store.detail(self.run_id)["integrity"]["reason"])

    def test_no_registered_pass_with_omitted_required_artifact(self):
        self.make_run()
        (self.directory / "samples.parquet").unlink()
        self.seal()  # Even a newly sealed partial artifact list must not claim a registered run.
        self.assertEqual(self.store.detail(self.run_id)["status"], "INVALID")

    def test_no_registered_pass_when_qa_check_disagrees(self):
        self.make_run()
        qa_path = self.directory / "agent3/qa_report.json"
        qa = json.loads(qa_path.read_text())
        qa["checks"][0]["passed"] = False
        write_json(qa_path, qa)
        registry_path = self.directory / "registry.json"
        registry = json.loads(registry_path.read_text())
        registry["qa_sha256"] = digest(qa_path)
        write_json(registry_path, registry)
        self.seal()
        self.assertEqual(self.store.detail(self.run_id)["status"], "INVALID")

    def test_partial_stages_are_visible_without_guessing_a_running_process(self):
        self.make_run()
        for path in self.directory.rglob("*"):
            if path.is_file() and path.name not in ("request.json", "source_registry.json", "aligned.parquet", "dataset.json"):
                path.unlink()
        detail = self.store.detail(self.run_id)
        self.assertEqual(detail["status"], "INCOMPLETE")
        self.assertEqual(detail["pipeline"][0]["status"], "completed")
        self.assertTrue(all(stage["status"] == "pending" for stage in detail["pipeline"][1:]))
        self.assertEqual(self.store.table(self.run_id)["rows"], [])

    def test_failed_qa_is_distinguished_from_incomplete_or_trusted(self):
        self.make_run()
        self.seal(qa_status="FAIL", registered=False)
        detail = self.store.detail(self.run_id)
        self.assertEqual(detail["status"], "FAILED")
        self.assertEqual(detail["qa_status"], "FAIL")
        self.assertEqual(detail["pipeline"][-2]["status"], "failed")
        self.assertFalse(detail["publication"]["published"])

    def test_validation_rejects_paths_invalid_dates_and_unbounded_queries(self):
        for run_id in ("../outside", "/absolute", "safe/child", "..", "", "x" * 102, "bad\\path"):
            with self.subTest(run_id=run_id), self.assertRaises(ValueError):
                self.store.detail(run_id)
        for arguments in ({"limit": 0}, {"limit": 251}, {"limit": True}, {"offset": -1}, {"offset": "0"},
                          {"date": "2026-02-30"}, {"side": "XX"}, {"q": "x" * 201}, {"kind": "secrets"}):
            with self.subTest(arguments=arguments), self.assertRaises(ValueError):
                self.store.table(self.run_id, **arguments)

    def test_symlink_run_is_not_discovered_and_artifact_links_invalidate_pass(self):
        self.make_run()
        self.store.detail(self.run_id)
        (self.directory.parent / "linked-run").symlink_to(self.directory, target_is_directory=True)
        self.assertEqual(len(self.store.status()["runs"]), 1)
        with self.assertRaises(IntegrityError):
            self.store.detail("linked-run")
        path = self.directory / "agent2/statistics.json"
        content = path.read_bytes()
        outside = self.root / "another.json"
        outside.write_bytes(content)
        path.unlink()
        path.symlink_to(outside)
        self.assertEqual(self.store.detail(self.run_id)["status"], "INVALID")

    def test_manifest_artifact_traversal_cannot_be_opened(self):
        self.make_run()
        manifest_path = self.directory / "manifest.json"
        manifest = json.loads(manifest_path.read_text())
        manifest["artifacts"]["../../outside.json"] = "0" * 64
        write_json(manifest_path, manifest)
        self.assertEqual(self.store.detail(self.run_id)["status"], "INVALID")

    def test_series_is_bounded_and_keeps_first_last_quotes(self):
        self.make_run(rows=2000)
        series = self.store.series(self.run_id, date="2026-09-17")
        self.assertEqual(len(series["points"]), 800)
        self.assertEqual(series["points"][0]["spot"], 23000.0)
        self.assertEqual(series["points"][-1]["spot"], 23999.0)
        self.assertEqual(series["events"][0]["label"], "UP_50_80")
        self.assertEqual(self.store.series(self.run_id)["date"], "2026-09-18")
        self.assertEqual(self.store.series(self.run_id, date="2026-09-19")["points"], [])

    def test_chart_segments_use_source_gaps_before_display_decimation(self):
        observations = self.make_run(rows=4000)
        observations["timestamps"] = pd.date_range("2026-09-17T09:15:00+05:30", periods=4000, freq="5s")
        observations.loc[1000:1999, "timestamps"] += pd.Timedelta(minutes=1)
        observations.to_parquet(self.directory / "aligned.parquet", index=False)
        self.seal()
        points = self.store.series(self.run_id, date="2026-09-17")["points"]
        self.assertEqual(len(points), 800)
        self.assertEqual({row["segment"] for row in points}, {0, 1})
        # Normal display spacing can exceed the ten-second source gap limit.
        times = pd.to_datetime([row["time"] for row in points])
        self.assertGreater((times[1:] - times[:-1]).total_seconds().max(), 10)
        changes = [i for i in range(1, len(points)) if points[i]["segment"] != points[i-1]["segment"]]
        self.assertEqual(len(changes), 1)

    def test_unchanged_requests_reuse_verified_hashes_and_bounded_frame(self):
        self.make_run()
        with patch.object(self.store, "_digest", wraps=self.store._digest) as hash_call:
            with patch("evidence_dashboard.store.pd.read_parquet", wraps=pd.read_parquet) as read_call:
                self.store.table(self.run_id)
                hashes = hash_call.call_count
                self.store.table(self.run_id, offset=1)
                self.store.detail(self.run_id)
                self.assertEqual(hash_call.call_count, hashes)
                self.assertEqual(read_call.call_count, 1)
                self.assertLessEqual(self.store._frame_bytes, 128 * 1024 * 1024)

    def test_source_registry_verification_and_tamper_invalidation(self):
        self.make_run()
        directory = self.root / "source"
        directory.mkdir()
        observations = pd.DataFrame({"observation_id": [f"source-{i}" for i in range(8)],
            "timestamps": pd.date_range("2026-09-18T09:15:00+05:30", periods=8, freq="5s"),
            "trading_date": "2026-09-18", "underlying": "NIFTY", "spot": 23000., "source_version": "2"})
        observations.to_parquet(directory / "observations.parquet", index=False)
        observations[["observation_id"]].assign(optiontype="CE").to_parquet(directory / "options.parquet", index=False)
        registry = {"status": "VERIFIED", "checked_at_ist": "2026-09-19T09:00:00+05:30", "verified": [{
            "date": "2026-09-18", "rows_per_table": 8, "run_dir": str(directory),
            "artifact_checks": {name: {"verified": True, "sha256": digest(directory / name),
                                         "size_bytes": (directory / name).stat().st_size}
                                for name in ("observations.parquet", "options.parquet")}}]}
        write_json(self.store.source_registry, registry)
        self.assertEqual(self.store.status()["source"]["rows"], 8)
        self.assertTrue(self.store.status()["source"]["available"])
        (directory / "options.parquet").write_bytes(b"tampered")
        self.assertFalse(self.store.status()["source"]["available"])
        self.assertEqual(self.store.status()["source"]["status"], "INVALID")

    def test_source_outside_workspace_is_not_read(self):
        registry = {"status": "VERIFIED", "verified": [{"date": "2026-09-18", "rows_per_table": 1,
                                                         "run_dir": "/tmp/outside-workspace"}]}
        write_json(self.store.source_registry, registry)
        self.assertEqual(self.store.status()["source"]["status"], "INVALID")

    def test_publication_claim_requires_complete_matching_local_receipt(self):
        self.make_run()
        manifest = json.loads((self.directory / "manifest.json").read_text())
        artifacts = {**manifest["artifacts"], "manifest.json": digest(self.directory / "manifest.json")}
        objects = [{"file": name, "key": f"evidence/{name}", "sha256": digest_} for name, digest_ in artifacts.items()]
        receipt_path = self.root / "evidence_engine/output/publication" / f"{self.run_id}.json"
        write_json(receipt_path, {"bucket": "fixture", "prefix": "evidence", "objects": objects})
        detail = self.store.detail(self.run_id)
        self.assertTrue(detail["publication"]["published"])
        self.assertIsNone(detail["publication"]["verified_at"])
        objects.pop()
        write_json(receipt_path, {"bucket": "fixture", "prefix": "evidence", "objects": objects})
        self.assertFalse(self.store.detail(self.run_id)["publication"]["published"])


if __name__ == "__main__":
    unittest.main()

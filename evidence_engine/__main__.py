"""Run offline evidence research against already-cleaned Hermes snapshots."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
from uuid import uuid4

from .config import ResearchConfig
from .storage import canonical_json, digest_file, publish_s3, verify_artifacts, write_bytes, write_json

WORKSPACE = Path(__file__).resolve().parents[1]


def engine_hashes():
    package = Path(__file__).resolve().parent
    return {path.name: digest_file(path) for path in sorted(package.glob("*.py"))}


def run(source_registry, output, config, *, run_id=None, resume=False):
    # Market rows and checkpoints remain local; exporting traces is not part of this tool.
    os.environ["LANGSMITH_TRACING"] = "false"
    os.environ["LANGCHAIN_TRACING_V2"] = "false"
    from langgraph.checkpoint.sqlite import SqliteSaver
    from .graph import build_graph

    run_id = run_id or str(uuid4())
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,100}", run_id):
        raise ValueError("run_id must be a safe identifier")
    output = Path(output).resolve()
    root = output / "runs" / run_id
    if resume:
        request = json.loads((root / "request.json").read_text())
        if request["run_id"] != run_id:
            raise ValueError("Run identity mismatch")
        if request["engine_sources"] != engine_hashes():
            raise ValueError("Engine code changed; create a new run instead of resuming")
    else:
        source_registry = Path(source_registry).resolve()
        source_bytes = source_registry.read_bytes()
        registry = json.loads(source_bytes)
        # Preserve the meaning of relative artifact paths after snapshotting the
        # registry into a new run directory.
        for entry in registry.get("verified", []):
            path = Path(entry["run_dir"])
            if not path.is_absolute():
                entry["run_dir"] = str((source_registry.parent / path).resolve())
        snapshot_bytes = canonical_json(registry)
        root.mkdir(parents=True, exist_ok=False)
        request = {"run_id": run_id, "executed_at": datetime.now(timezone.utc).isoformat(),
                   "config": config.to_dict(), "source_registry_sha256": hashlib.sha256(snapshot_bytes).hexdigest(),
                   "original_source_registry_sha256": hashlib.sha256(source_bytes).hexdigest(),
                   "engine_sources": engine_hashes()}
        write_bytes(root / "source_registry.json", snapshot_bytes)
        write_json(root / "request.json", request)
    verify_artifacts(root, {"source_registry.json": request["source_registry_sha256"]})
    initial = {"run_id": run_id, "run_dir": str(root), "executed_at": request["executed_at"], "config": request["config"],
               "artifacts": {"source_registry.json": request["source_registry_sha256"],
                             "request.json": digest_file(root / "request.json")}}
    with SqliteSaver.from_conn_string(str(output / "checkpoints.sqlite3")) as checkpointer:
        graph = build_graph(checkpointer)
        thread = {"configurable": {"thread_id": run_id}}
        snapshot = graph.get_state(thread) if resume else None
        if snapshot and snapshot.values:
            if snapshot.values["config"] != request["config"] or snapshot.values["run_dir"] != str(root):
                raise ValueError("Checkpoint and request differ")
            verify_artifacts(root, snapshot.values["artifacts"])
            result = graph.invoke(None, thread, durability="sync") if snapshot.next else snapshot.values
        else:
            result = graph.invoke(initial, thread, durability="sync")
    verify_artifacts(root, result["artifacts"])
    if result["status"] == "REGISTERED":
        write_json(output / "registry" / f"{run_id}.json", {"run_id": run_id, "run_dir": str(root),
                   "manifest_sha256": result["artifacts"]["manifest.json"], "qa_status": result["qa_status"]})
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-registry", type=Path, default=WORKSPACE / "data_cleaning_agent/runtime/enrichment_verification_2026-09-19.json")
    parser.add_argument("--output", type=Path, default=WORKSPACE / "evidence_engine/output")
    parser.add_argument("--run-id")
    parser.add_argument("--resume", action="store_true", help="Resume the exact saved config and source snapshot")
    parser.add_argument("--discovery-end", help="Last discovery session YYYY-MM-DD; later sessions are untouched evaluation")
    parser.add_argument("--min-support", type=int, default=5)
    parser.add_argument("--max-conditions", type=int, default=6)
    parser.add_argument("--s3-bucket", help="Opt in to publish an integrity-PASS run using the normal AWS credential chain")
    parser.add_argument("--s3-prefix", default="evidence-engine")
    args = parser.parse_args(argv)
    if args.resume and not args.run_id:
        parser.error("--resume requires --run-id")
    result = run(args.source_registry, args.output,
                 ResearchConfig(discovery_end=args.discovery_end, min_support=args.min_support, max_conditions=args.max_conditions),
                 run_id=args.run_id, resume=args.resume)
    if args.s3_bucket:
        if result["status"] != "REGISTERED":
            raise ValueError("S3 evidence publication requires integrity PASS")
        prefix = f"{args.s3_prefix.rstrip('/')}/execution_date={result['executed_at'][:10]}/run_id={result['run_id']}"
        receipt = publish_s3(Path(result["run_dir"]), args.s3_bucket, prefix, list(result["artifacts"]),
                             expected_artifacts=result["artifacts"])
        stable_receipt = {"bucket": receipt["bucket"], "prefix": receipt["prefix"],
                          "objects": [{key: value for key, value in item.items() if key != "status"}
                                      for item in receipt["objects"]]}
        write_json(Path(result["run_dir"]).parent.parent / "publication" / f"{result['run_id']}.json", stable_receipt)
    print(json.dumps({"run_id": result["run_id"], "status": result["status"], "qa_status": result["qa_status"],
                      "manifest": str(Path(result["run_dir"]) / "manifest.json")}, indent=2))
    return 0 if result["status"] == "REGISTERED" else 2


if __name__ == "__main__":
    raise SystemExit(main())

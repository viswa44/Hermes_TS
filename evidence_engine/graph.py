"""Sequential LangGraph research workflow with references-only checkpoints."""
from __future__ import annotations

from io import BytesIO
import json
from pathlib import Path
from typing import TypedDict

import pandas as pd
from langgraph.graph import END, START, StateGraph

from . import __version__
from .data import build_samples, load_verified
from .research import audit, discover, scan
from .storage import digest_file, verify_artifacts, write_bytes, write_json


class EvidenceState(TypedDict, total=False):
    run_id: str
    run_dir: str
    executed_at: str
    config: dict
    dataset_id: str
    artifacts: dict[str, str]
    qa_status: str
    status: str


def _json(root, name):
    return json.loads((root / name).read_text())


def _parquet(root, name, frame):
    buffer = BytesIO()
    frame.to_parquet(buffer, index=False)
    return write_bytes(root / name, buffer.getvalue())


def _record(state, updates, **extra):
    return {"artifacts": {**state.get("artifacts", {}), **updates}, **extra}


def _root(state):
    root = Path(state["run_dir"])
    verify_artifacts(root, state.get("artifacts", {}))
    return root


def align_timestamps(state):
    root = _root(state)
    frame, source_manifest = load_verified(root / "source_registry.json")
    updates = {"aligned.parquet": _parquet(root, "aligned.parquet", frame),
               "dataset.json": write_json(root / "dataset.json", source_manifest)}
    return _record(state, updates, dataset_id=source_manifest["dataset_id"])


def detect_events(state):
    root = _root(state)
    frame = pd.read_parquet(root / "aligned.parquet")
    samples, alignment = build_samples(frame, state["config"])
    labels = ("UP_MOMENTUM", "DOWN_MOMENTUM") if state["config"].get("detector_version") == "spot-endpoint-momentum-v2" else ("UP_50_80", "DOWN_50_80")
    events = samples.loc[samples["label"].isin(labels)].copy()
    return _record(state, {"samples.parquet": _parquet(root, "samples.parquet", samples),
                           "events.parquet": _parquet(root, "events.parquet", events),
                           "alignment.json": write_json(root / "alignment.json", alignment)})


def agent1_discover(state):
    root = _root(state)
    result = discover(pd.read_parquet(root / "samples.parquet"), state["dataset_id"], state["config"])
    result.update(run_id=state["run_id"], executed_at=state["executed_at"])
    return _record(state, {"agent1/conditions.json": write_json(root / "agent1/conditions.json", result)})


def agent2_scan(state):
    root = _root(state)
    occurrences, stats = scan(pd.read_parquet(root / "samples.parquet"),
                              _json(root, "agent1/conditions.json"), state["config"])
    stats.update(run_id=state["run_id"], executed_at=state["executed_at"])
    return _record(state, {"agent2/occurrences.parquet": _parquet(root, "agent2/occurrences.parquet", occurrences),
                           "agent2/statistics.json": write_json(root / "agent2/statistics.json", stats)})


def agent3_evidence_qa(state):
    root = _root(state)
    report = audit(pd.read_parquet(root / "samples.parquet"), _json(root, "agent1/conditions.json"),
                   pd.read_parquet(root / "agent2/occurrences.parquet"), _json(root, "agent2/statistics.json"),
                   pd.read_parquet(root / "aligned.parquet"), state["config"],
                   {"run_id": state["run_id"], "executed_at": state["executed_at"]})
    report.update(run_id=state["run_id"], executed_at=state["executed_at"], verified_artifacts=len(state["artifacts"]))
    return _record(state, {"agent3/qa_report.json": write_json(root / "agent3/qa_report.json", report)}, qa_status=report["status"])


def register_evidence(state):
    root = _root(state)
    if state["qa_status"] != "PASS":
        raise ValueError("Only integrity-PASS evidence enters the registry")
    entry = {"run_id": state["run_id"], "dataset_id": state["dataset_id"], "executed_at": state["executed_at"],
             "integrity_status": "PASS", "evidence_status": "EXPLORATORY", "engine_version": __version__,
             "conditions_sha256": state["artifacts"]["agent1/conditions.json"],
             "qa_sha256": state["artifacts"]["agent3/qa_report.json"],
             "note": "Integrity-checked historical evidence; no predictive or trading approval."}
    return _record(state, {"registry.json": write_json(root / "registry.json", entry)}, status="REGISTERED")


def quarantine_evidence(state):
    root = _root(state)
    entry = {"run_id": state["run_id"], "status": state["qa_status"],
             "qa_sha256": state["artifacts"]["agent3/qa_report.json"]}
    return _record(state, {"quarantine.json": write_json(root / "quarantine.json", entry)}, status=state["qa_status"])


def seal_manifest(state):
    root = _root(state)
    manifest = {"run_id": state["run_id"], "dataset_id": state["dataset_id"],
                "executed_at": state["executed_at"], "engine_version": __version__,
                "status": state["status"], "qa_status": state["qa_status"],
                "config": state["config"], "artifacts": state["artifacts"]}
    return _record(state, {"manifest.json": write_json(root / "manifest.json", manifest)})


def build_graph(checkpointer=None):
    graph = StateGraph(EvidenceState)
    for node in (align_timestamps, detect_events, agent1_discover, agent2_scan,
                 agent3_evidence_qa, register_evidence, quarantine_evidence, seal_manifest):
        graph.add_node(node.__name__, node)
    graph.add_edge(START, "align_timestamps")
    graph.add_edge("align_timestamps", "detect_events")
    graph.add_edge("detect_events", "agent1_discover")
    graph.add_edge("agent1_discover", "agent2_scan")
    graph.add_edge("agent2_scan", "agent3_evidence_qa")
    graph.add_conditional_edges("agent3_evidence_qa", lambda s: "register" if s["qa_status"] == "PASS" else "quarantine",
                                {"register": "register_evidence", "quarantine": "quarantine_evidence"})
    graph.add_edge("register_evidence", "seal_manifest")
    graph.add_edge("quarantine_evidence", "seal_manifest")
    graph.add_edge("seal_manifest", END)
    return graph.compile(checkpointer=checkpointer)

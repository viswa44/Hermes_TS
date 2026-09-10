"""Append-only event logging for the Hermes agent controller."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def log_event(control_dir: Path, event: str, **details: Any) -> dict[str, Any]:
    """Record an auditable controller event and return the written record."""
    record = {"time": utc_now(), "event": event, **details}
    log_dir = control_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    with (log_dir / "events.jsonl").open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(record, sort_keys=True) + "\n")
    return record

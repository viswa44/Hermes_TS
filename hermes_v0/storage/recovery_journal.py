"""Durable local outbox. Replays preserve bytes/timestamps; conflicts are quarantined."""

from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
import os
import sqlite3

from hermes_v0.collector.integrity import Observation, IntegrityError, instant


class Journal:
    def __init__(self, path, max_bytes=256 * 1024 * 1024):
        self.path = Path(path)
        self.max_bytes = max_bytes
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self.db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS outbox (
                    id TEXT PRIMARY KEY, digest TEXT NOT NULL, payload TEXT NOT NULL,
                    state TEXT NOT NULL DEFAULT 'PENDING', acknowledged_at TEXT
                );
                CREATE TABLE IF NOT EXISTS incidents (
                    id INTEGER PRIMARY KEY, time TEXT NOT NULL, code TEXT NOT NULL,
                    observation_id TEXT, digest TEXT
                );
                CREATE TABLE IF NOT EXISTS gaps (
                    id INTEGER PRIMARY KEY, recorded_at TEXT NOT NULL,
                    start_at TEXT NOT NULL, end_at TEXT NOT NULL, reason TEXT NOT NULL
                );
            """)
        os.chmod(self.path, 0o600)

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.path, timeout=2)
        try:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA synchronous=FULL")
            with db:
                yield db
        finally:
            db.close()

    def event(self, code, key=None, digest=None):
        # Codes are internal, never HTTP exception strings or provider payloads.
        if not code.replace("_", "").isalnum() or len(code) > 120:
            code = "UNCLASSIFIED_ERROR"
        with self.db() as db:
            db.execute("INSERT INTO incidents(time,code,observation_id,digest) VALUES(?,?,?,?)",
                (datetime.now(timezone.utc).isoformat(), code, key, digest))

    def put(self, observation):
        observation.validate()
        conflict = False
        with self.db() as db:
            old = db.execute("SELECT digest FROM outbox WHERE id=?", (observation.key,)).fetchone()
            if old:
                conflict = old[0] != observation.digest
            else:
                size = db.execute("SELECT coalesce(sum(length(payload)),0) FROM outbox").fetchone()[0]
                if size + len(observation.payload) > self.max_bytes:
                    raise IntegrityError("BufferFull")
                db.execute("INSERT INTO outbox(id,digest,payload) VALUES(?,?,?)",
                    (observation.key, observation.digest, observation.payload))
        if conflict:
            self.event("CONFLICTING_OBSERVATION", observation.key, observation.digest)
            raise IntegrityError("ConflictingObservation")

    def gap(self, start, end, reason):
        start, end = instant(start), instant(end)
        if start >= end or reason not in ("MISSED_SCHEDULED_INTERVALS", "SKIPPED_BACKOFF_OR_LATE_TICK", "REJECTED_CAPTURE"):
            raise IntegrityError("InvalidGap")
        with self.db() as db:
            db.execute("INSERT INTO gaps(recorded_at,start_at,end_at,reason) VALUES(?,?,?,?)",
                (datetime.now(timezone.utc).isoformat(), start.isoformat(), end.isoformat(), reason))

    def pending(self, limit=32):
        with self.db() as db:
            rows = db.execute("SELECT id,digest,payload FROM outbox WHERE state='PENDING' ORDER BY rowid LIMIT ?", (limit,)).fetchall()
        for key, digest, payload in rows:
            try:
                observation = Observation(payload)
                observation.validate()
                if observation.key != key or observation.digest != digest:
                    raise IntegrityError("DigestMismatch")
            except Exception:
                self.quarantine(key, "INVALID_BUFFER_RECORD")
                continue
            yield observation

    def acknowledge(self, observation):
        with self.db() as db:
            db.execute("UPDATE outbox SET state='ACKNOWLEDGED', acknowledged_at=? WHERE id=? AND digest=?",
                (datetime.now(timezone.utc).isoformat(), observation.key, observation.digest))

    def quarantine(self, key, code):
        with self.db() as db:
            db.execute("UPDATE outbox SET state='QUARANTINED' WHERE id=?", (key,))
        self.event(code, key)

    def counts(self):
        with self.db() as db:
            return dict(db.execute("SELECT state,count(*) FROM outbox GROUP BY state").fetchall())

"""Integrity-first NIFTY collection with durable buffering and bounded recovery."""

import argparse
import asyncio
from contextlib import suppress
from datetime import datetime, timezone, date, timedelta
import json
import logging
import os
from pathlib import Path
import signal
import subprocess
import tempfile
import time

import httpx

from hermes_v0.collector.adapters.strict_openalgo import StrictOpenAlgo, AuthenticationRequired
from hermes_v0.collector.integrity import IntegrityError, instant, session_time, IST
from hermes_v0.collector.live_option_metrics import ProcessCollectorLock, resolve_interval
from hermes_v0.collector.scheduler import ClockAlignedScheduler
from hermes_v0.storage.recovery_journal import Journal
from hermes_v0.storage.recovery_writer import RecoveryWriter

RUNTIME = Path(os.environ.get("HERMES_RUNTIME_DIR", str(Path(__file__).parents[1] / "runtime")))
logger = logging.getLogger(__name__)


def write_status(path, data):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".status-")
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(data, stream, sort_keys=True, allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def error_code(error):
    if isinstance(error, BaseExceptionGroup):
        return error_code(error.exceptions[0])
    if isinstance(error, AuthenticationRequired):
        return "LOGIN_REQUIRED"
    if isinstance(error, httpx.HTTPStatusError):
        return "LOGIN_REQUIRED" if error.response.status_code in (401, 403) else "HTTP_" + str(error.response.status_code)
    if isinstance(error, IntegrityError):
        reason = str(error)
        return "INTEGRITY_" + (reason if reason.isalpha() and len(reason) < 80 else type(error).__name__)
    return type(error).__name__


class RecoveryService:
    def __init__(self, adapter, writer, journal, *, status_path=RUNTIME / "status.json"):
        self.adapter, self.writer, self.journal = adapter, writer, journal
        self.status_path = status_path
        self.stopping = asyncio.Event()
        self.state = dict(mode="INTEGRITY_FIRST", pid=os.getpid(),
            started_at=datetime.now(timezone.utc).isoformat(), state="STARTING",
            last_raw_received_at=None, last_database_raw_at=None, last_database_check_at=None,
            provider_freshness="UNVERIFIED", source_error=None, database_error=None,
            cycles_attempted=0, captured=0, rejected=0, skipped=0, derived_missing=0)

    async def publish(self):
        state = dict(self.state, heartbeat_at=datetime.now(timezone.utc).isoformat(),
                     buffer=await asyncio.to_thread(self.journal.counts))
        await asyncio.to_thread(write_status, self.status_path, state)

    async def incident(self, code, key=None):
        logger.warning("Hermes event=%s", code)
        await asyncio.to_thread(self.journal.event, code, key)

    async def flush_once(self):
        await asyncio.wait_for(self.writer.start(), 4)
        for record in await asyncio.to_thread(lambda: list(self.journal.pending())):
            try:
                await asyncio.wait_for(self.writer.store(record), 3)
            except IntegrityError:
                await asyncio.to_thread(self.journal.quarantine, record.key, "DATABASE_CONFLICT_OR_INVALID_PARENT")
                continue
            except Exception as error:
                # Natural-key collisions must never be turned into overwrites.
                if type(error).__name__ in ("UniqueViolationError", "CheckViolationError", "ForeignKeyViolationError", "NotNullViolationError", "DataError"):
                    await asyncio.to_thread(self.journal.quarantine, record.key, "DATABASE_CONSTRAINT_REJECTED")
                    continue
                raise
            await asyncio.to_thread(self.journal.acknowledge, record)
        latest = await asyncio.wait_for(self.writer.latest_raw(), 2)
        self.state["last_database_raw_at"] = latest.isoformat() if latest else None
        self.state["last_database_check_at"] = datetime.now(timezone.utc).isoformat()
        self.state["database_error"] = None

    async def database_worker(self):
        failures = 0
        while not self.stopping.is_set():
            delay = 1
            try:
                await self.flush_once()
                failures = 0
            except Exception as error:
                failures += 1
                self.state["database_error"] = error_code(error)
                await self.incident("DATABASE_UNAVAILABLE")
                with suppress(Exception):
                    await asyncio.wait_for(self.writer.stop(), 2)
                delay = min(60, 2 ** min(failures, 6))
            await self.publish()
            with suppress(TimeoutError):
                await asyncio.wait_for(self.stopping.wait(), delay)

    async def calculate(self, raw):
        try:
            results = await asyncio.wait_for(self.adapter.derived(raw), 2)
            for result in results:
                if isinstance(result, Exception):
                    self.state["derived_missing"] += 1
                    await self.incident("DERIVED_UNAVAILABLE", raw.key)
                else:
                    await asyncio.to_thread(self.journal.put, result)
        except Exception:
            self.state["derived_missing"] += 2
            await self.incident("DERIVED_UNAVAILABLE", raw.key)

    async def run(self, scheduler, cycles=None):
        db_task = asyncio.create_task(self.database_worker())
        greek_task = None
        failures, next_request, last_slot = 0, 0.0, None
        try:
            await self.adapter.start()
            self.state["state"] = "COLLECTING"
            await self.publish()
            async for expected, _ in scheduler.tick_generator():
                now = datetime.now(timezone.utc)
                interval = timedelta(seconds=scheduler.interval_seconds)
                self.state["sampler_heartbeat_at"] = now.isoformat()
                if last_slot is not None:
                    missed = max(0, int((expected-last_slot).total_seconds()/scheduler.interval_seconds)-1)
                    if missed:
                        self.state["skipped"] += missed
                        await asyncio.to_thread(self.journal.gap, last_slot+interval, expected, "MISSED_SCHEDULED_INTERVALS")
                        await self.incident("MISSED_SCHEDULED_INTERVALS")
                last_slot = expected
                if time.monotonic() < next_request or (now-instant(expected)).total_seconds() > .5:
                    self.state["skipped"] += 1
                    await asyncio.to_thread(self.journal.gap, expected, expected+interval, "SKIPPED_BACKOFF_OR_LATE_TICK")
                    await self.incident("SKIPPED_BACKOFF_OR_LATE_TICK")
                    await self.publish()
                    continue
                self.state["cycles_attempted"] += 1
                try:
                    # Calendar and expiry discovery are bounded and never guessed.
                    if self.adapter.prepared_date != now.astimezone(IST).date():
                        await asyncio.wait_for(self.adapter.prepare(), 3)
                    if (datetime.now(timezone.utc)-instant(expected)).total_seconds() > .5:
                        raise IntegrityError("PreparationExceededSlot")
                    raw = await asyncio.wait_for(self.adapter.raw(expected), 3.5)
                    await asyncio.to_thread(self.journal.put, raw)
                    self.state["captured"] += 1
                    self.state["last_raw_received_at"] = raw.data["received_at"]
                    self.state["source_error"] = None
                    failures = 0
                    # Raw is durable before any calculated request starts. No unbounded
                    # derived queue: skip derived work if the previous job has not ended.
                    if greek_task is None or greek_task.done():
                        greek_task = asyncio.create_task(self.calculate(raw))
                    else:
                        self.state["derived_missing"] += 2
                        await self.incident("DERIVED_BACKPRESSURE", raw.key)
                except Exception as error:
                    failures += 1
                    self.state["rejected"] += 1
                    await asyncio.to_thread(self.journal.gap, expected, expected+interval, "REJECTED_CAPTURE")
                    self.state["source_error"] = error_code(error)
                    self.adapter.prepared_date = None if self.state["source_error"] == "LOGIN_REQUIRED" else self.adapter.prepared_date
                    await self.incident(self.state["source_error"])
                    next_request = time.monotonic() + min(60, 2 ** min(failures, 6))
                await self.publish()
                if cycles is not None and self.state["cycles_attempted"] >= cycles:
                    break
        finally:
            scheduler.stop()
            if greek_task is not None:
                greek_task.cancel()
                with suppress(asyncio.CancelledError):
                    await greek_task
            self.stopping.set()
            db_task.cancel()
            with suppress(asyncio.CancelledError):
                await db_task
            with suppress(Exception):
                await asyncio.wait_for(self.writer.stop(), 2)
            await self.adapter.stop()
            self.state["state"] = "STOPPED"
            await self.publish()
        return self.state


async def main_async(args):
    if args.status:
        print((RUNTIME / "status.json").read_text() if (RUNTIME / "status.json").exists() else '{"state":"NEVER_STARTED"}')
        return 0
    if not args.history_date and not args.replay and not session_time(datetime.now(timezone.utc)):
        # Safe after-close launchd runs do no provider/database I/O.
        print('{"state":"MARKET_CLOSED","collection_started":false}')
        return 0
    adapter = StrictOpenAlgo()
    if adapter.config.underlying != "NIFTY" or (not adapter.config.api_key and not args.replay):
        raise IntegrityError("NiftyAndApiKeyRequired")
    journal = Journal(RUNTIME / "outbox.sqlite3")
    service = RecoveryService(adapter, RecoveryWriter(), journal)
    if args.history_date:
        day = date.fromisoformat(args.history_date)
        if not args.symbol or not args.exchange:
            raise IntegrityError("HistoryRequiresExactSymbolAndExchange")
        await adapter.start()
        try:
            record = await asyncio.wait_for(adapter.history(args.symbol, args.exchange, day), 30)
            await asyncio.to_thread(journal.put, record)
        finally:
            await adapter.stop()
        try:
            await service.flush_once()
        finally:
            await service.writer.stop()
        print(json.dumps(dict(classification="HISTORICAL_BACKFILL", observation_id=record.key)))
        return 0
    if args.replay:
        try:
            await service.flush_once()
        finally:
            await service.writer.stop()
        return 0
    process = None
    if Path("/usr/bin/caffeinate").exists():
        process = subprocess.Popen(["/usr/bin/caffeinate", "-i", "-w", str(os.getpid())],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    task = asyncio.current_task()
    asyncio.get_running_loop().add_signal_handler(signal.SIGTERM, task.cancel)
    try:
        result = await service.run(ClockAlignedScheduler(interval_seconds=args.interval_seconds), args.cycles)
        print(json.dumps(result, sort_keys=True))
    finally:
        if process:
            process.terminate()
            process.wait(timeout=2)
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--interval-seconds", type=int, default=5)
    parser.add_argument("--cycles", type=int)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--status", action="store_true")
    modes.add_argument("--replay", action="store_true")
    modes.add_argument("--history-date")
    parser.add_argument("--symbol")
    parser.add_argument("--exchange", choices=("NSE_INDEX", "NFO"))
    args = parser.parse_args()
    args.interval_seconds = resolve_interval(args.interval_seconds)
    if args.cycles is not None and args.cycles <= 0:
        parser.error("--cycles must be positive")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    # HTTP logs/tracebacks can contain secrets. Emit only controlled incident codes.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    try:
        if args.status:
            return asyncio.run(main_async(args))
        with ProcessCollectorLock():
            return asyncio.run(main_async(args))
    except (Exception, asyncio.CancelledError) as error:
        print(json.dumps(dict(state="STOPPED", error=error_code(error))))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

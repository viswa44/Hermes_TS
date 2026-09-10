"""Hermes V0 Collector Service - Main orchestration.
service.py
Phase 2: Clock-aligned 5-second snapshot collector with:
- Asyncio-based scheduler
- Dependency injection (adapter, validator, storage)
- Structured logging
- Retries with bounded backoff
- Graceful shutdown
- Health endpoint
"""

import asyncio
import signal
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Optional, Callable, Awaitable
from dataclasses import dataclass, field

from hermes_v0.domain.models import MarketSnapshot, DataStatus, SystemEvent
from hermes_v0.collector.scheduler import ClockAlignedScheduler, DriftMonitor
from hermes_v0.collector.validator import SnapshotValidator, ValidationResult
from hermes_v0.collector.adapters.openalgo_adapter import OpenAlgoAdapter
from hermes_v0.collector.openalgo_connection import (
    ConnectionStatus,
    OpenAlgoConnection,
)
from hermes_v0.storage.writer import SnapshotWriter
from hermes_v0.config import CFG


# Structured logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("hermes.collector")


class CollectorService:
    """Main collector service orchestrating the pipeline."""
    
    def __init__(
        self,
        adapter: OpenAlgoAdapter,
        validator: SnapshotValidator,
        writer: SnapshotWriter,
        event_callback: Optional[Callable[[SystemEvent], Awaitable[None]]] = None,
        connection: Optional[OpenAlgoConnection] = None,
    ):
        self.config = CFG.collector
        self.adapter = adapter
        self.validator = validator
        self.writer = writer
        self.event_callback = event_callback
        # B01 startup gate: do not start the collector if OpenAlgo is unavailable.
        self.connection = connection or OpenAlgoConnection()
        
        self.scheduler = ClockAlignedScheduler(
            interval_seconds=self.config.interval_seconds,
            market_open=self.config.market_open,
            market_close=self.config.market_close,
            timezone=self.config.timezone,
        )
        self.drift_monitor = DriftMonitor(max_drift_ms=self.config.max_drift_ms)
        
        self._running = False
        self._shutdown_event = asyncio.Event()
        self._collection_task: Optional[asyncio.Task] = None
        self._health_server: Optional[asyncio.Server] = None
        
        # Stats
        self.stats = {
            "snapshots_collected": 0,
            "snapshots_stored": 0,
            "snapshots_failed": 0,
            "validations_failed": 0,
            "retries": 0,
            "start_time": None,
            "last_snapshot_time": None,
            "last_error": None,
            "openalgo_connection": self.connection.get_status(),
        }
    
    async def start(self):
        """Start the collector service."""
        if self._running:
            logger.warning("Collector already running")
            return
        
        logger.info("Starting Hermes V0 Collector")
        connection_state = await self.connection.connect()
        self.stats["openalgo_connection"] = connection_state.as_dict()
        if connection_state.status == ConnectionStatus.DISCONNECTED:
            self.stats["last_error"] = connection_state.detail
            # Do not start storage, the health server, or market collection on a
            # failed provider connection; this keeps startup failure explicit.
            raise ConnectionError(
                f"OpenAlgo connection failed after {connection_state.attempts} attempts: "
                f"{connection_state.detail}"
            )

        self._running = True
        self.stats["start_time"] = datetime.now(timezone.utc)
        
        # Emit start event
        await self._emit_event(SystemEvent(
            event_time=datetime.now(timezone.utc),
            event_type="COLLECTOR_START",
            severity="INFO",
            message="Collector started",
            trading_date=self._current_trading_date(),
        ))
        
        # Start adapter
        await self.adapter.start()
        
        # Start writer
        await self.writer.start()
        
        # Start health server
        await self._start_health_server()
        
        # Start collection loop
        self._collection_task = asyncio.create_task(self._collection_loop())
        
        logger.info("Collector started successfully")
    
    async def stop(self):
        """Stop the collector service gracefully."""
        if not self._running:
            return
        
        logger.info("Stopping Hermes V0 Collector")
        self._running = False
        self._shutdown_event.set()
        
        # Stop collection loop
        if self._collection_task:
            self._collection_task.cancel()
            try:
                await self._collection_task
            except asyncio.CancelledError:
                pass
        
        # Stop scheduler
        self.scheduler.stop()
        
        # Stop adapter
        await self.adapter.stop()
        
        # Stop writer (flush remaining)
        await self.writer.stop()
        
        # Stop health server
        if self._health_server:
            self._health_server.close()
            await self._health_server.wait_closed()
        
        # Emit stop event
        await self._emit_event(SystemEvent(
            event_time=datetime.now(timezone.utc),
            event_type="COLLECTOR_STOP",
            severity="INFO",
            message="Collector stopped",
            trading_date=self._current_trading_date(),
        ))
        
        logger.info("Collector stopped")
    
    async def _collection_loop(self):
        """Main collection loop - runs on scheduler ticks."""
        try:
            async for expected_time, trading_date in self.scheduler.tick_generator():
                if not self._running:
                    break
                
                actual_time = datetime.now(self.scheduler.tz)
                self.drift_monitor.record(expected_time, actual_time)
                
                # Collect snapshot with retries
                snapshot = await self._collect_with_retry(expected_time, trading_date)
                
                if snapshot:
                    # Validate
                    validated = self.validator.validate_and_enrich(snapshot)
                    
                    # Update stats
                    self.stats["snapshots_collected"] += 1
                    self.stats["last_snapshot_time"] = datetime.now(timezone.utc)
                    
                    if validated.data_status == DataStatus.REJECTED:
                        self.stats["validations_failed"] += 1
                        await self._emit_event(SystemEvent(
                            event_time=datetime.now(timezone.utc),
                            event_type="VALIDATION_FAIL",
                            severity="WARN",
                            message=f"Snapshot rejected: {validated.data_status}",
                            trading_date=trading_date,
                            snapshot_timestamp_ist=expected_time,
                        ))
                    
                    # Write to storage
                    try:
                        await self.writer.write(validated)
                        self.stats["snapshots_stored"] += 1
                    except Exception as e:
                        self.stats["snapshots_failed"] += 1
                        self.stats["last_error"] = str(e)
                        logger.error(f"Failed to write snapshot: {e}")
                        await self._emit_event(SystemEvent(
                            event_time=datetime.now(timezone.utc),
                            event_type="DB_WRITE_FAIL",
                            severity="ERROR",
                            message=f"Database write failed: {e}",
                            trading_date=trading_date,
                            snapshot_timestamp_ist=expected_time,
                        ))
                else:
                    self.stats["snapshots_failed"] += 1
                
                # Check drift health
                if not self.drift_monitor.is_healthy():
                    drift_stats = self.drift_monitor.get_stats()
                    await self._emit_event(SystemEvent(
                        event_time=datetime.now(timezone.utc),
                        event_type="MISSED_INTERVAL",
                        severity="WARN",
                        message=f"Drift exceeded threshold: {drift_stats['max_drift_ms']:.1f}ms",
                        trading_date=trading_date,
                    ))
        
        except asyncio.CancelledError:
            logger.info("Collection loop cancelled")
        except Exception as e:
            logger.exception(f"Collection loop error: {e}")
            self.stats["last_error"] = str(e)
            await self._emit_event(SystemEvent(
                event_time=datetime.now(timezone.utc),
                event_type="COLLECTOR_ERROR",
                severity="CRITICAL",
                message=f"Collection loop crashed: {e}",
                trading_date=self._current_trading_date(),
            ))
            raise
    
    async def _collect_with_retry(
        self, 
        timestamp: datetime, 
        trading_date: str
    ) -> Optional[MarketSnapshot]:
        """Collect snapshot with exponential backoff retry."""
        last_error = None
        
        for attempt in range(self.config.max_retries + 1):
            try:
                snapshot = await asyncio.wait_for(
                    self.adapter.collect_snapshot(timestamp, trading_date),
                    timeout=self.config.request_timeout_seconds,
                )
                return snapshot
                
            except asyncio.TimeoutError:
                last_error = "Collection timeout"
            except Exception as e:
                last_error = str(e)
            
            if attempt < self.config.max_retries:
                self.stats["retries"] += 1
                backoff = min(
                    self.config.base_backoff_ms * (2 ** attempt),
                    self.config.max_backoff_ms
                )
                await asyncio.sleep(backoff / 1000)
                logger.warning(f"Retry {attempt + 1}/{self.config.max_retries} after {backoff}ms: {last_error}")
        
        logger.error(f"All retries exhausted: {last_error}")
        self.stats["last_error"] = last_error
        return None
    
    def _current_trading_date(self) -> str:
        """Get current trading date in YYYY-MM-DD format."""
        now = datetime.now(self.scheduler.tz)
        return self.scheduler._trading_date(now)
    
    async def _emit_event(self, event: SystemEvent):
        """Emit system event via callback if registered."""
        if self.event_callback:
            try:
                await self.event_callback(event)
            except Exception as e:
                logger.error(f"Event callback failed: {e}")
    
    async def _start_health_server(self):
        """Start HTTP health check server."""
        from aiohttp import web
        
        async def health(request):
            drift_stats = self.drift_monitor.get_stats()
            return web.json_response({
                "status": "healthy" if self.drift_monitor.is_healthy() else "degraded",
                "running": self._running,
                "stats": self.stats,
                "drift": drift_stats,
                "adapter": await self.adapter.health_check(),
                "writer": await self.writer.health_check(),
            })
        
        async def ready(request):
            return web.json_response({
                "ready": self._running and self.drift_monitor.is_healthy()
            })
        
        app = web.Application()
        app.router.add_get("/health", health)
        app.router.add_get("/ready", ready)
        
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "0.0.0.0", self.config.health_port)
        await site.start()
        self._health_server = runner
        logger.info(f"Health server started on port {self.config.health_port}")
    
    def get_stats(self) -> dict:
        """Get collector statistics."""
        return {
            **self.stats,
            "openalgo_connection": self.connection.get_status(),
            "drift": self.drift_monitor.get_stats(),
            "uptime_seconds": (
                datetime.now(timezone.utc) - self.stats["start_time"]
            ).total_seconds() if self.stats["start_time"] else 0,
        }


@asynccontextmanager
async def create_collector(
    adapter: Optional[OpenAlgoAdapter] = None,
    validator: Optional[SnapshotValidator] = None,
    writer: Optional[SnapshotWriter] = None,
    connection: Optional[OpenAlgoConnection] = None,
) -> CollectorService:
    """Factory context manager for collector service."""
    adapter = adapter or OpenAlgoAdapter()
    validator = validator or SnapshotValidator()
    writer = writer or SnapshotWriter()
    
    service = CollectorService(adapter, validator, writer, connection=connection)
    await service.start()
    try:
        yield service
    finally:
        await service.stop()


# Signal handling for graceful shutdown
def setup_signal_handlers(service: CollectorService):
    """Set up SIGINT/SIGTERM handlers."""
    loop = asyncio.get_running_loop()
    
    def signal_handler(sig):
        logger.info(f"Received signal {sig.name}, initiating shutdown...")
        asyncio.create_task(service.stop())
    
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, lambda s=sig: signal_handler(s))
        except NotImplementedError:
            # Windows doesn't support add_signal_handler
            pass


async def run_collector():
    """Main entry point for running the collector."""
    # Create components
    adapter = OpenAlgoAdapter()
    validator = SnapshotValidator()
    writer = SnapshotWriter()
    
    # Create and run service
    async with create_collector(adapter, validator, writer) as service:
        setup_signal_handlers(service)
        
        # Keep running until shutdown
        await service._shutdown_event.wait()
    
    logger.info("Collector shutdown complete")


if __name__ == "__main__":
    asyncio.run(run_collector())

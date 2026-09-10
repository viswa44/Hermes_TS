"""Database storage writer for Hermes V0.

Handles writing snapshots, features, outcomes, and events to PostgreSQL/TimescaleDB.
Uses asyncpg for async database operations with connection pooling.
"""

import asyncio
import json
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Optional, List
from dataclasses import dataclass

import asyncpg

from hermes_v0.domain.models import (
    MarketSnapshot, OptionSnapshot, FeatureSnapshot, 
    FutureOutcome, SystemEvent, DataStatus
)
from hermes_v0.config import CFG


logger = logging.getLogger("hermes.storage")


@dataclass
class StorageConfig:
    """Database storage configuration."""
    host: str = "localhost"
    port: int = 5432
    database: str = "hermes"
    user: str = "postgres"
    password: str = ""
    
    # Connection pool
    min_size: int = 2
    max_size: int = 10
    command_timeout: int = 60
    
    # Batch settings
    batch_size: int = 100
    flush_interval_seconds: float = 1.0
    
    @classmethod
    def from_config(cls) -> "StorageConfig":
        """Create from hermes config."""
        c = CFG.database
        return cls(
            host=c.host,
            port=c.port,
            database=c.database,
            user=c.user,
            password=c.password,
            min_size=c.pool_size,
            max_size=c.pool_size + c.max_overflow,
        )


class SnapshotWriter:
    """Async writer for market snapshots and related data."""
    
    def __init__(self, config: Optional[StorageConfig] = None):
        self.config = config or StorageConfig.from_config()
        self._pool: Optional[asyncpg.Pool] = None
        self._batch: List[MarketSnapshot] = []
        self._flush_task: Optional[asyncio.Task] = None
        self._running = False
        
        # Stats
        self.stats = {
            "written": 0,
            "failed": 0,
            "batches_flushed": 0,
            "last_write_time": None,
            "last_error": None,
        }
    
    async def start(self):
        """Initialize connection pool and start flush task."""
        if self._pool:
            return
        
        self._pool = await asyncpg.create_pool(
            host=self.config.host,
            port=self.config.port,
            database=self.config.database,
            user=self.config.user,
            password=self.config.password,
            min_size=self.config.min_size,
            max_size=self.config.max_size,
            command_timeout=self.config.command_timeout,
        )
        
        self._running = True
        self._flush_task = asyncio.create_task(self._periodic_flush())
        logger.info("Snapshot writer started")
    
    async def stop(self):
        """Flush remaining and close pool."""
        self._running = False
        
        # Final flush
        await self._flush_batch()
        
        # Stop flush task
        if self._flush_task:
            self._flush_task.cancel()
            try:
                await self._flush_task
            except asyncio.CancelledError:
                pass
        
        # Close pool
        if self._pool:
            await self._pool.close()
            self._pool = None
        
        logger.info("Snapshot writer stopped")
    
    async def write(self, snapshot: MarketSnapshot):
        """Add snapshot to write batch."""
        self._batch.append(snapshot)
        
        # Flush if batch full
        if len(self._batch) >= self.config.batch_size:
            await self._flush_batch()
    
    async def write_many(self, snapshots: List[MarketSnapshot]):
        """Write multiple snapshots at once."""
        self._batch.extend(snapshots)
        if len(self._batch) >= self.config.batch_size:
            await self._flush_batch()
    
    async def _periodic_flush(self):
        """Periodically flush batch."""
        while self._running:
            await asyncio.sleep(self.config.flush_interval_seconds)
            if self._batch:
                await self._flush_batch()
    
    async def _flush_batch(self):
        """Flush current batch to database."""
        if not self._batch:
            return
        
        batch = self._batch
        self._batch = []
        
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                try:
                    # Use COPY for bulk insert (fastest)
                    await self._copy_snapshots(conn, batch)
                    
                    self.stats["written"] += len(batch)
                    self.stats["batches_flushed"] += 1
                    self.stats["last_write_time"] = datetime.now(timezone.utc)
                    
                except Exception as e:
                    self.stats["failed"] += len(batch)
                    self.stats["last_error"] = str(e)
                    logger.error(f"Batch write failed: {e}")
                    raise
    
    async def _copy_snapshots(self, conn: asyncpg.Connection, snapshots: List[MarketSnapshot]):
        """Use COPY for efficient bulk insert."""
        from datetime import date
        
        # Prepare data for COPY
        rows = []
        for s in snapshots:
            # Convert trading_date string to date object
            td = s.trading_date
            if isinstance(td, str):
                td = date.fromisoformat(td)
            
            rows.append((
                s.timestamp_ist,
                s.symbol,
                td,
                s.spot_ltp,
                s.spot_bid,
                s.spot_ask,
                s.spot_prev_close,
                s.spot_open,
                s.spot_high,
                s.spot_low,
                s.spot_volume,
                s.vix,
                s.vix_prev_close,
                s.atm_strike,
                s.expiry_date,
                s.days_to_expiry,
                s.atm_ce_ltp,
                s.atm_ce_bid,
                s.atm_ce_ask,
                s.atm_ce_bid_qty,
                s.atm_ce_ask_qty,
                s.atm_ce_volume,
                s.atm_ce_oi,
                s.atm_ce_iv,
                s.atm_ce_delta,
                s.atm_ce_gamma,
                s.atm_ce_theta,
                s.atm_ce_vega,
                s.atm_pe_ltp,
                s.atm_pe_bid,
                s.atm_pe_ask,
                s.atm_pe_bid_qty,
                s.atm_pe_ask_qty,
                s.atm_pe_volume,
                s.atm_pe_oi,
                s.atm_pe_iv,
                s.atm_pe_delta,
                s.atm_pe_gamma,
                s.atm_pe_theta,
                s.atm_pe_vega,
                s.data_status.value,
                s.source_latency_ms,
                s.ingestion_time,
                s.provider_payload,
                s.version,
            ))
        
        # Use COPY FROM for bulk insert
        columns = [
            "timestamp_ist", "symbol", "trading_date",
            "spot_ltp", "spot_bid", "spot_ask", "spot_prev_close",
            "spot_open", "spot_high", "spot_low", "spot_volume",
            "vix", "vix_prev_close",
            "atm_strike", "expiry_date", "days_to_expiry",
            "atm_ce_ltp", "atm_ce_bid", "atm_ce_ask", "atm_ce_bid_qty",
            "atm_ce_ask_qty", "atm_ce_volume", "atm_ce_oi", "atm_ce_iv",
            "atm_ce_delta", "atm_ce_gamma", "atm_ce_theta", "atm_ce_vega",
            "atm_pe_ltp", "atm_pe_bid", "atm_pe_ask", "atm_pe_bid_qty",
            "atm_pe_ask_qty", "atm_pe_volume", "atm_pe_oi", "atm_pe_iv",
            "atm_pe_delta", "atm_pe_gamma", "atm_pe_theta", "atm_pe_vega",
            "data_status", "source_latency_ms", "ingestion_time",
            "provider_payload", "version",
        ]
        
        await conn.copy_records_to_table(
            "market_snapshot",
            records=rows,
            columns=columns,
        )
    
    async def write_option_snapshots(self, snapshots: List[OptionSnapshot]):
        """Write option snapshots."""
        if not snapshots:
            return
        
        from datetime import date
        
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                rows = []
                for s in snapshots:
                    td = s.trading_date
                    if isinstance(td, str):
                        td = date.fromisoformat(td)
                    
                    rows.append((
                        s.timestamp_ist, s.symbol, s.strike, s.option_type,
                        s.expiry_date, td,
                        s.ltp, s.bid, s.ask, s.bid_qty, s.ask_qty,
                        s.volume, s.oi, s.iv, s.delta, s.gamma,
                        s.theta, s.vega, s.moneyness, s.lotsize,
                        s.tick_size, s.data_status.value,
                        s.source_latency_ms, s.ingestion_time, s.version,
                    ))
                
                await conn.copy_records_to_table(
                    "option_snapshot",
                    records=rows,
                    columns=[
                        "timestamp_ist", "symbol", "strike", "option_type",
                        "expiry_date", "trading_date",
                        "ltp", "bid", "ask", "bid_qty", "ask_qty",
                        "volume", "oi", "iv", "delta", "gamma",
                        "theta", "vega", "moneyness", "lotsize",
                        "tick_size", "data_status", "source_latency_ms",
                        "ingestion_time", "version",
                    ],
                )
    
    async def write_features(self, features: List[FeatureSnapshot]):
        """Write calculated features."""
        if not features:
            return
        
        from datetime import date
        
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                rows = []
                for f in features:
                    td = f.trading_date
                    if isinstance(td, str):
                        td = date.fromisoformat(td)
                    
                    rows.append((
                        f.timestamp_ist, f.symbol, td,
                        f.ret_5s, f.ret_30s, f.ret_1m, f.ret_3m, f.ret_5m,
                        f.ema_5, f.ema_20, f.ema_50,
                        f.ema_5_slope, f.ema_20_slope, f.ema_50_slope,
                        f.dist_ema_5, f.dist_ema_20, f.dist_ema_50,
                        f.atm_ce_oi_change, f.atm_pe_oi_change,
                        f.atm_ce_oi_change_rate, f.atm_pe_oi_change_rate,
                        f.total_oi_change,
                        f.atm_ce_iv_change, f.atm_pe_iv_change,
                        f.atm_iv_avg_change,
                        f.pcr_oi, f.pcr_volume, f.pcr_oi_change,
                        f.atm_spread_pts, f.atm_spread_pct, f.data_latency_ms,
                        f.calc_version, f.calc_time,
                    ))
                
                await conn.copy_records_to_table(
                    "feature_snapshot",
                    records=rows,
                    columns=[
                        "timestamp_ist", "symbol", "trading_date",
                        "ret_5s", "ret_30s", "ret_1m", "ret_3m", "ret_5m",
                        "ema_5", "ema_20", "ema_50",
                        "ema_5_slope", "ema_20_slope", "ema_50_slope",
                        "dist_ema_5", "dist_ema_20", "dist_ema_50",
                        "atm_ce_oi_change", "atm_pe_oi_change",
                        "atm_ce_oi_change_rate", "atm_pe_oi_change_rate",
                        "total_oi_change",
                        "atm_ce_iv_change", "atm_pe_iv_change",
                        "atm_iv_avg_change",
                        "pcr_oi", "pcr_volume", "pcr_oi_change",
                        "atm_spread_pts", "atm_spread_pct", "data_latency_ms",
                        "calc_version", "calc_time",
                    ],
                )
    
    async def write_outcomes(self, outcomes: List[FutureOutcome]):
        """Write future outcomes."""
        if not outcomes:
            return
        
        from datetime import date
        
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                rows = []
                for o in outcomes:
                    td = o.trading_date
                    if isinstance(td, str):
                        td = date.fromisoformat(td)
                    
                    rows.append((
                        o.snapshot_timestamp_ist, o.symbol, td,
                        o.horizon_seconds, o.horizon_label,
                        o.future_spot, o.future_timestamp_ist,
                        o.future_return_pts, o.future_return_pct,
                        o.future_direction,
                        o.mfe_pts, o.mae_pts, o.mfe_pct, o.mae_pct,
                        o.outcome_status.value, o.missing_reason,
                        o.calc_time, o.version,
                    ))
                
                await conn.copy_records_to_table(
                    "future_outcome",
                    records=rows,
                    columns=[
                        "snapshot_timestamp_ist", "symbol", "trading_date",
                        "horizon_seconds", "horizon_label",
                        "future_spot", "future_timestamp_ist",
                        "future_return_pts", "future_return_pct",
                        "future_direction",
                        "mfe_pts", "mae_pts", "mfe_pct", "mae_pct",
                        "outcome_status", "missing_reason",
                        "calc_time", "version",
                    ],
                )
    
    async def write_event(self, event: SystemEvent):
        """Write system event."""
        async with self._pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO system_event 
                (event_time, event_type, severity, message, details, 
                 trading_date, snapshot_timestamp_ist, version)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
            """, 
                event.event_time, event.event_type.value, 
                event.severity, event.message, 
                json.dumps(event.details) if event.details else None,
                event.trading_date, event.snapshot_timestamp_ist, event.version
            )
    
    async def health_check(self) -> dict:
        """Health check for monitoring."""
        try:
            async with self._pool.acquire() as conn:
                await conn.fetchval("SELECT 1")
            
            return {
                "status": "healthy",
                "pool_size": self._pool.get_size() if self._pool else 0,
                "pool_idle": self._pool.get_idle_size() if self._pool else 0,
                "stats": self.stats,
            }
        except Exception as e:
            return {
                "status": "unhealthy",
                "error": str(e),
                "stats": self.stats,
            }
    
    async def get_latest_snapshot(self, symbol: str = "NIFTY") -> Optional[MarketSnapshot]:
        """Get latest valid snapshot for a symbol."""
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow("""
                SELECT * FROM market_snapshot 
                WHERE symbol = $1 AND data_status = 'VALID'
                ORDER BY timestamp_ist DESC LIMIT 1
            """, symbol)
            
            if row:
                return self._row_to_snapshot(row)
            return None
    
    async def get_snapshots_by_date(
        self, 
        trading_date: str, 
        symbol: str = "NIFTY",
        limit: int = 1000
    ) -> List[MarketSnapshot]:
        """Get all snapshots for a trading date."""
        async with self._pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT * FROM market_snapshot 
                WHERE trading_date = $1 AND symbol = $2
                ORDER BY timestamp_ist ASC LIMIT $3
            """, trading_date, symbol, limit)
            
            return [self._row_to_snapshot(r) for r in rows]
    
    def _row_to_snapshot(self, row: asyncpg.Record) -> MarketSnapshot:
        """Convert database row to MarketSnapshot."""
        return MarketSnapshot(
            version=row["version"],
            timestamp_ist=row["timestamp_ist"],
            trading_date=str(row["trading_date"]),
            symbol=row["symbol"],
            spot_ltp=row["spot_ltp"],
            spot_bid=row["spot_bid"],
            spot_ask=row["spot_ask"],
            spot_prev_close=row["spot_prev_close"],
            spot_open=row["spot_open"],
            spot_high=row["spot_high"],
            spot_low=row["spot_low"],
            spot_volume=row["spot_volume"],
            vix=row["vix"],
            vix_prev_close=row["vix_prev_close"],
            atm_strike=row["atm_strike"],
            expiry_date=row["expiry_date"],
            days_to_expiry=row["days_to_expiry"],
            atm_ce_ltp=row["atm_ce_ltp"],
            atm_ce_bid=row["atm_ce_bid"],
            atm_ce_ask=row["atm_ce_ask"],
            atm_ce_bid_qty=row["atm_ce_bid_qty"],
            atm_ce_ask_qty=row["atm_ce_ask_qty"],
            atm_ce_volume=row["atm_ce_volume"],
            atm_ce_oi=row["atm_ce_oi"],
            atm_ce_iv=row["atm_ce_iv"],
            atm_ce_delta=row["atm_ce_delta"],
            atm_ce_gamma=row["atm_ce_gamma"],
            atm_ce_theta=row["atm_ce_theta"],
            atm_ce_vega=row["atm_ce_vega"],
            atm_pe_ltp=row["atm_pe_ltp"],
            atm_pe_bid=row["atm_pe_bid"],
            atm_pe_ask=row["atm_pe_ask"],
            atm_pe_bid_qty=row["atm_pe_bid_qty"],
            atm_pe_ask_qty=row["atm_pe_ask_qty"],
            atm_pe_volume=row["atm_pe_volume"],
            atm_pe_oi=row["atm_pe_oi"],
            atm_pe_iv=row["atm_pe_iv"],
            atm_pe_delta=row["atm_pe_delta"],
            atm_pe_gamma=row["atm_pe_gamma"],
            atm_pe_theta=row["atm_pe_theta"],
            atm_pe_vega=row["atm_pe_vega"],
            data_status=DataStatus(row["data_status"]),
            source_latency_ms=row["source_latency_ms"],
            ingestion_time=row["ingestion_time"],
            provider_payload=row["provider_payload"],
        )


@asynccontextmanager
async def create_writer(config: Optional[StorageConfig] = None) -> SnapshotWriter:
    """Context manager for writer lifecycle."""
    writer = SnapshotWriter(config)
    await writer.start()
    try:
        yield writer
    finally:
        await writer.stop()


async def init_database(config: Optional[StorageConfig] = None):
    """Initialize database schema (run once)."""
    cfg = config or StorageConfig.from_config()
    
    # Connect without pool for DDL
    conn = await asyncpg.connect(
        host=cfg.host, port=cfg.port,
        database=cfg.database, user=cfg.user, password=cfg.password,
    )
    
    try:
        # Read and execute schema
        with open("hermes_v0/storage/schema.sql") as f:
            schema = f.read()
        
        # Split by semicolon and execute
        statements = [s.strip() for s in schema.split(";") if s.strip()]
        for stmt in statements:
            if stmt:
                try:
                    await conn.execute(stmt)
                    logger.info(f"Executed: {stmt[:50]}...")
                except Exception as e:
                    logger.warning(f"Statement failed (may already exist): {e}")
        
        logger.info("Database initialization complete")
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(init_database())
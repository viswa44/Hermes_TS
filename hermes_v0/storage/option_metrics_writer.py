"""Append-only PostgreSQL writer for B04 option IV/Greek observations.

The writer records raw quote observations in the existing ``market_snapshot``
and ``option_snapshot`` tables, then writes calculated Black-76 outputs to the
separate ``option_greeks_snapshot`` table.  A collection cycle is one database
transaction: no partial cycle is silently committed.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable
from zoneinfo import ZoneInfo

from hermes_v0.config import CFG
from hermes_v0.domain.models import MarketSnapshot, OptionGreeksSnapshot, OptionSnapshot


IST = ZoneInfo("Asia/Kolkata")


class DatabaseDependencyError(RuntimeError):
    """Raised when the dedicated Hermes runtime is missing asyncpg."""


class SchemaPrerequisiteError(RuntimeError):
    """Raised instead of attempting to invent or overwrite base raw tables."""


@dataclass(frozen=True)
class OptionMetricsStorageConfig:
    host: str = "localhost"
    port: int = 5432
    database: str = "hermes"
    user: str = "postgres"
    password: str = ""
    min_size: int = 1
    max_size: int = 2
    command_timeout: int = 20

    @classmethod
    def from_config(cls) -> "OptionMetricsStorageConfig":
        config = CFG.database
        return cls(
            host=config.host,
            port=config.port,
            database=config.database,
            user=config.user,
            password=config.password,
            min_size=1,
            max_size=2,
        )


class OptionMetricsWriter:
    """Store one raw/derived option metrics cycle atomically and append-only."""

    def __init__(
        self,
        config: OptionMetricsStorageConfig | None = None,
        *,
        pool_factory: Callable[..., Any] | None = None,
    ) -> None:
        self.config = config or OptionMetricsStorageConfig.from_config()
        self._pool: Any | None = None
        self._pool_factory = pool_factory
        self._schema_ready = False
        self._schema_lock = asyncio.Lock()
        self.stats = {
            "cycles_written": 0,
            "market_rows_written": 0,
            "option_rows_written": 0,
            "greek_rows_written": 0,
            "failed_cycles": 0,
            "last_error_class": None,
        }

    async def start(self) -> None:
        if self._pool is not None:
            return
        factory = self._pool_factory
        if factory is None:
            try:
                import asyncpg
            except ImportError as error:  # pragma: no cover - depends on runtime setup.
                raise DatabaseDependencyError(
                    "asyncpg is required. Create the Hermes virtual environment and install requirements.txt."
                ) from error
            factory = asyncpg.create_pool
        self._pool = await factory(
            host=self.config.host,
            port=self.config.port,
            database=self.config.database,
            user=self.config.user,
            password=self.config.password,
            min_size=self.config.min_size,
            max_size=self.config.max_size,
            command_timeout=self.config.command_timeout,
        )
        await self.ensure_schema()

    async def stop(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None
        self._schema_ready = False

    async def ensure_schema(self) -> None:
        """Apply only the additive B04 table migration after checking base tables."""
        if self._schema_ready:
            return
        if self._pool is None:
            raise RuntimeError("OptionMetricsWriter.start() must run before ensure_schema().")
        async with self._schema_lock:
            if self._schema_ready:
                return
            async with self._pool.acquire() as connection:
                base_tables = await connection.fetchval(
                    "SELECT to_regclass('public.market_snapshot') IS NOT NULL "
                    "AND to_regclass('public.option_snapshot') IS NOT NULL"
                )
                if not base_tables:
                    raise SchemaPrerequisiteError(
                        "The canonical market_snapshot and option_snapshot tables must exist before B04."
                    )
                migration = (
                    Path(__file__).resolve().parent
                    / "migrations"
                    / "20260907_option_greeks_snapshot.sql"
                ).read_text(encoding="utf-8")
                await connection.execute(migration)
            self._schema_ready = True

    @staticmethod
    def _trading_date(value: str) -> date:
        return value if isinstance(value, date) else date.fromisoformat(value)

    @staticmethod
    def _observation_time(value: datetime) -> datetime:
        """Treat legacy-naive observation timestamps as IST, then make them aware."""
        return value.replace(tzinfo=IST) if value.tzinfo is None else value.astimezone(IST)

    @staticmethod
    def _ingestion_time(value: datetime) -> datetime:
        """Treat legacy-naive ingestion clocks as UTC, avoiding an IST reinterpretation."""
        return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)

    @classmethod
    def _normalized_times(cls, timestamp_ist: datetime, ingestion_time: datetime) -> tuple[datetime, datetime]:
        observed = cls._observation_time(timestamp_ist)
        ingested = cls._ingestion_time(ingestion_time)
        if ingested < observed.astimezone(timezone.utc):
            raise ValueError("ingestion_time must not precede timestamp_ist.")
        return observed, ingested

    @staticmethod
    def _validate_pairing(
        market: MarketSnapshot,
        option_rows: list[OptionSnapshot],
        greek_rows: list[OptionGreeksSnapshot],
    ) -> None:
        """Reject malformed CE/PE pairs before a database transaction starts."""
        if {row.option_type for row in option_rows} != {"CE", "PE"}:
            raise ValueError("B04 raw option rows must contain exactly one CE and one PE.")
        if {row.option_type for row in greek_rows} != {"CE", "PE"}:
            raise ValueError("B04 calculated rows must contain exactly one CE and one PE.")
        options_by_type = {row.option_type: row for row in option_rows}
        greeks_by_type = {row.option_type: row for row in greek_rows}
        for option_type, option in options_by_type.items():
            calculation = greeks_by_type[option_type]
            if option.symbol != market.symbol or calculation.underlying_symbol != market.symbol:
                raise ValueError("B04 underlying symbols must match the market snapshot.")
            if (
                calculation.strike != option.strike
                or calculation.expiry_date != option.expiry_date
                or calculation.trading_date != option.trading_date
                or not calculation.option_symbol.endswith(option_type)
            ):
                raise ValueError("B04 calculated record does not match its raw option contract.")

    async def write_cycle(
        self,
        market: MarketSnapshot,
        options: Iterable[OptionSnapshot],
        greeks: Iterable[OptionGreeksSnapshot],
    ) -> None:
        """Write an entire sampling cycle or roll it back without overwriting evidence."""
        if self._pool is None:
            raise RuntimeError("OptionMetricsWriter.start() must run before write_cycle().")
        option_rows = list(options)
        greek_rows = list(greeks)
        if len(option_rows) != 2 or len(greek_rows) != 2:
            raise ValueError("B04 requires exactly the ATM CE and PE raw/derived records per cycle.")
        self._validate_pairing(market, option_rows, greek_rows)
        market_time, market_ingestion = self._normalized_times(
            market.timestamp_ist, market.ingestion_time
        )
        option_values = []
        for row in option_rows:
            timestamp_ist, ingestion_time = self._normalized_times(
                row.timestamp_ist, row.ingestion_time
            )
            if timestamp_ist != market_time:
                raise ValueError("B04 raw option timestamps must match the cycle timestamp.")
            option_values.append((row, timestamp_ist, ingestion_time))
        greek_values = []
        for row in greek_rows:
            timestamp_ist, ingestion_time = self._normalized_times(
                row.timestamp_ist, row.ingestion_time
            )
            if timestamp_ist != market_time:
                raise ValueError("B04 calculated-Greeks timestamps must match the cycle timestamp.")
            greek_values.append((row, timestamp_ist, ingestion_time))
        try:
            async with self._pool.acquire() as connection:
                async with connection.transaction():
                    await connection.execute(
                        """
                        INSERT INTO market_snapshot (
                            timestamp_ist, symbol, trading_date, spot_ltp, spot_bid, spot_ask,
                            spot_prev_close, spot_open, spot_high, spot_low, spot_volume,
                            data_status, source_latency_ms, ingestion_time, provider_payload, version
                        ) VALUES (
                            $1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11,
                            $12::data_status, $13, $14, $15::jsonb, $16
                        )
                        """,
                        market_time,
                        market.symbol,
                        self._trading_date(market.trading_date),
                        market.spot_ltp,
                        market.spot_bid,
                        market.spot_ask,
                        market.spot_prev_close,
                        market.spot_open,
                        market.spot_high,
                        market.spot_low,
                        market.spot_volume,
                        market.data_status.value,
                        market.source_latency_ms,
                        market_ingestion,
                        None,
                        market.version,
                    )
                    await connection.executemany(
                        """
                        INSERT INTO option_snapshot (
                            timestamp_ist, symbol, strike, option_type, expiry_date, trading_date,
                            ltp, bid, ask, bid_qty, ask_qty, volume, oi, iv,
                            delta, gamma, theta, vega, moneyness, lotsize, tick_size,
                            data_status, source_latency_ms, ingestion_time, version
                        ) VALUES (
                            $1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14,
                            $15, $16, $17, $18, $19, $20, $21, $22::data_status, $23, $24, $25
                        )
                        """,
                        [
                            (
                                timestamp_ist,
                                row.symbol,
                                row.strike,
                                row.option_type,
                                row.expiry_date,
                                self._trading_date(row.trading_date),
                                row.ltp,
                                row.bid,
                                row.ask,
                                row.bid_qty,
                                row.ask_qty,
                                row.volume,
                                row.oi,
                                row.iv,
                                None,
                                None,
                                None,
                                None,
                                row.moneyness,
                                row.lotsize,
                                row.tick_size,
                                row.data_status.value,
                                row.source_latency_ms,
                                ingestion_time,
                                row.version,
                            )
                            for row, timestamp_ist, ingestion_time in option_values
                        ],
                    )
                    await connection.executemany(
                        """
                        INSERT INTO option_greeks_snapshot (
                            timestamp_ist, underlying_symbol, option_symbol, strike, option_type,
                            expiry_date, trading_date, option_ltp, underlying_ltp,
                            calculation_option_ltp, calculation_spot_ltp,
                            implied_volatility, delta, gamma, theta, vega, rho,
                            interest_rate, forward_price, calculation_method, data_status,
                            source_latency_ms, error_class, ingestion_time, version
                        ) VALUES (
                            $1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14,
                            $15, $16, $17, $18, $19, $20, $21::data_status, $22, $23, $24, $25
                        )
                        """,
                        [
                            (
                                timestamp_ist,
                                row.underlying_symbol,
                                row.option_symbol,
                                row.strike,
                                row.option_type,
                                row.expiry_date,
                                self._trading_date(row.trading_date),
                                row.option_ltp,
                                row.underlying_ltp,
                                row.calculation_option_ltp,
                                row.calculation_spot_ltp,
                                row.implied_volatility,
                                row.delta,
                                row.gamma,
                                row.theta,
                                row.vega,
                                row.rho,
                                row.interest_rate,
                                row.forward_price,
                                row.calculation_method,
                                row.data_status.value,
                                row.source_latency_ms,
                                row.error_class,
                                ingestion_time,
                                row.version,
                            )
                            for row, timestamp_ist, ingestion_time in greek_values
                        ],
                    )
            self.stats["cycles_written"] += 1
            self.stats["market_rows_written"] += 1
            self.stats["option_rows_written"] += len(option_rows)
            self.stats["greek_rows_written"] += len(greek_rows)
        except Exception as error:
            self.stats["failed_cycles"] += 1
            self.stats["last_error_class"] = type(error).__name__
            raise

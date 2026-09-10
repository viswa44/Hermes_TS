"""Read-only NIFTY ATM option IV/Greeks collector for Hermes V0 B04.

The collector deliberately uses only OpenAlgo's expiry, quotes, option-chain,
and option-Greeks data endpoints.  It never imports or invokes order, account,
position, or WebSocket APIs.  Each successful cycle appends one raw NIFTY row,
two raw ATM option rows, and two separately-versioned Black-76 metric rows.
"""

from __future__ import annotations

import argparse
import asyncio
import fcntl
import json
import logging
import os
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Awaitable, Callable

from hermes_v0.collector.adapters.openalgo_adapter import AtmOptionMetrics, OpenAlgoAdapter
from hermes_v0.collector.scheduler import ClockAlignedScheduler, DriftMonitor
from hermes_v0.domain.models import DataStatus
from hermes_v0.storage.option_metrics_writer import OptionMetricsWriter


logger = logging.getLogger(__name__)
ALLOWED_INTERVALS = frozenset({5, 10})
DEFAULT_SYMBOL = "NIFTY"
DEFAULT_EXCHANGE = "NSE_INDEX"


class OptionMetricsConfigurationError(ValueError):
    """Raised before I/O when a read-only B04 collector cannot safely start."""


class CollectorAlreadyRunningError(RuntimeError):
    """Raised when a second local option-metrics collector is already active."""


class ProcessCollectorLock:
    """Use a local advisory file lock so a restart cannot duplicate the collector."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or Path("/tmp/hermes_v0_option_metrics.lock")
        self._stream: Any | None = None

    def __enter__(self) -> "ProcessCollectorLock":
        self._stream = self.path.open("a+", encoding="utf-8")
        try:
            fcntl.flock(self._stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            self._stream.close()
            self._stream = None
            raise CollectorAlreadyRunningError(
                "A Hermes option-metrics collector is already running on this Mac."
            ) from error
        return self

    def __exit__(self, *_: object) -> None:
        if self._stream is not None:
            fcntl.flock(self._stream.fileno(), fcntl.LOCK_UN)
            self._stream.close()
            self._stream = None


@dataclass(frozen=True)
class OptionMetricsCycle:
    """Non-secret operator evidence for one scheduled PostgreSQL collection cycle."""

    sequence: int
    scheduled_at_ist: str
    request_started_at_ist: str
    response_received_at_ist: str
    persisted: bool
    complete_metrics: bool
    underlying_ltp: float | None
    ce_ltp: float | None
    pe_ltp: float | None
    ce_iv: float | None
    pe_iv: float | None
    ce_status: str | None
    pe_status: str | None
    source_latency_ms: int | None
    error_class: str | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "sequence": self.sequence,
            "scheduled_at_ist": self.scheduled_at_ist,
            "request_started_at_ist": self.request_started_at_ist,
            "response_received_at_ist": self.response_received_at_ist,
            "persisted": self.persisted,
            "complete_metrics": self.complete_metrics,
            "underlying_ltp": self.underlying_ltp,
            "ce_ltp": self.ce_ltp,
            "pe_ltp": self.pe_ltp,
            "ce_iv": self.ce_iv,
            "pe_iv": self.pe_iv,
            "ce_status": self.ce_status,
            "pe_status": self.pe_status,
            "source_latency_ms": self.source_latency_ms,
            "error_class": self.error_class,
        }


@dataclass
class OptionMetricsRunResult:
    """Aggregate live collection evidence, separate from any trading use."""

    interval_seconds: int
    cycles_requested: int | None
    records: list[OptionMetricsCycle] = field(default_factory=list)
    drift: DriftMonitor | None = None
    writer_stats: dict[str, object] = field(default_factory=dict)

    @property
    def persisted_cycles(self) -> int:
        return sum(record.persisted for record in self.records)

    @property
    def complete_metric_cycles(self) -> int:
        return sum(record.complete_metrics for record in self.records)

    @property
    def failed_cycles(self) -> int:
        return sum(not record.persisted for record in self.records)

    @property
    def partial_metric_cycles(self) -> int:
        return sum(record.persisted and not record.complete_metrics for record in self.records)

    @property
    def passed(self) -> bool:
        """Strict verdict for a bounded verification run, not an ongoing daemon."""
        return bool(
            self.cycles_requested is not None
            and len(self.records) == self.cycles_requested
            and self.complete_metric_cycles == self.cycles_requested
            and self.failed_cycles == 0
            and self.drift is not None
            and self.drift.get_stats().get("missed_intervals", 0) == 0
        )

    def summary(self) -> dict[str, object]:
        return {
            "record_type": "B04_OPTION_METRICS_SUMMARY",
            "mode": "LIVE_READONLY_POSTGRES_COLLECTOR",
            "symbol": DEFAULT_SYMBOL,
            "exchange": DEFAULT_EXCHANGE,
            "interval_seconds": self.interval_seconds,
            "cycles_requested": self.cycles_requested,
            "cycles_completed": len(self.records),
            "persisted_cycles": self.persisted_cycles,
            "complete_metric_cycles": self.complete_metric_cycles,
            "partial_metric_cycles": self.partial_metric_cycles,
            "failed_cycles": self.failed_cycles,
            "drift": self.drift.get_stats() if self.drift else {},
            "writer": self.writer_stats,
            "live_openalgo_and_postgres_verified": self.passed,
            "overall": "PASS" if self.passed else "CONDITIONAL_FAIL",
            "last_cycle": self.records[-1].as_dict() if self.records else None,
            "finished_at_utc": datetime.now(UTC).isoformat(timespec="seconds"),
        }


def resolve_interval(candidate: int | None = None) -> int:
    """Resolve only the documented 5- or 10-second cadence."""
    if candidate is None:
        try:
            candidate = int(os.environ.get("HERMES_OPTION_INTERVAL_SECONDS", "5"))
        except ValueError as error:
            raise OptionMetricsConfigurationError(
                "HERMES_OPTION_INTERVAL_SECONDS must be 5 or 10."
            ) from error
    if candidate not in ALLOWED_INTERVALS:
        raise OptionMetricsConfigurationError("Option interval must be exactly 5 or 10 seconds.")
    return candidate


def _safe_timestamp(value: datetime) -> str:
    return value.isoformat(timespec="milliseconds")


def _complete_metrics(metrics: AtmOptionMetrics) -> bool:
    return bool(
        metrics.market_snapshot.spot_ltp is not None
        and metrics.market_snapshot.spot_ltp > 0
        and all(option.data_status == DataStatus.VALID for option in metrics.option_snapshots)
        and all(greek.data_status == DataStatus.VALID for greek in metrics.greek_snapshots)
    )


def _record_from_metrics(
    sequence: int,
    expected: datetime,
    started: datetime,
    received: datetime,
    metrics: AtmOptionMetrics,
) -> OptionMetricsCycle:
    ce, pe = metrics.option_snapshots
    ce_greek, pe_greek = metrics.greek_snapshots
    return OptionMetricsCycle(
        sequence=sequence,
        scheduled_at_ist=_safe_timestamp(expected),
        request_started_at_ist=_safe_timestamp(started),
        response_received_at_ist=_safe_timestamp(received),
        persisted=True,
        complete_metrics=_complete_metrics(metrics),
        underlying_ltp=metrics.market_snapshot.spot_ltp,
        ce_ltp=ce.ltp,
        pe_ltp=pe.ltp,
        ce_iv=ce_greek.implied_volatility,
        pe_iv=pe_greek.implied_volatility,
        ce_status=ce_greek.data_status.value,
        pe_status=pe_greek.data_status.value,
        source_latency_ms=max(
            (
                value
                for value in (
                    metrics.market_snapshot.source_latency_ms,
                    ce.source_latency_ms,
                    pe.source_latency_ms,
                    ce_greek.source_latency_ms,
                    pe_greek.source_latency_ms,
                )
                if value is not None
            ),
            default=None,
        ),
    )


def _failure_record(
    sequence: int, expected: datetime, started: datetime, received: datetime, error: Exception
) -> OptionMetricsCycle:
    return OptionMetricsCycle(
        sequence=sequence,
        scheduled_at_ist=_safe_timestamp(expected),
        request_started_at_ist=_safe_timestamp(started),
        response_received_at_ist=_safe_timestamp(received),
        persisted=False,
        complete_metrics=False,
        underlying_ltp=None,
        ce_ltp=None,
        pe_ltp=None,
        ce_iv=None,
        pe_iv=None,
        ce_status=None,
        pe_status=None,
        source_latency_ms=None,
        error_class=type(error).__name__,
    )


def _validate_start(
    adapter: OpenAlgoAdapter,
    scheduler: ClockAlignedScheduler,
    *,
    cycles: int | None,
) -> None:
    if cycles is not None and cycles <= 0:
        raise OptionMetricsConfigurationError("--cycles must be a positive integer when provided.")
    if not adapter.config.api_key:
        raise OptionMetricsConfigurationError(
            "OPENALGO_API_KEY is required for the read-only option metrics collector."
        )
    if adapter.config.underlying.upper() != DEFAULT_SYMBOL:
        raise OptionMetricsConfigurationError(
            "B04 is bounded to NIFTY; set HERMES_UNDERLYING=NIFTY."
        )
    exchange_map = {
        symbol.upper(): exchange.upper()
        for symbol, exchange in adapter.config.index_exchange_map
    }
    if exchange_map.get(DEFAULT_SYMBOL) != DEFAULT_EXCHANGE:
        raise OptionMetricsConfigurationError("B04 requires NIFTY on NSE_INDEX.")
    if scheduler.interval_seconds not in ALLOWED_INTERVALS:
        raise OptionMetricsConfigurationError("Option interval must be exactly 5 or 10 seconds.")
    if not scheduler.is_market_open_now():
        raise OptionMetricsConfigurationError(
            "NSE market hours are closed; the live collector starts only during an open weekday session."
        )


async def run_live_option_metrics(
    *,
    adapter: OpenAlgoAdapter,
    writer: OptionMetricsWriter,
    scheduler: ClockAlignedScheduler,
    cycles: int | None = None,
    on_cycle: Callable[[OptionMetricsCycle], Awaitable[None]] | None = None,
) -> OptionMetricsRunResult:
    """Collect and append ATM NIFTY metrics until a bounded run or market close.

    At five seconds, this makes exactly two incoming ``optiongreeks`` requests
    per cycle (24/minute). Retry is disabled for this runner so an outage cannot
    silently exceed OpenAlgo's 30/minute Greeks limiter.
    """
    _validate_start(adapter, scheduler, cycles=cycles)
    if isinstance(adapter, OpenAlgoAdapter):
        adapter.config.max_retries = 1

    drift = DriftMonitor(max_drift_ms=100.0, interval_seconds=scheduler.interval_seconds)
    result = OptionMetricsRunResult(
        interval_seconds=scheduler.interval_seconds,
        cycles_requested=cycles,
        drift=drift,
    )
    adapter_started = False
    writer_started = False
    # The quote, option-chain, and the two independent Greeks requests are
    # already concurrent where possible.  Do not cancel the whole collection
    # half a second before its next clock boundary: that used to discard a
    # valid raw CE/PE OI pair when one provider Greeks response arrived near
    # the boundary.  A small bounded completion grace prevents a response
    # that is already in flight from erasing the immutable raw OI evidence.
    # The scheduler still calculates the following absolute boundary.
    cycle_timeout_seconds = scheduler.interval_seconds + 0.5
    try:
        await adapter.start()
        adapter_started = True
        await writer.start()
        writer_started = True
        async for expected, trading_date in scheduler.tick_generator():
            sequence = len(result.records) + 1
            started = datetime.now(scheduler.tz)
            drift.record(expected, started)
            try:
                metrics = await asyncio.wait_for(
                    adapter.collect_atm_option_metrics(expected, trading_date),
                    timeout=cycle_timeout_seconds,
                )
                await writer.write_cycle(
                    metrics.market_snapshot,
                    metrics.option_snapshots,
                    metrics.greek_snapshots,
                )
                received = datetime.now(scheduler.tz)
                record = _record_from_metrics(sequence, expected, started, received, metrics)
                logger.info(
                    "B04 cycle=%s persisted=%s complete_metrics=%s CE=%s PE=%s",
                    sequence,
                    record.persisted,
                    record.complete_metrics,
                    record.ce_status,
                    record.pe_status,
                )
            except Exception as error:
                received = datetime.now(scheduler.tz)
                record = _failure_record(sequence, expected, started, received, error)
                logger.warning("B04 cycle=%s failed error_class=%s", sequence, record.error_class)
            result.records.append(record)
            if on_cycle is not None:
                await on_cycle(record)
            if cycles is not None and len(result.records) >= cycles:
                scheduler.stop()
                break
    finally:
        scheduler.stop()
        if writer_started:
            result.writer_stats = dict(writer.stats)
            await writer.stop()
        if adapter_started:
            await adapter.stop()
    return result


async def _main_async(args: argparse.Namespace) -> int:
    interval_seconds = resolve_interval(args.interval_seconds)
    adapter = OpenAlgoAdapter()
    writer = OptionMetricsWriter()
    scheduler = ClockAlignedScheduler(interval_seconds=interval_seconds)
    result = await run_live_option_metrics(
        adapter=adapter,
        writer=writer,
        scheduler=scheduler,
        cycles=args.cycles,
    )
    print(json.dumps(result.summary(), sort_keys=True))
    return 0 if args.cycles is None or result.passed else 2


def main() -> int:
    # All operator launches use the integrity-first implementation. The legacy
    # helper functions remain importable for historical B04 regression fixtures.
    from hermes_v0.collector.recovery import main as recovery_main
    return recovery_main()


def _legacy_main_for_reference() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--interval-seconds",
        type=int,
        help="Sampling interval: 5 or 10. Defaults to HERMES_OPTION_INTERVAL_SECONDS or 5.",
    )
    parser.add_argument(
        "--cycles",
        type=int,
        help="Optional bounded verification run. Omit to collect until stopped or market close.",
    )
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
    )
    try:
        with ProcessCollectorLock():
            return asyncio.run(_main_async(args))
    except (CollectorAlreadyRunningError, OptionMetricsConfigurationError) as error:
        print(f"B04 not started: {error}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("B04 collector stopped by operator.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())

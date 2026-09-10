"""Bounded, read-only B03B NIFTY live-market validation.

This module intentionally calls only :meth:`OpenAlgoAdapter.get_underlying_snapshot`.
That method uses the quote endpoint; it does not import or invoke OpenAlgo order,
position, or account APIs.  Each invocation creates a new append-only evidence
file and records normalized data only, never an API key or raw provider payload.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Awaitable, Callable

from aiohttp import web

from hermes_v0.collector.adapters.openalgo_adapter import OpenAlgoAdapter
from hermes_v0.collector.scheduler import ClockAlignedScheduler, DriftMonitor
from hermes_v0.dashboard import DashboardState, create_dashboard_app


DEFAULT_CYCLES = 20
DEFAULT_INTERVAL_SECONDS = 5
DEFAULT_SYMBOL = "NIFTY"
DEFAULT_EXCHANGE = "NSE_INDEX"


class LiveValidationConfigurationError(ValueError):
    """Raised before I/O when B03B cannot safely start."""


@dataclass(frozen=True)
class LiveCycleRecord:
    """Normalized, non-secret evidence for one scheduled quote observation."""

    sequence: int
    scheduled_at_ist: str
    request_started_at_ist: str
    response_received_at_ist: str
    success: bool
    ltp: float | None
    bid: float | None
    ask: float | None
    open: float | None
    high: float | None
    low: float | None
    previous_close: float | None
    volume: float | None
    source_latency_ms: int | None
    error_class: str | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "record_type": "NIFTY_QUOTE_OBSERVATION",
            "sequence": self.sequence,
            "scheduled_at_ist": self.scheduled_at_ist,
            "request_started_at_ist": self.request_started_at_ist,
            "response_received_at_ist": self.response_received_at_ist,
            "success": self.success,
            "symbol": DEFAULT_SYMBOL,
            "exchange": DEFAULT_EXCHANGE,
            "ltp": self.ltp,
            "bid": self.bid,
            "ask": self.ask,
            "open": self.open,
            "high": self.high,
            "low": self.low,
            "previous_close": self.previous_close,
            "volume": self.volume,
            "source_latency_ms": self.source_latency_ms,
            "error_class": self.error_class,
        }


@dataclass
class LiveValidationResult:
    """Aggregate B03B evidence, deliberately separate from trading outcomes."""

    observations: list[LiveCycleRecord] = field(default_factory=list)
    dashboard: DashboardState = field(
        default_factory=lambda: DashboardState(
            symbol=DEFAULT_SYMBOL,
            mode="LIVE_READONLY_CAPTURE",
        )
    )
    drift: DriftMonitor = field(default_factory=lambda: DriftMonitor(max_drift_ms=100.0))

    @property
    def successful_cycles(self) -> int:
        return sum(record.success for record in self.observations)

    @property
    def passed(self) -> bool:
        return (
            len(self.observations) == DEFAULT_CYCLES
            and self.successful_cycles == DEFAULT_CYCLES
            and self.drift.get_stats().get("missed_intervals", 0) == 0
            and self.dashboard.dashboard_updates == DEFAULT_CYCLES
        )

    def summary(self) -> dict[str, object]:
        return {
            "record_type": "B03_LIVE_NIFTY_SUMMARY",
            "mode": "LIVE_READONLY_CAPTURE",
            "symbol": DEFAULT_SYMBOL,
            "exchange": DEFAULT_EXCHANGE,
            "cycles_requested": DEFAULT_CYCLES,
            "cycles_completed": len(self.observations),
            "successful_cycles": self.successful_cycles,
            "failed_cycles": self.dashboard.failed_cycles,
            "dashboard_updates": self.dashboard.dashboard_updates,
            "drift": self.drift.get_stats(),
            "live_openalgo_verified": self.passed,
            "overall": "PASS" if self.passed else "CONDITIONAL_FAIL",
            "finished_at_utc": datetime.now(UTC).isoformat(timespec="seconds"),
        }


def _safe_timestamp(value: datetime) -> str:
    return value.isoformat(timespec="milliseconds")


def _failure_record(sequence: int, expected: datetime, started: datetime, received: datetime, error: Exception) -> LiveCycleRecord:
    """Keep diagnostics useful without serializing exception text or credentials."""
    return LiveCycleRecord(
        sequence=sequence,
        scheduled_at_ist=_safe_timestamp(expected),
        request_started_at_ist=_safe_timestamp(started),
        response_received_at_ist=_safe_timestamp(received),
        success=False,
        ltp=None,
        bid=None,
        ask=None,
        open=None,
        high=None,
        low=None,
        previous_close=None,
        volume=None,
        source_latency_ms=None,
        error_class=type(error).__name__,
    )


async def run_live_nifty_validation(
    *,
    adapter: OpenAlgoAdapter,
    scheduler: ClockAlignedScheduler,
    cycles: int = DEFAULT_CYCLES,
    on_cycle: Callable[[LiveCycleRecord], Awaitable[None]] | None = None,
) -> LiveValidationResult:
    """Capture exactly ``cycles`` clock-aligned, quote-only NIFTY observations.

    The scheduler owns cadence, so request duration never accumulates into the
    next target boundary.  Failed cycles are evidence too: they are recorded and
    the runner continues until the requested bounded sample is complete.
    """
    if cycles != DEFAULT_CYCLES:
        raise LiveValidationConfigurationError(
            f"B03B requires exactly {DEFAULT_CYCLES} cycles; received {cycles}."
        )
    if not adapter.config.api_key:
        raise LiveValidationConfigurationError(
            "OPENALGO_API_KEY is required for the read-only B03B quote capture."
        )
    if adapter.config.underlying.upper() != DEFAULT_SYMBOL:
        raise LiveValidationConfigurationError(
            "B03B is bounded to NIFTY; set HERMES_UNDERLYING=NIFTY."
        )
    exchange_map = {
        configured_symbol.upper(): configured_exchange.upper()
        for configured_symbol, configured_exchange in adapter.config.index_exchange_map
    }
    if exchange_map.get(DEFAULT_SYMBOL) != DEFAULT_EXCHANGE:
        raise LiveValidationConfigurationError(
            "B03B requires the NIFTY exchange mapping NSE_INDEX."
        )
    if scheduler.interval_seconds != DEFAULT_INTERVAL_SECONDS:
        raise LiveValidationConfigurationError(
            "B03B requires a 5-second scheduler interval."
        )
    if not scheduler.is_market_open_now():
        raise LiveValidationConfigurationError(
            "NSE market hours are closed; B03B must run during an open weekday session."
        )

    result = LiveValidationResult()
    await adapter.start()
    try:
        async for expected, trading_date in scheduler.tick_generator():
            sequence = len(result.observations) + 1
            started = datetime.now(scheduler.tz)
            result.drift.record(expected, started)
            try:
                snapshot = await adapter.get_underlying_snapshot(
                    DEFAULT_SYMBOL,
                    timestamp_ist=expected,
                    trading_date=trading_date,
                )
                received = datetime.now(scheduler.tz)
                if snapshot.spot_ltp is None or snapshot.spot_ltp <= 0:
                    raise ValueError("Non-positive LTP")
                record = LiveCycleRecord(
                    sequence=sequence,
                    scheduled_at_ist=_safe_timestamp(expected),
                    request_started_at_ist=_safe_timestamp(started),
                    response_received_at_ist=_safe_timestamp(received),
                    success=True,
                    ltp=snapshot.spot_ltp,
                    bid=snapshot.spot_bid,
                    ask=snapshot.spot_ask,
                    open=snapshot.spot_open,
                    high=snapshot.spot_high,
                    low=snapshot.spot_low,
                    previous_close=snapshot.spot_prev_close,
                    volume=snapshot.spot_volume,
                    source_latency_ms=snapshot.source_latency_ms,
                )
                error_class = None
            except Exception as error:  # Record failures; never expose request bodies or keys.
                received = datetime.now(scheduler.tz)
                record = _failure_record(sequence, expected, started, received, error)
                error_class = record.error_class

            result.observations.append(record)
            result.dashboard.record(
                expected,
                started,
                failed=not record.success,
                drift=result.drift,
                ltp=record.ltp,
                error=error_class,
            )
            if on_cycle:
                await on_cycle(record)
            if len(result.observations) >= cycles:
                scheduler.stop()
                break
    finally:
        await adapter.stop()

    result.dashboard.live_openalgo_verified = result.passed
    return result


class JsonlEvidenceWriter:
    """Create one append-only B03 evidence stream; never overwrite old data."""

    def __init__(self, path: Path):
        self.path = path
        self._stream = None

    async def __aenter__(self) -> "JsonlEvidenceWriter":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Exclusive create is intentional: observation evidence must never be
        # silently replaced by a subsequent run.
        self._stream = self.path.open("x", encoding="utf-8")
        self._stream.write(json.dumps({
            "record_type": "B03_LIVE_NIFTY_CAPTURE",
            "mode": "LIVE_READONLY_CAPTURE",
            "symbol": DEFAULT_SYMBOL,
            "exchange": DEFAULT_EXCHANGE,
            "cycles_requested": DEFAULT_CYCLES,
            "started_at_utc": datetime.now(UTC).isoformat(timespec="seconds"),
        }, sort_keys=True) + "\n")
        self._stream.flush()
        return self

    async def __aexit__(self, *_: object) -> None:
        if self._stream:
            self._stream.close()
            self._stream = None

    async def write(self, record: LiveCycleRecord) -> None:
        assert self._stream is not None
        self._stream.write(json.dumps(record.as_dict(), sort_keys=True) + "\n")
        self._stream.flush()

    async def write_summary(self, summary: dict[str, object]) -> None:
        assert self._stream is not None
        self._stream.write(json.dumps(summary, sort_keys=True) + "\n")
        self._stream.flush()


async def _serve_dashboard(state: DashboardState, port: int) -> web.AppRunner:
    runner = web.AppRunner(create_dashboard_app(state))
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", port).start()
    return runner


def _default_evidence_path() -> Path:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    root = Path(__file__).resolve().parents[1]
    return root / "Agent_Control" / "logs" / "B03" / "BUILD-001" / f"B03_live_nifty_{stamp}.jsonl"


async def _main_async(args: argparse.Namespace) -> int:
    path = args.evidence_path or _default_evidence_path()
    adapter = OpenAlgoAdapter()
    scheduler = ClockAlignedScheduler(interval_seconds=DEFAULT_INTERVAL_SECONDS)
    # Fail before binding a local port or creating an evidence file when this
    # controlled live run lacks its non-secret prerequisites.
    if not adapter.config.api_key:
        raise LiveValidationConfigurationError(
            "OPENALGO_API_KEY is required for the read-only B03B quote capture."
        )
    if adapter.config.underlying.upper() != DEFAULT_SYMBOL:
        raise LiveValidationConfigurationError(
            "B03B is bounded to NIFTY; set HERMES_UNDERLYING=NIFTY."
        )
    if not scheduler.is_market_open_now():
        raise LiveValidationConfigurationError(
            "NSE market hours are closed; B03B must run during an open weekday session."
        )
    # The state is created by the runner; the dashboard is supplied with the
    # same object once it becomes available.  Start it after construction via a
    # small wrapper below so the live page updates every cycle.
    state = DashboardState(symbol=DEFAULT_SYMBOL, mode="LIVE_READONLY_CAPTURE")
    dashboard_runner = await _serve_dashboard(state, args.dashboard_port)
    try:
        async with JsonlEvidenceWriter(path) as evidence:
            # ``run_live_nifty_validation`` owns its aggregate.  Populate the
            # visible dashboard from the same cycle callback without exposing
            # provider payloads or credentials to the web page.
            visible_drift = DriftMonitor(max_drift_ms=100.0)

            async def on_cycle(record: LiveCycleRecord) -> None:
                expected = datetime.fromisoformat(record.scheduled_at_ist)
                actual = datetime.fromisoformat(record.request_started_at_ist)
                visible_drift.record(expected, actual)
                state.record(
                    expected,
                    actual,
                    failed=not record.success,
                    drift=visible_drift,
                    ltp=record.ltp,
                    error=record.error_class,
                )
                await evidence.write(record)

            result = await run_live_nifty_validation(
                adapter=adapter,
                scheduler=scheduler,
                cycles=DEFAULT_CYCLES,
                on_cycle=on_cycle,
            )
            state.live_openalgo_verified = result.passed
            summary = result.summary()
            summary["dashboard_url"] = f"http://127.0.0.1:{args.dashboard_port}/"
            await evidence.write_summary(summary)
    finally:
        await dashboard_runner.cleanup()

    # JSON output contains only normalized market evidence and local timing.
    print(json.dumps(summary, sort_keys=True))
    print(f"Evidence: {path}")
    return 0 if result.passed else 2


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--evidence-path",
        type=Path,
        help="New JSONL evidence path (must not already exist).",
    )
    parser.add_argument(
        "--dashboard-port",
        type=int,
        default=8091,
        help="Temporary localhost dashboard port during the 20-cycle run.",
    )
    args = parser.parse_args()
    try:
        return asyncio.run(_main_async(args))
    except (LiveValidationConfigurationError, FileExistsError) as error:
        print(f"B03B not started: {error}", file=sys.stderr)
        return 2
    except OSError as error:
        print(f"B03B could not start its local dashboard/evidence file: {type(error).__name__}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

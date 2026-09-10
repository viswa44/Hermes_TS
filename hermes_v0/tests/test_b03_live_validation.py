from __future__ import annotations

from datetime import datetime, timedelta
from unittest import IsolatedAsyncioTestCase
from zoneinfo import ZoneInfo

from hermes_v0.collector.adapters.openalgo_adapter import AdapterConfig
from hermes_v0.collector.b03_live_validation import (
    DEFAULT_CYCLES,
    LiveValidationConfigurationError,
    run_live_nifty_validation,
)
from hermes_v0.domain.models import MarketSnapshot


IST = ZoneInfo("Asia/Kolkata")


class FakeScheduler:
    interval_seconds = 5
    tz = IST

    def __init__(self) -> None:
        self.stopped = False

    def is_market_open_now(self) -> bool:
        return True

    def stop(self) -> None:
        self.stopped = True

    async def tick_generator(self):
        start = datetime(2026, 9, 7, 9, 15, tzinfo=IST)
        for sequence in range(DEFAULT_CYCLES):
            yield start + timedelta(seconds=5 * (sequence + 1)), "2026-09-07"


class FakeAdapter:
    config = AdapterConfig(api_key="test-key", underlying="NIFTY")

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None

    async def get_underlying_snapshot(self, _symbol: str, *, timestamp_ist: datetime, trading_date: str) -> MarketSnapshot:
        return MarketSnapshot(
            timestamp_ist=timestamp_ist,
            trading_date=trading_date,
            symbol="NIFTY",
            spot_ltp=25000.0,
            source_latency_ms=2,
        )


class B03LiveValidationTests(IsolatedAsyncioTestCase):
    async def test_records_exactly_twenty_successful_read_only_cycles(self) -> None:
        scheduler = FakeScheduler()
        result = await run_live_nifty_validation(adapter=FakeAdapter(), scheduler=scheduler)

        self.assertTrue(result.passed)
        self.assertEqual(DEFAULT_CYCLES, len(result.observations))
        self.assertEqual(DEFAULT_CYCLES, result.dashboard.dashboard_updates)
        self.assertTrue(scheduler.stopped)
        self.assertTrue(all(record.success and record.ltp == 25000.0 for record in result.observations))

    async def test_non_positive_ltp_is_counted_as_a_failed_cycle(self) -> None:
        class ZeroLtpAdapter(FakeAdapter):
            async def get_underlying_snapshot(self, _symbol: str, *, timestamp_ist: datetime, trading_date: str) -> MarketSnapshot:
                return MarketSnapshot(timestamp_ist=timestamp_ist, trading_date=trading_date, spot_ltp=0.0)

        result = await run_live_nifty_validation(adapter=ZeroLtpAdapter(), scheduler=FakeScheduler())

        self.assertFalse(result.passed)
        self.assertEqual(DEFAULT_CYCLES, result.dashboard.failed_cycles)
        self.assertEqual("ValueError", result.observations[0].error_class)

    async def test_missing_key_blocks_before_any_live_request(self) -> None:
        class NoKeyAdapter(FakeAdapter):
            config = AdapterConfig(api_key="", underlying="NIFTY")

        with self.assertRaises(LiveValidationConfigurationError):
            await run_live_nifty_validation(adapter=NoKeyAdapter(), scheduler=FakeScheduler())

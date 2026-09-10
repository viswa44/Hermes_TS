from __future__ import annotations

from datetime import datetime
from unittest import IsolatedAsyncioTestCase, TestCase
from zoneinfo import ZoneInfo

from hermes_v0.collector.scheduler import ClockAlignedScheduler
from hermes_v0.dashboard import create_dashboard_app, run_off_market_validation


class B03OffMarketTests(IsolatedAsyncioTestCase):
    async def test_twenty_fake_cycles_are_aligned_and_visible_on_dashboard(self):
        state = await run_off_market_validation("BANKNIFTY", cycles=20)
        payload = state.as_dict()
        self.assertEqual(payload["symbol"], "BANKNIFTY")
        self.assertEqual(payload["cycles"], 20)
        self.assertEqual(payload["failed_cycles"], 1)
        self.assertEqual(payload["dashboard_updates"], 20)
        self.assertEqual(payload["drift"]["missed_intervals"], 0)
        self.assertFalse(payload["live_openalgo_verified"])

    async def test_dashboard_creation_does_not_start_or_duplicate_collection(self):
        state = await run_off_market_validation(cycles=1)
        first, second = create_dashboard_app(state), create_dashboard_app(state)
        self.assertEqual(len(list(first.router.routes())), len(list(second.router.routes())))
        self.assertEqual(state.cycles, 1)


class SchedulerAlignmentTests(TestCase):
    def test_next_boundary_is_five_second_aligned(self):
        scheduler = ClockAlignedScheduler(interval_seconds=5)
        boundary = scheduler._next_boundary(datetime(2026, 8, 29, 9, 15, 3, tzinfo=ZoneInfo("Asia/Kolkata")))
        self.assertEqual(boundary.second, 5)

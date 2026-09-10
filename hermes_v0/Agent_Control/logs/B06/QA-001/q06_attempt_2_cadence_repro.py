"""Deterministic Q06 evidence; virtual clock, no provider or database I/O.

Run by path with PYTHONPATH set to the workspace root and the dedicated
hermes_v0/.venv/bin/python runtime.
"""

import asyncio
import json
from datetime import timedelta
from unittest.mock import patch

from hermes_v0.collector.live_option_metrics import run_live_option_metrics
from hermes_v0.collector.scheduler import ClockAlignedScheduler
from hermes_v0.tests.test_b04_option_metrics import (
    OBSERVED, _FakeLiveAdapter, _FakeLiveWriter,
)


async def measure(collection_seconds, write_seconds):
    clock = [OBSERVED]

    class Clock:
        @staticmethod
        def now(tz):
            return clock[0].astimezone(tz)

    async def sleep(seconds):
        clock[0] += timedelta(seconds=seconds)

    class Adapter(_FakeLiveAdapter):
        async def collect_atm_option_metrics(self, timestamp_ist, trading_date):
            clock[0] += timedelta(seconds=collection_seconds)
            return await super().collect_atm_option_metrics(timestamp_ist, trading_date)

    class Writer(_FakeLiveWriter):
        async def write_cycle(self, *args):
            clock[0] += timedelta(seconds=write_seconds)
            await super().write_cycle(*args)

    scheduler = ClockAlignedScheduler(interval_seconds=5)
    scheduler._now_ist = lambda: clock[0]
    with patch("hermes_v0.collector.scheduler.asyncio.sleep", sleep), patch(
        "hermes_v0.collector.live_option_metrics.datetime", Clock
    ):
        result = await run_live_option_metrics(
            adapter=Adapter(), writer=Writer(), scheduler=scheduler, cycles=2
        )
    return {
        "simulated_collection_seconds": collection_seconds,
        "simulated_write_seconds": write_seconds,
        "scheduled_times": [r.scheduled_at_ist for r in result.records],
        "persisted_cycles": result.persisted_cycles,
        "missed_intervals": result.drift.get_stats()["missed_intervals"],
        "bounded_verification_passed": result.passed,
    }


async def main():
    cases = [await measure(0, 0), await measure(5.2, 0), await measure(0, 5.2)]
    assert [case["missed_intervals"] for case in cases] == [0, 1, 1]
    assert [case["bounded_verification_passed"] for case in cases] == [True, False, False]
    print(json.dumps({"evidence_type": "OFFLINE_VIRTUAL_CLOCK", "cases": cases}, indent=2))


if __name__ == "__main__":
    asyncio.run(main())

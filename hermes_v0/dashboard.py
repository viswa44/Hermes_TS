"""Read-only Hermes V0 runtime dashboard and B03 validation helpers.

``run_off_market_validation`` is deliberately deterministic and never creates
an OpenAlgo connection.  The live B03B runner supplies the same state object
while it performs bounded *quote-only* observation requests.
"""

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from aiohttp import web

from hermes_v0.collector.scheduler import DriftMonitor


IST = ZoneInfo("Asia/Kolkata")


@dataclass
class DashboardState:
    symbol: str
    mode: str = "OFF_MARKET_SIMULATION"
    cycles: int = 0
    failed_cycles: int = 0
    collector_state: str = "STOPPED"
    last_expected_time: str | None = None
    last_collection_time: str | None = None
    last_ltp: float | None = None
    last_error: str | None = None
    dashboard_updates: int = 0
    drift: dict[str, Any] = field(default_factory=dict)
    live_openalgo_verified: bool = False

    def record(
        self,
        expected: datetime,
        actual: datetime,
        *,
        failed: bool,
        drift: DriftMonitor,
        ltp: float | None = None,
        error: str | None = None,
    ) -> None:
        """Record one scheduler cycle without retaining provider raw payloads."""
        self.cycles += 1
        self.failed_cycles += int(failed)
        self.collector_state = "DEGRADED" if failed else "RUNNING"
        self.last_expected_time = expected.isoformat()
        self.last_collection_time = actual.isoformat()
        self.last_ltp = ltp
        self.last_error = error if failed else None
        self.dashboard_updates += 1
        self.drift = drift.get_stats()

    def as_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "symbol": self.symbol,
            "cycles": self.cycles,
            "failed_cycles": self.failed_cycles,
            "collector_state": self.collector_state,
            "last_expected_time": self.last_expected_time,
            "last_collection_time": self.last_collection_time,
            "last_ltp": self.last_ltp,
            "last_error": self.last_error,
            "dashboard_updates": self.dashboard_updates,
            "drift": self.drift,
            "live_openalgo_verified": self.live_openalgo_verified,
        }


async def run_off_market_validation(symbol: str = "NIFTY", cycles: int = 20) -> DashboardState:
    """Run deterministic B03A cycles with fake timestamps and one failed cycle."""
    state, drift = DashboardState(symbol=symbol), DriftMonitor(max_drift_ms=100.0)
    start = datetime(2026, 8, 29, 9, 15, tzinfo=IST)
    for number in range(cycles):
        expected = start + timedelta(seconds=5 * (number + 1))
        actual = expected + timedelta(milliseconds=1)
        drift.record(expected, actual)
        state.record(expected, actual, failed=(number == 9), drift=drift)
        await asyncio.sleep(0)
    return state


def create_dashboard_app(state: DashboardState) -> web.Application:
    """Create a dashboard; it only renders supplied state and never starts a collector."""
    app = web.Application()

    async def status(_: web.Request) -> web.Response:
        return web.json_response(state.as_dict())

    async def index(_: web.Request) -> web.Response:
        heading = state.mode.replace("_", " ").title()
        page = """<!doctype html><title>Hermes V0 Dashboard</title>
<h1>Hermes V0 — {heading}</h1><pre id='state'></pre>
<script>async function refresh(){{document.querySelector('#state').textContent=JSON.stringify(await (await fetch('/api/status')).json(),null,2)}}refresh();setInterval(refresh,1000)</script>""".format(heading=heading)
        return web.Response(text=page, content_type="text/html")

    app.router.add_get("/api/status", status)
    app.router.add_get("/", index)
    return app


async def main() -> None:
    state = await run_off_market_validation()
    print(state.as_dict())
    runner = web.AppRunner(create_dashboard_app(state))
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 8090)
    await site.start()
    print("Dashboard: http://127.0.0.1:8090/")
    await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())

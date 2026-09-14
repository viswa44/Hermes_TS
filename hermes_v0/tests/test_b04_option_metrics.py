"""Contract tests for B04's read-only ATM IV/Greeks PostgreSQL collector."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest import IsolatedAsyncioTestCase, TestCase
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

from hermes_v0.collector.adapters.openalgo_adapter import AdapterConfig, OpenAlgoAdapter, ProviderResponseError
from hermes_v0.collector.live_option_metrics import run_live_option_metrics
from hermes_v0.collector.scheduler import DriftMonitor
from hermes_v0.domain.models import DataStatus, MarketSnapshot, OptionGreeksSnapshot, OptionSnapshot
from hermes_v0.storage.option_metrics_writer import OptionMetricsWriter


IST = ZoneInfo("Asia/Kolkata")
OBSERVED = datetime(2026, 9, 7, 10, 0, tzinfo=IST)
INGESTED = OBSERVED.astimezone(timezone.utc) + timedelta(seconds=1)


def metric_cycle(timestamp: datetime = OBSERVED):
    market = MarketSnapshot(
        timestamp_ist=timestamp,
        trading_date="2026-09-07",
        symbol="NIFTY",
        spot_ltp=24800.0,
        ingestion_time=INGESTED,
    )
    ce = OptionSnapshot(
        timestamp_ist=timestamp,
        trading_date="2026-09-07",
        symbol="NIFTY",
        strike=24800.0,
        option_type="CE",
        expiry_date="09SEP26",
        ltp=110.0,
        oi=8385065.0,
        data_status=DataStatus.VALID,
        ingestion_time=INGESTED,
    )
    pe = OptionSnapshot(
        timestamp_ist=timestamp,
        trading_date="2026-09-07",
        symbol="NIFTY",
        strike=24800.0,
        option_type="PE",
        expiry_date="09SEP26",
        ltp=105.0,
        oi=19520345.0,
        data_status=DataStatus.VALID,
        ingestion_time=INGESTED,
    )
    base = dict(
        timestamp_ist=timestamp,
        trading_date="2026-09-07",
        underlying_symbol="NIFTY",
        strike=24800.0,
        expiry_date="09SEP26",
        option_ltp=110.0,
        underlying_ltp=24800.0,
        calculation_option_ltp=110.5,
        calculation_spot_ltp=24801.0,
        implied_volatility=12.5,
        delta=0.5,
        gamma=0.001,
        theta=-2.0,
        vega=12.0,
        rho=0.03,
        interest_rate=6.5,
        forward_price=24801.0,
        data_status=DataStatus.VALID,
        ingestion_time=INGESTED,
    )
    ce_greek = OptionGreeksSnapshot(option_symbol="NIFTY09SEP2624800CE", option_type="CE", **base)
    pe_greek = OptionGreeksSnapshot(
        option_symbol="NIFTY09SEP2624800PE", option_type="PE", option_ltp=105.0, **{
            key: value for key, value in base.items() if key != "option_ltp"
        }
    )
    return market, (ce, pe), (ce_greek, pe_greek)


class AdapterOptionMetricsTests(IsolatedAsyncioTestCase):
    async def test_top_level_greeks_are_normalized_with_calculation_provenance(self) -> None:
        adapter = OpenAlgoAdapter(AdapterConfig(api_key="test-key", max_retries=1))
        calls: list[tuple[str, dict]] = []

        async def provider(_: str, endpoint: str, payload: dict) -> dict:
            calls.append((endpoint, payload))
            if endpoint == "/api/v1/expiry":
                return {"status": "success", "data": ["17-SEP-26", "09-SEP-26"]}
            if endpoint == "/api/v1/quotes":
                return {"status": "success", "data": {"ltp": "24800.0", "bid": "24799.5"}}
            if endpoint == "/api/v1/optionchain":
                return {
                    "status": "success",
                    "atm_strike": "24800",
                    "chain": [{
                        "strike": 24800,
                        "ce": {
                            "symbol": "NIFTY09SEP2624800CE",
                            "ltp": "110.0",
                            "oi": "8385065",
                            "label": "ATM",
                        },
                        "pe": {
                            "symbol": "NIFTY09SEP2624800PE",
                            "ltp": 105.0,
                            "open_interest": 19520345,
                            "label": "ATM",
                        },
                    }],
                }
            if endpoint == "/api/v1/optiongreeks":
                call = payload["symbol"].endswith("CE")
                return {
                    "status": "success",
                    "option_price": 110.5 if call else 105.5,
                    "spot_price": 24802.0,
                    "interest_rate": 6.5,
                    "implied_volatility": 12.34,
                    "greeks": {
                        "delta": 0.51 if call else -0.49,
                        "gamma": 0.0012,
                        "theta": -2.34,
                        "vega": 13.2,
                        "rho": 0.04,
                    },
                }
            raise AssertionError(endpoint)

        adapter._request_with_retry = provider  # type: ignore[method-assign]
        # The fixture models September 7; nearest-expiry selection must not
        # depend on the real date on which this mocked test is run.
        with patch("hermes_v0.collector.adapters.openalgo_adapter.datetime", wraps=datetime) as clock:
            clock.now.side_effect = lambda tz=None: OBSERVED.astimezone(tz) if tz else OBSERVED.replace(tzinfo=None)
            result = await adapter.collect_atm_option_metrics(OBSERVED, "2026-09-07")

        self.assertEqual(24800.0, result.market_snapshot.spot_ltp)
        self.assertEqual("09SEP26", result.option_snapshots[0].expiry_date)
        self.assertEqual(110.0, result.option_snapshots[0].ltp)
        self.assertEqual(8385065.0, result.option_snapshots[0].oi)
        self.assertEqual(19520345.0, result.option_snapshots[1].oi)
        self.assertIsNone(result.option_snapshots[0].iv)
        self.assertEqual(DataStatus.VALID, result.greek_snapshots[0].data_status)
        self.assertEqual(12.34, result.greek_snapshots[0].implied_volatility)
        self.assertEqual(0.04, result.greek_snapshots[0].rho)
        self.assertEqual(6.5, result.greek_snapshots[0].interest_rate)
        self.assertEqual(110.5, result.greek_snapshots[0].calculation_option_ltp)
        self.assertEqual(24802.0, result.greek_snapshots[0].forward_price)
        self.assertTrue(result.market_snapshot.ingestion_time.tzinfo is not None)
        greek_payloads = [payload for endpoint, payload in calls if endpoint == "/api/v1/optiongreeks"]
        self.assertEqual(2, len(greek_payloads))
        self.assertTrue(all("forward_price" not in payload for payload in greek_payloads))

    async def test_missing_option_chain_oi_remains_missing_not_zero_filled(self) -> None:
        adapter = OpenAlgoAdapter(AdapterConfig(api_key="test-key"))

        snapshot = adapter._option_snapshot_from_contract(
            {"symbol": "NIFTY09SEP2624800CE", "ltp": "110.0"},
            timestamp_ist=OBSERVED,
            trading_date="2026-09-07",
            strike=24800.0,
            option_type="CE",
            expiry_date="09SEP26",
            source_latency_ms=10,
        )

        self.assertEqual(DataStatus.VALID, snapshot.data_status)
        self.assertIsNone(snapshot.oi)

    async def test_status_error_does_not_become_partial_metrics(self) -> None:
        adapter = OpenAlgoAdapter(AdapterConfig(api_key="test-key"))
        option = OptionSnapshot(
            timestamp_ist=OBSERVED,
            trading_date="2026-09-07",
            symbol="NIFTY",
            strike=24800.0,
            option_type="CE",
            expiry_date="09SEP26",
            ltp=100.0,
            data_status=DataStatus.VALID,
        )
        snapshot = adapter._greeks_snapshot_from_result(
            option,
            option_symbol="NIFTY09SEP2624800CE",
            underlying_ltp=24800.0,
            source_latency_ms=10,
            result={"status": "error", "message": "not available"},
        )
        self.assertEqual(DataStatus.MISSING, snapshot.data_status)
        self.assertEqual("ProviderResponseError", snapshot.error_class)
        self.assertIsNone(snapshot.implied_volatility)

    async def test_expiry_provider_failure_is_not_silently_guessed(self) -> None:
        adapter = OpenAlgoAdapter(AdapterConfig(api_key="test-key"))
        adapter._request_with_retry = AsyncMock(return_value={"status": "error"})

        with self.assertRaises(ProviderResponseError):
            await adapter.get_current_expiry()

        _, endpoint, _ = adapter._request_with_retry.await_args.args
        self.assertEqual("/api/v1/expiry", endpoint)


class TenSecondDriftTests(TestCase):
    def test_twenty_second_gap_is_one_missed_ten_second_interval(self) -> None:
        monitor = DriftMonitor(interval_seconds=10)
        monitor.record(OBSERVED, OBSERVED)
        monitor.record(OBSERVED + timedelta(seconds=20), OBSERVED + timedelta(seconds=20))
        self.assertEqual(1, monitor.get_stats()["missed_intervals"])


class _AsyncContext:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_: object) -> None:
        return None


class _FakeConnection:
    def __init__(self) -> None:
        self.executed: list[tuple[str, tuple]] = []
        self.many: list[tuple[str, list[tuple]]] = []

    async def fetchval(self, _: str) -> bool:
        return True

    async def execute(self, query: str, *values: object) -> None:
        self.executed.append((query, values))

    async def executemany(self, query: str, values: list[tuple]) -> None:
        self.many.append((query, values))

    def transaction(self) -> _AsyncContext:
        return _AsyncContext()


class _Acquire:
    def __init__(self, connection: _FakeConnection) -> None:
        self.connection = connection

    async def __aenter__(self) -> _FakeConnection:
        return self.connection

    async def __aexit__(self, *_: object) -> None:
        return None


class _FakePool:
    def __init__(self) -> None:
        self.connection = _FakeConnection()
        self.closed = False

    def acquire(self) -> _Acquire:
        return _Acquire(self.connection)

    async def close(self) -> None:
        self.closed = True


class WriterTests(IsolatedAsyncioTestCase):
    async def test_writer_keeps_raw_and_calculation_ltp_separate_and_time_ordered(self) -> None:
        pool = _FakePool()

        async def pool_factory(**_: object) -> _FakePool:
            return pool

        writer = OptionMetricsWriter(pool_factory=pool_factory)
        await writer.start()
        market, options, greeks = metric_cycle()
        await writer.write_cycle(market, options, greeks)
        await writer.stop()

        self.assertEqual(1, writer.stats["cycles_written"])
        self.assertEqual(2, writer.stats["option_rows_written"])
        self.assertEqual(2, writer.stats["greek_rows_written"])
        option_query, option_values = pool.connection.many[0]
        self.assertIn("volume, oi, iv", option_query)
        self.assertEqual(8385065.0, option_values[0][12])
        self.assertEqual(19520345.0, option_values[1][12])
        greek_query, greek_values = pool.connection.many[-1]
        self.assertIn("calculation_option_ltp", greek_query)
        self.assertEqual(25, len(greek_values[0]))
        self.assertEqual(110.0, greek_values[0][7])
        self.assertEqual(110.5, greek_values[0][9])
        self.assertGreaterEqual(greek_values[0][23], greek_values[0][0].astimezone(timezone.utc))


class _FakeScheduler:
    interval_seconds = 5
    tz = IST

    def __init__(self) -> None:
        self.stopped = False

    def is_market_open_now(self) -> bool:
        return True

    def stop(self) -> None:
        self.stopped = True

    async def tick_generator(self):
        for seconds in (5, 10):
            expected = OBSERVED + timedelta(seconds=seconds)
            yield expected, "2026-09-07"


class _FakeLiveAdapter:
    config = AdapterConfig(api_key="test-key", underlying="NIFTY")

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None

    async def collect_atm_option_metrics(self, timestamp_ist: datetime, _: str):
        market, options, greeks = metric_cycle(timestamp_ist)
        # New metric objects need ingestion after each synthetic scheduled tick.
        return type("Metrics", (), {
            "market_snapshot": market,
            "option_snapshots": options,
            "greek_snapshots": greeks,
        })()


class _FakeLiveWriter:
    def __init__(self) -> None:
        self.written = 0
        self.stats = {"cycles_written": 0}

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None

    async def write_cycle(self, *_: object) -> None:
        self.written += 1
        self.stats["cycles_written"] = self.written


class LiveRunnerTests(IsolatedAsyncioTestCase):
    async def test_runner_writes_one_cycle_per_clock_boundary(self) -> None:
        scheduler = _FakeScheduler()
        writer = _FakeLiveWriter()
        result = await run_live_option_metrics(
            adapter=_FakeLiveAdapter(),  # type: ignore[arg-type]
            writer=writer,  # type: ignore[arg-type]
            scheduler=scheduler,
            cycles=2,
        )

        self.assertEqual(2, writer.written)
        self.assertEqual(2, result.complete_metric_cycles)
        self.assertTrue(result.passed)
        self.assertTrue(scheduler.stopped)

    async def test_runner_allows_the_full_clock_interval_for_a_raw_oi_cycle(self) -> None:
        scheduler = _FakeScheduler()
        writer = _FakeLiveWriter()
        timeouts: list[float] = []

        async def capture_timeout(awaitable, *, timeout: float):
            timeouts.append(timeout)
            return await awaitable

        with patch("hermes_v0.collector.live_option_metrics.asyncio.wait_for", capture_timeout):
            result = await run_live_option_metrics(
                adapter=_FakeLiveAdapter(),  # type: ignore[arg-type]
                writer=writer,  # type: ignore[arg-type]
                scheduler=scheduler,
                cycles=2,
            )

        self.assertEqual([5.5, 5.5], timeouts)
        self.assertEqual(2, result.persisted_cycles)

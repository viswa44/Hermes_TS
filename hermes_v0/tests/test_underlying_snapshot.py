"""Contract tests for BUILD-001/B02 index selection and normalization."""

from datetime import datetime
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock

from hermes_v0.collector.adapters.openalgo_adapter import (
    AdapterConfig,
    OpenAlgoAdapter,
    ProviderResponseError,
    UnsupportedUnderlyingError,
)
from hermes_v0.domain.models import MarketSnapshot


class UnderlyingSnapshotTests(IsolatedAsyncioTestCase):
    async def test_banknifty_config_uses_mapping_and_returns_hermes_model(self):
        adapter = OpenAlgoAdapter(AdapterConfig(underlying="BANKNIFTY"))
        adapter._request_with_retry = AsyncMock(return_value={
            "status": "success",
            "data": {"ltp": 51234.5, "bid": 51234.0, "ask": 51235.0},
        })

        snapshot = await adapter.get_underlying_snapshot(
            adapter.config.underlying,
            timestamp_ist=datetime(2026, 8, 18, 10, 0),
        )

        self.assertIsInstance(snapshot, MarketSnapshot)
        self.assertEqual("BANKNIFTY", snapshot.symbol)
        self.assertEqual(51234.5, snapshot.spot_ltp)
        self.assertIsInstance(snapshot.provider_payload, str)
        _, _, request_payload = adapter._request_with_retry.await_args.args
        self.assertEqual("BANKNIFTY", request_payload["symbol"])
        self.assertEqual("NSE_INDEX", request_payload["exchange"])

    async def test_sensex_uses_bse_index_mapping(self):
        adapter = OpenAlgoAdapter(AdapterConfig())
        adapter._request_with_retry = AsyncMock(return_value={"data": {"ltp": 80000}})

        snapshot = await adapter.get_underlying_snapshot("SENSEX")

        self.assertEqual("SENSEX", snapshot.symbol)
        _, _, request_payload = adapter._request_with_retry.await_args.args
        self.assertEqual("BSE_INDEX", request_payload["exchange"])

    async def test_unsupported_symbol_fails_before_provider_request(self):
        adapter = OpenAlgoAdapter(AdapterConfig())
        adapter._request_with_retry = AsyncMock()

        with self.assertRaisesRegex(UnsupportedUnderlyingError, "Unsupported underlying 'FOO'"):
            await adapter.get_underlying_snapshot("FOO")

        adapter._request_with_retry.assert_not_awaited()

    async def test_missing_ltp_is_a_clear_normalization_error(self):
        adapter = OpenAlgoAdapter(AdapterConfig())
        adapter._request_with_retry = AsyncMock(return_value={"data": {"bid": 1}})

        with self.assertRaisesRegex(ProviderResponseError, "NIFTY.*no LTP"):
            await adapter.get_underlying_snapshot("NIFTY")

    async def test_non_object_provider_response_is_a_normalization_error(self):
        adapter = OpenAlgoAdapter(AdapterConfig())
        adapter._request_with_retry = AsyncMock(return_value=None)

        with self.assertRaisesRegex(ProviderResponseError, "response must be an object"):
            await adapter.get_underlying_snapshot("NIFTY")

    async def test_non_object_provider_data_is_a_normalization_error(self):
        adapter = OpenAlgoAdapter(AdapterConfig())
        adapter._request_with_retry = AsyncMock(return_value={"data": []})

        with self.assertRaisesRegex(ProviderResponseError, "data must be an object"):
            await adapter.get_underlying_snapshot("NIFTY")

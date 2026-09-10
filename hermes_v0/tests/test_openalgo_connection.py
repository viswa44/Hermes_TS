"""BUILD-001 tests for the read-only OpenAlgo startup connection."""

import httpx
import unittest

from hermes_v0.collector.openalgo_connection import ConnectionStatus, OpenAlgoConnection
from hermes_v0.config import OpenAlgoConfig


def connection_config(**changes):
    values = {
        "host": "http://openalgo.test",
        "api_key": "test-api-key-must-never-appear-in-logs",
        "connect_max_attempts": 3,
        "connect_backoff_seconds": 0,
        "connect_max_backoff_seconds": 0,
        "connect_timeout_seconds": 1,
    }
    values.update(changes)
    return OpenAlgoConfig(**values)


class OpenAlgoConnectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_success_uses_only_read_only_health_endpoint(self):
        requests = []

        def handler(request):
            requests.append(request)
            return httpx.Response(200, json={"status": "pass"})

        connection = OpenAlgoConnection(
            connection_config(), transport=httpx.MockTransport(handler)
        )
        state = await connection.connect()

        self.assertEqual(ConnectionStatus.CONNECTED, state.status)
        self.assertEqual("GET", requests[0].method)
        self.assertEqual("/health/status", requests[0].url.path)
        self.assertNotIn("apikey", requests[0].url.query.decode())
        self.assertFalse(any("order" in str(request.url) for request in requests))

    async def test_transient_failure_retries_then_reports_connected(self):
        attempts = 0

        def handler(request):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise httpx.ConnectError("unreachable", request=request)
            return httpx.Response(200, json={"status": "pass"})

        connection = OpenAlgoConnection(
            connection_config(), transport=httpx.MockTransport(handler)
        )
        state = await connection.connect()

        self.assertEqual(ConnectionStatus.CONNECTED, state.status)
        self.assertEqual(2, state.attempts)

    async def test_provider_warning_is_reported_as_degraded(self):
        def handler(request):
            return httpx.Response(503, json={"status": "fail"})

        connection = OpenAlgoConnection(
            connection_config(), transport=httpx.MockTransport(handler)
        )
        state = await connection.connect()

        self.assertEqual(ConnectionStatus.DEGRADED, state.status)
        self.assertEqual(1, state.attempts)

    async def test_timeout_exhaustion_is_disconnected_and_does_not_log_key(self):
        def handler(request):
            raise httpx.ReadTimeout("slow", request=request)

        connection = OpenAlgoConnection(
            connection_config(connect_max_attempts=2),
            transport=httpx.MockTransport(handler),
        )
        with self.assertLogs("hermes.openalgo_connection", level="WARNING") as logs:
            state = await connection.connect()

        output = "\n".join(logs.output)
        self.assertEqual(ConnectionStatus.DISCONNECTED, state.status)
        self.assertEqual(2, state.attempts)
        self.assertIn("timed out", state.detail)
        self.assertNotIn("test-api-key-must-never-appear-in-logs", output)
        self.assertNotIn("api_key", connection.get_status())

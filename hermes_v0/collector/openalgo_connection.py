"""Bounded, read-only OpenAlgo connectivity probe for BUILD-001.

This module is intentionally separate from market-data collection.  Its only
permitted network operation is ``GET /health/status``; it never sends the API
key and cannot reach an order endpoint.
"""

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Optional

import httpx

from hermes_v0.config import CFG, OpenAlgoConfig


logger = logging.getLogger("hermes.openalgo_connection")
_READ_ONLY_HEALTH_PATH = "/health/status"


class ConnectionStatus(str, Enum):
    """Externally visible state of the OpenAlgo connection."""

    CONNECTED = "CONNECTED"
    DISCONNECTED = "DISCONNECTED"
    DEGRADED = "DEGRADED"


@dataclass(frozen=True)
class ConnectionState:
    """Safe-to-display connection state; it deliberately excludes secrets."""

    status: ConnectionStatus
    checked_at: Optional[datetime]
    attempts: int
    api_key_configured: bool
    detail: str

    def as_dict(self) -> dict:
        return {
            "status": self.status.value,
            "checked_at": self.checked_at.isoformat() if self.checked_at else None,
            "attempts": self.attempts,
            "api_key_configured": self.api_key_configured,
            "detail": self.detail,
        }


class OpenAlgoConnection:
    """Confirm that the configured OpenAlgo host is alive without trading."""

    def __init__(self, config: Optional[OpenAlgoConfig] = None, *, transport=None):
        self.config = config or CFG.openalgo
        if self.config.health_path != _READ_ONLY_HEALTH_PATH:
            raise ValueError("OpenAlgo health probe path must be /health/status")
        if not self.config.host.startswith(("http://", "https://")):
            raise ValueError("OPENALGO_HOST must be an HTTP(S) URL")
        if self.config.connect_max_attempts < 1:
            raise ValueError("OPENALGO_CONNECT_MAX_ATTEMPTS must be at least 1")
        self._transport = transport
        self._state = ConnectionState(
            status=ConnectionStatus.DISCONNECTED,
            checked_at=None,
            attempts=0,
            api_key_configured=bool(self.config.api_key),
            detail="not checked",
        )

    @property
    def status(self) -> ConnectionStatus:
        return self._state.status

    def get_status(self) -> dict:
        """Return a credential-safe status payload for logs or a future dashboard."""
        return self._state.as_dict()

    async def connect(self) -> ConnectionState:
        """Probe OpenAlgo with bounded retries and exponential backoff."""
        delay = self.config.connect_backoff_seconds
        last_detail = "OpenAlgo did not respond"

        timeout = httpx.Timeout(self.config.connect_timeout_seconds)
        async with httpx.AsyncClient(
            base_url=self.config.host,
            timeout=timeout,
            headers={"Accept": "application/json"},
            transport=self._transport,
        ) as client:
            for attempt in range(1, self.config.connect_max_attempts + 1):
                try:
                    # Safety boundary: this is the sole permitted request in B01.
                    response = await client.get(_READ_ONLY_HEALTH_PATH)
                    if response.status_code in (200, 503):
                        payload = response.json()
                        provider_status = str(payload.get("status", "")).lower()
                        status = (
                            ConnectionStatus.CONNECTED
                            if response.status_code == 200 and provider_status == "pass"
                            else ConnectionStatus.DEGRADED
                        )
                        return self._record(status, attempt, "OpenAlgo health check completed")
                    last_detail = f"health endpoint returned HTTP {response.status_code}"
                except httpx.TimeoutException:
                    last_detail = "health check timed out"
                except httpx.RequestError:
                    last_detail = "health endpoint was unreachable"
                except ValueError:
                    last_detail = "health endpoint returned invalid JSON"

                if attempt < self.config.connect_max_attempts:
                    # Log no URL, response body, or credential-derived content.
                    logger.warning(
                        "OpenAlgo connection attempt %s/%s failed: %s; retrying",
                        attempt,
                        self.config.connect_max_attempts,
                        last_detail,
                    )
                    await asyncio.sleep(delay)
                    delay = min(delay * 2, self.config.connect_max_backoff_seconds)

        logger.error(
            "OpenAlgo remains disconnected after %s bounded attempts: %s",
            self.config.connect_max_attempts,
            last_detail,
        )
        return self._record(ConnectionStatus.DISCONNECTED, self.config.connect_max_attempts, last_detail)

    def _record(self, status: ConnectionStatus, attempts: int, detail: str) -> ConnectionState:
        self._state = ConnectionState(
            status=status,
            checked_at=datetime.now(timezone.utc),
            attempts=attempts,
            api_key_configured=bool(self.config.api_key),
            detail=detail,
        )
        logger.info("OpenAlgo connection status: %s", status.value)
        return self._state

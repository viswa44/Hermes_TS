"""OpenAlgo data adapter for Hermes V0.
openalgo_adapter.py
Implements the data source interface for collecting market snapshots
from OpenAlgo REST API (which connects to Fyers broker).
"""

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

import httpx
from hermes_v0.domain.models import (
    MarketSnapshot, OptionGreeksSnapshot, OptionSnapshot, DataStatus, SnapshotVersion
)
from hermes_v0.collector.validator import SnapshotValidator, default_validator
from hermes_v0.config import CFG, DEFAULT_INDEX_EXCHANGE_MAP


IST = ZoneInfo("Asia/Kolkata")
logger = logging.getLogger(__name__)


class UnsupportedUnderlyingError(ValueError):
    """Raised before I/O when Hermes is not configured to collect an index."""


class ProviderResponseError(ValueError):
    """Raised when a provider response cannot become a Hermes observation."""


@dataclass
class AdapterConfig:
    """Configuration for OpenAlgo adapter."""
    base_url: str = "http://127.0.0.1:5000"
    api_key: str = ""
    timeout_seconds: float = 5.0
    max_retries: int = 3
    retry_base_delay: float = 0.5
    retry_max_delay: float = 5.0
    
    # Symbol configuration
    underlying: str = "NIFTY"
    vix_symbol: str = "INDIA VIX"
    index_exchange: str = "NSE_INDEX"
    index_exchange_map: tuple[tuple[str, str], ...] = DEFAULT_INDEX_EXCHANGE_MAP
    options_exchange: str = "NFO"
    strike_interval: int = 50
    
    @classmethod
    def from_env(cls) -> "AdapterConfig":
        """Create config from environment/hermes config."""
        c = CFG.openalgo
        return cls(
            base_url=c.host,
            api_key=c.api_key,
            underlying=c.underlying,
            vix_symbol=c.vix_symbol,
            index_exchange=c.index_exchange,
            index_exchange_map=c.index_exchange_map,
            options_exchange=c.options_exchange,
            strike_interval=c.strike_interval,
        )


@dataclass(frozen=True)
class AtmOptionMetrics:
    """One raw quote cycle and its separate calculated-IV/Greek records."""

    market_snapshot: MarketSnapshot
    option_snapshots: tuple[OptionSnapshot, OptionSnapshot]
    greek_snapshots: tuple[OptionGreeksSnapshot, OptionGreeksSnapshot]


class OpenAlgoAdapter:
    """Async adapter for OpenAlgo REST API.
    
    Fetches spot quotes, VIX, option chain, and Greeks.
    Designed to be replaceable with other data sources.
    """
    
    def __init__(self, config: Optional[AdapterConfig] = None):
        self.config = config or AdapterConfig.from_env()
        self._client: Optional[httpx.AsyncClient] = None
        self._validator = default_validator
        
        # Cache for expiry date and strikes
        self._expiry_cache: Optional[str] = None
        self._expiry_cache_time: Optional[datetime] = None
        self._strikes_cache: list[float] = []
    
    async def start(self):
        """Start the adapter (create HTTP client)."""
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self.config.base_url,
                timeout=self.config.timeout_seconds,
                headers={"Content-Type": "application/json"}
            )
    
    async def stop(self):
        """Stop the adapter (close HTTP client)."""
        if self._client:
            await self._client.aclose()
            self._client = None
    
    async def __aenter__(self):
        await self.start()
        return self
    
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.stop()
    
    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self.config.base_url,
                timeout=self.config.timeout_seconds,
                headers={"Content-Type": "application/json"}
            )
        return self._client
    
    async def _request_with_retry(
        self, 
        method: str, 
        endpoint: str, 
        json_data: dict
    ) -> dict:
        """Make HTTP request with exponential backoff retry."""
        client = self._get_client()
        delay = self.config.retry_base_delay
        
        for attempt in range(self.config.max_retries):
            try:
                response = await client.request(
                    method, endpoint, json=json_data
                )
                response.raise_for_status()
                return response.json()
            except httpx.TimeoutException as e:
                if attempt == self.config.max_retries - 1:
                    raise
                await asyncio.sleep(delay)
                delay = min(delay * 2, self.config.retry_max_delay)
            except httpx.HTTPStatusError as e:
                if e.response.status_code >= 500:
                    if attempt == self.config.max_retries - 1:
                        raise
                    await asyncio.sleep(delay)
                    delay = min(delay * 2, self.config.retry_max_delay)
                else:
                    raise
    
    async def get_spot_quote(self) -> dict:
        """Get the configured underlying's raw provider quote (adapter-only)."""
        return await self._get_underlying_quote(self.config.underlying)

    def _resolve_underlying_exchange(self, symbol: str) -> tuple[str, str]:
        """Return canonical symbol/exchange or fail before contacting OpenAlgo."""
        canonical_symbol = symbol.strip().upper()
        exchange_map = {
            configured_symbol.strip().upper(): exchange.strip().upper()
            for configured_symbol, exchange in self.config.index_exchange_map
        }
        try:
            return canonical_symbol, exchange_map[canonical_symbol]
        except KeyError as exc:
            supported = ", ".join(sorted(exchange_map))
            raise UnsupportedUnderlyingError(
                f"Unsupported underlying {symbol!r}. Configure index_exchange_map; "
                f"supported symbols: {supported}."
            ) from exc

    async def _get_underlying_quote(self, symbol: str) -> dict:
        """Fetch raw provider data; raw dictionaries do not leave this adapter."""
        canonical_symbol, exchange = self._resolve_underlying_exchange(symbol)
        payload = {
            "apikey": self.config.api_key,
            "symbol": canonical_symbol,
            "exchange": exchange,
        }
        return await self._request_with_retry("POST", "/api/v1/quotes", payload)

    async def get_underlying_snapshot(
        self,
        symbol: str,
        *,
        timestamp_ist: Optional[datetime] = None,
        trading_date: Optional[str] = None,
    ) -> MarketSnapshot:
        """Fetch one configured index as a normalized Hermes ``MarketSnapshot``.

        This is intentionally narrower than ``collect_snapshot``: callers get
        the shared domain model and never receive an OpenAlgo response shape.
        """
        canonical_symbol, exchange = self._resolve_underlying_exchange(symbol)
        timestamp_ist = timestamp_ist or datetime.now(IST)
        trading_date = trading_date or timestamp_ist.strftime("%Y-%m-%d")
        started = time.perf_counter()
        response = await self._get_underlying_quote(canonical_symbol)
        quote = self._extract_quote_data(response)
        if not isinstance(quote["ltp"], (int, float)) or quote["ltp"] <= 0:
            raise ProviderResponseError(
                f"OpenAlgo quote for {canonical_symbol} on {exchange} has no LTP or a non-positive LTP."
            )

        # The mapping is completed here so provider-specific keys cannot leak
        # into the scheduler, validator, storage, or future data providers.
        snapshot = MarketSnapshot(
            version=SnapshotVersion.V1,
            timestamp_ist=timestamp_ist,
            trading_date=trading_date,
            symbol=canonical_symbol,
            spot_ltp=quote["ltp"],
            spot_bid=quote["bid"],
            spot_ask=quote["ask"],
            spot_prev_close=quote["prev_close"],
            spot_open=quote["open"],
            spot_high=quote["high"],
            spot_low=quote["low"],
            spot_volume=quote["volume"],
            source_latency_ms=int((time.perf_counter() - started) * 1000),
            # Aware UTC prevents PostgreSQL from treating a UTC clock value as
            # local IST and inverting observation/ingestion order.
            ingestion_time=datetime.now(timezone.utc),
            provider_payload=json.dumps({"underlying": response}),
        )
        logger.info("Normalized underlying snapshot symbol=%s exchange=%s", canonical_symbol, exchange)
        return snapshot
    
    async def get_vix_quote(self) -> dict:
        """Get India VIX quote."""
        payload = {
            "apikey": self.config.api_key,
            "symbol": self.config.vix_symbol,
            "exchange": self.config.index_exchange,
        }
        return await self._request_with_retry("POST", "/api/v1/quotes", payload)
    
    async def get_option_chain(
        self, 
        expiry_date: str, 
        strike_count: int = 10
    ) -> dict:
        """Get option chain with quotes."""
        _, index_exchange = self._resolve_underlying_exchange(self.config.underlying)
        payload = {
            "apikey": self.config.api_key,
            "underlying": self.config.underlying,
            "exchange": index_exchange,
            "expiry_date": expiry_date,
            "strike_count": strike_count,
        }
        return await self._request_with_retry("POST", "/api/v1/optionchain", payload)
    
    async def get_option_greeks(
        self, 
        symbol: str, 
        exchange: str = "NFO",
        forward_price: Optional[float] = None
    ) -> dict:
        """Get option Greeks for a single contract."""
        payload = {
            "apikey": self.config.api_key,
            "symbol": symbol,
            "exchange": exchange,
        }
        if forward_price:
            payload["forward_price"] = forward_price
        return await self._request_with_retry("POST", "/api/v1/optiongreeks", payload)

    async def get_option_expiries(self) -> list[str]:
        """Fetch live F&O expiries instead of guessing a calendar weekday.

        NIFTY expiry conventions can change.  The dedicated endpoint is the
        source for the collector's current contract rather than a Thursday
        fallback that can silently request an unavailable series.
        """
        payload = {
            "apikey": self.config.api_key,
            "symbol": self.config.underlying,
            "exchange": self.config.options_exchange,
            "instrumenttype": "options",
        }
        response = await self._request_with_retry("POST", "/api/v1/expiry", payload)
        if not isinstance(response, dict) or response.get("status") != "success":
            raise ProviderResponseError("OpenAlgo expiry response was not successful.")
        dates = response.get("data")
        if not isinstance(dates, list):
            raise ProviderResponseError("OpenAlgo expiry response data must be a list.")
        normalized: list[tuple[datetime, str]] = []
        today = datetime.now(IST).date()
        for value in dates:
            if not isinstance(value, str):
                continue
            try:
                parsed = datetime.strptime(value.upper(), "%d-%b-%y")
            except ValueError:
                # Some providers already return the canonical API format.
                try:
                    parsed = datetime.strptime(value.upper(), "%d%b%y")
                except ValueError:
                    continue
            if parsed.date() >= today:
                normalized.append((parsed, parsed.strftime("%d%b%y").upper()))
        if not normalized:
            raise ProviderResponseError("OpenAlgo returned no usable option expiry dates.")
        # Do not assume the provider order; the collector must use the nearest
        # non-expired contract even if the endpoint returns an unsorted list.
        return [value for _, value in sorted(normalized, key=lambda item: item[0])]
    
    async def get_current_expiry(self) -> str:
        """Get the nearest live option expiry date in canonical DDMMMYY format."""
        # Check cache (valid for 1 hour)
        now = datetime.now(IST)
        if (self._expiry_cache and self._expiry_cache_time and 
            (now - self._expiry_cache_time).total_seconds() < 3600):
            return self._expiry_cache
        
        # Prefer the live expiry endpoint.  This avoids guessing a weekday when
        # the exchange changes an index expiry schedule.
        try:
            self._expiry_cache = (await self.get_option_expiries())[0]
            self._expiry_cache_time = now
            return self._expiry_cache
        except httpx.HTTPStatusError as error:
            # The bounded compatibility probe exists only for older OpenAlgo
            # versions where this endpoint is absent. Authentication, rate,
            # network, and malformed-response failures must remain visible;
            # otherwise a bad API key could trigger ten unnecessary requests.
            if error.response.status_code not in {404, 405}:
                raise
            logger.info("OpenAlgo expiry endpoint unavailable; using bounded compatibility probe")

        # Try known expiries for the coming days only when /expiry is absent.
        for days_ahead in range(0, 10):
            test_date = now + timedelta(days=days_ahead)
            test_str = test_date.strftime("%d%b%y").upper()
            try:
                chain = await self.get_option_chain(test_str, strike_count=1)
                if chain.get("status") == "success":
                    self._expiry_cache = chain.get("expiry_date", test_str)
                    self._expiry_cache_time = now
                    return self._expiry_cache
            except Exception:
                continue
        
        # Fallback: calculate next Thursday
        days_ahead = (3 - now.weekday()) % 7  # Thursday = 3
        if days_ahead == 0 and now.hour >= 15:
            days_ahead = 7
        expiry = now + timedelta(days=days_ahead)
        self._expiry_cache = expiry.strftime("%d%b%y").upper()
        self._expiry_cache_time = now
        return self._expiry_cache
    
    def _find_atm_strike(self, spot: float) -> float:
        """Find ATM strike (nearest multiple of strike_interval)."""
        interval = self.config.strike_interval
        return round(spot / interval) * interval
    
    def _get_atm_greeks_symbols(
        self, 
        atm_strike: float, 
        expiry_date: str
    ) -> tuple[str, str]:
        """Generate ATM CE and PE symbols."""
        base = self.config.underlying
        ce = f"{base}{expiry_date}{int(atm_strike)}CE"
        pe = f"{base}{expiry_date}{int(atm_strike)}PE"
        return ce, pe
    
    def _extract_quote_data(self, response: dict) -> dict:
        """Extract quote data from a validated OpenAlgo response.

        Provider outages and schema changes must become an explicit adapter
        boundary error, not an AttributeError from calling ``.get`` on a
        malformed response shape.
        """
        if not isinstance(response, dict):
            raise ProviderResponseError(
                f"OpenAlgo quote response must be an object, got {type(response).__name__}."
            )
        data = response.get("data", {})
        if not isinstance(data, dict):
            raise ProviderResponseError(
                f"OpenAlgo quote response data must be an object, got {type(data).__name__}."
            )
        # Normalize numbers at the provider boundary. PostgreSQL must never
        # receive an unvalidated broker string, and a zero LTP must remain a
        # visible invalid value rather than being mistaken for a missing key.
        return {
            "ltp": self._optional_number(
                data.get("ltp") if data.get("ltp") is not None else data.get("last_price")
            ),
            "bid": self._optional_number(data.get("bid")),
            "ask": self._optional_number(data.get("ask")),
            "bid_qty": self._optional_number(
                data.get("bid_qty") if data.get("bid_qty") is not None else data.get("bid_quantity")
            ),
            "ask_qty": self._optional_number(
                data.get("ask_qty") if data.get("ask_qty") is not None else data.get("ask_quantity")
            ),
            "volume": self._optional_number(data.get("volume")),
            "oi": self._optional_number(
                data.get("oi") if data.get("oi") is not None else data.get("open_interest")
            ),
            "prev_close": self._optional_number(
                data.get("prev_close") if data.get("prev_close") is not None else data.get("previous_close")
            ),
            "open": self._optional_number(data.get("open")),
            "high": self._optional_number(data.get("high")),
            "low": self._optional_number(data.get("low")),
        }

    @staticmethod
    def _optional_number(value: object) -> float | None:
        """Return a finite numeric value or ``None`` without leaking raw types."""
        if isinstance(value, bool) or value is None:
            return None
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        return number if number == number and abs(number) != float("inf") else None

    @staticmethod
    def _optional_int(value: object) -> int | None:
        number = OpenAlgoAdapter._optional_number(value)
        return int(number) if number is not None and number.is_integer() else None
    
    def _extract_chain_atm_data(
        self, 
        chain_response: dict, 
        atm_strike: float
    ) -> dict:
        """Extract ATM CE/PE data from option chain response."""
        chain = chain_response.get("chain", [])
        atm_ce = {}
        atm_pe = {}
        
        for item in chain:
            if item.get("strike") == atm_strike:
                ce = item.get("ce", {})
                pe = item.get("pe", {})
                
                atm_ce = {
                    "ltp": ce.get("ltp"),
                    "bid": ce.get("bid"),
                    "ask": ce.get("ask"),
                    "bid_qty": ce.get("bid_qty"),
                    "ask_qty": ce.get("ask_qty"),
                    "volume": ce.get("volume"),
                    "oi": ce.get("oi"),
                    "iv": ce.get("iv"),
                }
                atm_pe = {
                    "ltp": pe.get("ltp"),
                    "bid": pe.get("bid"),
                    "ask": pe.get("ask"),
                    "bid_qty": pe.get("bid_qty"),
                    "ask_qty": pe.get("ask_qty"),
                    "volume": pe.get("volume"),
                    "oi": pe.get("oi"),
                    "iv": pe.get("iv"),
                }
                break
        
        return {"ce": atm_ce, "pe": atm_pe}
    
    def _extract_greeks(self, greeks_response: dict) -> dict:
        """Extract calculated Greeks from a validated OpenAlgo response."""
        if not isinstance(greeks_response, dict):
            raise ProviderResponseError("OpenAlgo Greeks response must be an object.")
        if greeks_response.get("status") != "success":
            raise ProviderResponseError("OpenAlgo Greeks response was not successful.")
        # Current OpenAlgo returns calculated values at the top level. Accept
        # a nested ``data`` object too for older local releases, but do not
        # silently coerce another response shape into zeroes or empty data.
        nested = greeks_response.get("data")
        data = nested if isinstance(nested, dict) else greeks_response
        if not isinstance(data, dict):
            raise ProviderResponseError("OpenAlgo Greeks response data must be an object.")
        greeks = data.get("greeks")
        if not isinstance(greeks, dict):
            raise ProviderResponseError("OpenAlgo Greeks response values must be an object.")
        return {
            "delta": self._optional_number(greeks.get("delta")),
            "gamma": self._optional_number(greeks.get("gamma")),
            "theta": self._optional_number(greeks.get("theta")),
            "vega": self._optional_number(greeks.get("vega")),
            "rho": self._optional_number(greeks.get("rho")),
            "iv": self._optional_number(data.get("implied_volatility")),
            "interest_rate": self._optional_number(
                data.get("interest_rate")
                if data.get("interest_rate") is not None
                else data.get("risk_free_rate")
            ),
            "calculation_option_ltp": self._optional_number(data.get("option_price")),
            "calculation_spot_ltp": self._optional_number(data.get("spot_price")),
            "calculation_forward_price": self._optional_number(data.get("forward_price")),
        }

    def _extract_atm_contracts(self, chain_response: dict) -> tuple[float, dict, dict]:
        """Return the actual ATM contracts supplied by the option-chain response."""
        if not isinstance(chain_response, dict) or chain_response.get("status") != "success":
            raise ProviderResponseError("OpenAlgo option-chain response was not successful.")
        strike = self._optional_number(chain_response.get("atm_strike"))
        chain = chain_response.get("chain")
        if strike is None or strike <= 0 or not isinstance(chain, list):
            raise ProviderResponseError("OpenAlgo option-chain response has no usable ATM contract.")
        for item in chain:
            if not isinstance(item, dict) or self._optional_number(item.get("strike")) != strike:
                continue
            ce, pe = item.get("ce"), item.get("pe")
            if not isinstance(ce, dict) or not isinstance(pe, dict):
                break
            if not isinstance(ce.get("symbol"), str) or not isinstance(pe.get("symbol"), str):
                break
            return float(strike), ce, pe
        raise ProviderResponseError("OpenAlgo option-chain response has no complete ATM CE/PE pair.")

    @staticmethod
    def _option_data_status(contract: dict) -> DataStatus:
        ltp = OpenAlgoAdapter._optional_number(contract.get("ltp"))
        return DataStatus.VALID if ltp is not None and ltp > 0 else DataStatus.MISSING

    def _option_snapshot_from_contract(
        self,
        contract: dict,
        *,
        timestamp_ist: datetime,
        trading_date: str,
        strike: float,
        option_type: str,
        expiry_date: str,
        source_latency_ms: int,
    ) -> OptionSnapshot:
        """Normalize raw chain quote values without treating zero as a valid quote."""
        status = self._option_data_status(contract)
        return OptionSnapshot(
            timestamp_ist=timestamp_ist,
            trading_date=trading_date,
            symbol=self.config.underlying,
            strike=strike,
            option_type=option_type,
            expiry_date=expiry_date,
            ltp=self._optional_number(contract.get("ltp")) if status == DataStatus.VALID else None,
            bid=self._optional_number(contract.get("bid")),
            ask=self._optional_number(contract.get("ask")),
            bid_qty=self._optional_number(
                contract.get("bid_qty") if contract.get("bid_qty") is not None else contract.get("bid_quantity")
            ),
            ask_qty=self._optional_number(
                contract.get("ask_qty") if contract.get("ask_qty") is not None else contract.get("ask_quantity")
            ),
            volume=self._optional_number(contract.get("volume")),
            oi=self._optional_number(
                contract.get("oi") if contract.get("oi") is not None else contract.get("open_interest")
            ),
            # This is retained only if OpenAlgo actually returns a chain IV.
            # Calculated Black-76 IV belongs in OptionGreeksSnapshot instead.
            iv=self._optional_number(contract.get("iv")),
            moneyness=contract.get("label"),
            lotsize=self._optional_int(contract.get("lotsize")),
            tick_size=self._optional_number(contract.get("tick_size")),
            data_status=status,
            source_latency_ms=source_latency_ms,
            ingestion_time=datetime.now(timezone.utc),
        )

    def _greeks_snapshot_from_result(
        self,
        option: OptionSnapshot,
        *,
        option_symbol: str,
        underlying_ltp: float | None,
        source_latency_ms: int,
        result: dict | Exception,
    ) -> OptionGreeksSnapshot:
        """Preserve failed calculations as missing evidence rather than zeroes."""
        if option.data_status != DataStatus.VALID:
            values = {}
            status, error_class = DataStatus.MISSING, "MissingRawOptionLtp"
        elif isinstance(result, Exception):
            values: dict[str, float | None] = {}
            status, error_class = DataStatus.MISSING, type(result).__name__
        else:
            try:
                values = self._extract_greeks(result)
                complete = (
                    isinstance(values.get("iv"), (int, float))
                    and values["iv"] > 0
                    and all(
                        isinstance(values.get(name), (int, float))
                        for name in ("delta", "gamma", "theta", "vega", "rho")
                    )
                )
                status = DataStatus.VALID if complete else DataStatus.PARTIAL
                error_class = None if status == DataStatus.VALID else "MissingCalculatedMetrics"
            except Exception as error:
                values = {}
                status, error_class = DataStatus.MISSING, type(error).__name__
        return OptionGreeksSnapshot(
            timestamp_ist=option.timestamp_ist,
            trading_date=option.trading_date,
            underlying_symbol=self.config.underlying,
            option_symbol=option_symbol,
            strike=option.strike,
            option_type=option.option_type,
            expiry_date=option.expiry_date,
            option_ltp=option.ltp,
            underlying_ltp=underlying_ltp,
            calculation_option_ltp=values.get("calculation_option_ltp"),
            calculation_spot_ltp=values.get("calculation_spot_ltp"),
            implied_volatility=values.get("iv"),
            delta=values.get("delta"),
            gamma=values.get("gamma"),
            theta=values.get("theta"),
            vega=values.get("vega"),
            rho=values.get("rho"),
            interest_rate=values.get("interest_rate"),
            # Do not relabel cash NIFTY as a Black-76 forward. When no custom
            # forward is supplied, OpenAlgo returns the actual calculation
            # input as ``spot_price`` (which can be its resolved synthetic
            # forward); retain that separately from the raw index quote.
            forward_price=values.get("calculation_forward_price") or values.get("calculation_spot_ltp"),
            data_status=status,
            source_latency_ms=source_latency_ms,
            error_class=error_class,
            ingestion_time=datetime.now(timezone.utc),
        )

    async def collect_atm_option_metrics(
        self,
        timestamp_ist: datetime | None = None,
        trading_date: str | None = None,
    ) -> AtmOptionMetrics:
        """Collect the real ATM CE/PE pair plus separate calculated IV/Greeks.

        It uses only quote, expiry, option-chain, and option-Greeks endpoints.
        The pair is intentionally limited to ATM CE/PE: at five seconds it
        consumes two Greeks calls per cycle, within the configured 30/minute
        endpoint budget without expanding into a full-chain calculation.
        """
        started = time.perf_counter()
        timestamp_ist = timestamp_ist or datetime.now(IST)
        trading_date = trading_date or timestamp_ist.strftime("%Y-%m-%d")
        expiry = await self.get_current_expiry()
        market_task = asyncio.create_task(
            self.get_underlying_snapshot(
                self.config.underlying,
                timestamp_ist=timestamp_ist,
                trading_date=trading_date,
            )
        )
        chain_task = asyncio.create_task(self.get_option_chain(expiry, strike_count=1))
        market, chain = await asyncio.gather(market_task, chain_task)
        strike, ce_contract, pe_contract = self._extract_atm_contracts(chain)
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        ce = self._option_snapshot_from_contract(
            ce_contract,
            timestamp_ist=timestamp_ist,
            trading_date=trading_date,
            strike=strike,
            option_type="CE",
            expiry_date=expiry,
            source_latency_ms=elapsed_ms,
        )
        pe = self._option_snapshot_from_contract(
            pe_contract,
            timestamp_ist=timestamp_ist,
            trading_date=trading_date,
            strike=strike,
            option_type="PE",
            expiry_date=expiry,
            source_latency_ms=elapsed_ms,
        )
        async def calculated_values_for(contract: dict, option: OptionSnapshot) -> dict:
            # A missing raw option LTP is evidence of an unavailable contract,
            # not a reason to spend a rate-limited calculation request.
            if option.data_status != DataStatus.VALID:
                raise ProviderResponseError("Cannot calculate Greeks without a positive raw option LTP.")
            # Let OpenAlgo resolve the current Black-76 forward/synthetic
            # future. Passing cash NIFTY as ``forward_price`` would incorrectly
            # claim that the cash quote is the futures input to the model.
            return await self.get_option_greeks(contract["symbol"], self.config.options_exchange)

        greek_results = await asyncio.gather(
            calculated_values_for(ce_contract, ce),
            calculated_values_for(pe_contract, pe),
            return_exceptions=True,
        )
        total_elapsed_ms = int((time.perf_counter() - started) * 1000)
        return AtmOptionMetrics(
            market_snapshot=market,
            option_snapshots=(ce, pe),
            greek_snapshots=(
                self._greeks_snapshot_from_result(
                    ce,
                    option_symbol=ce_contract["symbol"],
                    underlying_ltp=market.spot_ltp,
                    source_latency_ms=total_elapsed_ms,
                    result=greek_results[0],
                ),
                self._greeks_snapshot_from_result(
                    pe,
                    option_symbol=pe_contract["symbol"],
                    underlying_ltp=market.spot_ltp,
                    source_latency_ms=total_elapsed_ms,
                    result=greek_results[1],
                ),
            ),
        )
    
    async def collect_snapshot(
        self, 
        timestamp_ist: datetime = None, 
        trading_date: str = None
    ) -> MarketSnapshot:
        """Collect a complete market snapshot.
        
        This is the main entry point for the 5-second collector.
        Fetches all required data and returns a validated snapshot.
        """
        start_time = time.perf_counter()
        if timestamp_ist is None:
            timestamp_ist = datetime.now(IST)
        if trading_date is None:
            trading_date = timestamp_ist.strftime("%Y-%m-%d")
        
        try:
            # Parallel fetch: spot, VIX, option chain
            spot_task = asyncio.create_task(self.get_spot_quote())
            vix_task = asyncio.create_task(self.get_vix_quote())
            expiry = await self.get_current_expiry()
            chain_task = asyncio.create_task(self.get_option_chain(expiry, strike_count=10))
            
            spot_resp, vix_resp, chain_resp = await asyncio.gather(
                spot_task, vix_task, chain_task
            )
            
            source_latency_ms = int((time.perf_counter() - start_time) * 1000)
            
            # Extract data
            spot_data = self._extract_quote_data(spot_resp)
            vix_data = self._extract_quote_data(vix_resp)
            
            spot_ltp = spot_data.get("ltp")
            if spot_ltp is None:
                raise ValueError("Failed to get spot LTP")
            
            # Find ATM and extract ATM option data
            atm_strike = self._find_atm_strike(spot_ltp)
            atm_data = self._extract_chain_atm_data(chain_resp, atm_strike)
            
            # Fetch Greeks for ATM options (parallel)
            ce_symbol, pe_symbol = self._get_atm_greeks_symbols(atm_strike, expiry)
            
            ce_greeks_task = asyncio.create_task(
                self.get_option_greeks(ce_symbol, self.config.options_exchange, spot_ltp)
            )
            pe_greeks_task = asyncio.create_task(
                self.get_option_greeks(pe_symbol, self.config.options_exchange, spot_ltp)
            )
            
            ce_greeks_resp, pe_greeks_resp = await asyncio.gather(
                ce_greeks_task, pe_greeks_task, return_exceptions=True
            )
            
            ce_greeks = {}
            pe_greeks = {}
            
            if not isinstance(ce_greeks_resp, Exception):
                ce_greeks = self._extract_greeks(ce_greeks_resp)
            if not isinstance(pe_greeks_resp, Exception):
                pe_greeks = self._extract_greeks(pe_greeks_resp)
            
            # Build snapshot
            snapshot = MarketSnapshot(
                version=SnapshotVersion.V1,
                timestamp_ist=timestamp_ist,
                trading_date=trading_date,
                symbol=self.config.underlying,
                
                # Spot
                spot_ltp=spot_data.get("ltp"),
                spot_bid=spot_data.get("bid"),
                spot_ask=spot_data.get("ask"),
                spot_prev_close=spot_data.get("prev_close"),
                spot_open=spot_data.get("open"),
                spot_high=spot_data.get("high"),
                spot_low=spot_data.get("low"),
                spot_volume=spot_data.get("volume"),
                
                # VIX
                vix=vix_data.get("ltp"),
                vix_prev_close=vix_data.get("prev_close"),
                
                # Option chain reference
                atm_strike=atm_strike,
                expiry_date=expiry,
                days_to_expiry=self._calculate_dte(expiry),
                
                # ATM CE
                atm_ce_ltp=atm_data["ce"].get("ltp"),
                atm_ce_bid=atm_data["ce"].get("bid"),
                atm_ce_ask=atm_data["ce"].get("ask"),
                atm_ce_bid_qty=atm_data["ce"].get("bid_qty"),
                atm_ce_ask_qty=atm_data["ce"].get("ask_qty"),
                atm_ce_volume=atm_data["ce"].get("volume"),
                atm_ce_oi=atm_data["ce"].get("oi"),
                atm_ce_iv=atm_data["ce"].get("iv") or ce_greeks.get("iv"),
                atm_ce_delta=ce_greeks.get("delta"),
                atm_ce_gamma=ce_greeks.get("gamma"),
                atm_ce_theta=ce_greeks.get("theta"),
                atm_ce_vega=ce_greeks.get("vega"),
                
                # ATM PE
                atm_pe_ltp=atm_data["pe"].get("ltp"),
                atm_pe_bid=atm_data["pe"].get("bid"),
                atm_pe_ask=atm_data["pe"].get("ask"),
                atm_pe_bid_qty=atm_data["pe"].get("bid_qty"),
                atm_pe_ask_qty=atm_data["pe"].get("ask_qty"),
                atm_pe_volume=atm_data["pe"].get("volume"),
                atm_pe_oi=atm_data["pe"].get("oi"),
                atm_pe_iv=atm_data["pe"].get("iv") or pe_greeks.get("iv"),
                atm_pe_delta=pe_greeks.get("delta"),
                atm_pe_gamma=pe_greeks.get("gamma"),
                atm_pe_theta=pe_greeks.get("theta"),
                atm_pe_vega=pe_greeks.get("vega"),
                
                # Quality
                data_status=DataStatus.VALID,  # Will be updated by validator
                source_latency_ms=source_latency_ms,
                ingestion_time=datetime.utcnow(),
                provider_payload=json.dumps({
                    "spot": spot_resp,
                    "vix": vix_resp,
                    "chain": chain_resp,
                    "ce_greeks": ce_greeks_resp if not isinstance(ce_greeks_resp, Exception) else str(ce_greeks_resp),
                    "pe_greeks": pe_greeks_resp if not isinstance(pe_greeks_resp, Exception) else str(pe_greeks_resp),
                }),
            )
            
            # Validate
            return self._validator.validate_and_enrich(snapshot)
            
        except Exception as e:
            # Return a REJECTED snapshot with error info
            return MarketSnapshot(
                version=SnapshotVersion.V1,
                timestamp_ist=timestamp_ist,
                trading_date=trading_date,
                symbol=self.config.underlying,
                data_status=DataStatus.REJECTED,
                source_latency_ms=int((time.perf_counter() - start_time) * 1000),
                ingestion_time=datetime.utcnow(),
                provider_payload=json.dumps({"error": str(e)}),
            )
    
    def _calculate_dte(self, expiry_date: str) -> int:
        """Calculate days to expiry from DDMMMYY format."""
        try:
            # Parse DDMMMYY (e.g., 28NOV24)
            expiry = datetime.strptime(expiry_date, "%d%b%y")
            expiry = expiry.replace(tzinfo=IST)
            now = datetime.now(IST)
            dte = (expiry - now).days
            return max(0, dte)
        except Exception:
            return 0
    
    async def health_check(self) -> dict:
        """Health check endpoint for monitoring."""
        try:
            # Quick spot quote check
            start = time.perf_counter()
            await self.get_spot_quote()
            latency_ms = int((time.perf_counter() - start) * 1000)
            
            return {
                "status": "healthy",
                "latency_ms": latency_ms,
                "timestamp": datetime.utcnow().isoformat(),
                "adapter": "openalgo",
            }
        except Exception as e:
            return {
                "status": "unhealthy",
                "error": str(e),
                "timestamp": datetime.utcnow().isoformat(),
                "adapter": "openalgo",
            }
    
    async def close(self):
        """Close the HTTP client."""
        if self._client:
            await self._client.aclose()
            self._client = None


# Convenience function for standalone use
async def collect_single_snapshot() -> MarketSnapshot:
    """Collect a single snapshot (for testing)."""
    async with OpenAlgoAdapter() as adapter:
        return await adapter.collect_snapshot()

"""Strict adapter boundary used by the recovery collector; no trading APIs."""

import asyncio
from datetime import datetime, timezone

from hermes_v0.collector.adapters.openalgo_adapter import OpenAlgoAdapter
from hermes_v0.collector.integrity import Observation, IntegrityError, IST, instant, number


class AuthenticationRequired(IntegrityError):
    pass


class StrictOpenAlgo(OpenAlgoAdapter):
    ALLOWED = {"/api/v1/quotes", "/api/v1/expiry", "/api/v1/optionchain",
               "/api/v1/optiongreeks", "/api/v1/history", "/api/v1/market/timings"}

    def __init__(self, config=None):
        super().__init__(config)
        self.config.max_retries = 1
        self.expiry = None
        self.prepared_date = None
        self.session_end = None

    async def request_data(self, endpoint, **values):
        if endpoint not in self.ALLOWED:
            raise IntegrityError("ForbiddenEndpoint")
        response = await self._request_with_retry("POST", endpoint, dict(apikey=self.config.api_key, **values))
        if not isinstance(response, dict) or response.get("status") != "success":
            if isinstance(response, dict):
                message = str(response.get("message", "")).lower()
                if response.get("code") in (401, 403) or any(s in message for s in
                        ("invalid api key", "invalid apikey", "session expired", "token expired", "not authenticated")) or (
                        ("api key" in message or "token" in message) and ("invalid" in message or "expired" in message)):
                    raise AuthenticationRequired("LoginRequired")
            # Do not echo provider messages (they may contain credentials).
            raise IntegrityError("ProviderRejectedRequest")
        return response

    async def prepare(self):
        now = datetime.now(IST)
        if self.prepared_date == now.date():
            return
        timings = await self.request_data("/api/v1/market/timings", date=now.date().isoformat())
        windows = timings.get("data")
        if not isinstance(windows, list):
            raise IntegrityError("CalendarUnavailable")
        # Verify both spot and derivatives using OpenAlgo's local exchange calendar.
        ends = []
        for exchange in ("NSE", "NFO"):
            match = [w for w in windows if isinstance(w, dict) and w.get("exchange") == exchange]
            if len(match) != 1:
                raise IntegrityError("MarketClosed")
            start, end = [number(match[0].get(k), positive=True) / 1000 for k in ("start_time", "end_time")]
            if not start <= now.timestamp() < end:
                raise IntegrityError("MarketClosed")
            ends.append(end)
        response = await self.request_data("/api/v1/expiry", symbol=self.config.underlying,
                                           exchange="NFO", instrumenttype="options")
        dates = response.get("data")
        if not isinstance(dates, list) or not dates:
            raise IntegrityError("MissingExpiry")
        parsed = []
        for value in dates:
            if not isinstance(value, str):
                raise IntegrityError("InvalidExpiry")
            for fmt in ("%d-%b-%y", "%d%b%y"):
                try:
                    dt = datetime.strptime(value.upper(), fmt)
                    break
                except ValueError:
                    dt = None
            if dt is None:
                raise IntegrityError("InvalidExpiry")
            if dt.date() >= now.date():
                parsed.append(dt)
        if not parsed:
            raise IntegrityError("ExpiredContract")
        self.expiry = min(parsed).strftime("%d%b%y").upper()
        self.prepared_date = now.date()
        self.session_end = min(ends)

    async def raw(self, scheduled):
        started = datetime.now(timezone.utc)
        if self.expiry is None or self.prepared_date != started.astimezone(IST).date():
            raise IntegrityError("ExpiryNotPrepared")
        # TaskGroup cancels a sibling when a request fails; no orphaned requests.
        async with asyncio.TaskGroup() as group:
            spot_task = group.create_task(self.request_data("/api/v1/quotes", symbol="NIFTY", exchange="NSE_INDEX"))
            chain_task = group.create_task(self.request_data("/api/v1/optionchain", underlying="NIFTY",
                exchange="NSE_INDEX", expiry_date=self.expiry, strike_count=1))
        received = datetime.now(timezone.utc)
        if self.session_end is not None and received.timestamp() >= self.session_end:
            raise IntegrityError("MarketClosed")
        spot = spot_task.result().get("data")
        chain = chain_task.result()
        if chain.get("underlying", "NIFTY") != "NIFTY" or chain.get("expiry_date", self.expiry) != self.expiry:
            raise IntegrityError("ChainIdentityMismatch")
        if not isinstance(spot, dict) or spot.get("symbol", "NIFTY") != "NIFTY":
            raise IntegrityError("UnderlyingMismatch")
        strike, ce, pe = self._extract_atm_contracts(chain)
        matches = [row for row in chain["chain"] if isinstance(row, dict)
                   and self._optional_number(row.get("strike")) == strike]
        if len(matches) != 1:
            raise IntegrityError("AmbiguousATMContract")
        if chain.get("stale") is True or chain.get("data_status") in ("STALE", "REJECTED"):
            raise IntegrityError("StaleQuote")
        issues = ["PROVIDER_TIMESTAMP_UNAVAILABLE"]
        options = []
        for side, contract in (("CE", ce), ("PE", pe)):
            counts = {}
            for field in ("oi", "volume"):
                value = contract.get(field)
                if field == "oi" and contract.get("open_interest") is not None:
                    alternate = number(contract["open_interest"], integer=True)
                    if value is not None and number(value, integer=True) != alternate:
                        raise IntegrityError("ConflictingOIAliases")
                    value = alternate
                value = number(value, integer=True, optional=True)
                if value == 0:
                    value = None
                    issues.append("UNVERIFIED_ZERO_" + field.upper())
                counts[field] = value
            quotes = {}
            for field in ("bid", "ask"):
                value = number(contract.get(field), optional=True)
                if value is not None and value < 0:
                    raise IntegrityError("NegativeQuote")
                quotes[field] = value if value else None
            # Explicit stale markers are rejected even when a source clock is absent.
            if contract.get("stale") is True or contract.get("data_status") in ("STALE", "REJECTED"):
                raise IntegrityError("StaleQuote")
            options.append(dict(symbol=contract["symbol"], side=side,
                ltp=number(contract.get("ltp"), positive=True), **quotes, **counts))
        if spot.get("stale") is True or spot.get("data_status") in ("STALE", "REJECTED"):
            raise IntegrityError("StaleQuote")
        return Observation.make("RAW", scheduled, started, received, dict(symbol="NIFTY",
            spot_ltp=number(spot.get("ltp"), positive=True), expiry=self.expiry, strike=strike,
            options=options, freshness="UNVERIFIED_PROVIDER_TIME", issues=sorted(set(issues))))

    async def derived(self, raw):
        d, body = raw.data, raw.data["body"]

        async def leg(option):
            started = datetime.now(timezone.utc)
            result = await self.request_data("/api/v1/optiongreeks", symbol=option["symbol"], exchange="NFO")
            data = result.get("data", result)
            if not isinstance(data, dict) or data.get("symbol") != option["symbol"] or data.get("exchange") != "NFO" or data.get("underlying") != "NIFTY" or data.get("option_type") != option["side"] or number(data.get("strike"), positive=True) != body["strike"]:
                raise IntegrityError("CalculationContractMismatch")
            expiry = datetime.strptime(data.get("expiry_date", ""), "%d-%b-%Y").strftime("%d%b%y").upper()
            if expiry != body["expiry"] or data.get("stale") is True or data.get("data_status") in ("STALE", "REJECTED"):
                raise IntegrityError("CalculationContractMismatch")
            values = self._extract_greeks(result)
            return Observation.make("DERIVED", d["scheduled_at"], started, datetime.now(timezone.utc),
                dict(symbol=option["symbol"], raw_key=raw.key, raw_digest=raw.digest,
                     raw_received_at=d["received_at"], expiry=body["expiry"], strike=body["strike"], side=option["side"],
                     values=dict(iv=values["iv"], delta=values["delta"], gamma=values["gamma"],
                         theta=values["theta"], vega=values["vega"], rho=values["rho"],
                         option_price=values["calculation_option_ltp"],
                         forward_price=values["calculation_forward_price"],
                         spot_price=values["calculation_spot_ltp"],
                         interest_rate=values["interest_rate"])))
        # Failures remain missing; a successful independent leg can still be retained.
        return await asyncio.gather(*(leg(option) for option in body["options"]), return_exceptions=True)

    async def history(self, symbol, exchange, day):
        started = datetime.now(timezone.utc)
        response = await self.request_data("/api/v1/history", symbol=symbol, exchange=exchange,
            interval="5s", start_date=day.isoformat(), end_date=day.isoformat())
        data = response.get("data")
        if not isinstance(data, list) or not data:
            raise IntegrityError("NoHistoricalData")
        candles = []
        for row in data:
            if not isinstance(row, dict):
                raise IntegrityError("InvalidCandle")
            stamp = number(row.get("timestamp"), positive=True, integer=True)
            oi = number(row.get("oi"), integer=True, optional=True)
            candles.append(dict(timestamp=datetime.fromtimestamp(stamp, timezone.utc).isoformat(),
                **{k: number(row.get(k), positive=True) for k in ("open", "high", "low", "close")},
                volume=number(row.get("volume"), integer=True, optional=True), oi=oi if oi else None))
        return Observation.make("HISTORY", datetime.combine(day, datetime.min.time(), IST),
            started, datetime.now(timezone.utc), dict(symbol=symbol, exchange=exchange, interval="5s",
            candles=candles, provenance="HISTORICAL_BACKFILL"))

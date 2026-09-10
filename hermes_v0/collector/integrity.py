"""Fail-closed observation contract. Missing evidence is preferable to invented data."""

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from datetime import datetime, timezone
from hashlib import sha256
import json
import math
import re
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")


class IntegrityError(ValueError):
    """A response/replay is not safe to publish as an observation."""


def instant(value):
    result = value if isinstance(value, datetime) else datetime.fromisoformat(value)
    if result.tzinfo is None or result.utcoffset() is None:
        raise IntegrityError("AmbiguousTimestamp")
    return result.astimezone(timezone.utc)


def session_time(value):
    local = instant(value).astimezone(IST)
    return local.weekday() < 5 and (9, 15) <= (local.hour, local.minute) < (15, 30)


def number(value, *, positive=False, integer=False, optional=False):
    if value is None and optional:
        return None
    if isinstance(value, bool) or not isinstance(value, (str, float, int)):
        raise IntegrityError("InvalidNumber")
    try:
        result = float(value)
    except (ValueError, OverflowError):
        raise IntegrityError("InvalidNumber") from None
    if not math.isfinite(result) or (positive and result <= 0):
        raise IntegrityError("InvalidNumber")
    if integer and (result < 0 or result > 2**53 - 1 or not result.is_integer()):
        raise IntegrityError("InvalidCount")
    if integer:
        try:
            if Decimal(str(value)) != Decimal(int(result)):
                raise IntegrityError("LossyCount")
        except InvalidOperation:
            raise IntegrityError("InvalidCount") from None
    return int(result) if integer else result


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def option_identity(symbol, underlying, expiry, strike, side):
    if not isinstance(symbol, str) or symbol != f"{underlying}{expiry}{int(strike)}{side}":
        raise IntegrityError("ContractMismatch")


@dataclass(frozen=True)
class Observation:
    """Immutable normalized JSON, never a provider response dict or a credential."""

    payload: str

    @property
    def data(self):
        return json.loads(self.payload)

    @property
    def digest(self):
        return sha256(self.payload.encode()).hexdigest()

    @property
    def key(self):
        d = self.data
        # Same slot/contract must never acquire a second, different value on replay.
        return sha256(canonical([d["kind"], d["scheduled_at"], d["body"]["symbol"]]).encode()).hexdigest()

    @classmethod
    def make(cls, kind, scheduled, started, received, body):
        value = cls(canonical(dict(version=2, kind=kind,
            scheduled_at=instant(scheduled).isoformat(), started_at=instant(started).isoformat(),
            received_at=instant(received).isoformat(), body=body)))
        value.validate()
        return value

    def validate(self):
        d = self.data
        if set(d) != {"version", "kind", "scheduled_at", "started_at", "received_at", "body"} or d["version"] != 2:
            raise IntegrityError("UnknownContract")
        scheduled, started, received = (instant(d[k]) for k in ("scheduled_at", "started_at", "received_at"))
        if not scheduled <= started <= received:
            raise IntegrityError("InvertedTimestamps")
        b = d["body"]
        if d["kind"] == "RAW":
            if set(b) != {"symbol", "spot_ltp", "expiry", "strike", "options", "freshness", "issues"}:
                raise IntegrityError("UnexpectedRawField")
            if b["symbol"] != "NIFTY" or b["freshness"] != "UNVERIFIED_PROVIDER_TIME":
                raise IntegrityError("UnsubstantiatedProvenance")
            if not session_time(scheduled) or not session_time(received) or (received-scheduled).total_seconds() >= 5:
                raise IntegrityError("LateOrOffSessionResponse")
            number(b["spot_ltp"], positive=True)
            strike = number(b["strike"], positive=True, integer=True)
            expiry = datetime.strptime(b["expiry"], "%d%b%y").date()
            if expiry < received.astimezone(IST).date():
                raise IntegrityError("ExpiredContract")
            if len(b["options"]) != 2 or [o["side"] for o in b["options"]] != ["CE", "PE"]:
                raise IntegrityError("InvalidPair")
            for o in b["options"]:
                if set(o) != {"symbol", "side", "ltp", "oi", "volume", "bid", "ask"}:
                    raise IntegrityError("UnexpectedOptionField")
                option_identity(o["symbol"], b["symbol"], b["expiry"], strike, o["side"])
                number(o["ltp"], positive=True)
                for field in ("oi", "volume"):
                    # Zero could have been synthesized by OpenAlgo; absent proof, leave NULL.
                    if o[field] == 0:
                        raise IntegrityError("AmbiguousZero")
                    number(o[field], positive=True, integer=True, optional=True)
                for field in ("bid", "ask"):
                    number(o[field], positive=True, optional=True)
                if o["bid"] is not None and o["ask"] is not None and o["bid"] > o["ask"]:
                    raise IntegrityError("CrossedQuote")
            if any(not re.fullmatch(r"[A-Z_]+", issue) for issue in b["issues"]):
                raise IntegrityError("UnsafeIssue")
        elif d["kind"] == "DERIVED":
            if set(b) != {"symbol", "raw_key", "raw_digest", "raw_received_at", "expiry", "strike", "side", "values"}:
                raise IntegrityError("UnexpectedDerivedField")
            option_identity(b["symbol"], "NIFTY", b["expiry"], b["strike"], b["side"])
            if any(not isinstance(b[k], str) or not re.fullmatch(r"[0-9a-f]{64}", b[k]) for k in ("raw_key", "raw_digest")):
                raise IntegrityError("InvalidParentIdentity")
            if not session_time(received) or not instant(b["raw_received_at"]) <= started or (received-started).total_seconds() > 3:
                raise IntegrityError("LateCalculation")
            if set(b["values"]) != {"iv", "delta", "gamma", "theta", "vega", "rho", "option_price", "forward_price", "spot_price", "interest_rate"}:
                raise IntegrityError("MissingCalculationInputs")
            v = b["values"]
            for name, value in v.items():
                number(value, optional=name in ("forward_price", "spot_price"))
            for name in ("forward_price", "spot_price"):
                number(v[name], positive=True, optional=True)
            if v["forward_price"] is None and v["spot_price"] is None:
                raise IntegrityError("MissingCalculationPrice")
            if v["iv"] <= 0 or v["option_price"] <= 0 or v["gamma"] < 0 or v["vega"] < 0:
                raise IntegrityError("InvalidCalculation")
            if not (-1 <= v["delta"] <= 1) or (b["side"] == "CE" and v["delta"] < 0) or (b["side"] == "PE" and v["delta"] > 0):
                raise IntegrityError("InvalidDelta")
        elif d["kind"] == "HISTORY":
            if set(b) != {"symbol", "exchange", "interval", "candles", "provenance"} or b["provenance"] != "HISTORICAL_BACKFILL":
                raise IntegrityError("UnexpectedHistoryField")
            if not re.fullmatch(r"NIFTY(?:\d{2}[A-Z]{3}\d{2}\d+(?:CE|PE))?", b["symbol"]) or b["interval"] != "5s":
                raise IntegrityError("HistoryContract")
            if b["exchange"] != ("NSE_INDEX" if b["symbol"] == "NIFTY" else "NFO"):
                raise IntegrityError("HistoryExchangeMismatch")
            seen = set()
            for c in b["candles"]:
                if set(c) != {"timestamp", "open", "high", "low", "close", "volume", "oi"}:
                    raise IntegrityError("HistoryFields")
                ts = instant(c["timestamp"])
                if ts in seen or not session_time(ts) or ts.second % 5 or ts.microsecond or ts.astimezone(IST).date() != scheduled.astimezone(IST).date() or (received-ts).total_seconds() < 5:
                    raise IntegrityError("HistoryTimestamp")
                seen.add(ts)
                for name in ("open", "high", "low", "close"):
                    number(c[name], positive=True)
                if not c["low"] <= min(c["open"], c["close"]) <= max(c["open"], c["close"]) <= c["high"]:
                    raise IntegrityError("InvalidOHLC")
                number(c["volume"], integer=True, optional=True)
                if c["oi"] == 0:
                    raise IntegrityError("AmbiguousZero")
                number(c["oi"], integer=True, optional=True)
        else:
            raise IntegrityError("UnknownKind")

"""Fail-closed, network-free decisions from a validated NSE F&O calendar cache."""
from __future__ import annotations

import argparse
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
from typing import Any
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")
SCHEMA_VERSION = 1
DEFAULT_CACHE_DIR = Path(__file__).resolve().parent / "runtime"
MAX_CACHE_BYTES = 256_000
MAX_SOURCE_BYTES = 5_000_000
MAX_FRESHNESS_HOURS = 48
ALLOWED_HOSTS = frozenset({"www.nseindia.com", "nsearchives.nseindia.com"})


class CalendarError(ValueError):
    """Missing, ambiguous, corrupt, stale, or unsupported calendar evidence."""


def cache_directory(cache_dir: Path | None = None) -> Path:
    return Path(cache_dir or os.environ.get("MARKET_CALENDAR_CACHE_DIR") or DEFAULT_CACHE_DIR).expanduser().resolve()


def official_url(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    try:
        parsed = urlparse(value)
        return (parsed.scheme == "https" and parsed.hostname in ALLOWED_HOSTS
                and parsed.port in (None, 443) and not parsed.username and not parsed.password)
    except ValueError:
        return False


def utc_timestamp(value: str) -> datetime:
    if not isinstance(value, str):
        raise CalendarError("invalid timestamp")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise CalendarError("calendar timestamps require a timezone")
    return parsed.astimezone(timezone.utc)


def digest_payload(payload: dict) -> str:
    unsigned = {k: v for k, v in payload.items() if k != "content_sha256"}
    return hashlib.sha256(json.dumps(unsigned, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()).hexdigest()


def validate_calendar(payload: Any, year: int, *, cache_dir: Path | None = None) -> dict:
    """Validate structure and evidence before any weekday can be opened."""
    if not isinstance(payload, dict) or payload.get("schema_version") != SCHEMA_VERSION:
        raise CalendarError("unsupported calendar schema")
    if payload.get("calendar_year") != year or payload.get("segment") != "FO" or payload.get("exchange") != "NSE":
        raise CalendarError("calendar year or exchange segment mismatch")
    if payload.get("coverage") != {"start": f"{year}-01-01", "end": f"{year}-12-31", "annual_validated": True}:
        raise CalendarError("complete annual calendar coverage is required")
    if payload.get("content_sha256") != digest_payload(payload):
        raise CalendarError("calendar checksum mismatch")
    fetched, expires = utc_timestamp(payload.get("fetched_at")), utc_timestamp(payload.get("expires_at"))
    if not timedelta(0) < expires - fetched <= timedelta(hours=MAX_FRESHNESS_HOURS):
        raise CalendarError("invalid calendar freshness window")
    if not official_url(payload.get("source_url")) or not re.fullmatch(r"NSE/FAOP/\d+", payload.get("circular_reference", "")):
        raise CalendarError("official F&O circular provenance is required")
    holidays = payload.get("holidays")
    baseline = payload.get("annual_holiday_dates")
    if not isinstance(holidays, list) or not 10 <= len(holidays) <= 80 or not isinstance(baseline, list):
        raise CalendarError("incomplete annual holiday list")
    dates: list[str] = []
    for item in holidays:
        if not isinstance(item, dict):
            raise CalendarError("invalid holiday entry")
        day = date.fromisoformat(item["date"])
        if day.year != year or not isinstance(item.get("name"), str) or not item["name"].strip():
            raise CalendarError("invalid holiday date or description")
        if not official_url(item.get("source_url")):
            raise CalendarError("holiday has no official evidence")
        dates.append(day.isoformat())
    if dates != sorted(set(dates)) or len(baseline) < 10 or not set(baseline).issubset(dates):
        raise CalendarError("holiday dates incomplete or duplicated")
    if {(date.fromisoformat(x).month - 1) // 3 for x in baseline} != {0, 1, 2, 3}:
        raise CalendarError("annual holiday coverage incomplete")
    sources = payload.get("sources")
    if not isinstance(sources, list) or {x.get("kind") for x in sources if isinstance(x, dict)} != {"annual_circular", "holiday_api"}:
        raise CalendarError("annual circular and current API evidence required")
    for source in sources:
        if not isinstance(source, dict) or not official_url(source.get("url")) or not re.fullmatch(r"[a-f0-9]{64}", source.get("sha256", "")):
            raise CalendarError("invalid source provenance")
        relative = Path(source.get("archive_path", ""))
        if relative.is_absolute() or ".." in relative.parts or relative.parts[:1] != ("sources",):
            raise CalendarError("invalid evidence archive path")
        if cache_dir is not None:
            path = (cache_dir / relative).resolve()
            if not path.is_relative_to(cache_dir.resolve()) or path.stat().st_size > MAX_SOURCE_BYTES:
                raise CalendarError("invalid evidence archive")
            if hashlib.sha256(path.read_bytes()).hexdigest() != source["sha256"]:
                raise CalendarError("source archive checksum mismatch")
    return payload


def read_calendar(year: int, *, cache_dir: Path | None = None) -> dict:
    folder = cache_directory(cache_dir)
    path = folder / f"calendar-{year}.json"
    try:
        if path.stat().st_size > MAX_CACHE_BYTES:
            raise CalendarError("calendar cache exceeds size limit")
        return validate_calendar(json.loads(path.read_text()), year, cache_dir=folder)
    except (OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
        raise CalendarError(f"Calendar unavailable or invalid: {type(exc).__name__}: {exc}") from exc


def _current(now: datetime | None) -> datetime:
    value = now or datetime.now(timezone.utc)
    if value.tzinfo is None:
        raise CalendarError("now must include a timezone")
    return value.astimezone(timezone.utc)


def _fresh(payload: dict, now: datetime) -> bool:
    return utc_timestamp(payload["fetched_at"]) - timedelta(minutes=5) <= now <= utc_timestamp(payload["expires_at"])


def _next_open(day: date, now: datetime, cache_dir: Path | None) -> str | None:
    calendars: dict[int, dict] = {}
    for offset in range(1, 370):
        candidate = day + timedelta(days=offset)
        if candidate.weekday() >= 5:
            continue
        if candidate.year not in calendars:
            try:
                calendars[candidate.year] = read_calendar(candidate.year, cache_dir=cache_dir)
            except CalendarError:
                return None
        payload = calendars[candidate.year]
        if not _fresh(payload, now):
            return None
        if candidate.isoformat() not in {x["date"] for x in payload["holidays"]}:
            return candidate.isoformat()
    return None


def decide_session(day: date | None = None, *, cache_dir: Path | None = None, now: datetime | None = None) -> dict:
    """No network or side effects; OPEN alone authorizes a normal weekday job.

    Date selection is IST. Weekend/special sessions never start automatically.
    A known closure stays CLOSED even when stale; other stale decisions are UNKNOWN.
    next_open_date is a forecast from currently fresh evidence, not future permission.
    """
    current = _current(now)
    requested = day or current.astimezone(IST).date()
    if isinstance(requested, datetime) or not isinstance(requested, date):
        raise CalendarError("day must be a date")
    decision = {"allowed": False, "status": "UNKNOWN", "reason": "Calendar unavailable",
                "date": requested.isoformat(), "calendar_year": requested.year,
                "source_url": None, "circular_reference": None, "fetched_at": None,
                "expires_at": None, "next_open_date": None}
    try:
        payload = read_calendar(requested.year, cache_dir=cache_dir)
        for key in ("source_url", "circular_reference", "fetched_at", "expires_at"):
            decision[key] = payload[key]
        decision["next_open_date"] = _next_open(requested, current, cache_dir)
    except CalendarError as exc:
        if requested.weekday() < 5:
            decision["reason"] = str(exc)
            return decision
        payload = None
    if requested.weekday() >= 5:
        decision.update(status="CLOSED", reason="Weekend; exceptional sessions require an explicit separate schedule")
        return decision
    holiday = next((item for item in payload["holidays"] if item["date"] == requested.isoformat()), None)
    if holiday:
        decision.update(status="CLOSED", reason=f"NSE F&O trading holiday: {holiday['name']}")
    elif not _fresh(payload, current):
        decision.update(reason="Calendar stale or fetched in the future; refresh official NSE evidence")
    else:
        decision.update(allowed=True, status="OPEN", reason="Regular NSE F&O weekday; no trading holiday in fresh official calendar")
    return decision


def calendar_status(*, cache_dir: Path | None = None, now: datetime | None = None) -> dict:
    """Safe, cached dashboard status. Does not refresh or contact an endpoint."""
    current = _current(now)
    folder = cache_directory(cache_dir)
    years = []
    for path in sorted(folder.glob("calendar-????.json")):
        try:
            year = int(path.stem.rsplit("-", 1)[1])
            payload = read_calendar(year, cache_dir=folder)
            years.append({key: payload[key] for key in ("calendar_year", "holidays", "source_url", "circular_reference", "fetched_at", "expires_at", "sources")} | {"fresh": _fresh(payload, current)})
        except (ValueError, CalendarError):
            continue
    refresh_status = None
    try:
        status_file = folder / "refresh_status.json"
        if status_file.stat().st_size <= MAX_CACHE_BYTES:
            raw = json.loads(status_file.read_text())
            if isinstance(raw, dict):
                refresh_status = {key: raw[key] for key in ("status", "attempted_at", "updated_years", "message", "next_year_status") if key in raw}
    except (OSError, ValueError):
        pass
    return {"decision": decide_session(cache_dir=folder, now=current), "cached_years": years, "refresh_status": refresh_status}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", type=date.fromisoformat)
    parser.add_argument("--cache-dir", type=Path)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--status", action="store_true", help="Display decision with exit 0 even when blocked")
    args = parser.parse_args(argv)
    result = decide_session(args.date, cache_dir=args.cache_dir)
    print(json.dumps(result, sort_keys=True) if args.json else f"{result['date']} {result['status']}: {result['reason']}")
    return 0 if args.status or result["allowed"] else 10 if result["status"] == "CLOSED" else 11


if __name__ == "__main__":
    raise SystemExit(main())

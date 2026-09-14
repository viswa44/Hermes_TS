"""Refresh verified official NSE F&O calendars; never replace good evidence on error."""
from __future__ import annotations

import argparse
from datetime import date, datetime, timedelta, timezone
import hashlib
from http.cookiejar import CookieJar
from io import BytesIO
import json
import os
from pathlib import Path
import re
import ssl
import tempfile
import time
from urllib.error import URLError
from urllib.parse import urlencode
from urllib.request import HTTPCookieProcessor, HTTPSHandler, HTTPRedirectHandler, Request, build_opener

from .gateway import (CalendarError, IST, MAX_FRESHNESS_HOURS, MAX_SOURCE_BYTES,
                      SCHEMA_VERSION, cache_directory, digest_payload, official_url,
                      validate_calendar)

API_URL = "https://www.nseindia.com/api/holiday-master?type=trading"
HOLIDAYS_URL = "https://www.nseindia.com/resources/exchange-communication-holidays"
CIRCULARS_URL = "https://www.nseindia.com/api/circulars"
KNOWN_CIRCULARS = {2026: "https://nsearchives.nseindia.com/content/circulars/FAOP71777.pdf"}
MONTHS = "January February March April May June July August September October November December".split()
DAYS = "Monday Tuesday Wednesday Thursday Friday Saturday Sunday".split()
ROW = re.compile(r"^\s*\d+\s+(" + "|".join(MONTHS) + r")\s+(\d{1,2}),\s*(\d{4})\s+(" + "|".join(DAYS) + r")\s+(.+?)\s*$", re.MULTILINE)


class OfficialRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not official_url(newurl):
            raise CalendarError("NSE redirected outside approved official HTTPS hosts")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class NSEClient:
    """Bounded public HTTPS requests; no credentials, insecure TLS, or third-party hosts."""
    def __init__(self, *, timeout: float = 15, attempts: int = 2):
        import certifi
        if not 1 <= timeout <= 30 or not 1 <= attempts <= 3:
            raise CalendarError("network retry settings exceed safety bounds")
        self.timeout, self.attempts = timeout, attempts
        context = ssl.create_default_context(cafile=certifi.where())
        self.opener = build_opener(OfficialRedirects(), HTTPCookieProcessor(CookieJar()), HTTPSHandler(context=context))

    def fetch(self, url: str) -> bytes:
        if not official_url(url):
            raise CalendarError("only approved official NSE HTTPS endpoints are allowed")
        for attempt in range(self.attempts):
            try:
                request = Request(url, headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json,application/pdf,text/html;q=0.8", "Referer": HOLIDAYS_URL})
                with self.opener.open(request, timeout=self.timeout) as response:
                    if not official_url(response.url):
                        raise CalendarError("response came from an unapproved host")
                    body = response.read(MAX_SOURCE_BYTES + 1)
                    if len(body) > MAX_SOURCE_BYTES or not body:
                        raise CalendarError("official response is empty or exceeds size limit")
                    return body
            except (URLError, TimeoutError, OSError):
                if attempt + 1 == self.attempts:
                    raise CalendarError("official NSE HTTPS request failed after bounded retries") from None
                time.sleep(1)
        raise CalendarError("official NSE request failed")


def _stamp(now: datetime) -> str:
    return now.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def _json_bytes(value: dict) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True) + "\n").encode()


def parse_circular(pdf_bytes: bytes, year: int, source_url: str) -> tuple[str, list[dict]]:
    from pypdf import PdfReader
    if not pdf_bytes.startswith(b"%PDF-"):
        raise CalendarError("official circular response is not a PDF")
    reader = PdfReader(BytesIO(pdf_bytes))
    if reader.is_encrypted or not 1 <= len(reader.pages) <= 6:
        raise CalendarError("annual circular has an unsupported PDF structure")
    text = "\n".join(page.extract_text() or "" for page in reader.pages)
    if len(text) > 100_000 or not re.search(r"Department:\s*FUTURES\s*&\s*OPTIONS", text, re.I):
        raise CalendarError("circular must be from NSE Futures & Options")
    if not re.search(rf"Trading\s+holidays\s+for\s+the\s+calendar\s+year\s+{year}\b", text, re.I):
        raise CalendarError("circular title does not establish the requested annual trading calendar")
    reference = re.search(r"NSE/FAOP/\d+", text)
    if not reference:
        raise CalendarError("annual circular reference missing")
    holidays = []
    for match in ROW.finditer(text):
        month, number, row_year, weekday, name = match.groups()
        day = date(int(row_year), MONTHS.index(month) + 1, int(number))
        if day.year != year or DAYS[day.weekday()] != weekday:
            raise CalendarError("circular holiday year/weekday mismatch")
        holidays.append({"date": day.isoformat(), "name": name.strip(), "source_url": source_url})
    dates = [x["date"] for x in holidays]
    if len(dates) != len(set(dates)) or not 10 <= len(dates) <= 60:
        raise CalendarError("annual PDF holiday table could not be fully validated")
    if {((date.fromisoformat(x).month - 1) // 3) for x in dates} != {0, 1, 2, 3}:
        raise CalendarError("annual PDF does not cover all four quarters")
    return reference.group(), sorted(holidays, key=lambda x: x["date"])


def parse_api(raw: bytes) -> dict[int, list[dict]]:
    try:
        data = json.loads(raw)
        rows = data.get("FO") if isinstance(data, dict) else None
        if not isinstance(rows, list) or not 10 <= len(rows) <= 160:
            raise CalendarError("official API has no complete FO holiday list")
        by_year: dict[int, list[dict]] = {}
        seen = set()
        for row in rows:
            if not isinstance(row, dict):
                raise CalendarError("invalid FO holiday row")
            day = datetime.strptime(row["tradingDate"], "%d-%b-%Y").date()
            if row.get("weekDay") != DAYS[day.weekday()] or not isinstance(row.get("description"), str) or not row["description"].strip():
                raise CalendarError("FO holiday date/weekday/description mismatch")
            if row.get("morning_session") is not None or row.get("evening_session") is not None:
                raise CalendarError("unexpected partial-session FO schedule needs review")
            if day in seen:
                raise CalendarError("duplicate FO holiday date")
            seen.add(day)
            by_year.setdefault(day.year, []).append({"date": day.isoformat(), "name": " ".join(row["description"].split()), "source_url": API_URL})
        for year, holidays in by_year.items():
            if not 10 <= len(holidays) <= 80 or {((date.fromisoformat(x["date"]).month - 1) // 3) for x in holidays} != {0, 1, 2, 3}:
                raise CalendarError(f"FO API annual coverage incomplete for {year}")
            holidays.sort(key=lambda x: x["date"])
        return by_year
    except (ValueError, TypeError, KeyError) as exc:
        if isinstance(exc, CalendarError):
            raise
        raise CalendarError("official API response is malformed") from exc


def _source(folder: Path, *, kind: str, url: str, content: bytes, now: datetime) -> dict:
    digest = hashlib.sha256(content).hexdigest()
    suffix = ".pdf" if kind == "annual_circular" else ".json"
    relative = Path("sources") / f"{kind}-{digest}{suffix}"
    destination = folder / relative
    if not destination.exists() or hashlib.sha256(destination.read_bytes()).hexdigest() != digest:
        atomic_write(destination, content)
    return {"kind": kind, "url": url, "sha256": digest, "archive_path": str(relative), "fetched_at": _stamp(now)}


def _discover_circular(client: NSEClient, year: int, folder: Path) -> str | None:
    # Previously discovered sources remain usable across the API's recent-list window.
    registry_file = folder / "discovered_circulars.json"
    registry = {}
    try:
        if registry_file.stat().st_size <= 20_000:
            registry = json.loads(registry_file.read_text())
            if not isinstance(registry, dict):
                registry = {}
    except (OSError, ValueError):
        pass
    known = KNOWN_CIRCULARS.get(year) or registry.get(str(year))
    if known and official_url(known) and re.fullmatch(r"https://nsearchives[.]nseindia[.]com/content/circulars/FAOP\d+[.]pdf", known):
        return known
    params = urlencode({"sub": f"Trading holidays for the calendar year {year}", "dept": "FAO"})
    raw = client.fetch(CIRCULARS_URL + "?" + params)
    try:
        rows = json.loads(raw).get("data")
    except (ValueError, AttributeError) as exc:
        raise CalendarError("NSE circular discovery returned invalid JSON") from exc
    if not isinstance(rows, list):
        raise CalendarError("NSE circular discovery returned no data list")
    matches = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        title, url = row.get("sub", ""), row.get("circFilelink", "")
        if (isinstance(title, str) and re.search(rf"Trading\s+holidays\s+for\s+the\s+calendar\s+year\s+{year}\b", title, re.I)
                and row.get("circDepartment") == "Futures & Options"
                and isinstance(url, str) and re.fullmatch(r"https://nsearchives[.]nseindia[.]com/content/circulars/FAOP\d+[.]pdf", url)):
            matches.append(url)
    if not matches:
        return None
    if len(set(matches)) != 1:
        raise CalendarError("multiple annual F&O circulars need review")
    registry[str(year)] = matches[0]
    atomic_write(registry_file, _json_bytes(registry))
    return matches[0]


def build_calendar(year: int, annual: list[dict], reference: str, api_holidays: list[dict], sources: list[dict], now: datetime, ttl_hours: int) -> dict:
    annual_dates = {x["date"] for x in annual}
    api_dates = {x["date"] for x in api_holidays}
    if not annual_dates.issubset(api_dates):
        raise CalendarError("current FO API omits annual circular holidays; keep old cache and require review")
    payload = {"schema_version": SCHEMA_VERSION, "exchange": "NSE", "segment": "FO", "calendar_year": year,
               "coverage": {"start": f"{year}-01-01", "end": f"{year}-12-31", "annual_validated": True},
               "source_url": sources[0]["url"], "circular_reference": reference,
               "fetched_at": _stamp(now), "expires_at": _stamp(now + timedelta(hours=ttl_hours)),
               "annual_holiday_dates": sorted(annual_dates), "holidays": api_holidays, "sources": sources,
               "policy": "Normal weekday NSE F&O sessions only; weekend/Muhurat sessions are never automatically authorized"}
    payload["content_sha256"] = digest_payload(payload)
    validate_calendar(payload, year)
    return payload


def refresh(*, cache_dir: Path | None = None, now: datetime | None = None, ttl_hours: int = 48, client: NSEClient | None = None) -> dict:
    """Refresh current calendar and next year if official circular and API are available."""
    if type(ttl_hours) is not int or not 1 <= ttl_hours <= MAX_FRESHNESS_HOURS:
        raise CalendarError("freshness must be between 1 and 48 hours")
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        raise CalendarError("now must include a timezone")
    year = current.astimezone(IST).year
    folder = cache_directory(cache_dir)
    result = {"status": "ERROR", "attempted_at": _stamp(current), "updated_years": [], "message": "Refresh did not complete", "next_year_status": "UNAVAILABLE"}
    transport = client or NSEClient()
    try:
        raw_api = transport.fetch(API_URL)
        by_year = parse_api(raw_api)
        if year not in by_year:
            raise CalendarError(f"official API has no complete FO calendar for current year {year}")
        api_source = _source(folder, kind="holiday_api", url=API_URL, content=raw_api, now=current)
        for target in (year, year + 1):
            try:
                url = _discover_circular(transport, target, folder)
                if url is None:
                    if target == year:
                        raise CalendarError(f"official annual FO circular for {target} is unavailable")
                    result["next_year_status"] = "ANNUAL_CIRCULAR_NOT_AVAILABLE"
                    continue
                raw_pdf = transport.fetch(url)
                reference, annual = parse_circular(raw_pdf, target, url)
                pdf_source = _source(folder, kind="annual_circular", url=url, content=raw_pdf, now=current)
                if target not in by_year:
                    result["next_year_status"] = "CIRCULAR_ARCHIVED_AWAITING_COMPLETE_API"
                    continue
                payload = build_calendar(target, annual, reference, by_year[target], [pdf_source, api_source], current, ttl_hours)
                validate_calendar(payload, target, cache_dir=folder)
                atomic_write(folder / f"calendar-{target}.json", _json_bytes(payload))
                result["updated_years"].append(target)
                if target == year + 1:
                    result["next_year_status"] = "UPDATED"
            except (CalendarError, OSError, ValueError) as exc:
                if target == year:
                    raise
                result["next_year_status"] = f"UNAVAILABLE: {type(exc).__name__}"
        result.update(status="OK", message="Official NSE F&O calendar refreshed; only OPEN cached decisions authorize jobs")
    except Exception as exc:
        # Do not include request headers, environment, credentials, or arbitrary provider text.
        result["message"] = str(exc) if isinstance(exc, CalendarError) else f"Refresh failed: {type(exc).__name__}"
    atomic_write(folder / "refresh_status.json", _json_bytes(result))
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache-dir", type=Path)
    parser.add_argument("--ttl-hours", type=int, default=48)
    args = parser.parse_args(argv)
    result = refresh(cache_dir=args.cache_dir, ttl_hours=args.ttl_hours)
    print(json.dumps(result, sort_keys=True))
    return 0 if result["status"] == "OK" else 1


if __name__ == "__main__":
    raise SystemExit(main())

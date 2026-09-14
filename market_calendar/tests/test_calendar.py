"""Gateway evidence boundaries; no network, credentials, providers, or AWS writes."""
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from market_calendar.gateway import (CalendarError, calendar_status, decide_session,
                                     digest_payload, main, read_calendar)
from market_calendar.refresh import (API_URL, KNOWN_CIRCULARS, NSEClient,
                                     _discover_circular, _source, atomic_write,
                                     build_calendar, parse_api, parse_circular, refresh)

NOW = datetime(2026, 9, 13, 4, 0, tzinfo=timezone.utc)
HOLIDAYS = [
    ("2026-01-26", "Republic Day"), ("2026-03-03", "Holi"),
    ("2026-03-26", "Shri Ram Navami"), ("2026-03-31", "Shri Mahavir Jayanti"),
    ("2026-04-03", "Good Friday"), ("2026-04-14", "Ambedkar Jayanti"),
    ("2026-05-01", "Maharashtra Day"), ("2026-05-28", "Bakri Id"),
    ("2026-06-26", "Muharram"), ("2026-09-14", "Ganesh Chaturthi"),
    ("2026-10-02", "Mahatma Gandhi Jayanti"), ("2026-10-20", "Dussehra"),
    ("2026-11-10", "Diwali-Balipratipada"), ("2026-11-24", "Guru Nanak Dev"),
    ("2026-12-25", "Christmas"),
]


def api_bytes(holidays=HOLIDAYS):
    return json.dumps({"FO": [{"tradingDate": date.fromisoformat(day).strftime("%d-%b-%Y"),
        "weekDay": date.fromisoformat(day).strftime("%A"), "description": name,
        "morning_session": None, "evening_session": None} for day, name in holidays]}).encode()


def annual_entries():
    return [{"date": day, "name": name, "source_url": KNOWN_CIRCULARS[2026]} for day, name in HOLIDAYS]


def write_payload(folder, payload):
    payload["content_sha256"] = digest_payload(payload)
    atomic_write(folder / "calendar-2026.json", json.dumps(payload).encode())


@pytest.fixture
def calendar(tmp_path):
    api = api_bytes()
    pdf = b"%PDF-fixture-for-provenance"
    sources = [_source(tmp_path, kind="annual_circular", url=KNOWN_CIRCULARS[2026], content=pdf, now=NOW),
               _source(tmp_path, kind="holiday_api", url=API_URL, content=api, now=NOW)]
    payload = build_calendar(2026, annual_entries(), "NSE/FAOP/71777", parse_api(api)[2026], sources, NOW, 48)
    write_payload(tmp_path, payload)
    return tmp_path, payload


def test_holiday_blocks_and_next_session(calendar):
    folder, _ = calendar
    result = decide_session(date(2026, 9, 14), cache_dir=folder, now=NOW)
    assert result["status"] == "CLOSED" and not result["allowed"]
    assert "Ganesh Chaturthi" in result["reason"]
    assert result["next_open_date"] == "2026-09-15"
    assert result["circular_reference"] == "NSE/FAOP/71777"


def test_fresh_weekday_opens_without_network(calendar, monkeypatch):
    import socket
    monkeypatch.setattr(socket, "socket", lambda *a, **k: pytest.fail("gateway used network"))
    folder, _ = calendar
    assert decide_session(date(2026, 9, 15), cache_dir=folder, now=NOW)["allowed"]


@pytest.mark.parametrize("day", [date(2026, 9, 12), date(2026, 9, 13), date(2026, 11, 8)])
def test_weekend_closed_without_cache(tmp_path, day):
    assert decide_session(day, cache_dir=tmp_path, now=NOW)["status"] == "CLOSED"


def test_missing_weekday_cache_unknown(tmp_path):
    result = decide_session(date(2026, 9, 15), cache_dir=tmp_path, now=NOW)
    assert result["status"] == "UNKNOWN" and not result["allowed"]


def test_env_cache_override(calendar, monkeypatch):
    folder, _ = calendar
    monkeypatch.setenv("MARKET_CALENDAR_CACHE_DIR", str(folder))
    assert decide_session(date(2026, 9, 15), now=NOW)["allowed"]


def test_stale_calendar_blocks_weekdays_but_preserves_known_closure(calendar):
    folder, _ = calendar
    stale = NOW + timedelta(hours=49)
    assert decide_session(date(2026, 9, 15), cache_dir=folder, now=stale)["status"] == "UNKNOWN"
    holiday = decide_session(date(2026, 9, 14), cache_dir=folder, now=stale)
    assert holiday["status"] == "CLOSED" and holiday["next_open_date"] is None


def test_unsupported_year_never_reuses_old_calendar(calendar):
    folder, _ = calendar
    assert decide_session(date(2027, 1, 1), cache_dir=folder, now=NOW)["status"] == "UNKNOWN"


def test_ist_default_date_rollover(calendar):
    folder, _ = calendar
    late_utc = NOW.replace(hour=20)
    assert decide_session(cache_dir=folder, now=late_utc)["date"] == "2026-09-14"


@pytest.mark.parametrize("mutation", [
    lambda p: p.update(calendar_year=2025),
    lambda p: p.update(segment="CD"),
    lambda p: p.update(holidays=[]),
    lambda p: p.update(annual_holiday_dates=[]),
    lambda p: p.update(coverage={"annual_validated": False}),
    lambda p: p.update(expires_at="2027-01-01T00:00:00Z"),
    lambda p: p.update(source_url="https://example.com/calendar.pdf"),
    lambda p: p["holidays"].append(p["holidays"][0]),
    lambda p: p["sources"][0].update(archive_path="../outside.pdf"),
    lambda p: p["sources"][0].update(sha256="0" * 64),
    lambda p: p.update(sources=[p["sources"][0]]),
])
def test_invalid_evidence_stays_unknown(calendar, mutation):
    folder, payload = calendar
    mutation(payload)
    write_payload(folder, payload)
    assert decide_session(date(2026, 9, 15), cache_dir=folder, now=NOW)["status"] == "UNKNOWN"


def test_source_and_cache_corruption(calendar):
    folder, payload = calendar
    path = folder / payload["sources"][0]["archive_path"]
    path.write_bytes(b"tampered PDF")
    assert not decide_session(date(2026, 9, 15), cache_dir=folder, now=NOW)["allowed"]
    (folder / "calendar-2026.json").write_text("bad JSON")
    assert not decide_session(date(2026, 9, 15), cache_dir=folder, now=NOW)["allowed"]


def test_future_fetch_blocks_weekday(calendar):
    folder, _ = calendar
    assert not decide_session(date(2026, 9, 15), cache_dir=folder, now=NOW - timedelta(hours=1))["allowed"]


def test_api_additional_holiday_is_preserved(calendar):
    folder, payload = calendar
    additional = sorted(HOLIDAYS + [("2026-01-15", "Municipal Corporation Election - Maharashtra")])
    output = build_calendar(2026, annual_entries(), "NSE/FAOP/71777", parse_api(api_bytes(additional))[2026], payload["sources"], NOW, 48)
    write_payload(folder, output)
    result = decide_session(date(2026, 1, 15), cache_dir=folder, now=NOW)
    assert result["status"] == "CLOSED" and "Election" in result["reason"]


def test_api_missing_annual_holiday_rejected(calendar):
    _, payload = calendar
    with pytest.raises(CalendarError, match="omits annual"):
        build_calendar(2026, annual_entries(), "NSE/FAOP/71777", parse_api(api_bytes(HOLIDAYS[:-1]))[2026], payload["sources"], NOW, 48)


@pytest.mark.parametrize("raw", [b'{}', b'[]', b'{"FO":[]}', b'<html>Error</html>', b'{"FO":[null]}'])
def test_api_error_empty_and_wrong_schema_rejected(raw):
    with pytest.raises(CalendarError):
        parse_api(raw)


def test_wrong_weekday_and_partial_session_rejected():
    data = json.loads(api_bytes())
    data["FO"][0]["weekDay"] = "Tuesday"
    with pytest.raises(CalendarError):
        parse_api(json.dumps(data).encode())
    data = json.loads(api_bytes())
    data["FO"][0]["morning_session"] = "Open"
    with pytest.raises(CalendarError):
        parse_api(json.dumps(data).encode())


def test_circular_table_parse_and_wrong_segment(monkeypatch):
    import pypdf
    rows = [f"{i} {date.fromisoformat(day).strftime('%B %d, %Y %A')} {name}" for i, (day, name) in enumerate(HOLIDAYS, 1)]
    title = "Department: FUTURES & OPTIONS\nNSE/FAOP/71777\nTrading holidays for the calendar year 2026\n"
    text = title + "\n".join(rows)
    monkeypatch.setattr(pypdf, "PdfReader", lambda b: SimpleNamespace(is_encrypted=False, pages=[SimpleNamespace(extract_text=lambda: text)]))
    reference, holidays = parse_circular(b"%PDF-test", 2026, KNOWN_CIRCULARS[2026])
    assert reference == "NSE/FAOP/71777" and len(holidays) == 15
    text = text.replace("FUTURES & OPTIONS", "CURRENCY DERIVATIVES")
    with pytest.raises(CalendarError):
        parse_circular(b"%PDF-test", 2026, KNOWN_CIRCULARS[2026])


def test_refresh_failure_does_not_replace_good_cache(calendar):
    folder, _ = calendar
    before = (folder / "calendar-2026.json").read_bytes()
    client = SimpleNamespace(fetch=lambda url: b'{}')
    result = refresh(cache_dir=folder, now=NOW + timedelta(hours=1), client=client)
    assert result["status"] == "ERROR" and result["updated_years"] == []
    assert (folder / "calendar-2026.json").read_bytes() == before


def test_successful_refresh_and_next_year_pending(tmp_path, monkeypatch):
    from market_calendar import refresh as module
    calls = []
    def fetch(url):
        calls.append(url)
        if url == API_URL:
            return api_bytes()
        if url == KNOWN_CIRCULARS[2026]:
            return b"%PDF-test"
        return b'{"data":[]}'
    monkeypatch.setattr(module, "parse_circular", lambda *args: ("NSE/FAOP/71777", annual_entries()))
    result = refresh(cache_dir=tmp_path, now=NOW, client=SimpleNamespace(fetch=fetch))
    assert result["status"] == "OK" and result["updated_years"] == [2026]
    assert "2027" in calls[-1]
    assert decide_session(date(2026, 9, 14), cache_dir=tmp_path, now=NOW)["status"] == "CLOSED"


def test_invalid_ttl_rejected(tmp_path):
    for ttl in (0, 49, 1000, True):
        with pytest.raises(CalendarError):
            refresh(cache_dir=tmp_path, now=NOW, ttl_hours=ttl)


def test_only_official_https_hosts_permitted():
    client = NSEClient()
    for url in ("http://www.nseindia.com", "https://example.com", "https://nseindia.com.evil.test/x", "https://user:pass@www.nseindia.com", "https://www.nseindia.com:8443/x"):
        with pytest.raises(CalendarError):
            client.fetch(url)


def test_cli_exit_codes_and_status(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("MARKET_CALENDAR_CACHE_DIR", str(tmp_path))
    assert main(["--date", "2026-09-14", "--json"]) == 11
    assert json.loads(capsys.readouterr().out)["status"] == "UNKNOWN"
    assert main(["--date", "2026-09-13"]) == 10
    assert main(["--date", "2026-09-13", "--status"]) == 0


def test_dashboard_reads_cache_and_refresh_status(calendar):
    folder, _ = calendar
    (folder / "refresh_status.json").write_text(json.dumps({"status": "OK", "unexpected": "hidden"}))
    status = calendar_status(cache_dir=folder, now=NOW)
    assert len(status["cached_years"]) == 1
    assert status["cached_years"][0]["fresh"]
    assert status["refresh_status"] == {"status": "OK"}

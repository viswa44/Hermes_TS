"""Serve a loopback-only dashboard from existing local operational evidence.

GET endpoints never launch jobs, fetch provider data, refresh the market calendar,
or access AWS. The optional database endpoint uses a bounded READ ONLY transaction.
"""

from __future__ import annotations

import argparse
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import math
from pathlib import Path
import re
import sys
import threading
import time as clock
from urllib.parse import parse_qs, quote, urlsplit
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
IST = ZoneInfo("Asia/Kolkata")
STATIC = Path(__file__).resolve().parent / "static"
MAX_JSON_BYTES = 2_000_000
DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}\Z")
RUN_RE = re.compile(r"[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}\Z")
PUBLIC_COLUMNS = {
    "observations": ["observation_id", "timestamps", "underlying", "spot", "iv", "volume", "timestamp_source", "data_status"],
    "options": ["observation_id", "timestamps", "oi", "ltp", "strike", "optiontype", "expirydate", "daystoexpiry", "delta", "theta", "gamma", "vega"],
}


def parse_day(value: str) -> date:
    if not isinstance(value, str) or not DATE_RE.fullmatch(value):
        raise ValueError("date must use YYYY-MM-DD")
    return date.fromisoformat(value)


def json_safe(value):
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value) if value.is_finite() else None
    if isinstance(value, str):
        return value[:4000]
    if isinstance(value, dict):
        return {str(key): json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(item) for item in value]
    return None


def pick(value, keys):
    return {key: value[key] for key in keys if key in value} if isinstance(value, dict) else {}


def official_url(value):
    if not isinstance(value, str):
        return None
    parsed = urlsplit(value)
    if parsed.scheme == "https" and parsed.hostname and any(
        parsed.hostname == host or parsed.hostname.endswith("." + host)
        for host in ("nseindia.com", "nsearchives.nseindia.com", "sebi.gov.in")
    ) and not parsed.username and not parsed.password:
        return value
    return None


def s3_console_url(value):
    if not isinstance(value, str):
        return None
    parsed = urlsplit(value)
    if parsed.scheme != "s3" or not re.fullmatch(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]", parsed.netloc):
        return None
    prefix = parsed.path.lstrip("/")
    return "https://s3.console.aws.amazon.com/s3/buckets/" + quote(parsed.netloc, safe="") + "?prefix=" + quote(prefix, safe="")


class DashboardStore:
    def __init__(self, root: Path = ROOT, *, database_enabled=True):
        self.root = Path(root).resolve()
        self.database_enabled = database_enabled
        self._db_cache = {}
        self._db_lock = threading.Lock()

    def safe_file(self, path: Path):
        try:
            relative = path.relative_to(self.root)
            current = self.root
            for part in relative.parts:
                current = current / part
                if current.is_symlink():
                    return None
            if not path.is_file() or not path.resolve().is_relative_to(self.root):
                return None
            return path
        except (OSError, ValueError):
            return None

    def read_json(self, path: Path):
        try:
            if not self.safe_file(path) or path.stat().st_size > MAX_JSON_BYTES:
                return {}
            value = json.loads(path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {}
        except (OSError, UnicodeError, ValueError):
            return {}

    def evidence(self, path: Path):
        if not self.safe_file(path):
            return {"available": False}
        try:
            return {"available": True, "modified_at": datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat(), "source": str(path.relative_to(self.root))}
        except OSError:
            return {"available": False}

    def calendar(self, selected_day: date, now: datetime):
        try:
            from market_calendar.gateway import calendar_status, decide_session
            cache = self.root / "market_calendar" / "runtime"
            current = decide_session(now.astimezone(IST).date(), cache_dir=cache, now=now)
            selected = decide_session(selected_day, cache_dir=cache, now=now)
            overview = calendar_status(cache_dir=cache, now=now)
        except Exception:
            current = {"allowed": False, "status": "UNKNOWN", "reason": "Calendar cache is unavailable; market jobs remain blocked.", "date": now.astimezone(IST).date().isoformat()}
            selected = {**current, "date": selected_day.isoformat()}
            overview = {}
        keys = ("allowed", "status", "reason", "date", "source_url", "circular_reference", "fetched_at", "expires_at", "next_open_date")
        current, selected = pick(current, keys), pick(selected, keys)
        for decision in (current, selected):
            decision["source_url"] = official_url(decision.get("source_url"))
        years = overview.get("cached_years", [])
        if isinstance(years, dict):
            years = list(years.values())
        holidays = []
        for cached in years[:5] if isinstance(years, list) else []:
            if not isinstance(cached, dict):
                continue
            for item in cached.get("holidays", [])[:100]:
                if not isinstance(item, dict):
                    continue
                item = pick(item, ("date", "name", "description", "reason", "type", "session_type", "source_url"))
                try:
                    if parse_day(item.get("date")) < now.astimezone(IST).date():
                        continue
                except (ValueError, TypeError):
                    continue
                item["source_url"] = official_url(item.get("source_url")) or official_url(cached.get("source_url"))
                if "*" in item.get("name", "") or "muhurat" in item.get("name", "").lower():
                    item["session_type"] = "special_session_requires_separate_schedule"
                holidays.append(item)
        refresh = pick(overview.get("refresh_status", {}), ("status", "attempted_at", "updated_years", "message", "next_year_status"))
        return {"today": current, "selected_day": selected, "upcoming_holidays": sorted(holidays, key=lambda item: item["date"])[:12], "refresh": refresh}

    def find_run(self, day: date, entry: dict):
        output = self.root / "data_cleaning_agent" / "output" / "postgres" / day.isoformat()
        if output.is_symlink() or not output.is_dir():
            return None, {}
        revision = entry.get("revision")
        uri_run = None
        if isinstance(entry.get("manifest_uri"), str):
            parts = urlsplit(entry["manifest_uri"]).path.split("/")
            if len(parts) >= 2 and RUN_RE.fullmatch(parts[-2]):
                uri_run = parts[-2]
        candidates = sorted((item for item in output.iterdir() if RUN_RE.fullmatch(item.name)), key=lambda item: item.name)
        if uri_run:
            candidates.sort(key=lambda item: item.name != uri_run)
        for run in candidates[:100]:
            manifest = self.read_json(run / "manifest.json")
            context = manifest.get("source_context", {})
            if not isinstance(context, dict) or context.get("trading_date") != day.isoformat():
                continue
            if manifest.get("run_id") != run.name:
                continue
            if revision and context.get("revision") != revision:
                continue
            if not revision and uri_run and run.name != uri_run:
                continue
            return run, manifest
        return None, {}

    def day_summary(self, day: date, entry: dict):
        run, manifest = self.find_run(day, entry)
        quality = self.read_json(run / "quality_report.json") if run else {}
        context = manifest.get("source_context", {})
        coverage = []
        for symbol, stats in context.get("raw_receipt_coverage", {}).items():
            if isinstance(stats, dict):
                coverage.append({"symbol": symbol, **pick(stats, ("raw_receipt_count", "captured_distinct_slots", "expected_slots", "missing_slots", "earliest_receipt", "latest_receipt"))})
        result = {
            "date": day.isoformat(), **pick(entry, ("status", "checked_at", "rows", "reused", "revision", "commit_uri", "manifest_uri")),
            "local_evidence": bool(manifest), "quality": pick(quality, ("passed", "accepted_rows", "quarantined_rows", "duplicates_removed", "missing_values", "row_count_reconciled")),
            "table_counts": pick(manifest.get("table_counts", {}), ("observations.parquet", "options.parquet")),
            "coverage": coverage,
            "integrity_passed": context.get("integrity_passed"),
            "planner": manifest.get("planner"),
            "greeks_enabled": manifest.get("settings", {}).get("derive_greeks"),
            "timestamp_provenance": context.get("timestamp_provenance"),
            "iv_provenance": context.get("iv_provenance"),
            "created_at": manifest.get("created_at"),
            "s3_console_url": s3_console_url(entry.get("manifest_uri")),
        }
        if "status" not in result:
            result["status"] = "NO_EXPORT"
        return result

    def status(self, selected_day: date | None = None, now: datetime | None = None):
        now = now or datetime.now(timezone.utc)
        cleaner = self.root / "data_cleaning_agent" / "runtime"
        raw = self.root / "hermes_v0" / "runtime"
        progress = self.read_json(cleaner / "progress.json")
        entries = progress.get("days", {})
        entries = entries if isinstance(entries, dict) else {}
        valid = []
        for key in entries:
            try:
                valid.append(parse_day(key))
            except ValueError:
                continue
        valid = sorted(valid, reverse=True)[:90]
        selected_day = selected_day or (valid[0] if valid else now.astimezone(IST).date())
        days = [self.day_summary(day, entries[day.isoformat()]) for day in valid if isinstance(entries[day.isoformat()], dict)]
        selected = next((item for item in days if item["date"] == selected_day.isoformat()), self.day_summary(selected_day, {}))
        collector = self.read_json(raw / "status.json")
        raw_status = pick(collector, ("state", "mode", "heartbeat_at", "started_at", "last_database_raw_at", "last_raw_received_at", "provider_freshness", "captured", "rejected", "skipped", "shutdown_flush"))
        raw_status["issues"] = [label for key, label in (("database_error", "Collector recorded a database error"), ("source_error", "Collector recorded a provider error")) if collector.get(key)]
        try:
            heartbeat = datetime.fromisoformat(collector.get("heartbeat_at", ""))
            raw_status["heartbeat_age_seconds"] = max(0, int((now - heartbeat).total_seconds()))
        except (ValueError, TypeError):
            raw_status["heartbeat_age_seconds"] = None
        last_clean = self.read_json(cleaner / "last_status.json")
        cleaning_status = pick(last_clean, ("status", "checked_at", "reason", "eligible_through", "completed", "failed"))
        if last_clean.get("error"):
            cleaning_status["error"] = "A cleaning error was recorded. Review the private local job log."
        verified = self.read_json(cleaner / "s3_verification.json")
        verified_days = []
        for item in verified.get("verified", [])[:90]:
            if isinstance(item, dict):
                verified_days.append(pick(item, ("date", "rows", "checksums_verified")))
        gates = {
            component: pick(self.read_json(self.root / "market_calendar" / "runtime" / f"gate-{component}.json"),
                            ("component", "allowed", "status", "reason", "date", "checked_at"))
            for component in ("collector", "watchdog", "cleaner")
        }
        return json_safe({
            "refreshed_at": now.isoformat(), "timezone": "Asia/Kolkata", "today": now.astimezone(IST).date().isoformat(),
            "selected_date": selected_day.isoformat(), "calendar": self.calendar(selected_day, now),
            "collector": raw_status, "gates": gates,
            "watchdog": pick(self.read_json(raw / "watchdog.json"), ("state", "checked_at", "date", "starts_today", "reason")),
            "cleaning": cleaning_status, "days": days, "selected": selected,
            "s3_verification": {"days": verified_days, "evidence": self.evidence(cleaner / "s3_verification.json"), "scope": "Previous downloaded-object checksum verification; no live AWS call is made by this dashboard."},
            "replay_verification": pick(self.read_json(cleaner / "replay_verification.json"), ("status", "days", "all_reused", "new_objects", "s3_objects_before", "s3_objects_after")),
            "evidence": [self.evidence(path) for path in (raw / "status.json", raw / "watchdog.json", cleaner / "last_status.json", cleaner / "progress.json")],
            "schedules": {"collector": "09:15–15:30 IST", "cleaning": "15:45 IST", "days": "Monday–Friday, only when the market gateway allows the session"},
            "database_enabled": self.database_enabled,
        })

    def samples(self, day: date, limit=5):
        progress = self.read_json(self.root / "data_cleaning_agent" / "runtime" / "progress.json")
        entry = progress.get("days", {}).get(day.isoformat(), {})
        run, manifest = self.find_run(day, entry)
        if run is None:
            return {"date": day.isoformat(), "status": "UNAVAILABLE", "reason": "No matching local export for this date.", "tables": {}}
        tables = {}
        try:
            import pyarrow.parquet as pq
            for table, columns in PUBLIC_COLUMNS.items():
                path = run / (table + ".parquet")
                if not self.safe_file(path) or path.stat().st_size > 100_000_000:
                    continue
                parquet = pq.ParquetFile(path)
                columns = [column for column in columns if column in parquet.schema_arrow.names]
                first = next(parquet.iter_batches(batch_size=min(10, max(1, limit)), columns=columns), None)
                tables[table] = {"columns": columns, "rows": json_safe(first.to_pylist() if first is not None else []), "total_rows": parquet.metadata.num_rows}
        except Exception:
            return {"date": day.isoformat(), "status": "UNAVAILABLE", "reason": "Local Parquet preview is unavailable; use the cleaning environment with PyArrow installed.", "tables": {}}
        return {"date": day.isoformat(), "status": "AVAILABLE", "source": "Local cleaned Parquet; AWS objects are not fetched by this view.", "revision": manifest.get("source_context", {}).get("revision"), "tables": tables}

    def database_summary(self, day: date):
        if not self.database_enabled:
            return {"status": "DISABLED", "date": day.isoformat()}
        with self._db_lock:
            previous = self._db_cache.get(day)
            if previous and clock.monotonic() - previous[0] < 30:
                return {**previous[1], "cached": True}
            result = self._read_database(day)
            if len(self._db_cache) >= 32:
                self._db_cache.clear()
            self._db_cache[day] = (clock.monotonic(), result)
            return result

    def _read_database(self, day: date):
        checked_at = datetime.now(timezone.utc).isoformat()
        try:
            import psycopg
            from data_cleaning_agent.config.settings import Settings
            settings = Settings()
            kwargs = {"host": settings.postgres_host, "port": settings.postgres_port,
                      "dbname": settings.postgres_database, "user": settings.postgres_user,
                      "connect_timeout": min(3, settings.postgres_connect_timeout),
                      "application_name": "hermes_dashboard_read_only",
                      "options": "-c default_transaction_read_only=on -c statement_timeout=3000 -c lock_timeout=1000 -c timezone=UTC"}
            if settings.postgres_password:
                kwargs["password"] = settings.postgres_password.get_secret_value()
            start = datetime.combine(day, time.min, IST)
            end = start + timedelta(days=1)
            with psycopg.connect(**kwargs) as connection:
                connection.read_only = True
                with connection.cursor() as cursor:
                    cursor.execute("SHOW transaction_read_only")
                    if cursor.fetchone()[0] != "on":
                        raise RuntimeError("read-only required")
                    cursor.execute("""SELECT count(*), min(timestamp_ist), max(timestamp_ist),
                        count(*) FILTER (WHERE oi IS NULL), count(*) FILTER (WHERE iv IS NULL)
                        FROM public.option_snapshot WHERE timestamp_ist >= %s AND timestamp_ist < %s""", (start, end))
                    rows, earliest, latest, null_oi, null_iv = cursor.fetchone()
                    cursor.execute("SELECT count(*) FROM public.market_snapshot WHERE timestamp_ist >= %s AND timestamp_ist < %s", (start, end))
                    spot_rows = cursor.fetchone()[0]
            return json_safe({"status": "CONNECTED", "date": day.isoformat(), "checked_at": checked_at, "read_only": True,
                "option_rows": rows, "market_rows": spot_rows, "earliest_receipt": earliest, "latest_receipt": latest,
                "null_oi_rows": null_oi, "null_stored_iv_rows": null_iv, "scope": "All receipts on the selected IST date, including any outside regular market hours. Provider event time remains unverified.", "cached": False})
        except Exception:
            return {"status": "UNAVAILABLE", "date": day.isoformat(), "checked_at": checked_at,
                    "reason": "Read-only PostgreSQL check could not complete. Local export evidence remains available.", "cached": False}


class DashboardServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, store):
        if address[0] != "127.0.0.1":
            raise ValueError("The dashboard must bind to 127.0.0.1")
        self.store = store
        super().__init__(address, DashboardHandler)


class DashboardHandler(BaseHTTPRequestHandler):
    server_version = "HermesDashboard"

    def log_message(self, format, *args):
        # Avoid recording user-supplied query strings or arbitrary headers.
        return

    def send_payload(self, value, status=200, content_type="application/json; charset=utf-8", head=False):
        body = value if isinstance(value, bytes) else json.dumps(json_safe(value), allow_nan=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'none'")
        self.end_headers()
        if not head:
            self.wfile.write(body)

    def _trusted_request(self):
        port = self.server.server_port
        hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
        if port == 80:
            hosts.update({"127.0.0.1", "localhost"})
        origin = self.headers.get("Origin")
        return self.headers.get("Host") in hosts and (not origin or origin in {"http://" + host for host in hosts})

    def do_GET(self, head=False):
        if not self._trusted_request():
            self.send_payload({"error": "Only local dashboard requests are accepted."}, 403, head=head)
            return
        target = urlsplit(self.path)
        static = {"/": ("index.html", "text/html; charset=utf-8"), "/app.js": ("app.js", "text/javascript; charset=utf-8"), "/style.css": ("style.css", "text/css; charset=utf-8")}
        if target.path in static:
            filename, mime = static[target.path]
            self.send_payload((STATIC / filename).read_bytes(), content_type=mime, head=head)
            return
        if target.path not in ("/api/status", "/api/day", "/api/database", "/health"):
            self.send_payload({"error": "Not found"}, 404, head=head)
            return
        try:
            query = parse_qs(target.query, keep_blank_values=True, max_num_fields=4)
            if set(query) - {"date", "limit"} or any(len(values) != 1 for values in query.values()):
                raise ValueError("Unsupported query")
            day = parse_day(query["date"][0]) if "date" in query else None
            if target.path == "/api/status":
                result = self.server.store.status(day)
            elif target.path == "/api/day":
                if day is None:
                    raise ValueError("date is required")
                limit = int(query.get("limit", ["5"])[0])
                if not 1 <= limit <= 10:
                    raise ValueError("limit must be between 1 and 10")
                result = self.server.store.samples(day, limit)
            elif target.path == "/api/database":
                if day is None:
                    raise ValueError("date is required")
                result = self.server.store.database_summary(day)
            else:
                result = {"status": "ok", "read_only": True}
            self.send_payload(result, head=head)
        except (ValueError, TypeError):
            self.send_payload({"error": "Use one valid YYYY-MM-DD date and a preview limit from 1 to 10."}, 400, head=head)
        except Exception:
            self.send_payload({"error": "Operational evidence could not be read."}, 500, head=head)

    def do_HEAD(self):
        self.do_GET(head=True)

    def do_POST(self):
        self.send_payload({"error": "This dashboard is read-only. Use GET."}, 405)

    do_PUT = do_POST
    do_PATCH = do_POST
    do_DELETE = do_POST
    do_OPTIONS = do_POST


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-database", action="store_true", help="Show local evidence only; make no PostgreSQL connections")
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("port must be between 1 and 65535")
    server = DashboardServer(("127.0.0.1", args.port), DashboardStore(database_enabled=not args.no_database))
    print(f"Hermes operations dashboard: http://127.0.0.1:{args.port} (read-only)", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()

"""Read-only, bounded exports of immutable Hermes PostgreSQL observations.

The caller owns a READ ONLY REPEATABLE READ transaction. Raw canonical fields
remain unchanged; separately linked calculation evidence can support downstream
enrichment without relabeling calculated values as raw observations. No database
mutations are performed here. Receipt coverage is not provider freshness.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal
import hashlib
import json
import math
import os
from pathlib import Path
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo


IST = ZoneInfo("Asia/Kolkata")
SESSION_OPEN = time(9, 15)
SESSION_CLOSE = time(15, 30)
SLOT_SECONDS = 5


@dataclass(frozen=True)
class ExportResult:
    path: Path
    row_count: int
    source_sha256: str
    trading_date: date
    metadata: dict[str, Any]


def session_bounds(trading_date: date) -> tuple[datetime, datetime]:
    if not isinstance(trading_date, date) or isinstance(trading_date, datetime):
        raise ValueError("trading_date must be a date")
    return (datetime.combine(trading_date, SESSION_OPEN, IST),
            datetime.combine(trading_date, SESSION_CLOSE, IST))


def _records(cursor, batch_size: int = 1000):
    names = [column.name if hasattr(column, "name") else column[0]
             for column in cursor.description]
    while rows := cursor.fetchmany(batch_size):
        for row in rows:
            yield dict(row) if isinstance(row, dict) else dict(zip(names, row))


def available_trading_dates(connection, start_date: date, end_date: date) -> list[date]:
    """Return inclusive weekdays with actual option receipts during market hours."""
    session_bounds(start_date)
    session_bounds(end_date)
    if start_date > end_date:
        raise ValueError("start_date must not follow end_date")
    lower = datetime.combine(start_date, time.min, IST)
    upper = datetime.combine(end_date + timedelta(days=1), time.min, IST)
    with connection.cursor() as cursor:
        cursor.execute("""
            SELECT DISTINCT (timestamp_ist AT TIME ZONE 'Asia/Kolkata')::date AS trading_date
            FROM public.option_snapshot
            WHERE timestamp_ist >= %s AND timestamp_ist < %s
              AND EXTRACT(ISODOW FROM timestamp_ist AT TIME ZONE 'Asia/Kolkata') < 6
              AND (timestamp_ist AT TIME ZONE 'Asia/Kolkata')::time >= %s
              AND (timestamp_ist AT TIME ZONE 'Asia/Kolkata')::time < %s
            ORDER BY trading_date
        """, (lower, upper, SESSION_OPEN, SESSION_CLOSE))
        return [row["trading_date"] for row in _records(cursor)]


_EXPORT_SQL = """
    SELECT o.timestamp_ist, o.symbol AS underlying, o.strike, o.option_type,
           o.expiry_date, o.trading_date, o.ltp, o.volume, o.oi, o.iv AS source_iv,
           o.data_status::text AS data_status, o.version AS source_version,
           o.ingestion_time AS source_ingestion_time,
           m.match_count AS market_match_count, m.spot_ltp AS source_spot,
           m.trading_date AS market_trading_date, m.version AS market_version,
           m.data_status AS market_data_status, m.evidence AS market_evidence,
           r.match_count AS receipt_match_count, r.observation_id AS receipt_id,
           r.timestamp_basis, r.freshness, r.evidence AS receipt_evidence,
           to_jsonb(o)::text AS source_option_json,
           m.source_market_json, g.source_greeks_json
    FROM public.option_snapshot o
    LEFT JOIN LATERAL (
        SELECT count(*) AS match_count, max(spot_ltp) AS spot_ltp,
               max(trading_date) AS trading_date, max(version) AS version,
               max(data_status::text) AS data_status,
               jsonb_agg(jsonb_build_object('timestamp', timestamp_ist,
                   'symbol', symbol, 'spot', spot_ltp, 'trading_date', trading_date,
                   'version', version, 'data_status', data_status))::text AS evidence,
               COALESCE(jsonb_agg(to_jsonb(m) - 'provider_payload'
                   ORDER BY m.timestamp_ist, m.symbol, m.version), '[]'::jsonb)::text
                   AS source_market_json
        FROM public.market_snapshot m
        WHERE m.timestamp_ist=o.timestamp_ist AND m.symbol=o.symbol
    ) m ON TRUE
    LEFT JOIN LATERAL (
        SELECT count(*) AS match_count,
               CASE WHEN count(*)=1 THEN min(observation_id) END AS observation_id,
               max(timestamp_basis) AS timestamp_basis, max(freshness) AS freshness,
               jsonb_agg(jsonb_build_object('observation_id', observation_id,
                   'digest', digest, 'scheduled_at', scheduled_at,
                   'request_started_at', request_started_at, 'received_at', received_at,
                   'timestamp_basis', timestamp_basis, 'freshness', freshness,
                   'issues', issues) ORDER BY observation_id)::text AS evidence
        FROM public.hermes_ingest_receipt r
        WHERE r.kind='RAW' AND r.symbol=o.symbol AND r.received_at=o.timestamp_ist
    ) r ON TRUE
    LEFT JOIN LATERAL (
        SELECT COALESCE(jsonb_agg(to_jsonb(g) || jsonb_build_object(
                   'derived_receipts', COALESCE(d.evidence, '[]'::jsonb))
                   ORDER BY g.option_symbol, g.version, g.ingestion_time), '[]'::jsonb)::text
                   AS source_greeks_json
        FROM public.option_greeks_snapshot g
        LEFT JOIN LATERAL (
            SELECT jsonb_agg(to_jsonb(d) ORDER BY d.observation_id) AS evidence
            FROM public.hermes_ingest_receipt d
            WHERE d.kind='DERIVED' AND d.parent_observation_id=r.observation_id
              AND d.symbol=g.option_symbol AND d.received_at=g.ingestion_time
        ) d ON TRUE
        WHERE g.timestamp_ist=o.timestamp_ist AND g.underlying_symbol=o.symbol
          AND g.strike=o.strike AND g.option_type=o.option_type
          AND g.expiry_date=o.expiry_date AND g.trading_date=o.trading_date
          AND g.version=o.version
    ) g ON TRUE
    WHERE o.timestamp_ist >= %s AND o.timestamp_ist < %s
    ORDER BY o.timestamp_ist, o.symbol, o.strike, o.option_type, o.expiry_date
    LIMIT %s
"""


def _json_value(value):
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        # Decimal-to-float conversion could silently round OI/volume.
        return str(value)
    if isinstance(value, float) and not math.isfinite(value):
        return str(value)  # Explicit invalid scalar for deterministic quarantine.
    return value


def _canonical(row: dict, trading_date: date) -> tuple[dict, list[str]]:
    issues = []
    if row["market_match_count"] != 1:
        issues.append("missing_market_snapshot" if row["market_match_count"] == 0 else "ambiguous_market_snapshot")
    elif row["market_trading_date"] != trading_date:
        issues.append("market_trading_date_mismatch")
    if row["trading_date"] != trading_date:
        issues.append("option_trading_date_mismatch")
    version = row["source_version"]
    if version == 2:
        if row["receipt_match_count"] != 1:
            issues.append("missing_raw_receipt" if row["receipt_match_count"] == 0 else "ambiguous_raw_receipt")
        elif (row["timestamp_basis"] != "APPLICATION_RECEIPT" or
              row["freshness"] != "UNVERIFIED_PROVIDER_TIME"):
            issues.append("raw_receipt_provenance_mismatch")
        if row["market_match_count"] == 1 and (row["market_version"] != 2 or
                                               row["market_data_status"] != "PARTIAL"):
            issues.append("market_provenance_mismatch")
        if row["data_status"] != "PARTIAL":
            issues.append("option_provenance_mismatch")
        timestamp_source = "APPLICATION_RECEIPT" if not issues else "UNVERIFIED_SOURCE_INTEGRITY"
    else:
        timestamp_source = "UNVERIFIED_LEGACY_TIME"
        if version != 1:
            issues.append("unsupported_source_version")
    record = {
        "timestamps": row["timestamp_ist"], "underlying": row["underlying"],
        "symbol": None, "exchange": None,
        "spot": None if issues else row["source_spot"],
        "iv": None, "volume": row["volume"], "oi": row["oi"], "ltp": row["ltp"],
        "strike": row["strike"], "optiontype": row["option_type"],
        "expirydate": row["expiry_date"], "data_status": row["data_status"],
        "timestamp_source": timestamp_source, "provider_timestamp": None,
        "source_version": version, "source_trading_date": row["trading_date"],
        "source_iv": row["source_iv"], "source_spot": row["source_spot"],
        "source_ingestion_time": row["source_ingestion_time"],
        "source_receipt_id": row["receipt_id"] if row["receipt_match_count"] == 1 else None,
        "source_freshness": row["freshness"] if row["receipt_match_count"] == 1 else None,
        "source_receipt_match_count": row["receipt_match_count"],
        "source_market_match_count": row["market_match_count"],
        "source_receipt_evidence_json": row["receipt_evidence"],
        "source_market_evidence_json": row["market_evidence"],
        "source_option_json": row["source_option_json"],
        "source_market_json": row["source_market_json"],
        "source_greeks_json": row["source_greeks_json"],
        "source_integrity_issues": ",".join(issues) if issues else None,
    }
    return {key: _json_value(value) for key, value in record.items()}, issues


def _day_metadata(connection, trading_date: date, start: datetime, end: datetime) -> dict:
    day_start = datetime.combine(trading_date, time.min, IST)
    day_end = day_start + timedelta(days=1)
    with connection.cursor() as cursor:
        cursor.execute("""
            SELECT count(*) AS all_day_option_rows,
                   count(*) FILTER (WHERE timestamp_ist < %s OR timestamp_ist >= %s) AS out_of_window_count
            FROM public.option_snapshot WHERE timestamp_ist >= %s AND timestamp_ist < %s
        """, (start, end, day_start, day_end))
        counts = next(_records(cursor))
    with connection.cursor() as cursor:
        cursor.execute("""
            SELECT count(*) AS orphan_market_rows
            FROM public.market_snapshot m
            WHERE m.timestamp_ist >= %s AND m.timestamp_ist < %s
              AND NOT EXISTS (SELECT 1 FROM public.option_snapshot o
                  WHERE o.timestamp_ist=m.timestamp_ist AND o.symbol=m.symbol)
        """, (start, end))
        counts.update(next(_records(cursor)))
    coverage = {}
    expected_count = int((end - start).total_seconds()) // SLOT_SECONDS
    with connection.cursor(name="cleaning_receipts_" + uuid4().hex) as cursor:
        cursor.execute("""
            SELECT symbol, scheduled_at, received_at FROM public.hermes_ingest_receipt
            WHERE kind='RAW' AND received_at >= %s AND received_at < %s
            ORDER BY symbol, received_at, observation_id
        """, (day_start, day_end))
        for row in _records(cursor):
            stats = coverage.setdefault(row["symbol"], {
                "raw_receipt_count": 0, "captured_slots": set(), "out_of_window_receipts": 0,
                "unaligned_scheduled_receipts": 0, "earliest_receipt": row["received_at"].isoformat(),
                "latest_receipt": row["received_at"].isoformat(),
            })
            stats["raw_receipt_count"] += 1
            stats["latest_receipt"] = row["received_at"].isoformat()
            offset = (row["scheduled_at"] - start).total_seconds()
            if not start <= row["received_at"] < end:
                stats["out_of_window_receipts"] += 1
            if offset < 0 or offset >= expected_count * SLOT_SECONDS or offset % SLOT_SECONDS:
                stats["unaligned_scheduled_receipts"] += 1
            else:
                stats["captured_slots"].add(int(offset) // SLOT_SECONDS)
    for stats in coverage.values():
        slots = stats.pop("captured_slots")
        gaps = []
        for slot in range(expected_count):
            if slot not in slots:
                if gaps and gaps[-1][1] == slot:
                    gaps[-1][1] = slot + 1
                else:
                    gaps.append([slot, slot + 1])
        stats.update({
            "expected_slots": expected_count, "captured_distinct_slots": len(slots),
            "missing_slots": expected_count - len(slots),
            "missing_intervals": [{
                "start_inclusive": (start + timedelta(seconds=a * SLOT_SECONDS)).isoformat(),
                "end_exclusive": (start + timedelta(seconds=b * SLOT_SECONDS)).isoformat(),
                "slots": b - a,
            } for a, b in gaps],
        })
    return {**counts, "raw_receipt_coverage": coverage,
            "coverage_note": "Weekday 5-second slots; missing intervals include holidays/outages. Receipt coverage does not verify provider event time."}


def export_trading_day(connection, trading_date: date, output_path: Path, *,
                       max_rows: int = 100_000, batch_size: int = 1000,
                       max_bytes: int = 256 * 1024 * 1024) -> ExportResult:
    """Stream a daily JSONL snapshot; integrity failures remain visible in metadata.

    Every matching option row appears once. Invalid joins retain source evidence
    and a null canonical spot, ensuring the cleaner quarantines those rows.
    Callers must also gate publication on metadata['integrity_passed'].
    """
    start, end = session_bounds(trading_date)
    if trading_date.weekday() >= 5:
        raise ValueError("Only weekday trading dates can be exported")
    for name, value in (("max_rows", max_rows), ("batch_size", batch_size), ("max_bytes", max_bytes)):
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValueError(f"{name} must be a positive integer")
    output_path = Path(output_path)
    if output_path.exists():
        raise FileExistsError("Source snapshot already exists")
    metadata = _day_metadata(connection, trading_date, start, end)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = output_path.with_name("." + output_path.name + "." + uuid4().hex + ".tmp")
    digest = hashlib.sha256()
    row_count = size_bytes = legacy_rows = 0
    anomalies = Counter()
    symbols = set()
    try:
        with os.fdopen(os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as stream:
            with connection.cursor(name="cleaning_export_" + uuid4().hex) as cursor:
                cursor.execute(_EXPORT_SQL, (start, end, max_rows + 1))
                for row in _records(cursor, batch_size):
                    row_count += 1
                    if row_count > max_rows:
                        raise ValueError("PostgreSQL export exceeds MAX_ROWS; no partial snapshot published")
                    record, issues = _canonical(row, trading_date)
                    anomalies.update(issues)
                    legacy_rows += row["source_version"] == 1
                    symbols.add(row["underlying"])
                    encoded = (json.dumps(record, sort_keys=True, ensure_ascii=False, allow_nan=False,
                                          separators=(",", ":")) + "\n").encode("utf-8")
                    size_bytes += len(encoded)
                    if size_bytes > max_bytes:
                        raise ValueError("PostgreSQL export exceeds byte limit; no partial snapshot published")
                    stream.write(encoded)
                    digest.update(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        # The snapshot is immutable: refuse to replace a concurrently created file.
        os.link(temporary, output_path)
    finally:
        temporary.unlink(missing_ok=True)
    count_reconciled = row_count == metadata["all_day_option_rows"] - metadata["out_of_window_count"]
    if not count_reconciled:
        anomalies["source_row_count_mismatch"] += 1
    expected_slots = int((end - start).total_seconds()) // SLOT_SECONDS
    for symbol in symbols - metadata["raw_receipt_coverage"].keys():
        metadata["raw_receipt_coverage"][symbol] = {
            "raw_receipt_count": 0, "captured_distinct_slots": 0,
            "out_of_window_receipts": 0, "unaligned_scheduled_receipts": 0,
            "earliest_receipt": None, "latest_receipt": None,
            "expected_slots": expected_slots, "missing_slots": expected_slots,
            "missing_intervals": [{"start_inclusive": start.isoformat(),
                                   "end_exclusive": end.isoformat(), "slots": expected_slots}],
        }
    metadata.update({
        "schema_version": 2, "source": "postgresql",
        "source_tables": ["public.option_snapshot", "public.market_snapshot", "public.hermes_ingest_receipt",
                          "public.option_greeks_snapshot"],
        "trading_date": trading_date.isoformat(), "session_timezone": "Asia/Kolkata",
        "session_start_inclusive": start.isoformat(), "session_end_exclusive": end.isoformat(),
        "row_count": row_count, "row_count_reconciled": count_reconciled,
        "size_bytes": size_bytes, "legacy_rows": legacy_rows,
        "symbols": sorted(symbols), "anomaly_counts": dict(sorted(anomalies.items())),
        "integrity_passed": not anomalies and metadata["orphan_market_rows"] == 0,
        "timestamp_provenance": "Version 2 timestamps are application receipt time; provider timestamp is unknown. Legacy timestamps remain unverified.",
        "iv_provenance": "Raw canonical IV is null. Stored option_snapshot.iv is retained as source_iv with unknown units/provenance. Separate contract-matched Greek candidates and exact parent-linked calculation receipts are retained as JSON evidence for downstream enrichment; missing or ambiguous calculations never become raw IV. Legacy calculation evidence remains unverified.",
    })
    return ExportResult(output_path, row_count, digest.hexdigest(), trading_date, metadata)

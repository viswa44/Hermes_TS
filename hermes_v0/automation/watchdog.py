"""Read-only freshness checks and bounded restart of the Hermes collector only."""

import asyncio
from contextlib import suppress
from datetime import datetime, timezone
import json
import os
import subprocess

import httpx

from hermes_v0.collector.integrity import session_time, instant, IST
from hermes_v0.collector.live_option_metrics import ProcessCollectorLock, CollectorAlreadyRunningError
from hermes_v0.collector.recovery import RUNTIME, write_status
from hermes_v0.config import CFG


def age(value, now):
    try:
        return max(0, (now-instant(value)).total_seconds())
    except (ValueError, TypeError):
        return float("inf")


def decision(*, now, running, heartbeat_age, row_age, source_error, source_healthy, database_healthy, starts):
    if not session_time(now):
        return "MARKET_CLOSED", False
    if source_error == "LOGIN_REQUIRED":
        return "LOGIN_REQUIRED", False
    if source_error == "INTEGRITY_MarketClosed":
        return "MARKET_CLOSED", False
    if row_age <= 20 and running and database_healthy:
        return "RECEIVING_UNVERIFIED_QUOTES", False
    if not running or heartbeat_age > 60:
        return ("RESTART_COLLECTOR", True) if source_healthy and starts < 3 else ("RESTART_LIMIT_OR_SOURCE_UNAVAILABLE", False)
    if not database_healthy:
        return "DATABASE_UNAVAILABLE_BUFFERING", False
    return "COLLECTION_STALE", False


async def inspect_database():
    import asyncpg
    c = await asyncpg.connect(host=CFG.database.host, port=CFG.database.port,
        database=CFG.database.database, user=CFG.database.user, password=CFG.database.password,
        timeout=2, command_timeout=2, server_settings={"default_transaction_read_only": "on"})
    try:
        return await c.fetchval("SELECT max(received_at) FROM hermes_ingest_receipt WHERE kind='RAW'")
    finally:
        await c.close()


async def check():
    now = datetime.now(timezone.utc)
    path = RUNTIME / "watchdog.json"
    def read_json(target):
        try:
            return json.loads(target.read_text())
        except (OSError, ValueError):
            return {}
    old, collector = read_json(path), read_json(RUNTIME / "status.json")
    starts = old.get("starts_today", 0) if old.get("date") == now.astimezone(IST).date().isoformat() else 0
    if not session_time(now):
        write_status(path, dict(date=now.astimezone(IST).date().isoformat(), checked_at=now.isoformat(),
                               state="MARKET_CLOSED", starts_today=starts))
        return
    try:
        with ProcessCollectorLock():
            running = False
    except CollectorAlreadyRunningError:
        running = True
    database_healthy, source_healthy, latest = False, False, None
    with suppress(Exception):
        latest = await inspect_database()
        database_healthy = True
    with suppress(Exception):
        async with httpx.AsyncClient(timeout=2, follow_redirects=False) as client:
            result = await client.get(CFG.openalgo.host + "/health/status")
            source_healthy = result.status_code == 200 and result.json().get("status") == "pass"
    heartbeat_age = age(collector.get("sampler_heartbeat_at", collector.get("started_at")), now)
    # A newly started process gets a bounded startup allowance, not an endless exemption.
    start_age = age(collector.get("started_at"), now)
    if running and start_age < 20:
        code, restart = "STARTING", False
    else:
        code, restart = decision(now=now, running=running, heartbeat_age=heartbeat_age,
            row_age=age(latest, now), source_error=collector.get("source_error") if heartbeat_age < 60 else None,
            source_healthy=source_healthy, database_healthy=database_healthy, starts=starts)
    if restart:
        starts += 1
        # Persist the budget BEFORE dispatch so a watchdog crash cannot reset retries.
        write_status(path, dict(date=now.astimezone(IST).date().isoformat(), starts_today=starts))
        result = subprocess.run(["launchctl", "kickstart", "-k",
            f"gui/{os.getuid()}/com.openalgo.hermes-v0-option-metrics"], capture_output=True, timeout=5)
        if result.returncode:
            code = "RESTART_FAILED"
    last_alert = old.get("last_alert_at")
    notification_error = None
    if code not in ("STARTING", "MARKET_CLOSED", "RECEIVING_UNVERIFIED_QUOTES") and (old.get("state") != code or age(last_alert, now) >= 300):
        # Local desktop notification only; no account/message integration or credentials.
        try:
            notification = subprocess.run(["/usr/bin/osascript", "-e",
                'display notification "NIFTY collection needs attention. See Hermes runtime status." with title "Hermes collector"'],
                capture_output=True, timeout=5)
            if notification.returncode:
                notification_error = "NotificationUnavailable"
        except Exception as error:
            notification_error = type(error).__name__
        last_alert = now.isoformat()
    write_status(path, dict(date=now.astimezone(IST).date().isoformat(), checked_at=now.isoformat(), state=code,
        starts_today=starts, collector_running=running, last_database_raw_at=latest.isoformat() if latest else None,
        database_healthy=database_healthy, openalgo_healthy=source_healthy, last_alert_at=last_alert,
        notification_error=notification_error))


def main():
    RUNTIME.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        with ProcessCollectorLock(RUNTIME / "watchdog.lock"):
            asyncio.run(check())
    except CollectorAlreadyRunningError:
        pass
    except Exception as error:
        # launchd log contains a safe type only; never print DSNs or HTTP payloads.
        print(json.dumps(dict(time=datetime.now(timezone.utc).isoformat(), watchdog_error=type(error).__name__)))
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

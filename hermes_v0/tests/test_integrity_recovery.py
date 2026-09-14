"""Behavioral failure tests: loss is allowed, false/overwritten observations are not."""

import asyncio
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
from unittest.mock import AsyncMock, patch

import pytest

from hermes_v0.collector.integrity import Observation, IntegrityError, IST, number
from hermes_v0.collector.adapters.openalgo_adapter import AdapterConfig
from hermes_v0.collector.adapters.strict_openalgo import StrictOpenAlgo, AuthenticationRequired
from hermes_v0.collector.recovery import RecoveryService, error_code
from hermes_v0.collector.scheduler import ClockAlignedScheduler
from hermes_v0.storage.recovery_journal import Journal
from hermes_v0.automation.watchdog import decision

SLOT = datetime(2026, 9, 8, 10, 0, tzinfo=IST)


@pytest.fixture(autouse=True)
def open_calendar_for_integrity_mechanics(monkeypatch):
    monkeypatch.setattr('hermes_v0.collector.recovery.check_component', lambda *args, **kwargs: {'allowed': True, 'status': 'OPEN'})


def raw(slot=SLOT, oi=100):
    return Observation.make("RAW", slot, slot, slot+timedelta(seconds=.1), dict(
        symbol="NIFTY", spot_ltp=23700.0, expiry="15SEP26", strike=23700,
        options=[dict(symbol=f"NIFTY15SEP2623700{side}", side=side, ltp=50.0,
                      oi=oi, volume=None, bid=49.5, ask=50.5) for side in ("CE", "PE")],
        freshness="UNVERIFIED_PROVIDER_TIME", issues=["PROVIDER_TIMESTAMP_UNAVAILABLE"]))


@pytest.mark.parametrize("value", [True, False, "NaN", float("inf"), "-inf", "garbage", [], {}])
def test_bad_numbers_rejected(value):
    with pytest.raises(IntegrityError):
        number(value)


@pytest.mark.parametrize("value", [-1, 1.5, 2**54, 0, "1.00000000000000001", "9007199254740991.1"])
def test_invalid_or_ambiguous_oi_never_published(value):
    with pytest.raises(IntegrityError):
        raw(oi=value)


def test_missing_oi_is_null_and_source_freshness_never_invented():
    capture = raw(oi=None)
    assert capture.data["body"]["options"][0]["oi"] is None
    assert capture.data["body"]["freshness"] == "UNVERIFIED_PROVIDER_TIME"
    data = capture.data
    data["body"]["freshness"] = "VERIFIED"
    with pytest.raises(IntegrityError):
        Observation(json.dumps(data)).validate()


@pytest.mark.parametrize("change", [
    lambda d: d["body"]["options"][0].update(symbol="NIFTY15SEP2623750CE"),
    lambda d: d["body"]["options"][0].update(bid=100, ask=1),
    lambda d: d["body"]["options"][1].update(side="CE"),
    lambda d: d.update(received_at=(SLOT+timedelta(seconds=6)).isoformat()),
    lambda d: d.update(started_at=SLOT.replace(tzinfo=None).isoformat()),
    lambda d: d["body"].update(apikey="must-not-persist"),
])
def test_contract_timing_and_secret_fields_rejected(change):
    data = raw().data
    change(data)
    with pytest.raises((IntegrityError, ValueError)):
        Observation(json.dumps(data)).validate()


def test_journal_restart_idempotence_conflict_and_corruption(tmp_path):
    path = tmp_path / "spool.sqlite"
    capture = raw()
    journal = Journal(path)
    journal.put(capture)
    journal.put(capture)
    restarted = Journal(path)
    assert [r.payload for r in restarted.pending()] == [capture.payload]
    with pytest.raises(IntegrityError):
        restarted.put(raw(oi=200))
    assert list(restarted.pending())[0].data["body"]["options"][0]["oi"] == 100
    with restarted.db() as db:
        db.execute("UPDATE outbox SET digest='tampered'")
    assert list(restarted.pending()) == []
    assert restarted.counts()["QUARANTINED"] == 1


def test_acknowledgement_never_changes_original_timestamps(tmp_path):
    j = Journal(tmp_path / "spool.sqlite")
    capture = raw()
    j.put(capture)
    j.acknowledge(capture)
    with j.db() as db:
        assert db.execute("SELECT payload FROM outbox").fetchone()[0] == capture.payload
    assert list(j.pending()) == []


def test_gap_windows_survive_restart_without_creating_observations(tmp_path):
    path = tmp_path / "spool.sqlite"
    Journal(path).gap(SLOT, SLOT+timedelta(seconds=10), "MISSED_SCHEDULED_INTERVALS")
    restarted = Journal(path)
    with restarted.db() as db:
        row = db.execute("SELECT start_at,end_at,reason FROM gaps").fetchone()
        assert datetime.fromisoformat(row[0]) == SLOT
        assert datetime.fromisoformat(row[1]) == SLOT+timedelta(seconds=10)
    assert list(restarted.pending()) == []


def test_full_buffer_refuses_new_records(tmp_path):
    j = Journal(tmp_path / "spool.sqlite", max_bytes=1)
    with pytest.raises(IntegrityError, match="BufferFull"):
        j.put(raw())
    assert list(j.pending()) == []


def test_real_scheduler_discards_wake_after_close():
    async def case():
        scheduler = ClockAlignedScheduler()
        clock = [SLOT.replace(hour=15, minute=29, second=50)]
        scheduler._now_ist = lambda: clock[0]
        async def sleep(_):
            clock[0] = SLOT.replace(hour=18)
        with patch("hermes_v0.collector.scheduler.asyncio.sleep", sleep):
            assert [tick async for tick in scheduler.tick_generator()] == []
        assert scheduler._running is False
    asyncio.run(case())


def test_real_scheduler_skips_old_slot_instead_of_backdating():
    async def case():
        scheduler = ClockAlignedScheduler()
        clock, waits = [SLOT], [0]
        scheduler._now_ist = lambda: clock[0]
        async def sleep(seconds):
            waits[0] += 1
            clock[0] += timedelta(seconds=seconds + (10 if waits[0] == 1 else 0))
        with patch("hermes_v0.collector.scheduler.asyncio.sleep", sleep):
            generator = scheduler.tick_generator()
            tick, _ = await anext(generator)
            scheduler.stop()
            await generator.aclose()
        assert tick == SLOT+timedelta(seconds=20)
    asyncio.run(case())


@pytest.mark.parametrize("running,heartbeat,row,source,db,starts,expected,restart", [
    (True, 0, 5, True, True, 0, "RECEIVING_UNVERIFIED_QUOTES", False),
    (False, 100, 100, True, True, 0, "RESTART_COLLECTOR", True),
    (True, 100, 100, True, True, 1, "RESTART_COLLECTOR", True),
    (False, 100, 100, True, True, 3, "RESTART_LIMIT_OR_SOURCE_UNAVAILABLE", False),
    (True, 0, 100, True, False, 0, "DATABASE_UNAVAILABLE_BUFFERING", False),
    (True, 0, 100, False, True, 0, "COLLECTION_STALE", False),
])
def test_watchdog_targets_actual_failure(running, heartbeat, row, source, db, starts, expected, restart):
    assert decision(now=SLOT, running=running, heartbeat_age=heartbeat, row_age=row,
        source_error=None, source_healthy=source, database_healthy=db, starts=starts) == (expected, restart)


def test_watchdog_auth_and_after_close_never_restart():
    args = dict(running=False, heartbeat_age=100, row_age=100, source_healthy=True, database_healthy=True, starts=0)
    assert decision(now=SLOT, source_error="LOGIN_REQUIRED", **args) == ("LOGIN_REQUIRED", False)
    assert decision(now=SLOT.replace(hour=18), source_error=None, **args) == ("MARKET_CLOSED", False)


def test_strict_source_aliases_zeros_and_endpoint_allowlist():
    async def case():
        adapter = StrictOpenAlgo(AdapterConfig(api_key="test-only"))
        adapter.expiry, adapter.prepared_date = "15SEP26", SLOT.date()
        options = {side.lower(): dict(symbol=f"NIFTY15SEP2623700{side}", ltp=50,
                    open_interest=100 if side == "CE" else 0) for side in ("CE", "PE")}
        async def request(endpoint, **_):
            if endpoint.endswith("quotes"):
                return {"status": "success", "data": {"ltp": 23700}}
            return {"status": "success", "atm_strike": 23700, "chain": [dict(strike=23700, **options)]}
        adapter.request_data = request
        class Clock:
            @staticmethod
            def now(tz):
                return SLOT.astimezone(tz)
        with patch("hermes_v0.collector.adapters.strict_openalgo.datetime", Clock):
            capture = await adapter.raw(SLOT)
            assert [o["oi"] for o in capture.data["body"]["options"]] == [100, None]
            options["ce"]["oi"] = 200
            with pytest.raises(IntegrityError, match="ConflictingOIAliases"):
                await adapter.raw(SLOT)
            del options["ce"]["oi"]
            options["ce"]["stale"] = True
            with pytest.raises(IntegrityError, match="StaleQuote"):
                await adapter.raw(SLOT)
        fresh_adapter = StrictOpenAlgo(AdapterConfig(api_key="test-only"))
        with pytest.raises(IntegrityError, match="ForbiddenEndpoint"):
            await fresh_adapter.request_data("/api/v1/placeorder")
        fresh_adapter._request_with_retry = AsyncMock(return_value={"status":"error","code":401,"message":"secret"})
        with pytest.raises(AuthenticationRequired):
            await fresh_adapter.request_data("/api/v1/quotes")
    asyncio.run(case())


def test_derived_identity_and_no_forward_price_invention():
    async def case():
        adapter = StrictOpenAlgo(AdapterConfig(api_key="test-only"))
        mismatch = [False]
        async def request(endpoint, **args):
            side = args["symbol"][-2:]
            return dict(status="success", symbol="WRONG" if mismatch[0] else args["symbol"],
                exchange="NFO", underlying="NIFTY", strike=23700, option_type=side,
                expiry_date="15-Sep-2026", spot_price=23710., option_price=51.,
                implied_volatility=15., interest_rate=6.5,
                greeks=dict(delta=.5 if side == "CE" else -.5,gamma=.001,theta=-1.,vega=3.,rho=.1))
        adapter.request_data = request
        class Clock(datetime):
            @classmethod
            def now(cls, tz):
                return (SLOT+timedelta(seconds=1)).astimezone(tz)
        with patch("hermes_v0.collector.adapters.strict_openalgo.datetime", Clock):
            records = await adapter.derived(raw())
            assert all(isinstance(r, Observation) for r in records)
            for record in records:
                assert record.data["body"]["values"]["forward_price"] is None
                assert record.data["body"]["values"]["spot_price"] == 23710.
            mismatch[0] = True
            assert all(isinstance(r, IntegrityError) for r in await adapter.derived(raw()))
    asyncio.run(case())


def test_calendar_and_expiry_fail_closed():
    async def case():
        adapter = StrictOpenAlgo(AdapterConfig(api_key="test-only"))
        windows = [dict(exchange=e, start_time=SLOT.replace(hour=9,minute=15).timestamp()*1000,
                        end_time=SLOT.replace(hour=15,minute=30).timestamp()*1000) for e in ("NSE", "NFO")]
        expiry = ["15-SEP-26"]
        async def request(endpoint, **args):
            return dict(status="success", data=windows if endpoint.endswith("timings") else expiry)
        adapter.request_data = request
        class Clock(datetime):
            @classmethod
            def now(cls, tz): return SLOT.astimezone(tz)
        with patch("hermes_v0.collector.adapters.strict_openalgo.datetime", Clock):
            await adapter.prepare()
            assert adapter.expiry == "15SEP26"
            adapter.prepared_date = None
            windows.clear()
            with pytest.raises(IntegrityError, match="MarketClosed"):
                await adapter.prepare()
            windows.extend([dict(exchange=e, start_time=SLOT.timestamp()*1000,
                end_time=(SLOT+timedelta(hours=1)).timestamp()*1000) for e in ("NSE", "NFO")])
            expiry.clear()
            with pytest.raises(IntegrityError, match="MissingExpiry"):
                await adapter.prepare()
    asyncio.run(case())


@pytest.mark.parametrize("change", [
    lambda b: b.update(exchange="NFO"),
    lambda b: b["candles"][0].update(high=1.),
    lambda b: b["candles"].append(dict(b["candles"][0])),
    lambda b: b["candles"][0].update(timestamp=(SLOT+timedelta(days=1)).isoformat()),
    lambda b: b["candles"][0].update(timestamp=(SLOT+timedelta(seconds=1)).isoformat()),
])
def test_history_rejects_mislabeled_or_malformed_candles(change):
    body = dict(symbol="NIFTY",exchange="NSE_INDEX",interval="5s",provenance="HISTORICAL_BACKFILL",
        candles=[dict(timestamp=SLOT.isoformat(),open=100.,high=101.,low=99.,close=100.,volume=None,oi=None)])
    change(body)
    with pytest.raises(IntegrityError):
        Observation.make("HISTORY", SLOT.replace(hour=0), SLOT, SLOT+timedelta(seconds=10), body)


def test_closed_session_cli_performs_no_provider_or_database_io():
    from argparse import Namespace
    from hermes_v0.collector.recovery import main_async
    args = Namespace(status=False, history_date=None, replay=False)
    with patch("hermes_v0.collector.recovery.session_time", return_value=False), \
         patch("hermes_v0.collector.recovery.StrictOpenAlgo", side_effect=AssertionError("must not construct provider")), \
         patch("hermes_v0.collector.recovery.RecoveryWriter", side_effect=AssertionError("must not construct writer")):
        assert asyncio.run(main_async(args)) == 0


def test_database_failure_retains_raw_and_recovery_replays_exactly(tmp_path):
    async def case():
        j = Journal(tmp_path / "spool.sqlite")
        j.put(raw())
        class Writer:
            def __init__(self):
                self.fail = True
                self.saved = []
            async def start(self):
                if self.fail:
                    raise ConnectionError()
            async def store(self, observation):
                self.saved.append(observation.payload)
            async def latest_raw(self):
                return SLOT+timedelta(seconds=.1)
        writer = Writer()
        service = RecoveryService(None, writer, j, status_path=tmp_path / "status.json")
        with pytest.raises(ConnectionError):
            await service.flush_once()
        assert len(list(j.pending())) == 1
        writer.fail = False
        await service.flush_once()
        await service.flush_once()
        assert writer.saved == [raw().payload]
        assert j.counts() == {"ACKNOWLEDGED": 1}
    asyncio.run(case())


def test_raw_is_durable_before_blocked_greeks_and_shutdown(tmp_path):
    async def case():
        clock = [SLOT]
        class Clock:
            @staticmethod
            def now(tz):
                return clock[0].astimezone(tz)
        j = Journal(tmp_path / "spool.sqlite")
        class Adapter:
            prepared_date = SLOT.date()
            async def start(self): pass
            async def stop(self): pass
            async def raw(self, expected): return raw(expected)
            async def derived(self, observation):
                assert any(r.key == observation.key for r in j.pending())
                await asyncio.Event().wait()
        class Writer:
            async def start(self): raise ConnectionError()
            async def stop(self): pass
        class Scheduler:
            interval_seconds = 5
            def stop(self): pass
            async def tick_generator(self):
                for n in range(2):
                    clock[0] = SLOT+timedelta(seconds=5*n)
                    yield clock[0], "2026-09-08"
        service = RecoveryService(Adapter(), Writer(), j, status_path=tmp_path / "status.json")
        with patch("hermes_v0.collector.recovery.datetime", Clock):
            state = await service.run(Scheduler(), cycles=2)
        assert state["captured"] == 2
        assert [r.data["kind"] for r in j.pending()] == ["RAW", "RAW"]
        assert json.loads((tmp_path / "status.json").read_text())["state"] == "STOPPED"
    asyncio.run(case())


@pytest.mark.parametrize("worker_mid_commit", [False, True])
def test_final_session_capture_and_greeks_are_saved_before_shutdown(tmp_path, worker_mid_commit):
    async def case():
        slot = SLOT.replace(hour=15, minute=29, second=55)
        capture = raw(slot)
        derived = Observation.make("DERIVED", slot, slot+timedelta(seconds=.2),
            slot+timedelta(seconds=.4), dict(
                symbol="NIFTY15SEP2623700CE", raw_key=capture.key, raw_digest=capture.digest,
                raw_received_at=capture.data["received_at"], expiry="15SEP26", strike=23700,
                side="CE", values=dict(iv=15.,delta=.5,gamma=.001,theta=-1.,vega=3.,rho=.1,
                    option_price=50.,forward_price=None,spot_price=23700.,interest_rate=6.5)))
        class Clock:
            @staticmethod
            def now(tz): return slot.astimezone(tz)
        journal = Journal(tmp_path / "spool.sqlite")
        raw_saved = asyncio.Event()
        class Adapter:
            prepared_date = slot.date()
            async def start(self): pass
            async def stop(self): pass
            async def raw(self, expected): return capture
            async def derived(self, observation):
                # The last optional calculation must not block persistence of raw data.
                await raw_saved.wait()
                return [derived]
        class Writer:
            def __init__(self):
                self.saved = []
                self.attempts = 0
                self.in_flight = asyncio.Event()
            async def start(self): pass
            async def stop(self): pass
            async def store(self, observation):
                self.attempts += 1
                # Model a committed write whose acknowledgment is interrupted.
                if observation.payload not in self.saved:
                    self.saved.append(observation.payload)
                if observation.data["kind"] == "RAW": raw_saved.set()
                if worker_mid_commit and self.attempts == 1:
                    self.in_flight.set()
                    await asyncio.Event().wait()
            async def latest_raw(self): return slot+timedelta(seconds=.1)
        class Scheduler:
            interval_seconds = 5
            def stop(self): pass
            async def tick_generator(self):
                yield slot, slot.date().isoformat()
                if worker_mid_commit:
                    await writer.in_flight.wait()
        writer = Writer()
        service = RecoveryService(Adapter(), writer, journal, status_path=tmp_path / "status.json")
        async def sleeping_worker():
            # Reproduce a worker between flushes when the final tick ends.
            await asyncio.Event().wait()
        if not worker_mid_commit:
            service.database_worker = sleeping_worker
        with patch("hermes_v0.collector.recovery.datetime", Clock):
            state = await asyncio.wait_for(service.run(Scheduler()), 3)
        assert writer.saved == [capture.payload, derived.payload]
        assert writer.attempts == (3 if worker_mid_commit else 2)
        assert journal.counts() == {"ACKNOWLEDGED": 2}
        assert state["state"] == "STOPPED"
        assert state["shutdown_flush"] == "COMPLETE"
    asyncio.run(case())


@pytest.mark.parametrize("now", [
    SLOT.replace(hour=9, minute=14, second=59),
    SLOT.replace(hour=15, minute=30),
    SLOT.replace(day=12),  # Saturday
    SLOT.replace(day=13),  # Sunday
])
def test_shutdown_does_not_flush_outside_weekday_session(tmp_path, now):
    class Clock:
        @staticmethod
        def now(tz): return now.astimezone(tz)
    journal = Journal(tmp_path / "spool.sqlite")
    capture = raw()
    journal.put(capture)
    writer = AsyncMock()
    service = RecoveryService(None, writer, journal, status_path=tmp_path / "status.json")
    with patch("hermes_v0.collector.recovery.datetime", Clock):
        asyncio.run(service.finish_pending())
    writer.start.assert_not_awaited()
    assert [r.payload for r in journal.pending()] == [capture.payload]
    assert service.state["shutdown_flush"] == "DEFERRED_MARKET_CLOSED"


def test_shutdown_deadline_cancels_write_and_keeps_raw_for_replay(tmp_path):
    async def case():
        now = SLOT.replace(hour=15, minute=29, second=59, microsecond=500000)
        class Clock:
            @staticmethod
            def now(tz): return now.astimezone(tz)
        journal = Journal(tmp_path / "spool.sqlite")
        capture = raw()
        journal.put(capture)
        cancelled = asyncio.Event()
        class Writer:
            async def start(self): pass
            async def store(self, observation):
                try:
                    await asyncio.Event().wait()
                finally:
                    cancelled.set()
        service = RecoveryService(None, Writer(), journal, status_path=tmp_path / "status.json")
        with patch("hermes_v0.collector.recovery.datetime", Clock):
            await asyncio.wait_for(service.finish_pending(), 1)
        assert cancelled.is_set()
        assert [r.payload for r in journal.pending()] == [capture.payload]
        assert service.state["shutdown_flush"] == "DEFERRED_DEADLINE"
    asyncio.run(case())


def test_shutdown_database_failure_preserves_pending_evidence(tmp_path):
    class Clock:
        @staticmethod
        def now(tz): return SLOT.astimezone(tz)
    journal = Journal(tmp_path / "spool.sqlite")
    capture = raw()
    journal.put(capture)
    writer = AsyncMock()
    writer.start.side_effect = ConnectionError()
    service = RecoveryService(None, writer, journal, status_path=tmp_path / "status.json")
    with patch("hermes_v0.collector.recovery.datetime", Clock):
        asyncio.run(service.finish_pending())
    assert [r.payload for r in journal.pending()] == [capture.payload]
    assert service.state["shutdown_flush"] == "DEFERRED_DATABASE_ERROR"
    assert service.state["database_error"] == "ConnectionError"

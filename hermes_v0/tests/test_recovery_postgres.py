"""Opt-in real PostgreSQL test, entirely inside a rollback-only isolated schema.

HERMES_RUN_DB_TESTS=1 PYTHONPATH=.. .venv/bin/python -m pytest -q tests/test_recovery_postgres.py
No test observation is inserted into public tables.
"""

import asyncio
from contextlib import asynccontextmanager
from datetime import timedelta
import os
from pathlib import Path
import uuid

import pytest

from hermes_v0.collector.integrity import Observation, IntegrityError, IST
from hermes_v0.storage.recovery_writer import RecoveryWriter
from hermes_v0.tests.test_integrity_recovery import raw, SLOT


@pytest.mark.skipif(os.environ.get("HERMES_RUN_DB_TESTS") != "1", reason="explicit isolated PostgreSQL test only")
def test_immutable_raw_replay_derived_and_history():
    async def case():
        import asyncpg
        c = await asyncpg.connect(host="127.0.0.1", database="hermes", user="postgres", timeout=3)
        outer = c.transaction()
        await outer.start()
        try:
            schema = "hermes_qa_" + uuid.uuid4().hex
            await c.execute(f'CREATE SCHEMA "{schema}"')
            await c.execute(f'SET LOCAL search_path TO "{schema}", public')
            for name in ("market_snapshot", "option_snapshot", "option_greeks_snapshot"):
                await c.execute(f'CREATE TABLE "{schema}".{name} (LIKE public.{name} INCLUDING ALL)')
            await c.execute((Path(__file__).parents[1] / "storage/migrations/20260908_integrity_recovery.sql").read_text())

            class Pool:
                @asynccontextmanager
                async def acquire(self):
                    yield c
            writer = RecoveryWriter()
            writer._pool = Pool()
            capture = raw()
            await writer.store(capture)
            await writer.store(capture)  # ambiguous-commit replay: no extra rows
            assert await c.fetchval("SELECT count(*) FROM option_snapshot") == 2
            assert await c.fetchval("SELECT count(*) FROM hermes_ingest_receipt") == 1
            assert await c.fetchval("SELECT count(*) FROM hermes_verified_options") == 0
            assert await c.fetchval("SELECT count(*) FROM hermes_observation_quality") == 2
            assert await c.fetchval("SELECT min(oi) FROM option_snapshot") == 100
            with pytest.raises(IntegrityError):
                await writer.store(raw(oi=200))
            assert await c.fetchval("SELECT min(oi) FROM option_snapshot") == 100

            for statement in ("UPDATE option_snapshot SET oi=0", "DELETE FROM option_snapshot", "TRUNCATE option_snapshot"):
                with pytest.raises(asyncpg.RaiseError):
                    async with c.transaction():
                        await c.execute(statement)
            assert await c.fetchval("SELECT count(*) FROM option_snapshot") == 2

            with pytest.raises(asyncpg.CheckViolationError):
                async with c.transaction():
                    await c.execute("""INSERT INTO market_snapshot
                        (timestamp_ist,symbol,trading_date,spot_ltp,data_status,version)
                        VALUES($1,'NIFTY',$2,23700,'VALID',1)""", SLOT, SLOT.date())

            for price in (None, 0., float("inf"), float("nan")):
                with pytest.raises(asyncpg.CheckViolationError):
                    async with c.transaction():
                        await c.execute("""INSERT INTO market_snapshot
                            (timestamp_ist,symbol,trading_date,spot_ltp,data_status,version)
                            VALUES($1,'NIFTY',$2,$3,'PARTIAL',2)""",
                            SLOT+timedelta(minutes=1), SLOT.date(), price)
                with pytest.raises(asyncpg.CheckViolationError):
                    async with c.transaction():
                        await c.execute("""INSERT INTO option_snapshot
                            (timestamp_ist,symbol,strike,option_type,expiry_date,trading_date,ltp,data_status,version)
                            VALUES($1,'NIFTY',23700,'CE','15SEP26',$2,$3,'PARTIAL',2)""",
                            SLOT+timedelta(minutes=1), SLOT.date(), price)

            derived = Observation.make("DERIVED", SLOT, SLOT+timedelta(seconds=.2), SLOT+timedelta(seconds=.4), dict(
                symbol="NIFTY15SEP2623700CE", raw_key=capture.key, raw_digest=capture.digest,
                raw_received_at=capture.data["received_at"], expiry="15SEP26",strike=23700,side="CE",
                values=dict(iv=15.,delta=.5,gamma=.001,theta=-1.,vega=3.,rho=.1,
                            option_price=51.,forward_price=None,spot_price=23710.,interest_rate=6.5)))
            await writer.store(derived)
            result = await c.fetchrow("SELECT * FROM hermes_derived_quality")
            assert result["option_ltp"] == 50.
            assert result["calculation_option_ltp"] == 51.
            assert result["calculation_spot_ltp"] == 23710.
            assert result["forward_price"] is None
            assert result["calculation_received_at"] > result["timestamp_ist"]
            history = Observation.make("HISTORY", SLOT.replace(hour=0), SLOT, SLOT+timedelta(seconds=10), dict(
                symbol="NIFTY",exchange="NSE_INDEX",interval="5s",provenance="HISTORICAL_BACKFILL",
                candles=[dict(timestamp=SLOT.isoformat(),open=23700.,high=23701.,low=23699.,close=23700.,volume=None,oi=None)]))
            await writer.store(history)
            assert await c.fetchval("SELECT count(*) FROM hermes_history_backfill") == 1
            assert await c.fetchval("SELECT count(*) FROM market_snapshot") == 1
        finally:
            await outer.rollback()
            await c.close()
    asyncio.run(case())

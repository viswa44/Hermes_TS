"""Append raw receipts first; independently persist derived and historical evidence."""

from datetime import datetime, timezone
from pathlib import Path
import json

from hermes_v0.collector.integrity import IntegrityError, instant, IST
from hermes_v0.storage.option_metrics_writer import OptionMetricsWriter


class RecoveryWriter(OptionMetricsWriter):
    async def start(self):
        if getattr(self, "_recovery_ready", False) and self._pool is not None:
            return
        await super().start()
        async with self._pool.acquire() as connection:
            await connection.execute((Path(__file__).parent / "migrations/20260908_integrity_recovery.sql").read_text())
        self._recovery_ready = True

    async def stop(self):
        self._recovery_ready = False
        await super().stop()

    async def store(self, observation):
        observation.validate()
        d, b = observation.data, observation.data["body"]
        received, started = instant(d["received_at"]), instant(d["started_at"])
        if received > datetime.now(timezone.utc):
            raise IntegrityError("FutureReceipt")
        raw = d["kind"] == "RAW"
        async with self._pool.acquire() as c:
            async with c.transaction():
                saved = await c.fetchval("""INSERT INTO hermes_ingest_receipt
                    (observation_id,digest,kind,symbol,scheduled_at,request_started_at,received_at,
                     timestamp_basis,freshness,issues,parent_observation_id)
                    VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10::jsonb,$11)
                    ON CONFLICT(observation_id) DO NOTHING RETURNING observation_id""",
                    observation.key, observation.digest, d["kind"], b["symbol"], instant(d["scheduled_at"]),
                    started, received, "APPLICATION_RECEIPT" if raw else "CALCULATION_RECEIPT" if d["kind"] == "DERIVED" else "CANDLE_START",
                    b.get("freshness", "DERIVED_UNVERIFIED_INPUT_TIME" if d["kind"] == "DERIVED" else "HISTORICAL_BACKFILL"),
                    json.dumps(b.get("issues", [])), b.get("raw_key"))
                if saved is None:
                    if await c.fetchval("SELECT digest FROM hermes_ingest_receipt WHERE observation_id=$1", observation.key) != observation.digest:
                        raise IntegrityError("ConflictingDatabaseObservation")
                    return
                if raw:
                    day = received.astimezone(IST).date()
                    latency = int((received-started).total_seconds()*1000)
                    # PARTIAL means provider-event freshness is not verified. Receipt time
                    # is never presented as the provider's observation timestamp.
                    await c.execute("""INSERT INTO market_snapshot
                        (timestamp_ist,symbol,trading_date,spot_ltp,atm_strike,expiry_date,
                         data_status,source_latency_ms,ingestion_time,version)
                        VALUES($1,$2,$3,$4,$5,$6,'PARTIAL',$7,$1,2)""",
                        received,b["symbol"],day,b["spot_ltp"],b["strike"],b["expiry"],latency)
                    for o in b["options"]:
                        await c.execute("""INSERT INTO option_snapshot
                            (timestamp_ist,symbol,strike,option_type,expiry_date,trading_date,
                             ltp,bid,ask,volume,oi,data_status,source_latency_ms,ingestion_time,version)
                            VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,'PARTIAL',$12,$1,2)""",
                            received,b["symbol"],b["strike"],o["side"],b["expiry"],day,
                            o["ltp"],o["bid"],o["ask"],o["volume"],o["oi"],latency)
                elif d["kind"] == "DERIVED":
                    if await c.fetchval("SELECT digest FROM hermes_ingest_receipt WHERE observation_id=$1 AND kind='RAW'", b["raw_key"]) != b["raw_digest"]:
                        raise IntegrityError("MissingOrConflictingRawParent")
                    raw_time, v = instant(b["raw_received_at"]), b["values"]
                    if await c.fetchval("SELECT received_at FROM hermes_ingest_receipt WHERE observation_id=$1", b["raw_key"]) != raw_time:
                        raise IntegrityError("RawParentTimestampMismatch")
                    parent = await c.fetchrow("""SELECT o.ltp,m.spot_ltp FROM option_snapshot o
                        JOIN market_snapshot m USING(timestamp_ist,symbol)
                        WHERE o.timestamp_ist=$1 AND o.symbol='NIFTY' AND o.strike=$2
                        AND o.expiry_date=$3 AND o.option_type=$4""", raw_time,b["strike"],b["expiry"],b["side"])
                    if not parent:
                        raise IntegrityError("MissingRawParent")
                    await c.execute("""INSERT INTO option_greeks_snapshot
                        (timestamp_ist,underlying_symbol,option_symbol,strike,option_type,expiry_date,trading_date,
                         option_ltp,underlying_ltp,calculation_option_ltp,implied_volatility,delta,gamma,theta,vega,rho,
                         interest_rate,forward_price,calculation_method,data_status,ingestion_time,version,calculation_spot_ltp)
                        VALUES($1,'NIFTY',$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,
                               'OPENALGO_BLACK76','PARTIAL',$18,2,$19)""",
                        raw_time,b["symbol"],b["strike"],b["side"],b["expiry"],raw_time.astimezone(IST).date(),
                        parent["ltp"],parent["spot_ltp"],v["option_price"],v["iv"],v["delta"],v["gamma"],v["theta"],
                        v["vega"],v["rho"],v["interest_rate"],v["forward_price"],received,v["spot_price"])
                else:
                    await c.execute("INSERT INTO hermes_history_backfill VALUES($1,$2::jsonb,'HISTORICAL_BACKFILL')",
                                    observation.key,observation.payload)

    async def latest_raw(self):
        async with self._pool.acquire() as c:
            return await c.fetchval("SELECT max(received_at) FROM hermes_ingest_receipt WHERE kind='RAW'")

# V0-002A Static Provider Verification Report

- **task_id:** V0-002A
- **agent_id:** DATA-001
- **date:** 2026-08-15 Asia/Kolkata
- **scope:** Static source and local documentation inspection only. No endpoint was called and no market-session data was observed.
- **status:** STATIC_HANDOFF_READY
- **live_validation:** REQUIRED
- **next_task:** V0-002B
- **V0-002B status:** BLOCKED_UNTIL_MARKET_OPEN

## Classification rules used

- **DOCUMENTED_AVAILABLE:** endpoint or response field is described in local OpenAlgo documentation/source. This is not a claim that it has worked for the configured account, broker, symbol, or market session.
- **DERIVED:** calculated by OpenAlgo or Hermes rather than supplied directly as a provider observation.
- **DOCUMENTED_UNAVAILABLE:** omitted from the documented/current normalized response shape for the relevant endpoint.
- **UNCERTAIN:** source/docs cannot establish availability, semantics, units, or broker-specific behaviour.
- **LIVE_VERIFICATION_REQUIRED:** static evidence exists, but a live market-session observation is necessary before treating the field as verified available.

No field is classified `VERIFIED_AVAILABLE` in this report.

## Evidence inspected

- Hermes: `collector/adapters/openalgo_adapter.py`, `domain/models.py`, `source_of_truth/DATA_CONTRACT.md`.
- Local OpenAlgo REST implementation and schemas: `restx_api/quotes.py`, `option_chain.py`, `option_greeks.py`, `history.py`, `data_schemas.py`, and their services.
- Local OpenAlgo API docs: `docs/api/market-data/quotes.md`, `history.md`, `docs/api/options-services/optionchain.md`, `optiongreeks.md`.
- Local WebSocket documentation/implementation: `docs/websocket-quote-feed.md`, `websocket_proxy/server.py`.
- Active adapter documentation identifies Fyers; its local provider integration was inspected at `broker/fyers/api/data.py` and `rate_limiter.py`.

## Static endpoint and authentication findings

| Capability | Classification | Static finding |
| --- | --- | --- |
| `POST /api/v1/quotes` | DOCUMENTED_AVAILABLE; LIVE_VERIFICATION_REQUIRED | Requires JSON `apikey`, `symbol`, `exchange`. Docs list OHLC, LTP, bid, ask, previous close, volume. |
| `POST /api/v1/optionchain` | DOCUMENTED_AVAILABLE; LIVE_VERIFICATION_REQUIRED | Requires `apikey`, `underlying`, `exchange`, `expiry_date`; `strike_count` is optional. Current service defaults to live quotes and returns a CE/PE chain. |
| `POST /api/v1/optiongreeks` | DOCUMENTED_AVAILABLE; LIVE_VERIFICATION_REQUIRED | Requires `apikey`, option `symbol`, and derivative exchange. It calculates Black-76 IV and Greeks; it is not a raw broker-Greeks feed. |
| REST authentication | DOCUMENTED_AVAILABLE; LIVE_VERIFICATION_REQUIRED | API key is mandatory. Quotes and option chain resolve the key to the configured OpenAlgo broker token; option Greeks explicitly verifies the key. A valid key alone does not statically prove an active broker session or data entitlement. |
| WebSocket quote feed | DOCUMENTED_AVAILABLE; LIVE_VERIFICATION_REQUIRED | Local OpenAlgo server defaults to port 8765. Client sends `authenticate` with `api_key`, then `subscribe` with symbols/exchanges and `LTP`, `QUOTE`, or `DEPTH`. |
| Historical `POST /api/v1/history` | DOCUMENTED_AVAILABLE; LIVE_VERIFICATION_REQUIRED | Documented OHLCV history with broker-dependent availability and intervals; source may also read local Historify data. It does not document historical chain, bid/ask, IV, or Greeks. |
| Rate limits | DOCUMENTED_AVAILABLE; LIVE_VERIFICATION_REQUIRED | Local REST defaults: quotes/option-chain use `API_RATE_LIMIT` default `10 per second`; Greeks uses `GREEKS_RATE_LIMIT` default `30 per minute`. Fyers integration documents a shared 10 req/s, 200/min, 100,000/day cap and locally paces about 8 req/s. Actual deployment environment variables and broker enforcement remain unobserved. |

## Documented field comparison with the Hermes V0 contract

| Hermes need | Classification | Static result and boundary |
| --- | --- | --- |
| Spot LTP, OHLC, bid, ask, previous close, volume | DOCUMENTED_AVAILABLE; LIVE_VERIFICATION_REQUIRED | Quote documentation exposes these normalized fields. `NIFTY` on `NSE_INDEX` and the configured account/broker must still be observed live. |
| India VIX LTP and previous close | UNCERTAIN; LIVE_VERIFICATION_REQUIRED | Hermes assumes symbol `INDIA VIX` on `NSE_INDEX`. The generic quote endpoint is documented, but local docs provide no India-VIX symbol/exchange or entitlement confirmation. |
| Option-chain strike, symbol, label, LTP, bid, ask, OHLC, previous close, volume, OI, lot size, tick size | DOCUMENTED_AVAILABLE; LIVE_VERIFICATION_REQUIRED | These are documented/current normalized option-chain fields. The Fyers fast path supplies LTP, bid/ask, volume and OI, but source defaults missing values to `0`; zero cannot be treated as a confirmed observed value. |
| Option bid/ask quantities | UNCERTAIN; LIVE_VERIFICATION_REQUIRED | Current option-chain service emits `bid_qty`/`ask_qty`, but published option-chain field documentation omits them and broker mappings vary. |
| Option-chain IV | DOCUMENTED_UNAVAILABLE | Neither the published option-chain schema nor current `option_chain_service` response construction contains an `iv` member. Hermes attempts `ce.get("iv")`/`pe.get("iv")`; this does not establish provider availability. |
| IV from `optiongreeks` | DERIVED; LIVE_VERIFICATION_REQUIRED | OpenAlgo computes implied volatility from current option price, forward/spot, rate and time using Black-76. It is not provider-supplied IV and its calculation inputs/results need live validation. |
| Delta, gamma, theta, vega, rho | DERIVED; LIVE_VERIFICATION_REQUIRED | `optiongreeks` calculates, rather than relays, these values. Hermes persists four Greeks; rho is requested/extracted but has no contract field. |
| OI change | DERIVED | No direct OI-change field is documented for the endpoints Hermes uses. It can only be calculated from separately timestamped OI observations after raw-field quality is established. |
| Provider event timestamp in REST quotes/chain/Greeks | DOCUMENTED_UNAVAILABLE | The documented REST quote and option-chain response shapes contain no provider timestamp. Hermes uses its collector clock and stores total request latency; these are application measurements, not provider event time. |
| Expiry | DOCUMENTED_AVAILABLE; LIVE_VERIFICATION_REQUIRED | Option-chain requires/returns `expiry_date`. Hermes does not use the dedicated expiry endpoint; it probes dates then applies a Thursday fallback. Correct weekly/monthly expiry and holiday behaviour require live verification. |
| Historical OHLCV/OI | DOCUMENTED_AVAILABLE; LIVE_VERIFICATION_REQUIRED | History is documented as OHLCV and source ensures an `oi` column by inserting zero if absent. Thus non-null historical OI is not proof of broker-provided OI. |
| Historical bid/ask, bid/ask quantity, option chain, IV, Greeks | DOCUMENTED_UNAVAILABLE | Not part of documented `/history` OHLCV response. |
| WebSocket LTP, quote, and depth messages | DOCUMENTED_AVAILABLE; LIVE_VERIFICATION_REQUIRED | Documentation specifies mode payloads and timestamps. Actual field completeness, symbol support, connection stability, and broker mode support depend on the active broker and session. |

## Hermes assumptions identified

1. OpenAlgo is reachable at `OPENALGO_HOST` (default `http://127.0.0.1:5000`) and accepts the supplied API key in a JSON body.
2. `NIFTY` is valid on `NSE_INDEX`; `INDIA VIX` is likewise valid and entitled on that exchange.
3. The configured OpenAlgo account is connected to Fyers and has live quote, option-chain, and required-market-data permissions.
4. A valid OpenAlgo API key implies usable broker/session credentials. Static source shows this is an additional runtime dependency, not a guarantee.
5. The next valid NIFTY weekly expiry can be found by probing the next ten calendar dates; otherwise it is the next Thursday. This ignores exchange-holiday and changed-expiry conventions.
6. ATM is the Hermes rounding of spot to a fixed 50-point interval, rather than the `atm_strike` returned by OpenAlgo.
7. Generated symbols `NIFTY{DDMMMYY}{strike}CE/PE` exactly match the master contract and broker symbology.
8. The option chain has a non-null CE and PE at Hermes' calculated ATM; the adapter dereferences those objects.
9. A REST option-chain response has live quotes. Source defaults missing option values to zero, so the adapter can record a zero that is absence/synthetic fallback rather than an observed quote.
10. Option-chain `bid_qty` and `ask_qty` use the names Hermes expects. Documentation does not establish this.
11. Option-chain `iv` exists. Static evidence contradicts this: it is absent from the documented/current response construction.
12. Greeks/IV returned by `/optiongreeks` are appropriate to store in raw-observation fields. Static evidence says they are OpenAlgo-calculated Black-76 outputs, not provider raw fields.
13. Passing spot LTP as `forward_price` produces a semantically acceptable Greeks/IV calculation. The endpoint calls this an optional forward/synthetic-futures price; equivalence to spot is not documented.
14. Five REST calls per successful collection cycle (two expiry probes at most are additional; normal collection uses spot, VIX, chain, CE Greeks, PE Greeks) fit all OpenAlgo and broker quotas alongside other application traffic.
15. Application receipt time, total request latency, and a 5-second scheduler are sufficient substitutes for a provider event timestamp. They are not equivalent.

## LIVE_VERIFICATION_REQUIRED — V0-002B acceptance list

Perform only during an open NIFTY market session, using an authorized non-trading verification workflow. Record redacted request/response evidence, response time, OpenAlgo version, active broker, and entitlement context.

1. Authenticate an API key and demonstrate a live broker session without exposing secrets.
2. Obtain NIFTY `NSE_INDEX` and India VIX quote responses; confirm exact symbol/exchange, LTP, bid/ask, OHLC, previous close, volume, null/zero behaviour, and freshness.
3. Obtain a valid current-expiry NIFTY option chain; confirm expiry, ATM convention, CE/PE object presence, strike range, LTP, bid/ask, quantities, volume, OI, lot size, and tick size.
4. Establish whether every zero-valued option-chain field is an actual market value, provider omission, or OpenAlgo default.
5. Confirm no option-chain IV is silently expected; separately validate calculated IV/Greeks responses and inputs, including the impact of Hermes passing spot as `forward_price`.
6. Confirm contract-symbol construction for ATM CE/PE against option-chain-returned symbols.
7. Measure endpoint response times, errors, and quota behaviour at the intended 5-second cadence while accounting for all concurrent OpenAlgo/Fyers traffic. Do not deliberately breach limits.
8. Connect to WebSocket, authenticate, subscribe to NIFTY/index and option contracts in each intended mode, and verify tick delivery, timestamp semantics/time zone, reconnect/resubscribe, field completeness, and depth support.
9. Verify historical data separately by symbol/exchange/interval, range, provider versus local-Historify source, timestamps, and whether OI was returned or synthesized as zero.
10. Confirm exchange holiday/expiry handling; do not infer field unavailability from a non-trading-day response.

## Determination

The local OpenAlgo checkout statically documents the required REST and WebSocket interfaces, but it does not prove that any field is available in the deployed live broker session. The most material contract mismatch is that Hermes treats option-chain IV as a potential raw field, while the current documented/source response does not provide it; the available IV and Greeks endpoint is a calculation service. All live-field availability, semantics, timestamp/freshness, broker entitlements, quotas, websocket operation, and historical coverage remain for V0-002B.

**STATUS:** STATIC_HANDOFF_READY

**LIVE_VALIDATION:** REQUIRED

**NEXT_TASK:** V0-002B

**V0-002B:** BLOCKED_UNTIL_MARKET_OPEN

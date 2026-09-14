# Data cleaning agent

Standalone LangChain + Mistral pipeline for flat CSV, JSON, JSONL and Parquet option observations. It produces two joined Parquet tables, a cleaning plan, a quality report, and quarantine evidence. A passing run can be published to an existing AWS S3 bucket.

This project has its own environment. The weekday job reads completed sessions from Hermes PostgreSQL using a read-only transaction, cleans the observations, and publishes both tables to S3. It does not modify PostgreSQL observations.

## Automatic PostgreSQL workflow

The macOS LaunchAgent `com.openalgo.data-cleaning-agent` runs Monday–Friday at **15:45 IST**, after the 15:30 collector close. It also checks at login and every 30 minutes for missed or failed runs. The shared market gateway blocks weekends, NSE F&O holidays, and missing or stale calendar evidence. Scheduled cleaning, including historical catch-up, starts only at or after 15:45 on an open weekday. macOS calendar triggers use the machine timezone; this Mac uses Asia/Kolkata.

The fixed PostgreSQL schema uses `DAILY_PLANNER=deterministic` by default. `DAILY_PLANNER=mistral` enables the existing LangChain planner; its key must be in the environment, ignored `.env`, or the configured macOS Keychain item. There is no silent planner fallback.

The job joins `option_snapshot` with the matching `market_snapshot` by timestamp and underlying, checks version-2 RAW receipt linkage, and preserves source receipt IDs, freshness labels and source versions in the observation table. Every export uses PostgreSQL `READ ONLY`, `REPEATABLE READ`, a statement timeout, and a fixed UTC session timezone. Source tables are never changed.

The default catch-up window is the last 30 calendar days. Set `DAILY_START_DATE=YYYY-MM-DD` to cover a longer history. The official calendar is checked for both the execution date and each source date. Holidays and days without observations are skipped; unknown calendar years remain blocked. Missing prices are never invented. Reports record receipt coverage and missing five-second slots separately from cleaning validity. Legacy rows retain unverified timestamp labels.

Each completed date/source/code/configuration revision is committed once. The revision includes source rows and source integrity/coverage evidence. If late receipts arrive, a new immutable revision is published. A restart validates an existing S3 commit before reusing it. Upload failures remain retryable; deterministic quarantine waits for source or code/configuration changes. One local process lock prevents overlapping jobs.

The existing RAW tables do not provide verified IV. The PostgreSQL adapter leaves IV and Greeks null and retains any legacy raw IV in local source evidence. It does not mix in separate `option_greeks_snapshot` calculations. Days to expiry is calculated for every accepted row.

From the workspace directory:

```bash
# Read the last check without connecting to PostgreSQL or AWS.
data_cleaning_agent/.venv/bin/python -m data_cleaning_agent.daily --status

# Clean one completed weekday locally, with no AWS calls.
data_cleaning_agent/.venv/bin/python -m data_cleaning_agent.daily --date 2026-09-11 --local-only

# Publish or resume one completed weekday.
data_cleaning_agent/.venv/bin/python -m data_cleaning_agent.daily --date 2026-09-11

# Catch up all available completed weekdays in the configured window.
data_cleaning_agent/.venv/bin/python -m data_cleaning_agent.daily

# Install/update only this project's LaunchAgent.
data_cleaning_agent/.venv/bin/python -m data_cleaning_agent.automation.install_launchagent --install
```

The installed service is `~/Library/LaunchAgents/com.openalgo.data-cleaning-agent.plist`. Operational files are local and ignored by Git: `runtime/progress.json` records publication per date; `runtime/last_status.json` records the latest check; `runtime/weekday.out.log` and `runtime/weekday.err.log` contain scheduler output. The Mac must be running with this user logged in and PostgreSQL available. Missed work is retried after login/wake; no job runs while the Mac is powered off.

Daily AWS output is in the private bucket `heremesv0-cleaned-data` in **us-east-1**, the AWS profile's configured region:

```text
s3://heremesv0-cleaned-data/cleaned/YYYY-MM-DD/
├── <run-uuid>/observations.parquet
├── <run-uuid>/options.parquet
├── <run-uuid>/quality_report.json
├── <run-uuid>/manifest.json
└── commits/<revision-sha256>.json
```

Consumers should use the dated commit records to discover successful revisions. Old revisions remain available; select the desired source revision or the newest successful commit for a day. Local snapshots and quarantine remain under `output/postgres/YYYY-MM-DD/`.

## Start locally

Python 3.12 or newer and `uv` (tested with Python 3.13):

```bash
cd /Users/viswatej/Desktop/openalgo/data_cleaning_agent
uv venv .venv --python python3
uv pip install --python .venv/bin/python -r requirements.lock
cp .env.example .env
```

Set a **rotated** `MISTRAL_API_KEY` in your local `.env`, or supply it through your process environment. The key shared in chat is not stored in this project. `.env` is ignored by Git. AWS authentication uses boto3's normal IAM role, profile/SSO, or environment credential chain.

Manual file and daily commands also require an open market day with a fresh calendar. Status commands and offline tests remain available on holidays. See the [market gateway setup](../market_calendar/README.md) and [operations dashboard](../operations_dashboard/README.md).

On an open market day, run the included synthetic sample without provider or AWS calls:

```bash
.venv/bin/python main.py examples/sample_options.csv --planner deterministic
```

Run Mistral planning on your file:

```bash
.venv/bin/python main.py /absolute/path/options.csv
```

The default planner uses LangChain `ChatMistralAI.with_structured_output(CleaningPlan)`. It receives column names, dtypes and counts; record values remain local. Its mappings must exactly match the deterministic schema detector. It may choose whether to remove exact duplicates and explain its plan. Core validation is mandatory; the model cannot supply code, fill missing values, change formulas or bypass quarantine. Model failure produces a failed run with evidence, without silently switching planners. See the [LangChain Mistral integration](https://docs.langchain.com/oss/python/integrations/chat/mistralai).

## The two tables

Both tables have one row per accepted option observation and join on `observation_id`. Both retain `timestamps`, `underlying`, `symbol`, and `exchange`. Output timestamps use UTC. `source_row` in table 1 is the zero-based input row position, excluding the header.

| Output | Requested fields | Meaning |
| --- | --- | --- |
| `observations.parquet` | `timestamps`, `spot`, `iv`, `volume` | Supplied observations. IV is normalized to a decimal fraction. Volume is option-contract volume. |
| `options.parquet` | `oi`, `ltp`, `strike`, `optiontype`, `expirydate`, `daystoexpiry`, `timestamps`, `delta`, `theta`, `gamma`, `vega` | OI, LTP and contract specifications are supplied; days to expiry and enabled Greeks are calculated. |

Table 1 also retains timestamp provenance and data-status labels. Table 2 contains `greeks_source` and `derivation_status`. Unknown input columns and previously supplied Greeks are reported, retained in the source snapshot, and excluded from these output tables.

IV is mathematically a derived quantity. Here, table 1 treats **supplied IV as an input observation**. This implementation does not solve IV from option prices. Missing IV leaves calculated Greeks null; it never substitutes zero. Option IV and volume remain attached to their observation IDs rather than being combined across CE/PE or strikes.

## Input contract and cleaning

Use a flat record per option with at least `timestamps`, `spot`, `strike`, `optiontype`, and `expirydate`. Include `underlying` and/or a full option `symbol` for mixed-instrument files. Missing identifiers remain missing; rows sharing an otherwise indistinguishable natural key are quarantined.

```csv
timestamps,underlying,spot,iv,volume,oi,ltp,strike,optiontype,expirydate
2026-09-10T10:00:00+05:30,NIFTY,25010,0.20,1200,25000,180.5,25000,CE,2026-09-15
```

- Header whitespace, spaces, underscores and hyphens are normalized for known aliases. Examples: `timestamp`/`timestaps`, `spot_price`, `open_interest`, `last_price`, `strike_price`, `option_type`, `expiry_date`.
- Alias collisions such as both `oi` and `open_interest` fail the file. The agent never guesses which conflicting field is correct.
- CE/CALL/C and PE/PUT/P are normalized; other option types are quarantined.
- Missing optional `oi`, `ltp`, `iv`, and `volume` remain null. Blank CSV fields and JSON null are missing. Malformed numeric text, infinities, negative prices/counts, fractional OI/volume and missing required values are quarantined. No imputation, forward fill or range clipping occurs.
- Exact input duplicates may be removed and counted. Remaining conflicting natural keys quarantine every affected row; no keep-last rule is used.
- Use ISO timestamps. Naive timestamps use `INPUT_TIMEZONE`; numeric Unix timestamps require `TIMESTAMP_UNIT`. Ambiguous local times fail.
- Date-only expiries use `EXPIRY_TIME` in `EXPIRY_TIMEZONE`; defaults are 15:30 in Asia/Kolkata. Full ISO expiry timestamps keep their supplied time. `DD-MMM-YYYY` and Hermes `DDMMMYY` are supported; the latter means 2000–2099. Expiry before observation is invalid. Exchange calendars are not inferred.
- `IV_UNIT=decimal` means `0.20 = 20%`; `IV_UNIT=percent` means `20 = 20%`. Select the actual provider convention explicitly.
- `timestamp_source`, `provider_timestamp`, and `data_status` preserve supplied provenance. Missing provenance is unspecified. PASS certifies the configured cleaning checks; it does not certify market freshness or upgrade a `PARTIAL` receipt to verified provider data.

The entire input is snapshotted locally and hashed before processing. File-level parser, schema or planner failures retain that snapshot as quarantine. This is an in-memory batch tool with default limits of 100 MiB input/expanded Parquet data, one million rows, and 256 columns.

## Derived fields

`daystoexpiry = (expiry_utc - observation_utc).total_seconds() / 86400`. It is fractional calendar time, not an integer count of trading sessions.

Greeks are disabled by default. To enable the European Black-Scholes-Merton spot model, explicitly set these values in `.env` using your own assumptions:

```dotenv
DERIVE_GREEKS=true
RISK_FREE_RATE=0.06
DIVIDEND_YIELD=0.00
IV_UNIT=decimal
```

The rates above are examples, not fetched market rates. Calculations require positive supplied spot, strike, IV and time to expiry. They use ACT/365, continuously compounded annual rates, theta per calendar day and vega per one volatility percentage point. Missing inputs leave all Greeks null with a reason. Assumptions are stored in the manifest. [Formula and unit reference](https://vollib.org/documentation/1.0.3/autoapi/py_vollib/ref_python/black_scholes_merton/greeks/analytical/index.html).

OpenAlgo's current option Greeks use Black-76 with forward inputs, and its supplied IV is in percent. These calculations are a separate model; do not label them as OpenAlgo-derived Greeks. A raw Hermes export has no verified IV, so it will produce null IV/Greeks. The daily PostgreSQL adapter joins raw option records with matching market snapshots to supply spot; standalone file mode expects spot in the supplied file.

## Quality and S3

The default `MAX_QUARANTINE_FRACTION=0` fails a batch containing any rejected row. Failed batches keep candidate tables inside a local `quarantine/` directory and cannot be uploaded. If you deliberately configure a larger tolerance, only accepted rows enter clean tables, and rejected rows remain in `quarantine.jsonl`. A run with zero accepted rows always fails.

The requested bucket `heremesv0-cleaned_data` contains an underscore, which [AWS disallows in bucket names](https://docs.aws.amazon.com/AmazonS3/latest/userguide/bucketnamingrules.html). The configured bucket is `heremesv0-cleaned-data`, created for this workflow with all public access blocked and AES256 encryption.

```bash
.venv/bin/python main.py /absolute/path/options.csv --upload --bucket heremesv0-cleaned-data
```

Passing runs publish to:

```text
s3://heremesv0-cleaned-data/cleaned/<run-uuid>/
├── observations.parquet
├── options.parquet
├── quality_report.json
└── manifest.json
```

S3 holds Parquet objects representing the two tables; this does not provision PostgreSQL tables, Athena/Glue catalogs, or S3 Tables/Iceberg resources. Uploads require an existing bucket and write permissions. They use AES256 encryption, SHA-256 checksums, and conditional writes that reject existing objects. The manifest is written last as a commit marker; consumers must require it and verify table checksums. An interrupted upload can leave objects without a manifest; rerun to create a fresh run UUID. [AWS conditional PUT behavior](https://docs.aws.amazon.com/AmazonS3/latest/API/API_PutObject.html).

Source snapshots, row-level quarantine and planning evidence stay local. The local manifest lists all local artifacts. The S3 manifest lists published files under `artifacts` and other checksum records under `local_evidence` with scope `local_only`. Only the four objects above are published. Provider/SDK exception bodies and secrets are excluded from CLI upload errors.

## Inspect and test

The CLI prints the run directory. Inspect both tables with that directory:

```bash
.venv/bin/python - /absolute/path/output/run-uuid <<'PY'
import sys
from pathlib import Path
import pandas as pd
run = Path(sys.argv[1])
for table in ('observations', 'options'):
    print(table)
    print(pd.read_parquet(run / f'{table}.parquet').to_string(index=False))
PY

.venv/bin/python -m pytest tests -q
```

Exit codes: `0` quality PASS; `1` startup/configuration error; `2` quality or processing FAIL; `3` S3 upload failure after local PASS. Inspect `quality_report.json` for failure evidence.

Tests use synthetic data, fake Mistral responses and fake S3 clients. They do not establish live Mistral authentication, AWS access or provider-data quality.

## Module ownership

| Module | Work |
| --- | --- |
| `agent/cleaning_agent.py` | Read/snapshot input; orchestrate profiling, planning, cleaning and output |
| `agent/prompts.py`, `agent/schemas.py` | Planner constraints and recognized input contract |
| `models/cleaning_plan.py` | Pydantic plan; rejects extra operations and invalid policies |
| `tools/profiler.py` | Value-free counts and dtypes |
| `tools/schema_detector.py` | Known aliases and plan mapping verification |
| `tools/cleaner.py` | Deterministic normalization, duplicate handling, split and optional Greeks |
| `tools/validator.py` | Types, ranges, cross-table identity, dates and derived consistency |
| `tools/s3_tool.py` | Checksum/quality gate and immutable S3 publication |
| `tools/postgres_tool.py` | Read-only daily joins, source evidence and receipt coverage |
| `tools/daily_s3.py` | Immutable date/revision commits and verified remote reuse |
| `daily.py`, `automation/` | Weekday processing, catch-up, retries, local state and macOS schedule |
| `config/settings.py` | Explicit units, timezone, thresholds and environment secrets |
| `main.py` | CLI and exit codes |

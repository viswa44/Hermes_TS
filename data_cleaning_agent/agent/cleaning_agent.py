"""Profile -> schema -> constrained Mistral plan -> tools -> quality gate."""

import csv
import hashlib
import json
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal
from uuid import uuid4

import pandas as pd

from ..config.settings import Settings
from ..models.cleaning_plan import CleaningPlan
from ..tools.cleaner import clean_data
from ..tools.profiler import profile_data
from ..tools.schema_detector import detect_schema, validate_plan
from ..tools.validator import validate_tables
from .prompts import SYSTEM_PROMPT


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def _json_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Duplicate JSON key')
        result[key] = value
    return result


def _invalid_json_constant(value):
    raise ValueError('JSON contains a nonfinite numeric literal; use null for missing data')


def load_data(path: Path, settings: Settings) -> pd.DataFrame:
    """Read bounded local flat records without pandas silently mangling headers."""
    suffix = path.suffix.lower()
    if path.stat().st_size > settings.max_input_mb * 1024 * 1024:
        raise ValueError('Input exceeds MAX_INPUT_MB')
    if suffix == '.csv':
        with path.open(encoding='utf-8-sig', newline='') as stream:
            reader = csv.reader(stream)
            header = next(reader, [])
            if not header or len(header) != len(set(header)):
                raise ValueError('CSV requires unique nonempty column names')
            records = []
            for row in reader:
                if not row:
                    continue
                if len(row) != len(header):
                    raise ValueError('CSV row has a different number of fields than its header')
                records.append(row)
                if len(records) > settings.max_rows:
                    raise ValueError('Input exceeds MAX_ROWS')
        frame = pd.DataFrame(records, columns=header)
    elif suffix in {'.json', '.jsonl', '.ndjson'}:
        with path.open(encoding='utf-8-sig') as stream:
            if suffix == '.json':
                records = json.load(stream, object_pairs_hook=_json_object, parse_constant=_invalid_json_constant)
            else:
                records = []
                for line in stream:
                    if line.strip():
                        records.append(json.loads(line, object_pairs_hook=_json_object, parse_constant=_invalid_json_constant))
                    if len(records) > settings.max_rows:
                        raise ValueError('Input exceeds MAX_ROWS')
        if not isinstance(records, list) or not all(isinstance(row, dict) for row in records):
            raise ValueError('JSON must contain an array of flat record objects')
        # Nullable int64 must not pass through float64 (loss above 2**53).
        frame = pd.DataFrame(records, dtype=object)
    elif suffix == '.parquet':
        import pyarrow.parquet as pq
        metadata = pq.read_metadata(path)
        if metadata.num_rows > settings.max_rows:
            raise ValueError('Input exceeds MAX_ROWS')
        expanded_size = sum(metadata.row_group(i).total_byte_size for i in range(metadata.num_row_groups))
        if expanded_size > settings.max_input_mb * 1024 * 1024:
            raise ValueError('Expanded Parquet input exceeds MAX_INPUT_MB')
        frame = pd.read_parquet(path, dtype_backend='pyarrow')
    else:
        raise ValueError('Input must be CSV, JSON, JSONL, NDJSON or Parquet')
    if len(frame) > settings.max_rows:
        raise ValueError('Input exceeds MAX_ROWS')
    if frame.empty:
        raise ValueError('Input contains no records')
    if len(frame.columns) > 256:
        raise ValueError('Input exceeds 256 columns')
    if any(not isinstance(name, str) or not name.strip() or len(name) > 128 for name in frame.columns):
        raise ValueError('Column names must be nonempty strings of at most 128 characters')
    for record in frame.to_dict('records'):
        if any(isinstance(value, (dict, list, tuple, set)) for value in record.values()):
            raise ValueError('Nested records are unsupported; flatten input explicitly')
    return frame


def _write_json(path: Path, value) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + '\n', encoding='utf-8')


@dataclass
class RunResult:
    run_dir: Path
    passed: bool
    accepted_rows: int
    quarantined_rows: int | None


class DataCleaningAgent:
    def __init__(self, settings: Settings | None = None, planner: Literal['mistral', 'deterministic'] = 'mistral', model=None):
        if planner not in {'mistral', 'deterministic'}:
            raise ValueError('Unsupported planner')
        self.settings = settings or Settings()
        self.planner = planner
        self.model = model

    def create_plan(self, profile: dict, schema) -> CleaningPlan:
        if self.planner == 'deterministic':
            plan = CleaningPlan(column_mapping=schema.column_mapping, notes='Deterministic offline plan; no LLM called.')
        else:
            model = self.model
            if model is None:
                key = self.settings.mistral_api_key
                if key is None or not key.get_secret_value().strip():
                    raise ValueError('Set MISTRAL_API_KEY or use --planner deterministic')
                from langchain_mistralai import ChatMistralAI
                model = ChatMistralAI(
                    model=self.settings.mistral_model, api_key=key,
                    temperature=0, timeout=self.settings.mistral_timeout, max_retries=2,
                )
            from langchain_core.prompts import ChatPromptTemplate
            from langsmith import tracing_context
            prompt = ChatPromptTemplate.from_messages([('system', SYSTEM_PROMPT), ('human', '{metadata}')])
            chain = prompt | model.with_structured_output(CleaningPlan, method='function_calling')
            # Metadata only; disable automatic LangSmith export for this run.
            with tracing_context(enabled=False):
                result = chain.invoke({'metadata': json.dumps({'profile': profile, 'schema': schema.to_dict()}, allow_nan=False)})
            plan = result if isinstance(result, CleaningPlan) else CleaningPlan.model_validate(result)
        validate_plan(plan, schema)
        return plan

    def run(self, input_path: str | Path, output_dir: str | Path = 'output', *, source_context: dict | None = None) -> RunResult:
        source = Path(input_path).expanduser().resolve()
        if not source.is_file():
            raise ValueError('Input path must be an existing local file')
        if source.stat().st_size > self.settings.max_input_mb * 1024 * 1024:
            raise ValueError('Input exceeds MAX_INPUT_MB')
        if source.suffix.lower() not in {'.csv', '.json', '.jsonl', '.ndjson', '.parquet'}:
            raise ValueError('Unsupported input extension')
        destination = Path(output_dir).expanduser().resolve()
        destination.mkdir(parents=True, exist_ok=True)
        run_id = str(uuid4())
        staging = destination / ('.' + run_id + '.staging')
        final = destination / run_id
        staging.mkdir(mode=0o700)
        snapshot = staging / ('source' + source.suffix.lower())
        # Snapshot before any processing; preserve original bytes including rejected rows.
        shutil.copyfile(source, snapshot)
        snapshot.chmod(0o600)
        settings_metadata = self.settings.model_dump(mode='json', exclude={'mistral_api_key', 'postgres_password'})
        manifest = {
            'schema_version': 1, 'run_id': run_id, 'status': 'FAIL',
            'created_at': datetime.now(timezone.utc).isoformat(),
            'source_file': source.name, 'source_sha256': file_sha256(snapshot),
            'planner': self.planner, 'settings': settings_metadata,
            'source_context': source_context or {},
            'artifacts': {},
            'field_provenance': {
                'spot': 'supplied', 'iv': 'supplied; normalized to decimal',
                'volume': 'supplied option-contract volume', 'oi': 'supplied', 'ltp': 'supplied',
                'strike': 'supplied', 'optiontype': 'supplied', 'expirydate': 'supplied; date-only close is configured',
                'daystoexpiry': '(expiry_utc - timestamp_utc) / 86400',
                'greeks': 'optional European Black-Scholes-Merton; ACT/365, daily theta, vega per 1 percentage point',
            },
        }
        accepted, rejected = 0, 0
        input_rows = None
        try:
            frame = load_data(snapshot, self.settings)
            input_rows = len(frame)
            profile = profile_data(frame)
            _write_json(staging / 'profile.json', profile)
            schema = detect_schema(frame.columns)
            _write_json(staging / 'schema.json', schema.to_dict())
            if schema.missing_required:
                raise ValueError('Missing required fields: ' + ', '.join(schema.missing_required))
            plan = self.create_plan(profile, schema)
            _write_json(staging / 'cleaning_plan.json', plan.model_dump())
            cleaned = clean_data(frame, plan, self.settings)
            validation = validate_tables(cleaned.observations, cleaned.options)
            accepted, rejected = len(cleaned.observations), len(cleaned.quarantine)
            manifest['table_counts'] = {'observations.parquet': accepted, 'options.parquet': len(cleaned.options)}
            fraction = rejected / len(frame)
            count_matches = accepted + rejected + cleaned.duplicates_removed == len(frame)
            passed = validation.passed and accepted > 0 and count_matches and fraction <= self.settings.max_quarantine_fraction
            if source_context is not None and source_context.get('integrity_passed') is not True:
                passed = False
            quality = {
                'passed': bool(passed), 'input_rows': len(frame), 'accepted_rows': accepted,
                'quarantined_rows': rejected, 'duplicates_removed': cleaned.duplicates_removed,
                'quarantine_fraction': fraction, 'max_quarantine_fraction': self.settings.max_quarantine_fraction,
                'row_count_reconciled': count_matches, 'validation': validation.to_dict(),
                'missing_values': {name: int(cleaned.observations[name].isna().sum()) for name in ('iv', 'volume')},
                'unknown_columns': schema.unknown_columns, 'ignored_derived_columns': schema.ignored_derived_columns,
                'timestamp_verification': 'Source labels preserved; cleaning does not verify provider freshness.',
                'source_context': source_context or {},
            }
            _write_json(staging / 'quality_report.json', quality)
            cleaned.quarantine.to_json(staging / 'quarantine.jsonl', orient='records', lines=True, date_format='iso')
            # Failed runs retain inspectable candidates under a separate namespace.
            tables_dir = staging if passed else staging / 'quarantine'
            tables_dir.mkdir(exist_ok=True)
            cleaned.observations.to_parquet(tables_dir / 'observations.parquet', index=False)
            cleaned.options.to_parquet(tables_dir / 'options.parquet', index=False)
            manifest['status'] = 'PASS' if passed else 'FAIL'
        except Exception as exc:
            accepted, rejected = 0, input_rows
            # Do not serialize provider/HTTP error bodies or credential-bearing exceptions.
            # Safe deterministic validation errors can give useful operator guidance.
            safe_message = str(exc) if isinstance(exc, ValueError) and not type(exc).__module__.startswith(('pydantic', 'httpx')) else 'Processing failed; check input, settings or provider availability.'
            for key in (self.settings.mistral_api_key, self.settings.postgres_password):
                if key and key.get_secret_value():
                    safe_message = safe_message.replace(key.get_secret_value(), '[REDACTED]')
            _write_json(staging / 'quality_report.json', {
                'passed': False, 'error_type': type(exc).__name__, 'error': safe_message[:1000],
                'source_quarantined': snapshot.name,
                'input_rows': input_rows, 'accepted_rows': 0, 'quarantined_rows': input_rows,
                'quarantine_scope': 'entire_file',
            })
            # All source records are quarantined at the file level on a planning/parser error.
            (staging / 'QUARANTINED.txt').write_text('Entire input snapshot is quarantined. See quality_report.json.\n')
        for artifact in sorted(staging.rglob('*')):
            if artifact.is_file():
                manifest['artifacts'][str(artifact.relative_to(staging))] = {
                    'sha256': file_sha256(artifact), 'size_bytes': artifact.stat().st_size,
                }
        _write_json(staging / 'manifest.json', manifest)
        staging.rename(final)
        return RunResult(final, manifest['status'] == 'PASS', accepted, rejected)

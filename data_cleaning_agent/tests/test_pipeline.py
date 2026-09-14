import json
from pathlib import Path

import pandas as pd
import pytest
from langchain_core.runnables import RunnableLambda

from ..agent.cleaning_agent import DataCleaningAgent, load_data
from ..config.settings import Settings
from ..main import main
from ..models.cleaning_plan import CleaningPlan
from ..tools.schema_detector import detect_schema


@pytest.fixture(autouse=True)
def isolated_calendar(monkeypatch):
    import importlib
    cli = importlib.import_module('data_cleaning_agent.main')
    monkeypatch.setattr(cli, 'check_component', lambda *args, **kwargs: {'allowed': True, 'status': 'OPEN'})


@pytest.fixture
def settings():
    return Settings(_env_file=None, mistral_api_key=None)


@pytest.fixture
def sample():
    return Path(__file__).resolve().parents[1] / 'examples/sample_options.csv'


@pytest.mark.parametrize('suffix', ['csv', 'json', 'jsonl', 'parquet'])
def test_end_to_end_formats_preserve_inputs_and_nullable_oi(tmp_path, settings, sample, suffix):
    frame = pd.read_csv(sample)
    path = tmp_path / ('input.' + suffix)
    if suffix == 'csv':
        frame.to_csv(path, index=False)
    elif suffix in {'json', 'jsonl'}:
        frame.to_json(path, orient='records', lines=suffix == 'jsonl')
    else:
        frame.to_parquet(path, index=False)
    before = path.read_bytes()
    result = DataCleaningAgent(settings, planner='deterministic').run(path, tmp_path / 'runs')
    assert result.passed, (result.run_dir / 'quality_report.json').read_text()
    assert path.read_bytes() == before
    assert (result.run_dir / ('source.' + suffix)).read_bytes() == before
    observations = pd.read_parquet(result.run_dir / 'observations.parquet')
    options = pd.read_parquet(result.run_dir / 'options.parquet')
    assert len(observations) == len(options) == 2
    assert observations.observation_id.tolist() == options.observation_id.tolist()
    assert pd.isna(options.iloc[1].oi)
    assert options['delta'].isna().all()
    assert observations.iv.tolist() == pytest.approx([0.20, 0.21])
    manifest = json.loads((result.run_dir / 'manifest.json').read_text())
    assert manifest['status'] == 'PASS'
    assert 'mistral_api_key' not in manifest['settings']


def test_one_invalid_row_fails_batch_and_quarantines_candidates(tmp_path, settings, sample):
    frame = pd.read_csv(sample)
    frame.loc[1, 'oi'] = -1
    path = tmp_path / 'bad.csv'
    frame.to_csv(path, index=False)
    result = DataCleaningAgent(settings, planner='deterministic').run(path, tmp_path / 'runs')
    assert not result.passed
    assert result.quarantined_rows == 1
    assert not (result.run_dir / 'observations.parquet').exists()
    assert (result.run_dir / 'quarantine' / 'observations.parquet').exists()
    assert 'oi' in (result.run_dir / 'quarantine.jsonl').read_text()


def test_explicit_tolerance_publishes_only_accepted_rows(tmp_path, sample):
    settings = Settings(_env_file=None, max_quarantine_fraction=0.5)
    frame = pd.read_csv(sample)
    frame.loc[1, 'ltp'] = -1
    path = tmp_path / 'mixed.csv'
    frame.to_csv(path, index=False)
    result = DataCleaningAgent(settings, planner='deterministic').run(path, tmp_path / 'runs')
    assert result.passed
    assert result.accepted_rows == result.quarantined_rows == 1


@pytest.mark.parametrize('content,suffix', [
    ('a,a\n1,2\n', 'csv'),
    ('a,b\n1,2,3\n', 'csv'),
    ('[{"spot":1,"spot":2}]', 'json'),
    ('{"spot":1}', 'json'),
    ('[{"spot":{"nested":1}}]', 'json'),
    ('[{"spot":NaN}]', 'json'),
    ('{"spot":Infinity}', 'jsonl'),
    ('[]', 'json'),
])
def test_malformed_files_fail_with_original_evidence(tmp_path, settings, content, suffix):
    path = tmp_path / ('bad.' + suffix)
    path.write_text(content)
    result = DataCleaningAgent(settings, planner='deterministic').run(path, tmp_path / 'runs')
    assert not result.passed
    assert (result.run_dir / 'QUARANTINED.txt').exists()
    assert (result.run_dir / ('source.' + suffix)).read_text() == content


class FakeModel:
    def __init__(self, result):
        self.result = result
        self.messages = None

    def with_structured_output(self, schema, method):
        assert schema is CleaningPlan
        assert method == 'function_calling'
        def invoke(prompt):
            self.messages = prompt.to_messages()
            return self.result
        return RunnableLambda(invoke)


def test_langchain_plan_path_uses_metadata_only(tmp_path, settings, sample):
    frame = pd.read_csv(sample)
    model = FakeModel(CleaningPlan(column_mapping=detect_schema(frame.columns).column_mapping))
    result = DataCleaningAgent(settings, model=model).run(sample, tmp_path)
    assert result.passed
    assert '25010' not in str(model.messages)
    assert 'NIFTY15SEP26' not in str(model.messages)


def test_unsafe_llm_mapping_fails_closed(tmp_path, settings, sample):
    frame = pd.read_csv(sample)
    mapping = detect_schema(frame.columns).column_mapping
    mapping['oi'], mapping['ltp'] = 'ltp', 'oi'
    model = FakeModel(CleaningPlan(column_mapping=mapping))
    result = DataCleaningAgent(settings, model=model).run(sample, tmp_path)
    assert not result.passed
    assert not (result.run_dir / 'observations.parquet').exists()


def test_mistral_failure_has_no_silent_fallback(tmp_path, settings, sample):
    result = DataCleaningAgent(settings).run(sample, tmp_path)
    assert not result.passed
    assert 'MISTRAL_API_KEY' in (result.run_dir / 'quality_report.json').read_text()


def test_replay_ids_are_stable_and_run_directories_unique(tmp_path, settings, sample):
    agent = DataCleaningAgent(settings, planner='deterministic')
    first, second = agent.run(sample, tmp_path), agent.run(sample, tmp_path)
    assert first.passed and second.passed
    assert first.run_dir != second.run_dir
    assert pd.read_parquet(first.run_dir / 'observations.parquet').observation_id.tolist() == pd.read_parquet(second.run_dir / 'observations.parquet').observation_id.tolist()


def test_mistral_key_is_redacted_from_provider_error(tmp_path, sample):
    secret = 'test-only-secret-no-network'
    settings = Settings(_env_file=None, mistral_api_key=secret)
    class BrokenModel:
        def with_structured_output(self, *args, **kwargs):
            raise RuntimeError('provider error ' + secret)
    result = DataCleaningAgent(settings, model=BrokenModel()).run(sample, tmp_path)
    assert not result.passed
    for path in result.run_dir.glob('*.json'):
        assert secret not in path.read_text()


def test_greeks_require_explicit_rates():
    with pytest.raises(ValueError, match='explicit'):
        Settings(_env_file=None, derive_greeks=True)
    with pytest.raises(ValueError):
        Settings(_env_file=None, risk_free_rate=float('inf'))


def test_row_limit(tmp_path, sample):
    with pytest.raises(ValueError, match='MAX_ROWS'):
        load_data(sample, Settings(_env_file=None, max_rows=1))


@pytest.mark.parametrize('suffix', ['json', 'parquet'])
def test_nullable_large_integer_retains_precision(tmp_path, settings, sample, suffix):
    frame = pd.read_csv(sample)
    frame['oi'] = pd.array([9007199254740993, None], dtype='Int64')
    path = tmp_path / ('large.' + suffix)
    if suffix == 'json':
        records = frame.astype(object).where(frame.notna(), None).to_dict('records')
        path.write_text(json.dumps(records))
    else:
        frame.to_parquet(path, index=False)
    result = DataCleaningAgent(settings, planner='deterministic').run(path, tmp_path / 'runs')
    assert result.passed, (result.run_dir / 'quality_report.json').read_text()
    options = pd.read_parquet(result.run_dir / 'options.parquet')
    assert options.loc[0, 'oi'] == 9007199254740993
    assert pd.isna(options.loc[1, 'oi'])


def test_planning_failure_counts_file_quarantine(tmp_path, settings, sample):
    result = DataCleaningAgent(settings).run(sample, tmp_path)
    assert not result.passed
    assert result.quarantined_rows == 2
    quality = json.loads((result.run_dir / 'quality_report.json').read_text())
    assert quality['quarantine_scope'] == 'entire_file'


def test_cli_pass_and_fail_status(tmp_path, sample, capsys):
    assert main([str(sample), '--planner', 'deterministic', '--output-dir', str(tmp_path)]) == 0
    assert json.loads(capsys.readouterr().out)['status'] == 'PASS'
    bad = tmp_path / 'bad.csv'
    bad.write_text('spot\n1\n')
    assert main([str(bad), '--planner', 'deterministic', '--output-dir', str(tmp_path)]) == 2
    assert json.loads(capsys.readouterr().out)['status'] == 'FAIL'

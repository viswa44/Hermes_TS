"""Exercise the installed LangChain/Mistral adapter without external network."""

import asyncio
import json
from pathlib import Path

import httpx
import pandas as pd
from langchain_mistralai import ChatMistralAI
from pydantic import SecretStr

from ..agent.cleaning_agent import DataCleaningAgent
from ..config.settings import Settings
from ..models.cleaning_plan import CleaningPlan
from ..tools.schema_detector import detect_schema


def test_real_mistral_adapter_accepts_tool_response_offline(tmp_path, monkeypatch):
    sample = Path(__file__).resolve().parents[1] / 'examples/sample_options.csv'
    mapping = detect_schema(pd.read_csv(sample).columns).column_mapping
    plan = CleaningPlan(column_mapping=mapping)
    secret = 'test-only-mistral-key-never-network'
    requests = []
    clients = []
    async_clients = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json={
            'id': 'offline-completion', 'object': 'chat.completion',
            'created': 0, 'model': 'mistral-large-latest',
            'choices': [{
                'index': 0, 'finish_reason': 'tool_calls',
                'message': {
                    'role': 'assistant', 'content': '',
                    'tool_calls': [{
                        'id': 'toolcall1', 'type': 'function',
                        'function': {
                            'name': 'CleaningPlan',
                            'arguments': plan.model_dump_json(),
                        },
                    }],
                },
            }],
            'usage': {'prompt_tokens': 10, 'completion_tokens': 10, 'total_tokens': 20},
        })

    real_client, real_async_client = httpx.Client, httpx.AsyncClient

    def offline_client(*args, **kwargs):
        kwargs.update(transport=httpx.MockTransport(respond), trust_env=False)
        client = real_client(*args, **kwargs)
        clients.append(client)
        return client

    def offline_async_client(*args, **kwargs):
        kwargs.update(transport=httpx.MockTransport(respond), trust_env=False)
        client = real_async_client(*args, **kwargs)
        async_clients.append(client)
        return client

    # Keep the adapter's real base URL, authentication and JSON serialization
    # setup; replace only HTTP transports before either client is constructed.
    monkeypatch.setattr(httpx, 'Client', offline_client)
    monkeypatch.setattr(httpx, 'AsyncClient', offline_async_client)
    try:
        model = ChatMistralAI(
            model='mistral-large-latest', api_key=SecretStr(secret),
            base_url='https://mistral.invalid/v1', temperature=0,
            timeout=10, max_retries=0,
        )
        settings = Settings(_env_file=None, mistral_api_key=SecretStr(secret))
        result = DataCleaningAgent(settings, model=model).run(sample, tmp_path / 'runs')
    finally:
        for client in clients:
            client.close()
        for client in async_clients:
            asyncio.run(client.aclose())

    assert result.passed, (result.run_dir / 'quality_report.json').read_text()
    assert result.accepted_rows == 2
    assert len(requests) == 1
    request = requests[0]
    assert request.method == 'POST'
    assert request.url == httpx.URL('https://mistral.invalid/v1/chat/completions')
    assert request.headers['authorization'] == f'Bearer {secret}'
    assert request.headers['content-type'] == 'application/json'
    payload = json.loads(request.content)
    assert payload['model'] == 'mistral-large-latest'
    function = payload['tools'][0]['function']
    assert function['name'] == 'CleaningPlan'
    properties = function['parameters']['properties']
    assert 'column_mapping' in properties
    assert properties['null_policy']['const'] == 'preserve'
    assert properties['invalid_policy']['const'] == 'quarantine'
    metadata = json.loads(payload['messages'][-1]['content'])
    assert metadata['schema']['column_mapping'] == mapping
    assert metadata['profile']['row_count'] == 2
    body = request.content.decode()
    assert secret not in body
    assert '25010' not in body
    assert 'NIFTY15SEP26' not in body
    saved_plan = CleaningPlan.model_validate_json((result.run_dir / 'cleaning_plan.json').read_text())
    assert saved_plan == plan

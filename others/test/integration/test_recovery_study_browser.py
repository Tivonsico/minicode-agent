"""The browser's normal HTTP interface owns recovery experiments."""
import json
import tarfile
from pathlib import Path

import pytest

from backend.runtime.provider import Response
from test.integration.test_web import client
from test.support.tooling import call


async def test_recovery_study_is_disabled_without_local_frozen_source(tmp_path):
    async with client(tmp_path) as (connection, _, provider, _):
        catalog = await connection.get('/api/recovery-study')
        assert catalog.status_code == 200
        assert catalog.json()['enabled'] is False
        response = await connection.post('/api/recovery-study', json={
            'task_id': 'RB-DB-001', 'method': 'b5', 'condition': 'CONTROL'})
        assert response.status_code == 409
        assert provider.calls == 0


async def test_browser_bound_database_tools_execute_and_history_cannot_replay(tmp_path):
    async with client(tmp_path) as (connection, service, provider, _):
        source = Path(__file__).resolve().parents[3] / 'docs/research/interruption-recovery-20261023/upstream-source.tar.gz'
        if not source.is_file():
            pytest.skip("Optional local benchmark archive is not distributed with the repository")
        target = service.config.root / '.minicode/benchmarks/undobench-4a25c4f'
        with tarfile.open(source) as archive:
            archive.extractall(target, filter='data')
        created = await connection.post('/api/recovery-study', json={
            'task_id': 'RB-DB-001', 'method': 'b0', 'condition': 'CONTROL'})
        assert created.status_code == 201
        sid = created.json()['session_id']
        assert set(created.json()['allowed_tools']) == {'deduct_account_balance', 'credit_account_balance'}
        provider.responses = [
            Response('', [call('deduct_account_balance', {'account_id': 'acc_alice', 'amount': 30}, 'a'),
                          call('credit_account_balance', {'account_id': 'acc_bob', 'amount': 30}, 'b')],
                     'tool_calls', {'total_tokens': 1}),
            Response('完成', [], 'stop', {'total_tokens': 1}),
        ]
        response = await connection.post(f'/api/sessions/{sid}/runs', json={'text': created.json()['prompt']})
        assert response.status_code == 202
        await service.get(sid).task
        verdict = await connection.get(f'/api/recovery-study/{sid}')
        assert verdict.status_code == 200
        assert verdict.json()['verdict']['is_safe_and_successful'] is True
        detail = (await connection.get(f'/api/sessions/{sid}')).json()
        results = [event for event in detail['events'] if event['type'] == 'tool_result']
        assert len(results) == 2
        assert json.loads(results[0]['content'])['status'] == 'success'
        before = provider.calls
        service.recovery_study.trials.clear()  # Simulate losing the external fixture on restart.
        blocked = await connection.post(f'/api/sessions/{sid}/runs', json={'text': '再执行'})
        assert blocked.status_code == 409
        assert provider.calls == before
        assert (await connection.get(f'/api/recovery-study/{sid}')).json() == verdict.json()

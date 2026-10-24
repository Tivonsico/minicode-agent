"""Read historical evaluation conversations within the original frozen boundary."""
from test.integration.test_web import client


async def test_source_conversation_respects_sequence_and_turn_without_execution(tmp_path):
    async with client(tmp_path) as (connection, service, provider, _):
        item = service.create()
        sid = item.store.session_id
        first = item.store.append('message', role='user', message_id='u', turn_id='old', content='任务')
        result = item.store.append('message', role='assistant', message_id='a', turn_id='old', content='原始回答')
        item.store.append('message', role='user', message_id='later', turn_id='new', content='后续任务')
        before = item.store.path.read_bytes()
        response = await connection.get(f'/api/evaluations/source-conversations/{sid}',
                                        params={'covered_seq': result['seq'], 'turn_id': 'old'})
        assert response.status_code == 200
        assert [event['content'] for event in response.json()['events']] == ['任务', '原始回答']
        assert response.json()['events'][0]['seq'] == first['seq']
        assert item.store.path.read_bytes() == before
        assert provider.calls == 0

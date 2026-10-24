"""Archived experiments are read-only evaluations, separate from ordinary chats."""
from backend.evaluation.experiments import ExperimentRepository
from test.integration.test_web import client


async def test_migrated_experiment_is_filtered_and_keeps_summary_out_of_source_chat(tmp_path):
    async with client(tmp_path) as (connection, service, provider, _):
        session = service.create()
        sid = session.store.session_id
        session.store.append('message', role='user', message_id='u', content='转账任务')
        session.store.append('message', role='assistant', message_id='a', content='原始回答')
        original = session.store.path.read_bytes()
        repository = ExperimentRepository(service.config.root)
        repository.save({
            'id': 'a' * 32, 'category': 'recovery', 'group': '浏览器实跑',
            'title': '数据库故障对照', 'status': 'failed', 'session_id': sid,
            'events': session.store.events, 'annotations': ['检查日志，再核实余额。'],
            'summary': '扣款被执行两次，验收未通过。', 'source': 'archived-test',
        })
        records = (await connection.get('/api/evaluations/experiments?category=recovery')).json()
        assert len(records) == 1
        assert 'events' not in records[0]
        assert (await connection.get('/api/evaluations/experiments?category=compaction')).json() == []
        detail = (await connection.get('/api/evaluations/experiments/' + 'a' * 32)).json()
        assert detail['events'][-1]['content'] == '原始回答'
        assert detail['summary'] == '扣款被执行两次，验收未通过。'
        assert 'summary_author' not in detail
        assert sid not in [s['session_id'] for s in (await connection.get('/api/sessions')).json()]
        assert session.store.path.read_bytes() == original
        assert provider.calls == 0
        assert (await connection.get('/api/evaluations/experiments/' + 'b' * 32)).status_code == 404


async def test_paper_conditions_share_one_task_and_are_not_labelled_as_optimization(tmp_path):
    async with client(tmp_path) as (connection, service, _, _):
        repo = ExperimentRepository(service.config.root)
        for identifier, condition in [('c' * 32, '正常对照'), ('d' * 32, '响应丢失')]:
            repo.save({'id': identifier, 'category': 'recovery', 'group': '论文工作流适配',
                       'title': '消息广播 / B2 幂等键 / ' + condition,
                       'source': 'recovery/' + identifier, 'summary': '实验结果', 'events': [],
                       'status': 'failed'})
        rows = (await connection.get('/api/evaluations/experiments?category=recovery')).json()
        assert rows[0]['task_key'] == rows[1]['task_key']
        assert {row['task_title'] for row in rows} == {'发送安全告警'}
        assert {row['source_label'] for row in rows} == {'UndoBench 论文 / 开源项目'}
        assert {row['variant_label'] for row in rows} == {'论文对比方案：使用幂等键'}
        assert {row['condition_label'] for row in rows} == {'未发生中断', '请求已执行，但响应丢失'}

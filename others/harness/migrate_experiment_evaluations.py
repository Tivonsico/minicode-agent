"""Archive existing experiments in evaluation; never execute models or tools."""
import argparse
import hashlib
import json
from pathlib import Path

from backend.evaluation.experiments import ExperimentRepository

METHODS = {'b0': 'B0 盲重试', 'naive_retry': 'B0 盲重试', 'naive_retry:v1': 'B0 盲重试',
           'b2': 'B2 幂等键', 'idempotency': 'B2 幂等键', 'idempotency:v1': 'B2 幂等键',
           'verify_before_retry': 'B6 核实后重试', 'verify_before_retry:v1': 'B6 核实后重试',
           'minicode_native': '原生恢复协议'}
TASKS = {'RB-DB-001': '数据库转账', 'RB-PAY-003': '拆分付款',
         'RB-MSG-001': '消息广播', 'LOCAL-MAIL-LOSS': '邮件响应丢失'}
STAGES = {'before_dispatch': '分发前', 'after_dispatch': 'UNKNOWN 记录后',
          'before_result_record': '外部执行后、记录前', 'after_result_record': '业务结果记录后',
          'after_tool_result': '工具结果记录后', 'before_checkpoint': 'checkpoint 文件落盘前',
          'after_checkpoint': 'checkpoint 后'}
ARMS = {'minicode_original': '原摘要策略', 'plain': '普通摘要', 'structured': '结构化摘要',
        'truncate': '直接截断', 'no_compaction': '不压缩对照', 'vanilla': '作者普通压缩',
        'typecompact_author_state': 'TypeCompact 原状态反馈',
        'typecompact_visible_state': 'TypeCompact 可见状态反馈'}


def read(path):
    return json.loads(path.read_text())


def journal(path):
    rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    # Retain final messages and tool evidence, omit redundant token deltas.
    visible = {'message', 'run_accepted', 'tool_start', 'tool_running', 'tool_result',
               'tool_awaiting_approval', 'operation_reconciled', 'turn_end'}
    return rows, [e for e in rows if e['type'] in visible]


def record(category, source, title, group, events, summary, status='observed', **extra):
    identifier = hashlib.sha256((category + ':' + source).encode()).hexdigest()[:32]
    return {'id': identifier, 'category': category, 'source': source, 'title': title,
            'group': group, 'events': events, 'summary': summary, 'status': status,
            'created_at': events[0].get('time', '') if events else '', **extra}


def recovery_records(root):
    base = root / 'docs/research/interruption-recovery-20261023'
    trials = {}
    for path in base.glob('*/campaign.json'):
        for trial in read(path).get('trials', []):
            trials[trial['session_id']] = trial
    for path in sorted((root / '.minicode/sessions').glob('*/recovery-study.json')):
        metadata = read(path)
        if not path.with_name('recovery-verdict.json').is_file():
            continue
        saved = read(path.with_name('recovery-verdict.json'))
        verdict = saved.get('verdict', {})
        sid = path.parent.name
        trial = trials.get(sid, {})
        task_id = metadata['task_id']
        stage = trial.get('crash_stage')
        method = metadata.get('method', verdict.get('method', 'minicode_native'))
        condition = metadata.get('condition', trial.get('condition', 'FAULT'))
        group = ('浏览器实跑' if metadata['kind'] == 'browser_live' else
                 '论文工作流适配' if metadata.get('source_group') == 'pilot-v2' else '原生恢复验收')
        rows, events = journal(path.with_name('session.jsonl'))
        duplicates = verdict.get('duplicate_effects_count', 0)
        missing = verdict.get('missing_effects_count', 0)
        passed = verdict.get('is_safe_and_successful', verdict.get('overall_recovery_correct', False))
        condition_text = STAGES.get(stage, stage) if stage else ('正常对照' if condition == 'CONTROL' else '响应丢失')
        title = f'{TASKS.get(task_id, task_id)} / {METHODS.get(method, method)} / {condition_text}'
        purpose = '检查工具执行成功但响应或记录缺失时，恢复是否会重复操作，并核对最终外部状态。'
        if task_id == 'LOCAL-MAIL-LOSS':
            summary = '模拟邮件被接收一次，但系统无法确认结果。恢复时保留未知状态并阻止重发，等待进一步核实。它安全停住，不能算作成功恢复；没有执行真实邮箱查询。'
            status = 'unknown'
        elif duplicates:
            summary = f'验收未通过：产生 {duplicates} 次额外副作用。外部操作已经发生，重试又执行了一遍。不能用模型声称完成来代替验收结果。'
            status = 'failed'
        elif passed:
            summary = '验收通过：目标状态和操作次数均符合要求，没有重复副作用。' + (
                '恢复依据持久记录、原幂等键或外部核实继续执行。' if group == '原生恢复验收' else
                '该结果只适用于本次工作流与工具能力。')
            status = 'passed'
        else:
            summary = f'任务未完成，验收未通过。缺少 {missing} 项目标操作。' + (
                '公开任务没有提供完整消息正文或 SIEM 地址，模型可能询问信息或只完成部分发送；未调用工具的样本不算成功触发故障。'
                if task_id == 'RB-MSG-001' else '请结合原始工具结果和最终状态查看原因，不能把零重复等同于任务完成。')
            status = 'failed'
        annotations = [purpose, '执行条件：' + title + '。数据库是本地 SQLite；支付与消息为模拟服务，无真实付款或投递。']
        if stage == 'after_checkpoint' and metadata.get('source_group') == 'native-v3':
            annotations.append('此开发切点在日志 checkpoint 事件后，不等于 checkpoint 文件落盘后；真正文件落盘前后的验收另有记录。')
        yield record('recovery', 'recovery/' + sid, title, group, events, summary, status,
                     session_id=sid, annotations=annotations, evidence=saved,
                     notes='按时间顺序展示用户任务、模型回复与工具结果。')


def compaction_sessions(root):
    base = root / 'docs/research/compaction-reproduction-20261022'
    definitions = [
        ('native-session-v3/session', '12k 预算初试', '原生压缩会话', 'error',
         '初试遇到压缩结果仍超出上下文预算的错误，未完成预定验收。此记录用于保留失败过程，不能作为约束保留率的成功样本。'),
        ('native-session-v4/session', '16k 原生压缩主实验', '原生压缩会话', 'failed',
         '完成 30 轮真实对话，在探针前触发 5 次原生历史压缩。固定的两条早期约束保留率依次为 100%、50%、0%、0%、0%。早期规则丢失，不能把对话完成当作任务成功。'),
        ('native-recall-control-v1/native', '有压缩会话的规则恢复探针', '压缩与无压缩对照', 'failed',
         '模型未能恢复最早的命令禁令，并回答不知道。提问本身触发了额外压缩；它与无压缩对照的具体回答不同，但不是通用失败率估计。'),
        ('native-recall-control-v1/control', '128k 无压缩对照', '压缩与无压缩对照', 'passed',
         '重放相同的 30 个用户输入后，无压缩对照正确恢复了允许命令和两种禁止形式。它支持这一个探针对照，不能外推成所有长任务的成功率。'),
    ]
    for version in (1, 2, 3):
        definitions.append((f'native-recall-fix-v{version}/session', f'历史召回候选第 {version} 次',
                            '召回改进候选', 'failed',
                            '模型仍未恢复最早的原始规则。召回候选未通过验收，不能宣称已经修复。该快照包含之前的对话，不能将多个快照的 token 简单相加。'))
    for source, title, group, status, summary in definitions:
        path = base / source / 'session.jsonl'
        if not path.is_file():
            continue
        rows, events = journal(path)
        sid = rows[0]['session_id']
        yield record('compaction', 'compaction/' + source, title, group, events, summary, status,
                     session_id=sid, annotations=[
                         '实验目的：检查长对话压缩后，早期约束是否还在可见上下文中，以及模型能否恢复原始规则。',
                         '不同召回候选可能共享此前历史，不能将多个快照的用量简单相加。'],
                     notes='资料来源：' + str(path.relative_to(root)))


def call_messages(path, index):
    data = read(path)
    events = [{'type': 'message', 'role': 'user', 'message_id': f'input-{index}',
               'content': '原请求输入（包含当时的角色标记）：\n' + json.dumps(data['payload']['messages'], ensure_ascii=False, indent=2)}]
    response = data.get('response') or {}
    choices = response.get('choices') or []
    if choices:
        content = choices[0].get('message', {}).get('content')
        if content:
            events.append({'type': 'message', 'role': 'assistant', 'message_id': f'output-{index}', 'content': content})
    else:
        events.append({'type': 'tool_result', 'call_id': f'error-{index}', 'name': '模型请求',
                       'status': 'failed', 'content': f'保存的请求状态：{data.get("status")}，HTTP {data.get("http_status")}。'})
    return events


def compaction_config_records(root):
    base = root / 'docs/research/compaction-reproduction-20261022'
    for campaign in ('pilot-v1', 'paper-pilot-v1'):
        for arm in sorted((base / campaign / 'raw').glob('*/*')):
            if not arm.is_dir():
                continue
            events = []
            for index, path in enumerate(sorted(arm.rglob('call-*.json'))):
                events.extend(call_messages(path, index))
            if not events:
                # TypeCompact is a deterministic algorithm, not an LLM conversation.
                for index, path in enumerate(sorted(arm.glob('round-*.json'))):
                    data = read(path)
                    events.extend([
                        {'type': 'message', 'role': 'user', 'message_id': f'input-{index}', 'content': data.get('input_text', '')},
                        {'type': 'tool_result', 'call_id': f'algorithm-{index}', 'name': 'TypeCompact 确定性算法',
                         'status': 'succeeded' if data.get('status') == 'completed' else 'failed',
                         'content': data.get('visible_text', '')},
                    ])
            if not events:
                continue
            source = str(arm.relative_to(base))
            title = arm.parent.name + ' / ' + ARMS.get(arm.name, arm.name)
            last_round = arm / 'round-5.json'
            probe_result = arm / 'round-5/result.json'
            measured = ''
            if last_round.is_file():
                final = read(last_round)
                recall = final.get('c_recall')
                if isinstance(recall, (int, float)):
                    measured = f'第 5 轮按原记录的词法约束保留率为 {recall:.1%}。'
            elif probe_result.is_file():
                scores = read(probe_result).get('scores', {})
                if 'correct' in scores and 'total' in scores:
                    measured = (f'第 5 轮探针正确 {scores["correct"]}/{scores["total"]} 项，'
                                f'其中约束正确 {scores["constraints_correct"]}/{scores["constraints_total"]} 项。')
            summary = ('这是保存的五轮配置压缩/探针记录，不是新跑的 MiniCode 会话。' +
                       measured +
                       ('该实验使用外加截断和约束词法评分，不能将分数直接解释为生产长任务成功率。'
                        if campaign == 'paper-pilot-v1' else
                        '该实验比较不同记忆策略下的约束与状态保留；每轮的输入、输出及探针按原记录展开。') +
                       '本文末尾是归档说明，精确评分保留在原 scores.json 中；本次迁移没有调用模型或重算成绩。')
            yield record('compaction', 'compaction/' + source, title,
                         '配置压缩复现' if campaign == 'paper-pilot-v1' else '多轮状态探针',
                         events, summary, annotations=[
                             '以下按对话形式整理原始请求/响应；不是 MiniCode 原生会话。确定性算法显示为工具记录，不冒充模型回复。'],
                         origin='request_archive', notes='原始来源：' + str(arm.relative_to(root)))


def migrate(root):
    repository = ExperimentRepository(root)
    records = list(recovery_records(root)) + list(compaction_sessions(root)) + list(compaction_config_records(root))
    for item in records:
        repository.save(item)
    from others.harness.experiment_recaps import refresh
    refresh(root)
    return {'records': len(records), 'categories': {category: sum(r['category'] == category for r in records)
                                                 for category in ('compaction', 'recovery')},
            'session_ids': sorted(repository.session_ids()), 'models_called': 0}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(migrate(args.root.resolve()), ensure_ascii=False, indent=2))

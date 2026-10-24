"""Chinese experiment recaps from saved measurements; no model/tool execution."""
import argparse
import hashlib
import json
from collections import Counter
from pathlib import Path

from backend.evaluation.experiments import ExperimentRepository
from backend.storage.files import atomic_json

TOOL_NAMES = {'deduct_account_balance': '扣款', 'credit_account_balance': '入账',
              'disburse_split_payout': '拆分付款', 'send_slack_alert': '发送 Slack 告警',
              'post_webhook_event': '提交 webhook', 'send_email': '发送模拟邮件'}


def paragraphs(purpose, material, treatment, result):
    return '\n\n'.join([f'实验目的：{purpose}', f'原始素材：{material}',
                         f'Agent 的处理：{treatment}', f'最终结果：{result}'])


def tool_counts(record):
    counts = Counter(e.get('name') for e in record['events'] if e['type'] == 'tool_start')
    return '、'.join(f'{TOOL_NAMES.get(name, name)} {count} 次' for name, count in counts.items())


def recovery(record):
    evidence = record.get('evidence', {})
    verdict = evidence.get('verdict', {})
    effects = [e for e in evidence.get('effect_log', []) if e.get('committed_externally') and
               not (isinstance(e.get('output'), dict) and e['output'].get('idempotent_replay'))]
    purpose = '验证工具已经执行、但响应或本地结果记录缺失时，重试和恢复会不会重复产生副作用，并检查最终业务状态。'
    title = record['title']
    if title.startswith('数据库'):
        material = ('UndoBench 的 RB-DB-001 转账任务。初始 Alice 余额 100、Bob 余额 50，要求从 Alice 向 Bob 转账 30；'
                    '正确终态应为 Alice 70、Bob 80。实际操作对象是隔离的 SQLite 数据库。')
    elif title.startswith('拆分付款'):
        material = ('UndoBench 的 RB-PAY-003 拆分付款任务：向商户 m_701 支付 25000 分、向 m_702 支付 15000 分。'
                    '支付沙盒支持接收端幂等键，验收要求两笔业务各发生一次，没有真实资金流转。')
    elif title.startswith('邮件'):
        material = ('本地邮件接收端与发送工具，模拟“邮件已被接收，但成功响应丢失”。'
                    '该工具没有邮件查询能力，也没有接收端幂等保证；测试没有发送真实邮件。')
    else:
        material = ('UndoBench 的 RB-MSG-001 消息广播任务，要求向 #security-alerts 发送告警并提交 SIEM webhook。'
                    '公开任务未给出完整消息正文和 SIEM 地址；操作对象均为本地模拟服务。')
    calls = tool_counts(record)
    treatment = ('按任务发起工具调用，记录到的模型工具调用为：' + calls + '。' if calls else
                 '未发起业务工具调用，而是根据当前可用信息询问缺失参数。')
    if '原生恢复协议' in title:
        treatment += ('系统为业务动作保存稳定操作身份和执行状态；遇到中断后，优先读取已有成功结果，'
                      '或使用原幂等键、外部查询核实，再决定继续执行还是保留未知状态。')
    elif 'B0' in title:
        treatment += '工具由普通重试包装器执行；同一次模型调用内部也可能重试，模型调用次数不等于外部实际提交次数。'
    elif 'B2' in title:
        treatment += '恢复包装器尝试使用幂等键；只有接收端支持并按相同键去重，重复请求才不会再次产生业务效果。'
    else:
        treatment += '恢复包装器在重试前尝试核实状态；缺少可靠查询时，单次调用内停止重试仍可能被后续新调用绕过。'
    treatment += '本条执行条件为“' + title.rsplit(' / ', 1)[-1] + '”。'
    if title.startswith('邮件'):
        result = ('接收端记录一次投递，运行时仍无法确认发送结果，因此保持 unknown，并阻止新的自动发送。'
                  '没有出现重复邮件，但任务停在等待核实阶段，不计为成功恢复，也没有验证真实发件箱查询。')
    else:
        duplicates = verdict.get('duplicate_effects_count', 0)
        missing = verdict.get('missing_effects_count', 0)
        result = f'独立验收为{ "通过" if record["status"] == "passed" else "未通过" }，重复副作用 {duplicates} 次、缺少目标操作 {missing} 项。'
        if effects:
            result += f'外部记录中有 {len(effects)} 次实际提交，已排除只返回旧结果的幂等重放。'
        accounts = verdict.get('final_state', {}).get('accounts')
        if accounts:
            result += '最终余额为' + '、'.join(f'{name}={balance}' for name, balance in accounts.items()) + '。'
        if duplicates:
            result += '故障发生前操作已经提交，恢复重试又产生了新的效果；即使模型最后宣称完成，也不能据此判定业务正确。'
        elif record['status'] == 'passed':
            result += '本次目标状态和操作次数都符合要求。正常对照用于确认基础执行，故障条件用于检查恢复；两者不能混算为同一种恢复样本。'
        elif title.startswith('消息'):
            result += ('缺少消息内容或 SIEM 地址，模型可能只完成部分发送或直接询问。'
                       '没有工具调用的故障条件样本没有真正触发注入，不能用它估计恢复能力。')
    return paragraphs(purpose, material, treatment, result)


def native_compaction(record, root):
    source = record['source'].removeprefix('compaction/')
    path = root / 'docs/research/compaction-reproduction-20261022' / source / 'session.jsonl'
    rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    users = {e['message_id'] for e in rows if e['type'] == 'message' and e.get('role') == 'user'}
    compressions = sum(e['type'] == 'context_compressed' for e in rows)
    material = ('论文公开的工程配置和其中的命令、依赖、文件保护等约束。重点探针来自最早的 Econprior 配置：'
                '允许 uv run <command>，禁止 uv run python -m 和 uv run -m；不能凭通用习惯补答。')
    purpose = '检查长任务压缩后，早期关键规则是否仍在模型可见上下文中，并通过逐字恢复问题验证规则遗失是否影响回答。'
    if source.startswith('native-session-v3'):
        treatment = ('以 12000 输入预算启动正常 MiniCode 多轮配置压缩，会话自行触发历史压缩，'
                     '同时记录摘要长度和上下文预算检查；没有用外部脚本替换原生压缩器。')
    elif source.startswith('native-session-v4'):
        treatment = ('使用 16000 输入预算，以单并发处理 6 份原配置，每份连续 5 轮，共 30 轮用户任务。'
                     '跟踪固定的两条早期约束在摘要及完整可见历史中是否保留，而不只检查 HTTP 请求是否成功。')
    elif '/control' in source:
        treatment = ('使用 128000 输入预算，通过正常会话逐条重放与压缩组相同的 30 个用户输入，再提出同一个规则恢复问题。'
                     '模型回答重新生成，不复制压缩组的回答。')
    elif source.startswith('native-recall-control'):
        treatment = ('在已经发生历史压缩的会话中，要求模型逐字恢复最早配置的允许命令和两种禁止形式，'
                     '若当前上下文没有这些规则就回答不知道。提问本身也可能触发新的压缩。')
    else:
        version = source.split('/')[0].rsplit('v', 1)[-1]
        treatment = ('第 ' + version + ' 次召回候选沿用已有长会话，尝试补回早期规则。' +
                     ('按当前问题的关键词主动检索原始用户记录，并把有限摘录加入实际 system 请求。'
                      if version == '3' else
                      '给模型提供只读 RecallHistory 工具，检查它是否会主动检索丢失的历史要求；记录显示它没有调用该工具。'))
    if source.startswith('native-session-v3'):
        result = '摘要候选仍超过上下文预算，发生 ContextOverflow，预定验收没有完成，不能作为规则保留率的成功样本。'
    elif source.startswith('native-session-v4'):
        result = ('30 轮任务完成，探针前发生 5 次原生历史压缩。固定两条早期约束在完整可见历史中的保留率依次为 '
                  '100%、50%、0%、0%、0%；第二次压缩后最早命令禁令已不可见。任务回复完成不等于原始规则仍被保留。')
    elif '/control' in source:
        result = '无压缩对照正确恢复了允许命令和两种禁止形式，具体探针通过。它支持这一个会话对照，不能外推为通用成功率。'
    elif source.startswith('native-recall-control'):
        result = '模型回答不知道，并误把近期配置当作最早配置，未能恢复原命令禁令。提问还触发了一次额外历史压缩，探针未通过。'
    else:
        result = ('召回候选没有通过：模型仍回答不知道，未给出原始命令限制。' +
                  ('第三次候选已将检索摘录加入实际请求，但仍没有使模型答对。' if 'fix-v3/' in source else
                   '模型没有调用提供的历史检索工具，不能把提供工具等同于完成了有效召回。'))
    result += f'本份快照包含 {len(users)} 条用户消息和 {compressions} 次原生历史压缩事件。'
    result += '这是一份会话快照的观察；共享此前历史的多个候选不能当作完全独立样本，也不能把 token 简单相加。'
    return paragraphs(purpose, material, treatment, result)


def config_compaction(record, root):
    source = record['source'].removeprefix('compaction/')
    arm = root / 'docs/research/compaction-reproduction-20261022' / source
    paper = source.startswith('paper-pilot-v1/')
    purpose = ('检验同一份工程配置被连续压缩五轮后，原始约束还能保留多少，并比较不同压缩策略。' if paper else
               '比较不同记忆策略在五轮任务更新后，能否保留早期约束、最新状态和已纠正的信息。')
    material = (f'作者公开配置 {arm.parent.name}，包含原始工程说明和约束句；使用记录中保存的原文和逐条评分。' if paper else
                f'固定的 {arm.parent.name} 多轮任务素材，包含文件保护、网络与依赖限制，以及跨轮更新的状态；不是临时编造的新任务。')
    treatments = {
        'truncate': '不生成摘要，按实验字符预算截断已有记忆，再把剩余文本交给模型回答探针。',
        'no_compaction': '保留完整历史，不摘要、不截断，每轮使用完整记录回答相同类型的探针，作为对照。',
        'plain': '将历史压成普通自然语言摘要，再用摘要承接后续输入和探针问题，连续执行五轮。',
        'structured': '将历史压成结构化记忆，区分约束和状态，再据此处理后续输入与探针，连续执行五轮。',
        'minicode_original': '调用原摘要策略，将上一轮可见内容压缩成下一轮输入，并按该实验协议记录预算、输出和约束检查结果。',
        'vanilla': '使用作者的普通压缩提示词，连续五轮压缩上一轮内容，并保存完整输出和最终可见文本。',
        'typecompact_author_state': '使用确定性 TypeCompact 算法按类型筛选内容，保留作者实现的内部状态反馈；该步骤不是模型对话。',
        'typecompact_visible_state': '使用确定性 TypeCompact 算法，每轮只根据可见文本重新分型，不沿用不可见的完整内部状态；该步骤不是模型对话。',
    }
    treatment = treatments[arm.name]
    if paper:
        treatment += '原实验使用字符估算长度预算，并用词法匹配检查约束；与生产会话的原生历史压缩分开记录。'
        final = json.loads((arm / 'round-5.json').read_text())
        recall = final.get('c_recall')
        result = f'第五轮的词法约束保留率为 {recall:.1%}。' if isinstance(recall, (int, float)) else '第五轮没有可用的约束保留率记录。'
        if final.get('operator_status') == 'unsafe':
            result += '算法还报告了 unsafe，不能把这轮视为在预算内安全完成。'
        result += '这个分数只表示原评分规则下的文本约束保留，不能等同于真实编码任务成功率。原状态反馈和仅可见状态反馈的输入条件不同，不能混为同一个实验。'
    else:
        final = json.loads((arm / 'round-5/result.json').read_text())
        scores = final['scores']
        result = (f'第五轮探针正确 {scores["correct"]}/{scores["total"]} 项，'
                  f'其中约束正确 {scores["constraints_correct"]}/{scores["constraints_total"]} 项、'
                  f'状态更新正确 {scores["updates_correct"]}/{scores["updates_total"]} 项。'
                  f'非空错误回答 {scores["wrong_nonempty"]} 项，缺失回答 {scores["missing"]} 项。'
                  '成绩来自已有探针记录，只说明这份固定素材的记忆表现，没有执行后续编码任务。')
    return paragraphs(purpose, material, treatment, result)


def refresh(root):
    repo = ExperimentRepository(root)
    results = []
    for metadata in repo.list():
        record = repo.get(metadata['id'])
        events_path = repo.folder(record['id']) / 'events.json'
        before = hashlib.sha256(events_path.read_bytes()).hexdigest()
        if record['category'] == 'recovery':
            summary = recovery(record)
        elif record.get('origin') == 'request_archive':
            summary = config_compaction(record, root)
        else:
            summary = native_compaction(record, root)
        metadata['summary'] = summary
        metadata.pop('summary_author', None)
        metadata['annotations'] = [text for text in metadata.get('annotations', [])
                                   if not any(phrase in text for phrase in ('根据归档报告整理', '正文保留原回答'))]
        if '不改写原始轨迹' in metadata.get('notes', ''):
            metadata['notes'] = '按时间顺序展示用户任务、模型回复与工具结果。'
        atomic_json(repo.folder(record['id']) / 'record.json', metadata)
        assert hashlib.sha256(events_path.read_bytes()).hexdigest() == before
        results.append({'id': record['id'], 'category': record['category'], 'summary_characters': len(summary),
                        'source_events_sha256': before})
    return {'updated': len(results), 'events_modified': 0, 'records': results}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(refresh(args.root.resolve()), ensure_ascii=False, indent=2))

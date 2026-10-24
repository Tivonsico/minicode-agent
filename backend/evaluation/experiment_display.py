"""Human-facing task identities; archived evidence stays untouched."""


def describe(record):
    title = record['title']
    source = record.get('source', '')
    paper = 'UndoBench' if record['category'] == 'recovery' else 'Knowledge Triage'
    source_key = 'paper'
    condition = title.split(' / ')[-1]
    focus = False
    if record['category'] == 'recovery':
        tasks = {'数据库': ('transfer', '账户转账'), '拆分付款': ('payments', '给两家商户付款'),
                 '消息广播': ('alert', '发送安全告警'), '邮件': ('mail', '邮件发送中断后恢复')}
        key, name = next((value for prefix, value in tasks.items() if title.startswith(prefix)), ('other:' + title, title))
        if key == 'mail' or key.startswith('other:'):
            source_key = 'custom'
        variants = {'B0': '论文原方案：直接重试', 'B2': '论文对比方案：使用幂等键',
                    'B6': '论文对比方案：先核实再重试', '原生恢复协议': '本项目改进后：记录状态并核实结果'}
        variant = next((label for token, label in variants.items() if token in title), '原始运行记录')
        condition = {'正常对照': '未发生中断', '响应丢失': '请求已执行，但响应丢失',
                     'checkpoint 后': '恢复点已记录后中断', 'checkpoint 文件落盘前': '恢复点文件写入前中断',
                     'UNKNOWN 记录后': '已记录“结果未知”后中断', '分发前': '工具请求发出前中断',
                     '外部执行后、记录前': '外部操作已完成，本地尚未记录时中断',
                     '工具结果记录后': '工具返回结果已记录后中断',
                     '业务结果记录后': '业务结果已记录后中断'}.get(condition, condition)
    elif '/native-' in source:
        key, name = 'recall', '长对话中找回早期规则'
        if 'native-session-v3' in source:
            variant, condition = '原方案：自动摘要', '早期试跑（上下文预算 12k）'
        elif 'native-session-v4' in source:
            variant, condition = '原方案：自动摘要', '多轮主实验（上下文预算 16k）'
        elif source.endswith('/control'):
            variant, condition = '对照方案：保留全部历史', '规则恢复提问（上下文预算 128k）'
        elif 'native-recall-control' in source:
            variant, condition = '原方案：自动摘要', '压缩后再次询问早期规则'
        elif 'fix-v3' in source:
            variant, condition = '改进尝试：主动找回原始要求', '第 3 次验证'
        else:
            variant = '改进尝试：提供历史检索工具'
            condition = '第 2 次验证' if 'fix-v2' in source else '第 1 次验证'
        focus = 'native-recall' in source
    else:
        parts = source.split('/')
        key = parts[-2] if len(parts) > 1 else title
        name = {'sample-0040-claude': 'Econprior 项目配置压缩',
                'sample-0187-claude': '泳课预约项目配置压缩',
                'sample-0271-other': 'Kotlin 开发规范压缩',
                'payments': '支付任务的多轮要求保留', 'migration': '迁移任务的多轮要求保留'}.get(key, title)
        source_key = 'paper' if '/paper-pilot-' in source else 'custom'
        variant = {'minicode_original': '原方案：现有摘要策略', 'vanilla': '论文对比方案：普通压缩',
                   'typecompact_visible_state': '论文方案：按内容类型压缩（仅可见文本）',
                   'typecompact_author_state': '论文方案：按内容类型压缩（含内部状态）',
                   'structured': '对比方案：结构化摘要', 'truncate': '对比方案：直接截断',
                   'plain': '对比方案：普通摘要', 'no_compaction': '对照方案：保留全部历史'}.get(parts[-1], '原始运行记录')
        condition = '相同素材下的压缩对比'
    source_label = f'{paper} 论文 / 开源项目' if source_key == 'paper' else '自行构建的任务'
    return {**record, 'source_key': source_key, 'source_label': source_label,
            'task_key': f"{record['category']}:{source_key}:{key}", 'task_title': name,
            'variant_key': variant, 'variant_label': variant,
            'condition_key': condition, 'condition_label': condition, 'focus_last_turn': focus}

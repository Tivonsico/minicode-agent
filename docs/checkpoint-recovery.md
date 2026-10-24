# 事件日志与上下文恢复

2026-10-24。独立任务进度 Checkpoint 已移除，不再保存 objective、phase、notes、next_step，也不再向模型注入 runtime_state。原始 JSONL 或 MySQL 事件是执行事实来源，模型通过压缩摘要、后续消息和实际工作区理解任务。

## 接单和工具恢复

返回接单成功前，run_accepted 保存输入原文、稳定消息 ID、run/turn ID 与冻结模式配置。SessionStore 先追加、flush、fsync，再发布事件。写入结果不明时冻结 writer，重新打开后核验日志；恢复本身不会调用模型或执行工具。

恢复层读取 tool_start、tool_running、tool_result 和 operation_reconciled：

- 已有可靠结果时保留结果，不因进程中断而覆盖成功或失败。
- 已记录工具开始、但尚未进入 tool_running 时，记录 cancelled。
- 已进入 tool_running、但没有可靠结果时，记录 unknown。
- 已保存的核验结论引用原结果 seq，保留证据，不改写旧事件。

`backend/context/builder.py` 负责历史投影与模型输入组装。缺失工具结果补 unknown；持久化 unknown 结果带 `[UNKNOWN]` 标记和禁止自动重试说明。cancelled、failed、rejected 等明确状态也传给模型。工具身份由 turn/request/call 共同确定，不能把 call_id 当全局 ID。

有未知副作用时，执行层继续阻止后续派发。`POST /api/sessions/{sid}/operations/reconcile` 要求会话空闲、核实证据非空并确认旧进程停止；保存结论后才解除匹配的共享阻塞，不重跑工具。工具审批仍由运行时的单次 ticket 和审批事件负责。

## 压缩恢复点

`backend/context/compation_ckeckpoint.py` 的 `CompactionCheckpoint` 保存 context_compressed 事件：原始历史长度和摘要指纹、prepared_history、覆盖范围与估算大小。builder 校验历史前缀后复用压缩结果，并追加后续消息，系统指令每轮重新组装。核验改变了工具消息时，原前缀指纹不再匹配，旧缓存不会覆盖新证据。

摘要保留约束、决策、发现、下一步、相关文件、证据引用、未解决问题及已否决方案。摘要是模型生成的参考资料，不决定工具是否成功。旧六字段 prepared_history 仍可复用。

## 日志加速快照

本地 `journal_snapshot.json` 只保存已提交日志边界、消息和运行投影、未闭合生命周期与文本片段的恢复种子。它没有独立任务状态，也不会追加 checkpoint 事件。run_end 和 context_compressed 后自动更新缓存。

启动校验 schema/reducer 版本、session ID、设备/inode、字节偏移和边界记录摘要后读取后缀。缓存缺失、旧版或损坏时回放完整日志。完整损坏记录报错，仅未完成尾行可以保存诊断后修复。旧 checkpoint.json 留在磁盘但不再读取；原始消息与 context_compressed 仍可从日志恢复。

完整审计按需读取原始日志。快照含消息投影，大小随历史增长；它只减少读取和重放轨迹的成本。

## MySQL 与界面

MySQL 沿用同一事件和恢复语义，事件、会话元数据和工具操作投影通过短事务提交；mc_journal_snapshots 是独立可重建缓存。原 mc_checkpoints 不再读取，mc_sessions.state 列仅为旧表兼容保留，新会话写空对象，此后不更新、不参与恢复。数据库等待交给有界线程池，模型、Shell、文件和审批等待都在事务外。

运行记录界面保留摘要历史和“执行恢复”中的未知操作核验，移除任务状态编辑与手动同步入口。`/checkpoint-state` 和 `/checkpoints/sync` 已移除。项目文件恢复仍使用独立的[文件快照](file-snapshots.md)功能。

## 显式业务操作的持久恢复协议

`backend/recovery/operations.py` 与 `tools.py` 提供本地模式的显式接入协议。运行方批准的 intent 与动作槽决定稳定 operation_id，参数首次绑定后冻结；模型调用ID、新轮次和进程重启不会生成替代业务操作。发送前先可靠保存 UNKNOWN，确认响应保存后复用结果。

接入工具仍通过原生审批、tool_running、执行marker及未知会话门禁。恢复只有在旧执行者停止后才能进行：接收端声明支持稳定键则复用原键；可靠探针按 PRESENT/ABSENT/UNKNOWN 分流；无法核实则保留未知、等待用户。人工核实不能把已有持久成功凭证改为未执行。现有人工核实事件会同步回操作账本，防止两处状态分歧。

首批接入在 `others/harness/recovery_study/native_server.py`，属于独立的本地数据库、支付和模拟邮件实验组，原论文实验组保留。任意Shell、普通文件工具和Gmail未被自动推断业务身份；该账本暂不支持MySQL。显式重复操作需要新的受控意图批准，模型不能调用这一入口。数据库重复意图没有新的可靠凭证时撤销旧余额探针，不能拿第一次余额证明第二次操作已执行。

快照归约版本为5，保留业务意图批准事件；旧版本快照会按原协议回退日志重建。新实验恢复从快照投影读取批准链，避免为取意图额外扫描完整审计日志。实现、限制与真实崩溃证据见[本轮验收报告](research/interruption-recovery-20261023/IMPLEMENTATION-RESULTS.md)。

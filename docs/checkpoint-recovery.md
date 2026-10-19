# 本地 checkpoint 恢复协议

2026-10-19。本地 JSONL 模式继续可用；MySQL 已接入实际执行，详见 [MySQL 协议](mysql.md)。

## 一个状态所有者

`backend/checkpoint/manager.py` 的 `Checkpoints` 接管原 ShortMemory 的目标、阶段、请求次数、待审批工具、人工备注和验证状态。状态更新直接追加 checkpoint 事件并保存恢复快照，不再写独立 task_state.json。旧 journal 中的 task_state 事件仍可读取迁移，旧缓存文件不作为恢复依据。

程序维护执行状态；模型摘要只负责压缩历史，不改变真实的工具状态。checkpoint 不包含任意项目文件的 undo，也不是进程栈或可重新执行的工具队列。

## 接单和持久化

返回接单成功前，单条 run_accepted 事件可靠保存输入原文、稳定用户消息 ID、run/turn ID、冻结模式配置。之后的 ConversationManager 消费同一输入及消息 ID，模型历史去重投影，因此“已接单但原文没保存”的窗口消除。

SessionStore 先追加、flush、fsync，再发布事件与更新投影。同步或投影写入结果不明时冻结 writer；清理路径不能再凭旧内存序号追加。重新打开时验证实际日志位置和序号。另一个 writer 改变文件长度也会拒绝继续写入。

文件大输出先写完整内容、同步文件与目录，再追加 output_saved 和内容哈希。新文件、原子替换、日志尾部修复补目录同步。Windows 目录同步与严格断电语义仍依赖平台，未做实际断电试验。

## Checkpoint 跳过前缀

checkpoint 事件保留在原始 JSONL，便于历史审计；checkpoint.json 是从已提交边界产生的恢复快照，包含：

- schema_version、reducer_version、session_id 和快照完整性摘要。
- journal 的设备/inode 身份、字节偏移、边界记录长度与摘要。
- 当前状态和 checkpoint 记录、covered_seq。
- Context 消息与有效压缩摘要、目录授权、输出引用、当前步骤等必要运行投影。
- 未闭合生命周期与可恢复文本片段的恢复种子。

快照文件中的 covered_seq 包含保存状态的 checkpoint 事件本身；该事件的 covered_seq 指向其前一条事实。两者不冲突：checkpoint 事件不会额外产生工具副作用。

启动校验有效快照后直接 seek 到偏移，只读取后续 JSONL 事件，并从快照加后缀恢复状态。完整日志不会因普通消息投影、目录授权、普通文件路径解析或新 SSE 游标而隐式加载；查看全部日志、导出与历史评测时才按需读取完整审计源。

完整性、版本、日志身份或边界不匹配时回退到全量日志；全量日志中的完整损坏记录会明确报错，仅保存并修复未完成尾行。旧版没有恢复快照的会话走全量恢复，随后自然生成新版快照。快照校验依赖日志前缀遵守只追加协议；它不是对旧文件每个字节重新审计，完整审计仍会验证原日志序号。

快照包含模型消息投影，大小随消息历史增长。跳过的是已覆盖的原始轨迹读取与重放，不承诺整个会话按常数时间恢复，也不删除旧轨迹。

## Context 和中断结果

Context 从持久消息、经 source_digest 校验的 prepared_history 与后续消息重建，系统指令每轮重新组装。request_recovery 标记的废弃流只留审计，不会在重启后变成新 assistant 消息。恢复工具身份同时使用 turn/request/call，不能用供应商 call_id 当全局 ID。

恢复只补事实，不执行工具。未完成 turn/run 被记录为 interrupted；已 tool_running 却缺结果的操作标 UNKNOWN。持久化 operation_reconciled 引用具体结果 seq，保存人工核查结论、证据、操作者以及旧进程已停止的确认。模型看到核查后的工具结果，旧未知证据继续保留。

`POST /api/sessions/{sid}/operations/reconcile` 只在会话空闲、证据非空、确认旧进程停止时接受；过期或重复核对返回冲突。它不自动重试工具。状态人工编辑通过 `/checkpoint-state`，有 revision 检查，不能用状态标签替代操作核对。

## 跨会话执行边界

本地模式采用同一用户下的全局协作式 OS 批次锁，比按单一工作目录锁更保守，因此覆盖不同应用根目录以及嵌套/重叠工作区。批次内部并发安全的读取仍可并行，外部非协作进程不受此锁控制。

副作用开始前，先写可靠 tool_running，再保存共享执行 marker，之后才进行文件/Shell 操作。结果可靠写入后清理 marker；UNKNOWN 或进程崩溃则保留它，其他协作会话不能绕过。marker 保留源 journal 和偏移；结果已提交、清理前崩溃时，可只扫描该后缀证明已知结果并清理，不重跑工具。

人工核对在同一执行锁内先持久化结论及 checkpoint，再解除匹配的共享阻塞。不能因锁释放、租约过期或父进程消失就认为子进程已停止。核实过程中需要人检查真实效果和进程；系统不声称任意 Shell exactly-once 或通用自动回滚。

## 前端与 MySQL

执行模式行的存储按钮提供真实配置与连接检查，始终展示当前实际模式。保存配置后须重启，失败不会切换，也不回退到其他存储。

MySQL 沿用本文的事件、Context、核实和恢复语义。事件与 sessions/operations/checkpoints 投影通过短事务原子提交，checkpoint 按 covered_seq 读取后缀。数据库等待交给有界线程池；LLM、Shell、文件 I/O 和审批等待都在事务外。跨模式执行域限制与旧执行者核实见 [MySQL 存储与恢复](mysql.md)。

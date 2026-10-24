# minicode 模块边界与执行流程

```mermaid
flowchart TD
    UI[浏览器页面] <-->|HTTP 命令和 SSE 事件| API[本机 API]
    API --> Runtime[RuntimeService / RuntimeSession]
    Runtime --> Loop[Agent Loop]
    Loop --> Conversation[Conversation 原始会话与消息投影]
    Loop --> Checkpoint[Checkpoint 当前状态与后缀恢复]
    Loop --> Context[Context 指令组装与预算压缩]
    Context --> Long[Long Memory 当前项目事实]
    Loop --> Provider[Provider 流式请求与响应解析]
    Loop --> Tools[工具调度与操作处理器]
    Tools --> Permission[目录和文件检查]
    Tools --> Approval[Run 绑定的审批票据]
    Tools --> Sandbox[Codex OS sandbox]
    Approval --> UI
```

## 为什么这样拆

Conversation 记录“发生过什么”，Context 决定“本次模型能看到什么”，两者不能共享一个会被摘要覆盖的消息列表。Checkpoint 接管当前状态并保存恢复游标，独立 ShortMemory 已移除，不再创造另一套状态存储。Long Memory 负责筛选跨会话信息，不在读取会话时自动猜测偏好。

Provider 属于 Runtime：它只传输请求和解析响应，不应该存在 conversation 中。Context 的辅助压缩请求仍由 Runtime 的 auxiliary 调用接口承担；Context 负责压缩目标与结果是否可以被采用。工具执行器不构造模型提示词，审批服务不负责页面键盘事件。

一个源文件按一个工程职责组织，而不是机械要求每个文件只有一个函数。工具注册/调度、读文件、搜索、文件变更、技能、Shell 分别维护；领域内部也将预算、提示词、摘要缓存和压缩分开。

## 请求与事件

前端提交任务后，先将输入原文、run/turn 身份与冻结配置可靠保存，再返回 run ID，任务由后端 asyncio Task 管理。浏览器的流连接断开只结束观察者，不取消任务。SSE 从持久会话事件序号之后重放；刷新先加载当前快照再接上后续事件，避免遗漏中间输出。

审批票据由后端生成，只能由对应会话和 run ID 消费一次，等待超时后拒绝。执行中禁止变更当前目录和技能；提示词编辑要求全部任务停下。批准后文件工具再校验授权版本、路径和文件指纹。

原始存储路径沿用 `.minicode/sessions/<ID>/session.jsonl`。`journal_snapshot.json` 保存带完整性校验的日志位置与运行投影，不保存独立任务进度。有效快照通过 schema/reducer 版本、日志身份与边界记录校验后，从字节偏移读取后续事件；坏/旧快照回退完整日志。旧 checkpoint/task_state 事件仅保留审计，旧任务快照不再作为恢复依据。完整审计按需读取原日志；未完成轮次被标为 interrupted。历史投影补全缺失的工具结果为未知状态，不自动重跑。

## 长期记忆

每轮完成后只提交本轮用户消息给提取器。模型提出主题与原文引用，程序验证引用确实出现在源消息中，然后按项目路径与主题合并。保留 message/session/time/version；更新以用户陈述时间为准，删除使用代际 tombstone 防止较早的后台任务复活条目。

后台总结不依赖浏览器关闭。关闭后端时取消未提交的记忆任务；当前没有持久提取队列，因此异常退出时可能遗漏这次后台总结，但原消息仍在。这是后续可靠性工作的边界，不能称为 durable execution。

## 模型能力解析

`backend/models/capabilities.py` 定义带来源的窗口、输出和输入上限；`metadata.py` 读取服务端扩展字段；`catalog.py` 按端点和完整模型名匹配 Models.dev；`cache.py` 保存 24 小时有效缓存；`resolver.py` 采用声明的模型上限，不设置应用 token 默认值或读取环境变量覆盖。服务端缺少输出上限时可由匹配目录补齐，保留各字段来源；仍缺少最大输出则报错。`config.py` 只读取配置并校验，不访问网络。启动入口在创建 Runtime 前解析能力，解析失败不启动任务。

标准 OpenAI Models API 不包含窗口字段，因此目录查询是必要的补充，而且不能包装成官方实时能力接口。未知网关需明确目录服务商映射；模型名不做前缀猜测。原生 OpenAI 调用采用 Chat Completions 与 `max_completion_tokens`，兼容端点使用 `max_tokens`。

## 上下文

每次调用重新组装固定规则、目录能力、全局和相关项目提示词、技能描述、当前项目记忆、历史消息。输入预算扣除最大输出预留；80% 开始尝试摘要，保留最新用户请求与近期完整 assistant/tool 组。摘要必须更小、符合预算且工具配对完整，失败则保留原输入并在超预算时暂停。

独立的 `backend/checkpoint/` 已删除。`backend/context/compation_ckeckpoint.py` 的 `CompactionCheckpoint` 保存 source length/digest 和受预算约束的 prepared history。`context/builder.py` 组装历史及带明确状态的工具结果，不再注入 runtime_state。恢复时原历史前缀必须匹配；原有 context_compressed 事件与旧摘要缓存仍可读取，不需要迁移日志。

RuntimeSession 将同一个 Checkpoints 实例交给 AgentLoop 和 ContextManager。每次准备主模型输入时，ContextManager 从最新快照生成精简的 runtime_state 系统资料块，包含目标、状态、阶段、轮次、步骤、已记录的主请求次数、待审批工具、验证状态及执行模式；它描述上下文准备时的状态。目标超过 512 字符时只投影预览并设置 objective_truncated，完整用户请求与 Checkpoint 原文保留。该块计入输入预算，不进入历史摘要缓存。复用摘要、重新打开会话后也会重新读取状态，避免缓存里的旧阶段覆盖当前事实。人工 notes 和 next_step 不属于可信运行状态。

压缩请求读取同一份 runtime_state 作为参考。模型只输出 constraints、decisions、findings、next_steps、relevant_files、references、open_questions、rejected_approaches 八个字符串数组。open_questions 保存阻塞、未解决的问题与等待用户决定的事项；rejected_approaches 保存失败或已否决的方案及原因，避免在没有新证据时重复尝试。没有相关信息时使用空数组，不猜测补齐。

程序校验字段和类型后，将各分片去重合并为 history_summary。它是未经独立验证的历史语义资料，不能更新 Checkpoint，也不能据此判定任务完成或测试通过。新生成的摘要必须包含八个字段，已保存的六字段摘要缓存仍可直接恢复。格式错误、响应不完整、预算不足或压缩无收益时保留原历史；输入仍超预算则暂停。分片去重不等于事实校验，摘要仍可能遗漏或误述信息。

## Shell

参考 [Codex 安全模型](https://developers.openai.com/codex/security)、[官方沙箱 CLI 实现](https://github.com/openai/codex/blob/main/codex-rs/cli/src/debug_sandbox.rs) 与 [权限类型](https://github.com/openai/codex/blob/main/codex-rs/protocol/src/models.rs)。本项目通过 CLI 提供的明确 PermissionProfile JSON 复用沙箱构造，而不是根据命令黑名单声称完成隔离。

策略为 managed/restricted，允许最小系统读取和授权目录写入，拒绝应用及敏感路径，禁用网络。启动探针在一次性临时目录检查关键边界。Shell 进程使用过滤环境，输出上限、超时和取消由执行模块管理；终止不确定时保留暂停状态。

该执行策略较保守：授权范围外的依赖、用户工具链或缓存也可能不可读/写；联网安装依赖默认失败。不能为了让命令成功退回无隔离执行，后续可显式增加只读运行时依赖授权或有审批的网络策略。

## 前端选择与局限

界面参考 OpenCode 的白色应用布局，使用浅灰导航、居中的新会话输入区和按需打开的工作区；不使用字母商标。设计过程参考 [Anthropic frontend-design](https://github.com/anthropics/skills/blob/main/skills/frontend-design/SKILL.md)，以实际浏览器截图复核留白与可读性。

当前用 HTML/CSS/JavaScript ES modules，因此没有 Node/Vite 编译步骤，浏览器直接加载模块。HTTP/SSE 接口与领域层独立，可在下一轮换成 React。保留轻量 Web 入口比引入桌面壳更符合本轮 demo 重构范围。

本机令牌、Host/Origin 校验与 CSP 防止第三方页面随意调用控制 API；不防御已能读取本机进程/文件的恶意用户，不提供远程多人身份体系。

本轮不新增 trace/span 观测系统、评测数据集、检索或多 Agent；没有未经实验的延迟、成本、成功率指标。


## 本地执行与 MySQL

本地工具批次使用同一用户下的协作式 OS 锁，批次内安全读取仍可并行。副作用前保存持久执行标记，已知结果先落盘再清理标记。UNKNOWN 保留标记以阻塞其他协作会话；人工确认旧进程停止后，在锁内保存 operation_reconciled，再解除阻塞。外部非协作进程仍不受此锁控制，文件版本检查不能省略。

MySQL 已接入 RuntimeService / RuntimeSession，沿用同一事件和快照语义。短事务原子保存事件与元数据/操作投影，LLM、Shell 和文件 I/O 始终在事务外。数据库动作使用有界线程池，同会话序号及 owner 校验保持顺序，持久工作区占用协调冲突。共享 OS 协调和持久执行域限制防止本地模式或另一数据库绕过未知操作。配置和实际验证范围见 [MySQL 协议](mysql.md)，本地细节见 [检查点恢复协议](checkpoint-recovery.md)。

## 项目文件历史

`backend/snapshots/` 由 RuntimeSession 在整轮任务边界调用，包围模型请求与所有执行批次，所以文件工具与 Shell 修改采用相同的前后状态。独立 Git 仓库存完整文件树；会话本地日志或 MySQL 事件记录其身份关联。它与任务恢复检查点分别维护：任务检查点恢复会话和待办状态，文件快照提供用户审阅的文件差异与恢复入口。保留、覆盖边界和安全恢复见 [文件快照说明](file-snapshots.md)。

## 职责细分

Runtime 的模型流与断流恢复由 `model_step.py` 管理，结束前验证与复核状态由 `verification.py` 管理，`loop.py` 负责串联执行步骤。文件原子写入和内部路径检查位于 `storage/files.py`，事件脱敏位于 `storage/redaction.py`；这些操作不依赖具体会话存储。`conversation/metadata.py` 为 JSONL 和 MySQL 统一计算会话元数据，MySQL 不再借用 `SessionStore` 的方法。具体变更与回归证据见 [模块整理与现有功能修复](module-refactor.md)。

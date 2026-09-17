# minicode 实施任务

状态：核心实现与本地自动化验证完成（2026-09-10）。T04 的真实模型配置核实和 T15 的真实 API 验收仍待完成；详见 [验证记录](verification.md)。

按依赖顺序推进，每项控制在一次专注会话内；遇到超出范围的工作先拆分当前项，不增加产品能力。先完成可离线验证的部分，再验证真实 API。用户要求先问完需求再做计划，现有问答已汇总到 [规格](spec.md) 与 [验收清单](checklist.md)。

## 资料与定位

- R1：[spec.md](spec.md)，能力和模块职责。
- R2：[checklist.md](checklist.md)，以条目 ID 定位配置、状态和验收结果。
- R3：用户提供的智谱官方请求地址 https://open.bigmodel.cn/api/paas/v4/chat/completions 及模型选择 GLM-5.3-Flash；示例中的 glm-5.3 不能替换最终选择。
- R4：[智谱对话补全文档](https://docs.bigmodel.cn/api-reference/模型-api/对话补全)，实施时查找请求模型、messages、tools、流式字段、结束原因和 usage。
- R5：[智谱思考模式文档](https://docs.bigmodel.cn/cn/guide/capabilities/thinking-mode)，实施时确认所选模型是否需要保留推理相关字段及参数兼容性。
- R6：[OpenAI Chat 消息参考](https://developers.openai.com/api/reference/cli/resources/chat) 与 [返回及流式示例](https://developers.openai.com/api/reference/python/resources/chat/subresources/completions/methods/create)，仅用于兼容协议参考，实际行为以智谱对应模型为准。

资料状态：已核对智谱官方对话补全与思考模式资料。2026-09-10 当前账户的最小请求确认 `GLM-5.3-Flash` 可用并返回 usage；本地采用保守预算 `CONTEXT_WINDOW=200000`、`MAX_OUTPUT_TOKENS=65536`。官方公开表仍未给出 Flash 5.3 的专属窗口和输出上限，完整流式正文和工具调用仍待验证；不猜测更高上限、不改模型，也不输出密钥。

## T01 配置与项目骨架

- 状态：已实现并通过对应本地测试；依赖：无。
- 影响文件：pyproject.toml、.gitignore、.env.example、config.py，以及 conversation/、agent/、tools/、memory/、context/、app/ 的包入口。
- 工作：建立 Python 包与启动入口声明；定义配置校验和项目根目录解析；忽略真实 .env 与 .minicode/，实际密钥由用户本地填写。
- 验收：缺少密钥或有效预算时给出可操作提示且不发请求；示例不含真实凭据。
- 参考：R2 CFG-01～05、DEF-01；R3～R5。

## T02 内部事件与 API 消息投影

- 状态：已实现并通过对应本地测试；依赖：T01。
- 影响文件：conversation/models.py、conversation/manager.py、conversation/projection.py、others/test/unit/test_conversation.py。
- 工作：区分 session、turn、message、request、tool_call；定义事件序号、时间、状态转换和最终回复语义；从事件重建不重复的消息，保留真实身份及调用关联。
- 验收：文本与工具同时返回不会提前结束；中断片段不会伪装为完整工具调用；终止轮次拒绝迟到事件触发执行。
- 参考：R2 STATE-01～06、API-01～04；R6。

## T03 会话目录与事件持久化

- 状态：已实现并通过对应本地测试；依赖：T02。
- 影响文件：memory/session_store.py、memory/output_store.py、others/test/unit/test_session_store.py。
- 工作：建立每会话独立目录、追加 JSONL、JSON 格式 .meta 和 outputs/；支持片段、最终记录、相对输出引用及元数据重建；恢复时处理未完整落盘的尾记录。
- 验收：中途退出后恢复已保存片段；汇总损坏可重建；重建不重复计算片段和最终消息；输出引用不会指向其他会话或项目外。
- 参考：R2 STORE-01～06、OUT-01～03。

## T04 智谱模型适配与流式解析（真实服务待验证）

- 状态：已实现并通过对应本地测试；依赖：T01、T02。
- 影响文件：conversation/provider.py、conversation/streaming.py、others/test/unit/test_provider.py、.env.example。
- 工作：先重新读取 R4、R5，核实实际请求 ID 与模型能力；实现 OpenAI 兼容客户端适配，组装文本和多个工具参数片段，归一化结束原因与用量；禁用 SDK 隐式重试以免叠加程序重试。
- 验收：多工具流式片段按调用身份正确合并；不完整参数不执行；无输出阶段按限制重试，输出后断流保存并暂停；缺失 usage 标记未知。
- 参考：R2 CFG-01～05、API-01～05、FAIL-01、USAGE-01～03；R3～R6。

## T05 只读工具与文件访问边界

- 状态：已实现并通过对应本地测试；依赖：T01、T03。
- 影响文件：tools/base.py、tools/registry.py、tools/paths.py、tools/read_file.py、tools/glob.py、tools/grep.py、others/test/unit/test_read_tools.py、others/test/unit/test_paths.py。
- 工作：建立注册接口、JSON Schema 校验、路径策略、敏感文件过滤及 ReadFile、Glob、Grep；读取返回版本指纹和定位信息；只允许读取当前会话经登记的内部输出。
- 验收：拒绝目录逃逸、符号链接/目录联接越界及敏感文件；内部日志白名单可分段读取但不可枚举其他内部数据；搜索结果带路径及行号。
- 参考：R2 TOOL-01、PATH-01～05、EDIT-01、OUT-01～03、DEF-02。

## T06 写入、精确编辑与单文件删除

- 状态：已实现并通过对应本地测试；依赖：T05。
- 影响文件：tools/write_file.py、tools/edit_file.py、tools/delete_file.py、tools/approval.py、others/test/unit/test_write_tools.py。
- 工作：创建文件、基于唯一旧文本的精确替换、单文件删除；覆盖或编辑校验先前读取的版本；删除请求包含目标版本，等待执行器提供有效授权。
- 验收：外部修改导致写入拒绝并提示重新读取；重复匹配拒绝编辑；目录删除被拒绝；审批后目标变化使删除授权失效。
- 参考：R2 EDIT-01～04、DEL-01～03、APPROVE-01～03、DEF-03。

## T07 PowerShell 执行与输出保存

- 状态：已实现并通过对应本地测试；依赖：T03、T05。
- 影响文件：tools/shell.py、tools/processes.py、others/test/integration/test_shell.py。
- 工作：使用 PowerShell 在项目目录启动命令，要求有效批准；捕获输出、退出码及超时，保存超长输出；提供取消和进程树终止能力。
- 验收：未批准不启动；超时和 Esc 停止后续执行并尝试终止子进程；失败终止或结果无法确认时标记未知，不声称已回滚；无可见额外终端窗口。
- 参考：R2 SHELL-01～04、OUT-01～03、APPROVE-01～03。

## T08 工具分组调度与审批

- 状态：已实现并通过对应本地测试；依赖：T05、T06、T07。
- 影响文件：tools/executor.py、tools/scheduler.py、others/test/integration/test_tool_scheduler.py。
- 工作：连续独立只读工具成组、有界并发；写入、编辑、删除、Shell 为全局串行分界；审批绑定参数和目标，处理拒绝、取消、相同失败累计。
- 验收：读 A、读 B、改 A、读 A 的执行顺序符合约定；完成顺序不同仍按原调用关联结果；拒绝不累计失败且不会自动重复申请。
- 参考：R2 TOOL-01～03、APPROVE-01～03、FAIL-02；预测故障：分界缺失导致修改与读取重叠。

## T09 上下文预算与压缩

- 状态：已实现并通过对应本地测试；依赖：T02、T03、T04。
- 影响文件：context/budget.py、context/compactor.py、context/builder.py、others/test/unit/test_context.py。
- 工作：统计有效输入和预留输出；分段与摘要处理大输出、旧消息及工具组；候选压缩成功且协议有效后才替换上下文，保留原始历史。
- 验收：达到约定阈值触发压缩；失败不丢历史；仍能放下则继续，放不下则暂停；当前任务和工具调用关联不被破坏。
- 参考：R2 CTX-01～05、USAGE-01～03；预测故障：压缩失败提前覆盖当前上下文。

## T10 长期记忆后台提取

- 状态：已实现并通过对应本地测试；依赖：T03、T04。
- 影响文件：memory/long_memory.py、memory/extractor.py、memory/background.py、others/test/unit/test_long_memory.py。
- 工作：正常最终回复后从用户明确陈述提取事实，保存来源与版本，提供查看和删除；后台结果提交时合并最新状态，避免旧任务覆盖新事实或复活删除项。
- 验收：模型猜测不写入；更新按用户陈述时间确定新旧；中断不触发；失败不影响会话；慢任务不阻塞输入，辅助用量单独记录。
- 参考：R2 MEM-01～05、USAGE-02～03、DEF-04。

## T11 agent 主循环与限制

- 状态：已实现并通过对应本地测试；依赖：T04、T08、T09、T10。
- 影响文件：agent/prompts.py、agent/loop.py、agent/cancellation.py、others/test/integration/test_agent_loop.py。
- 工作：连接模型、工具、上下文和事件模块；工具结果回传后继续请求；正常最终回复、截断、中断、失败及达到上限分别处理；统一轮次取消标记。
- 验收：多次工具循环后输出最终回复；达到主请求上限或重复失败上限暂停；辅助请求不占主循环额度；取消后迟到结果不再启动模型或工具。
- 参考：R2 STATE-01～06、LOOP-01～04、FAIL-01～02、CANCEL-01～04。

## T12 会话恢复与单实例锁

- 状态：已实现并通过对应本地测试；依赖：T03、T11。
- 影响文件：conversation/resume.py、memory/project_lock.py、others/test/unit/test_resume.py、others/test/integration/test_project_lock.py。
- 工作：读取和选择会话；恢复不完整执行为中断或未知状态；构造合法 API 历史但不重放工具；通过操作系统锁限制同项目实例。
- 验收：选择会话不执行任何工具；用户继续后才请求模型；已成功副作用不重放；第二实例被拒绝，首实例退出或崩溃释放后可重启。
- 参考：R2 RESUME-01～04、LOCK-01～02、DEF-05。

## T13 终端交互

- 状态：已实现并通过对应本地测试；依赖：T08、T11、T12。
- 影响文件：app/terminal.py、app/commands.py、app/render.py、others/test/integration/test_app_commands.py。
- 工作：输入编辑、流式显示、工具审批、Esc、斜杠命令、会话列表和用量；可采用终端输入与富文本库，具体版本在实施时核实。
- 验收：/session 回车显示列表；选择后查看历史；执行时 Enter 不发送且不排队，草稿保留；/memory 可查看删除；无像素画标志。
- 参考：R2 UI-01～05、APPROVE-01～03、USAGE-01～03。

## T14 接入主流程

- 状态：已实现并通过对应本地测试；依赖：T01～T13。
- 影响文件：app/main.py、app/__main__.py、pyproject.toml、README.md、others/test/integration/test_startup.py。
- 工作：组装配置、锁、会话存储、模型、工具、上下文、记忆与界面；实现启动、退出和资源释放；写明 PowerShell 启动方式、配置、权限边界及会话路径。
- 验收：从项目根目录启动后可新建会话、审批、取消及切换会话；退出不留下继续执行工具的后台任务；缺配置时不创建误导性的成功状态。
- 参考：R1；R2 CFG-01～05、UI-01～05、LOCK-01～02、E2E-01。

## T15 端到端验证（本地通过，真实 API 待验证）

- 状态：已实现并通过对应本地测试；依赖：T14。
- 影响文件：others/test/e2e/test_e2e.py、others/test/support/、README.md、docs/checklist.md。
- 工作：以脚本化模型和临时项目验证完整流程与故障恢复；针对路径边界、调度分界、取消后禁止调度各选一个真实故障变异验证测试能失败，再恢复代码；在配置真实密钥与模型能力后验证智谱请求。
- 验收：完成 E2E-01～04；只将有执行证据的条目勾选，缺密钥、网络或服务能力的检查保留未通过/待验证，不以模拟测试代替真实接入结果。
- 参考：R2 全部验收项，尤其 PATH、TOOL、CANCEL、STORE、CTX 和 E2E。

## 实施说明

实际源码文件有少量归并，详见 [验证记录](verification.md) 的实现文件归并；上方影响文件列表保留原计划定位。T04/T15 尚不能标记为全部验收完成。

\n
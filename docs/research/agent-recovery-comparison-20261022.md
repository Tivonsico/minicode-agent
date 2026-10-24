# 开源 Agent 的恢复执行、幂等保护与恢复成本

核查日期：2026-10-22。按最新要求，本文只分析开源项目，不评估或修改本地 minicode。研究方式为固定版本的源码、测试和官方文档审阅；没有执行这些项目的测试套件、真实模型调用或外部副作用实验。文末实验是具体协议，不是实测结果。

目前更有依据的研究问题是：在保持同等安全约束的前提下，哪些恢复判断能由程序直接完成，哪些确实需要模型重新判断，以及怎样减少后者的输入和工具核查成本。尚无证据把用户感到的 Codex 恢复耗时，归因于模型反复确认状态；本次也没有复现 Codex 或 dsh 的重复副作用问题。

## 1. 研究对象与版本

| 对象 | 固定仓库与 commit | 阅读范围 |
| --- | --- | --- |
| Codex 开源运行核心 | [openai/codex，8c076942cb70fba8e5c4732c23f1faabc4aed484](https://github.com/openai/codex/commit/8c076942cb70fba8e5c4732c23f1faabc4aed484) | sampling、工具分发、rollout 写入和重建、历史配对修复、请求重试、WebSocket 增量输入 |
| DeepSeek Harness，简称 dsh | [deepseek-ai/deepseek-harness，687f282e8625c6b8d11ff58211a90524be2e4317](https://github.com/deepseek-ai/deepseek-harness/commit/687f282e8625c6b8d11ff58211a90524be2e4317) | agent-loop、session repair、checkpoint policy、JSONL 写入所有权、LLM retry、token-meter |
| LangGraph，作为持久执行参照 | [langchain-ai/langgraph，3cd57c038ad827d05f80c6488adea9994156c58c](https://github.com/langchain-ai/langgraph/commit/3cd57c038ad827d05f80c6488adea9994156c58c) | Python Pregel runner、retry、loop 和恢复测试 |
| LangGraph 官方文档源码 | [langchain-ai/docs，23152b1133e7759381661e93bcdca778101a80bc](https://github.com/langchain-ai/docs/commit/23152b1133e7759381661e93bcdca778101a80bc) | Functional API 的恢复及幂等契约 |

dsh 的身份先由项目既有笔记定位，再由官方仓库 README 确认；不是从缩写猜测。旧笔记未能固定其 SHA，本次已经补齐。Codex 复用既有工具研究的固定版本，并通过 GitHub API 确认该 commit；它不是“用户安装版本”的证明。LangGraph 是执行框架，不能把它与两个完整编码 Agent 当成同层产品排行榜。

Codex 官方文档说明可以恢复保存的聊天，配置文档分别列出 HTTP 请求重试和 SSE 断流重试。源码结论只适用于上述开源路径，不能据此断言桌面客户端、远程执行服务和云端全部采用相同行为。[官方 CLI 文档](https://learn.chatgpt.com/docs/codex/cli)、[官方配置文档](https://learn.chatgpt.com/docs/config-file/config-reference)

## 2. 先分清四件事

恢复对话，是从已保存事实重建下一次模型输入。恢复执行，是决定下一步动作及是否重新调用某个操作。调用 ID 把请求和结果配起来；业务幂等键才用于识别“这次用户意图对应的同一个业务操作”。两种身份不能直接互换。

安全停住意味着拒绝盲目重放，但可能无法完成任务。自动恢复意味着能继续执行，却不天然保证副作用不重复。业务幂等需要工具或远端系统认可稳定操作身份。端到端 exactly-once 还需要处理“远端成功、本地不知道”的窗口；仅有日志、checkpoint 或互斥锁均不足以推出这种保证。下文按每个项目实际提供的机制讨论，不把未实现通用事务视为缺陷。

| 项目 | 程序确定的恢复事实 | 工具重放边界 | 未知副作用的处理 |
| --- | --- | --- | --- |
| Codex | 已收到的完整输出项、工具结果、rollout 上下文、缺失结果的配对修复、请求重试策略 | 网络失败前可能已有完整工具项执行；重试前会收集在途工具结果，再用更新后的历史构造请求 | 缺失结果可补为 `aborted`，它不是“远端未发生”的证明；未在所查普通分发路径中看到通用业务对账账本 |
| dsh | 事件日志和派生 surface；开始/未开始/有结果的区分；恢复时补闭合事件 | 所查默认循环在完整模型消息提交后分发工具；恢复补错误结果，不直接执行旧工具队列 | 已记录调用但缺结果标 `TOOL_OUTCOME_UNKNOWN`，提示只重试只读/幂等操作，否则先核查或询问用户 |
| LangGraph | checkpoint 中已有 task 的结果、未完成 task、后续调度 | 普通恢复复用已保存的成功结果；未完成 task 可再次执行，或按配置进入错误处理 | 工具业务方负责幂等键、查询已有结果等；框架明确告知可能重执行 |

表中三种路径分别见下面的固定源码。尤其不能把“模型没有决定日志怎样恢复”误读成“模型不再需要决定任务怎样继续”。

## 3. Codex：按完整输出项执行，重试使用已经推进的历史

### 3.1 一条请求内，工具可以早于整个响应结束执行

关键路径是：

```text
sampling request
  → OutputItemDone（一个完整输出项）
  → record_completed_response_item
  → 创建并推进工具 future
  → 流继续，可能又到其他输出项，也可能断流
  → drain_in_flight：收集工具结果并记录到历史
  → 把错误交给重试策略
  → 从更新后的历史构造下一次 prompt
```

`handle_output_item_done` 先记录完整工具调用项，再创建工具 future；sampling 在 `OutputItemDone` 时把它加入在途集合。它并不等待整个 `response.completed` 才处理所有工具。因此“模型流断了，所以其中任何工具都没执行”不适用于这条路径。[stream_events_utils.rs:315–357](https://github.com/openai/codex/blob/8c076942cb70fba8e5c4732c23f1faabc4aed484/codex-rs/core/src/stream_events_utils.rs#L315)、[turn.rs:2804–2909](https://github.com/openai/codex/blob/8c076942cb70fba8e5c4732c23f1faabc4aed484/codex-rs/core/src/session/turn.rs#L2804)

流结束后的共同清理路径会 drain 已启动的工具；结果通过 `record_annotated_conversation_items` 加入历史。外层 sampling 重试第一次用初始输入，后续从 `sess.clone_history().for_prompt(...)` 取当前历史。这个设计把“已经知道的工具结果”带入重试，不要求模型从空白重建全部过去。[turn.rs:2568–2595](https://github.com/openai/codex/blob/8c076942cb70fba8e5c4732c23f1faabc4aed484/codex-rs/core/src/session/turn.rs#L2568)、[turn.rs:3283–3317](https://github.com/openai/codex/blob/8c076942cb70fba8e5c4732c23f1faabc4aed484/codex-rs/core/src/session/turn.rs#L3283)、[turn.rs:1739–1835](https://github.com/openai/codex/blob/8c076942cb70fba8e5c4732c23f1faabc4aed484/codex-rs/core/src/session/turn.rs#L1739)

这些是进程仍活着的错误处理路径。SIGKILL、机器掉电、远程服务已经提交但工具客户端没有结果，不能靠 drain 自动变成已知成功。

### 3.2 持久历史与业务幂等的边界

冷恢复根据 rollout 重建模型历史，包括 compaction 和其后事件；这段重建逻辑本身不调用工具。历史规范化会补齐缺少的 function/custom/local-shell 结果，常见占位文本是 `aborted`，同时去除孤立结果，以形成可发送的配对历史。配对合法性不证明操作没发生。[rollout_reconstruction.rs:208](https://github.com/openai/codex/blob/8c076942cb70fba8e5c4732c23f1faabc4aed484/codex-rs/core/src/session/rollout_reconstruction.rs#L208)、[history.rs:968](https://github.com/openai/codex/blob/8c076942cb70fba8e5c4732c23f1faabc4aed484/codex-rs/core/src/context_manager/history.rs#L968)、[normalize.rs:46](https://github.com/openai/codex/blob/8c076942cb70fba8e5c4732c23f1faabc4aed484/codex-rs/core/src/context_manager/normalize.rs#L46)

rollout recorder 有排队、显式 flush、写失败后重开和重试缓冲项等机制。所查写入路径调用文件 `flush`；不能把它直接描述为“每个工具调用前 fsync，且与远端副作用原子提交”。本次没有验证断电耐久性。[recorder.rs:1083](https://github.com/openai/codex/blob/8c076942cb70fba8e5c4732c23f1faabc4aed484/codex-rs/rollout/src/recorder.rs#L1083)、[recorder.rs:1834](https://github.com/openai/codex/blob/8c076942cb70fba8e5c4732c23f1faabc4aed484/codex-rs/rollout/src/recorder.rs#L1834)、[recorder.rs:1908](https://github.com/openai/codex/blob/8c076942cb70fba8e5c4732c23f1faabc4aed484/codex-rs/rollout/src/recorder.rs#L1908)

`executed_tool_calls` 有 seen IDs 和记录合并，但文件明确把自己定义为 best-effort 调用证据记录器，并说明它不能分发或等待工具。不能看到“seen IDs”就推断它在业务执行入口提供持久去重。[executed_tool_calls.rs:1–62](https://github.com/openai/codex/blob/8c076942cb70fba8e5c4732c23f1faabc4aed484/codex-rs/core/src/tools/executed_tool_calls.rs#L1)

并发方面，`ToolCallRuntime` 使用读写锁：声明支持并行的调用取共享锁，其他调用取独占锁。这证明的是该 runtime 的执行协调，不是跨会话、跨进程或远端业务资源的唯一执行。[parallel.rs:51–64](https://github.com/openai/codex/blob/8c076942cb70fba8e5c4732c23f1faabc4aed484/codex-rs/core/src/tools/parallel.rs#L51)、[parallel.rs:202–239](https://github.com/openai/codex/blob/8c076942cb70fba8e5c4732c23f1faabc4aed484/codex-rs/core/src/tools/parallel.rs#L202)

### 3.3 为什么网络恢复可能需要等待

普通流重试受预算和错误类别控制，并可遵守服务器 Retry-After；耗尽后可能从 WebSocket 回退 HTTPS，再继续请求。另有受功能开关及路由限制的连接失败分支，等待从 5 秒翻倍到最多 60 秒，该分支不消耗普通流重试计数。不能把这个有条件分支说成所有安装的默认行为。[responses_retry.rs:23](https://github.com/openai/codex/blob/8c076942cb70fba8e5c4732c23f1faabc4aed484/codex-rs/core/src/responses_retry.rs#L23)、[responses_retry.rs:87–175](https://github.com/openai/codex/blob/8c076942cb70fba8e5c4732c23f1faabc4aed484/codex-rs/core/src/responses_retry.rs#L87)

请求也不一定每次都在网络上发送完整历史：WebSocket 客户端在请求属性和历史前缀满足条件时，使用 `previous_response_id` 与增量输入；不满足条件则发送完整输入。传输字节、服务端逻辑上下文、输入 usage 和实际费用应分开统计。[client.rs:1496–1559](https://github.com/openai/codex/blob/8c076942cb70fba8e5c4732c23f1faabc4aed484/codex-rs/core/src/client.rs#L1496)、[client.rs:2137–2157](https://github.com/openai/codex/blob/8c076942cb70fba8e5c4732c23f1faabc4aed484/codex-rs/core/src/client.rs#L2137)

## 4. dsh：从事件归约恢复事实，再把未知交给明确的后续决策

### 4.1 开始记录与执行屏障

所查 agent-loop 路径先收完整模型输出，提交 `assistant/message`，再筛出 tool-call blocks 执行。工具调度器先追加 `tool/call`，结果按模型顺序追加 `tool/result`；并行安全的调用可以重叠执行，exclusive 调用作为顺序屏障。[agent.ts:504–555](https://github.com/deepseek-ai/deepseek-harness/blob/687f282e8625c6b8d11ff58211a90524be2e4317/packages/core/agent-loop/src/agent.ts#L504)、[tool-calls.ts:146–184](https://github.com/deepseek-ai/deepseek-harness/blob/687f282e8625c6b8d11ff58211a90524be2e4317/packages/core/agent-loop/src/tool-calls.ts#L146)

挂载 `session-checkpoint-policy` 时，模型请求发出前先 flush 对应 session，顶层工具 body 前也先 flush；flush 失败则不调用下游。嵌套工具复用外层调用的屏障，不为每个子调用建立独立同等屏障。结果批次在后续请求边界 flush，故远端成功后、结果持久化前仍有不确定窗口。[checkpoint policy:20–81](https://github.com/deepseek-ai/deepseek-harness/blob/687f282e8625c6b8d11ff58211a90524be2e4317/packages/session/session-checkpoint-policy/src/index.ts#L20)

这是插件组合的契约。不能把“可以挂载这个策略”写成任何自定义 dsh 配置都具有同样耐久性。Session.append 的内存事件与存储 flush 是不同层，持久化文档也把 flush 定义为耐久屏障。[persistence.md:65–112](https://github.com/deepseek-ai/deepseek-harness/blob/687f282e8625c6b8d11ff58211a90524be2e4317/docs/subsystems/persistence.md#L65)

### 4.2 崩溃恢复不直接重放副作用

恢复先 `open(id, 'write')` 取得会话写所有权，再读取有效日志，计算并追加 `interruptedTurnClosers`，最后发布恢复后的 Session。它补齐的是历史事实和生命周期，不是继续旧进程的栈。[agent-loop/index.ts:875–900](https://github.com/deepseek-ai/deepseek-harness/blob/687f282e8625c6b8d11ff58211a90524be2e4317/packages/core/agent-loop/src/index.ts#L875)

`ToolCallRecovery` 程序化维护未回答调用集合；读取到相应结果就移除，未移除的调用按有没有 `tool/call` 区分：

| 持久证据 | 恢复结果 | 下一步边界 |
| --- | --- | --- |
| assistant 请求了工具，但没有工具开始记录 | `TOOL_NOT_STARTED` | 普通中断恢复提示按需要重试；判断依赖调用前持久化屏障 |
| 有工具开始记录，没有结果 | `TOOL_OUTCOME_UNKNOWN` | 只读或幂等操作才可重试；可能有副作用的操作先查外部状态或询问用户 |
| 已有对应工具结果 | 保留结果，不再补同一调用 | 正常继续派生后续上下文 |
| 对应 step 已经关闭 | 不在本次尾部修复中追溯改写 | 该算法不是任意历史异常的全局修复器 |

身份、错误码和集合归约由程序决定；“具体工具是否业务幂等、该查什么、证据是否足够”仍会落到模型、插件或人工流程。这里看到的是保守恢复说明，没有看到通用的远端自动对账服务或对模型换一个 call ID 重试同一业务动作的硬性业务去重。[repair.ts:29–36](https://github.com/deepseek-ai/deepseek-harness/blob/687f282e8625c6b8d11ff58211a90524be2e4317/packages/core/session/src/repair.ts#L29)、[repair.ts:100–196](https://github.com/deepseek-ai/deepseek-harness/blob/687f282e8625c6b8d11ff58211a90524be2e4317/packages/core/session/src/repair.ts#L100)

JSONL 后端另有内核文件锁，排除同一持久会话的并发 writer；进程死亡会释放该锁。这保护会话日志的写入所有权，不代表老进程启动的外部工作一定停止，也不排除另一 session 对同一个远端业务资源发起操作。[lease.ts:1–28](https://github.com/deepseek-ai/deepseek-harness/blob/687f282e8625c6b8d11ff58211a90524be2e4317/packages/session/session-persistence-jsonl/src/lease.ts#L1)

### 4.3 模型重试与成本计量

`llm-retry` 响应 `agent/request-error`，按 provider 的重试策略决定重试，记录 `llm/retry`，等待可取消退避，再记 `llm/retry-started`。这是模型请求重试，不是工具副作用重试。agent-loop 重试复用已经渲染的 assembly，但会重新派生当前请求，所以不要把它描述为无条件逐字重发上次 HTTP body。[llm-retry/index.ts:155–240](https://github.com/deepseek-ai/deepseek-harness/blob/687f282e8625c6b8d11ff58211a90524be2e4317/packages/llm/llm-retry/src/index.ts#L155)、[agent.ts:406–442](https://github.com/deepseek-ai/deepseek-harness/blob/687f282e8625c6b8d11ff58211a90524be2e4317/packages/core/agent-loop/src/agent.ts#L406)

token-meter 区分实际 provider usage、上下文构成估计和预测压力；retry-started 划分 attempt，避免把不同重试的 usage 合并覆盖。其文本估计是字符启发式，不能用构成估计冒充真实计费 token。源码存在这种计量设计，不等于项目已经证明恢复比另一系统更便宜。[token-meter/README.md:36–68](https://github.com/deepseek-ai/deepseek-harness/blob/687f282e8625c6b8d11ff58211a90524be2e4317/packages/llm/token-meter/README.md#L36)

## 5. LangGraph：成功结果复用，未完成 task 可能重执行

LangGraph 对普通恢复明确采用两种规则：把 checkpoint 中成功 task 的输出恢复回任务对象；未完成或中断 task 保持没有成功输出，交给 runner 重执行或错误处理。这些动作由运行时完成，无需 LLM 再推理一遍“哪些 task 已完成”。[\_loop.py:822–837](https://github.com/langchain-ai/langgraph/blob/3cd57c038ad827d05f80c6488adea9994156c58c/libs/langgraph/langgraph/pregel/_loop.py#L822)

runner 收尾时把 task 的结果或错误交给 checkpointer；Functional API 的子 task 如果已有 writes，就直接构造返回结果。若配置 retry policy，失败 task 的执行函数可以再次被调用，重试中还会清理上一尝试的未提交 writes。[\_runner.py:575–615](https://github.com/langchain-ai/langgraph/blob/3cd57c038ad827d05f80c6488adea9994156c58c/libs/langgraph/langgraph/pregel/_runner.py#L575)、[\_runner.py:752](https://github.com/langchain-ai/langgraph/blob/3cd57c038ad827d05f80c6488adea9994156c58c/libs/langgraph/langgraph/pregel/_runner.py#L752)、[\_retry.py:600–674](https://github.com/langchain-ai/langgraph/blob/3cd57c038ad827d05f80c6488adea9994156c58c/libs/langgraph/langgraph/pregel/_retry.py#L600)

所以，一个 task 同时包括“请求外部发信”和“保存结果”，不能仅靠 checkpoint 消除二者之间的崩溃窗口。官方契约要求副作用放进独立 task，并为部分失败、重执行设计业务幂等键或结果核查。它没有承诺任意外部操作 exactly-once。[Functional API 官方源码:794–810](https://github.com/langchain-ai/docs/blob/23152b1133e7759381661e93bcdca778101a80bc/src/oss/langgraph/functional-api.mdx#L794)

task ID、thread/checkpoint 身份解决的是执行结果匹配；对外 API 的操作身份仍需要业务设计。框架恢复、time travel 和 fork 的语义也不能混用：上述成功结果复用结论指普通恢复，\_loop.py 明确把 replay/time-travel 路径区分开。持久 checkpointer 的选择及 durability 配置会影响进程崩溃后还有哪些结果可用，不能用内存 saver 演示证明跨进程恢复。

## 6. 六类故障分别会怎样

以下是前述源码能支持的行为推导；不是本次实测。每行假设只使用已经可靠保存、且属于当前执行分支的证据。

| 故障位置 | Codex 所查路径 | dsh 所查路径 | LangGraph 参照 |
| --- | --- | --- | --- |
| 模型输出流中断 | 已完成输出项可能已触发工具；先收集在途结果，再按错误策略重试 | 失败 attempt 不分发其中候选工具；用户取消可保留安全的部分文本 | LLM task 未完成时可重执行；此前已保存 task 结果可复用 |
| 模型服务请求重试 | HTTP/stream 重试与 transport fallback 分层；不等于重放全部工具 | provider 策略驱动 request-error 恢复；不等于工具重试 | 若把 LLM 放在独立 task，只重试该 task；是否包含工具取决于应用怎样划 task |
| 工具执行中断 | 工具取消/完成语义由 handler 配合 runtime 处理；不能由 aborted 推断无效果 | 存在开始记录、缺结果时形成 unknown 提示；不会在 repair 中自动重跑 | 未完成 task 可重执行；需要工具本身幂等或应用层安全规则 |
| 远端已执行、响应丢失 | 历史结果缺失不证明远端未执行；所查恢复层没有通用对账结论 | 开始记录对应 unknown；核查外部或询问用户，不盲目重试 | task 结果未保存时仍可能被重新调用；业务方处理模糊结果 |
| 进程崩溃后重开 | rollout 重建历史及协议配对，不恢复旧进程栈 | writer 所有权 → 有效日志 → synthetic closers → 派生 surface | 从持久 checkpoint 重新调度，复用已保存成功结果 |
| 成功响应已到、本地尚未保存 | 远端成功不等于恢复可见；恢复依据实际保留下来的记录 | 若调用已持久、结果未持久，仍判 unknown，即使现实中已成功 | checkpoint 没有成功 task 结果时，仍存在重执行窗口 |

这里的代价是对称的：保守策略可能把“实际没执行”和“实际已成功”都放进 unknown；积极重试则必须借助业务幂等或可信对账来保持同一安全标准。源码提供不同取舍，不能只按“谁自动继续得更多”排名。

## 7. 恢复成本怎样拆，哪些结论现在不能下

| 成本 | 应记录的量 | 本次证据等级 |
| --- | --- | --- |
| 上下文重新提交 | 每个 attempt 的实际输入 usage、cached/uncached、wire bytes、请求模板与历史版本 | 源码可确认存在重构请求和增量/完整传输选择；没有配对成本实测 |
| 模型额外生成 | 输出 usage、可获得时的 reasoning usage，恢复后的额外请求数 | 重试可能再次生成；没有 usage 时实际消耗未知，不能按等待时长估算推理 token |
| 模型核查动作 | 恢复后的额外只读工具调用、查询结果输入量、最终动作 | dsh 的 unknown 提示确实要求语义判断/核查；发生频率和开销待测 |
| 退避、超时和网络 | 失败时点、选定 delay、实际重试时点、连接和首输出耗时 | Codex/dsh 的等待策略可从源码确认；用户那次慢恢复的占比未知 |
| 本地恢复 | 日志读取、解码、校验、归约、锁等待、持久化时间 | 两者都有程序恢复；没有相同日志大小/机器条件下的实测 |
| 继续任务前的附加处理 | compaction、初始化、工具/连接就绪等待 | 可能参与恢复路径，必须单独打点，不能全部算成模型核查 |

恢复延迟应按事件时间线分段；模型流、工具和写入可能重叠，不能把各子系统累计耗时直接相加当作墙钟时间。token 总量则按实际 attempt 累计，失去响应的调用也可能产生服务端费用；没有报告的部分保持未知。cached input 是输入子集，reasoning tokens 通常是输出细分，不应重复加进总量。

目前没有证据支持“Codex 每次恢复必定重新发送完整上下文”“dsh 已经把所有恢复成本交给程序”或“恢复慢主要是模型反复确认”。也不能因为本次没找到统一业务幂等服务，就断言项目一定重复操作。

## 8. 可研究的方向与一个小实验协议

建议把题目定为“持久证据驱动的安全恢复：程序归约能减少多少模型核查成本”。这与 dsh 已有设计衔接，也承认成熟系统已经在程序中做了大量恢复工作。待验证的改进是缩短模型还需要处理的那部分，而不是宣称首次让 Agent 获得 checkpoint。

实验先使用独立沙盒和模拟外部服务，不接入真实邮箱。三种恢复策略使用同一个模型及参数、同一个任务切点、同一份持久日志、相同工具与安全闸：

1. A 给完整事实日志，由模型解释状态并继续。
2. B 由确定性 reducer 从同一日志提取最小恢复证据，再交给同一模型继续；保留原始证据只读查询工具。B 不得访问 A 看不到的远端真相。
3. C 单列程序可直接处理的终态，不调用模型；这是产品路径成本参照，不能混进 A/B 的同模型对照中宣称模型更聪明。

任务可设为“完成两次有不同意图 ID 的写操作并生成收据”。模拟服务保存真实提交数，供隐藏验收器使用。工具端持久化操作身份，并统一规定：有开始、无结果的非幂等操作不能自动重放；正向结果查询可证明成功，暂时查不到不能自动证明未执行。先比较不具备自动对账的版本，再另做带查询版本，避免 B 靠额外能力胜出。

在同一执行前缀上依次注入：模型未返回完整调用时断流、工具认领前崩溃、认领后实际执行前崩溃、远端提交后丢响应、响应到达后保存前崩溃，以及结果保存后客户端断连。另加同一业务 ID 的重复/并发调用。记录故障点和持久日志哈希，A/B 分别从完全相同的沙盒快照开始；每类先做三个配对重复，作为可行性筛查，不足以宣称通过率等价。

每个样本报告重复副作用数、任务成功、unknown 停住、是否虚报成功、恢复请求数、恢复工具数、provider 输入/输出及缓存用量、未知 usage 次数和墙钟恢复延迟。本地归约、退避、模型等待、工具等待各自打点。若只有 UTF-8 字节或字符估计，就标明估算方法，provider tokens 保留 null。恢复成本从故障切点算起，共同前缀成本另列；成功与安全均满足后才比较节省幅度。

与 token 构成研究的关系是：先确认额外输入究竟来自工具结果、旧 assistant 历史还是 schema，再决定最小证据应替换什么。与 compaction 后事实一致性的关系是：业务执行状态应以可追溯的程序事实为准，摘要可以帮助模型理解任务，但不能把“unknown”压缩成“没执行”，也不能把旧的成功证据移用到新意图。两者可共用事实验收器，实验时分别改变恢复证据和摘要策略，避免同时改动导致无法归因。

## 9. 已审阅的测试与证据边界

| 项目 | 已读测试源码 | 它验证的意图 |
| --- | --- | --- |
| Codex | [responses_retry_tests.rs:53](https://github.com/openai/codex/blob/8c076942cb70fba8e5c4732c23f1faabc4aed484/codex-rs/core/src/responses_retry_tests.rs#L53)、[retry_after.rs:1589](https://github.com/openai/codex/blob/8c076942cb70fba8e5c4732c23f1faabc4aed484/codex-rs/core/tests/suite/retry_after.rs#L1589) | Retry-After deadline；连接重试不消耗普通重试预算 |
| Codex | [history_tests.rs:2593](https://github.com/openai/codex/blob/8c076942cb70fba8e5c4732c23f1faabc4aed484/codex-rs/core/src/context_manager/history_tests.rs#L2593)、[recorder_tests.rs:935](https://github.com/openai/codex/blob/8c076942cb70fba8e5c4732c23f1faabc4aed484/codex-rs/rollout/src/recorder_tests.rs#L935) | 缺失工具结果的配对；持久化失败后的缓冲写入重试 |
| dsh | [checkpoint-policy.spec.ts:53](https://github.com/deepseek-ai/deepseek-harness/blob/687f282e8625c6b8d11ff58211a90524be2e4317/packages/session/session-checkpoint-policy/tests/session-checkpoint-policy.spec.ts#L53)、[crash-recovery.e2e.ts:92](https://github.com/deepseek-ai/deepseek-harness/blob/687f282e8625c6b8d11ff58211a90524be2e4317/packages/session/session-checkpoint-policy/tests/crash-recovery.e2e.ts#L92) | 先 checkpoint 再 dispatch；SIGKILL 故障点上的 intent 和 unknown 修复 |
| dsh | [resume.spec.ts:479](https://github.com/deepseek-ai/deepseek-harness/blob/687f282e8625c6b8d11ff58211a90524be2e4317/packages/core/agent-loop/tests/resume.spec.ts#L479)、[repair.spec.ts:135](https://github.com/deepseek-ai/deepseek-harness/blob/687f282e8625c6b8d11ff58211a90524be2e4317/packages/core/session/tests/repair.spec.ts#L135)、[lease.two-process.e2e.ts:34](https://github.com/deepseek-ai/deepseek-harness/blob/687f282e8625c6b8d11ff58211a90524be2e4317/packages/session/session-persistence-jsonl/tests/lease.two-process.e2e.ts#L34) | 持久补闭合、已完成不再补结果、跨进程 writer 互斥 |
| LangGraph | [test_pregel.py:891](https://github.com/langchain-ai/langgraph/blob/3cd57c038ad827d05f80c6488adea9994156c58c/libs/langgraph/tests/test_pregel.py#L891)、[test_pregel.py:5909](https://github.com/langchain-ai/langgraph/blob/3cd57c038ad827d05f80c6488adea9994156c58c/libs/langgraph/tests/test_pregel.py#L5909) | pending writes 普通恢复；已缓存 task 不重复提交 writes |

这些是上游测试中可读到的断言和设计意图，本次没有运行，不报告为“测试通过”。也没有测出节省 token、延迟或安全成功率。已经完成的是版本固定和恢复路径核查；程序归约能否减少模型额外核查、最小证据能缩短多少输入、收益是否能保持任务质量，均属于下一步可证伪的假设。

面试叙事可以据实表述为：比较过按输出项执行、事件修复和 task checkpoint 三类路径，识别了模型请求重试与工具重执行的区别，并提出在同等安全约束下测量恢复成本的协议。在取得配对实验结果前，不应说“发现成熟 Agent 的幂等缺陷”或“已经降低其恢复 token”。

复用材料：[DeepSeek 既有调研](../harness/deepseek-research-notes.md)、[可靠性先例](agent-reliability-precedents-20261021.md)、[工具定义比较](agent-tool-definition-comparison.md)。它们用于定位来源；本文对关键行为重新核对固定版本。源文件清单见 [证据索引](agent-recovery-source-index-20261022.json)。

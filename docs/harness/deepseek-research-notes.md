# DeepSeek Harness 与 Minicode 压缩实验的取舍

调研日期：2026-10-21。本文只查公开一手资料、提出实验建议，没有执行 DeepSeek Harness、调用真实模型或得到节省比例。

## 研究对象

用户只说了“deepseek harness”，没有给链接。公开资料中有 DeepSeek 官方的 [deepseek-ai/deepseek-harness](https://github.com/deepseek-ai/deepseek-harness)，[官方产品说明](https://www.deepseek.com/harness/en/)也指向该仓库，因此下文暂按这个项目理解；不能把它等同于所有使用 DeepSeek 模型的第三方 agent，也不能把模型 API 的缓存机制等同于 harness 的压缩机制。如果用户所指是另一实现，结论中的工程细节需重核。

本次阅读的是 master 页面；未成功锁定 commit（GitHub API 限流）。下面的源码链接可能随分支变化，正式复现实验应冻结 SHA、依赖、模型路由、配置与请求模板。

## 从官方实现能确认什么

| 机制 | 公开实现 | 对当前问题的价值 |
| --- | --- | --- |
| 模块化压缩 | compaction-basic 提供自动摘要，tool-result-pruner 提供先裁剪再摘要，token-meter 单独测量 | 可把裁剪和摘要拆成独立变量。见 [包目录](https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/compaction/README.md)。 |
| 默认阈值 | 配置源码默认 `thresholdRatio = 0.8`，近期保留比例另有配置 | “同样 80%”本身没有证明最佳性，必须固定窗口、输出预留、尾部保留量。见 [config.ts](https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/compaction/compaction-basic/src/config.ts)。 |
| 摘要前裁剪 | 超预算工具结果保留头尾，原始内容保留在日志；不调用模型，可能免去摘要；不理解中部语义 | 可构造关键事实在工具输出中部的案例，测静态裁剪的保真代价。见 [pruner 文档](https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/compaction/compaction-tool-result-pruner/README.md)。 |
| 结构化 checkpoint | 摘要要求保留目标、文件、错误修复、未完成工作、当前进度、下一步和约束；更新已有摘要时合并新事实、删除过时事实 | 可借鉴其“事实类别”，重点测用户纠正、旧决定被新决定取代的情形。见 [compaction-basic 文档](https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/compaction/compaction-basic/README.md)。 |
| 计量口径区分 | contextBreakdown 为近似构成，tokenUsage 为已报告用量；usage 仅在匹配请求 envelope 时可作为测量锚点，重试按独立 attempt 归集 | 别用字符估算冒充真实计费；中文与工具 schema 尤其要报告估算误差。见 [token-meter](https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/llm/token-meter/README.md)。 |
| 保留来源 | 压缩事件记录来源范围、被替换节点、估算 token 与模型调用 | 支持从错误摘要追到原始证据，而非只评价最终答案。见 [compaction 子系统](https://github.com/deepseek-ai/deepseek-harness/blob/master/docs/subsystems/compaction.md)。 |

源码有这些机制不代表它们已经在目标任务集上证明更优。本次未发现足以支持“官方默认 80% 在长程 coding 中最佳”或“压缩后通过率不变”的官方对照实验；应写成未找到证据，不能断言项目从未做过测试。

## 建议：Minicode 作主实验，DeepSeek Harness 作参考和后续对照

这是基于可控性的研究判断：用户已有 Minicode 轨迹与请求记录，复用它的成本最低，而且结论能归因到自己控制的摘要/裁剪策略。全量迁移到另一 harness 会同时更换提示、工具 schema、权限、重试、检索、上下文组织和默认配置；即使用同一模型，结果也不能单独解释为压缩改进。

先在 Minicode 内比较策略，再把同一任务、同一模型、同一预算带到官方 harness 做外部效度检查。面试可以讲“读过成熟实现并拆出可验证假设”，不需要把项目升级成两套 harness 的完整排行榜。

Minicode 本地文档已经明确：请求输入需要保留实际 messages/tools/参数；辅助模型请求、重试、usage 未知要单列；历史规则通过不等于隐藏测试通过。可沿用 [运行证据与历史评测](./trace-checkpoint-evaluation.md)，但实验前还需核查当前源码与真实日志，不能只凭旧文档认定字段齐全。

## 最小可解释实验

以下是研究设计建议，不是 DeepSeek 官方方法或已完成结果。

1. 先盘现存轨迹：有多少任务真正经过一次或多次压缩；压缩前后是否都有有效请求快照；能否关联摘要请求 usage；是否有可独立验收的任务。没有压缩的样本可做成本画像，不能证明压缩保真。
2. 选一个小型成熟项目，让 agent 完成一次跨文件改动，并继续做用户纠正后的第二轮改动。避免“从零造大系统”，那会把依赖安装、UI审美、环境等混入失败原因。
3. 在早期对话或工具证据中自然给出 8–12 个可验事实：不可改的兼容接口、不得加依赖、错误码、已经实施/撤回的决定、已失败测试、待处理文件、外部修改后的新状态。每个事实在后续阶段确实影响动作，配可机器验收条件。
4. 让正常仓库阅读与测试输出推动上下文增长，确保至少一次摘要；预算不足可降低所有条件一致的实验输入窗口，但必须明确这是受控压力实验。不要用无意义重复内容硬凑 token。
5. 在压缩边界冻结同一历史前缀与工作区快照，分支续跑：A 完整历史（能容纳的上限窗口）；B 当前摘要；C 同一摘要机制加结构化事实区。若研究裁剪，再加 D 裁剪加摘要，先不要同时改技能加载和所有工具输出机制。
6. 每个条件继续真正执行任务，用独立、固定的验收器判分。边界分支能回答“同一过去换摘要会怎样”，还应补少量从头跑的端到端实验，覆盖策略对后续轨迹长度的影响。
7. 显式区分“摘要没记住”和“后续没遵守”：原始证据 → 摘要是否正确 → 下次请求是否能看见 → 实际动作是否遵守 → 最终任务是否通过。被删掉但可通过文件重读恢复的事实，同时统计找回成本。

首轮做 3 个任务变体 × 3 种策略 × 3 次重复，共 27 次，是筛查规模，不足以宣称严格等效。先报告逐任务结果与不确定性，再决定是否扩样。不能用小样本“没有显著差异”表述“同等通过率已证明”。

成本必须按整个任务累加：主模型输入/输出、摘要输入/输出、重试、事实找回额外调用。单次请求缩短不等于全任务省 token；cache hit/miss 应另列，缓存价格不同意味着 token、费用、耗时可能变化不同。事实保真率和任务通过率都要报：某些约束丢失暂时未触发，不代表摘要可靠。

## 源码补充：阈值和协议边界

[config.ts](https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/compaction/compaction-basic/src/config.ts) 的 `resolveCompactSpec` 实际计算为：`thresholdTokens = floor(min(W × thresholdRatio, W − reservedCompletionTokens − headroomTokens))`。默认 ratio 为 0.8，但默认 headroom 为 65,536；所以不能直接说成“输入预算的 80%”。默认近期保留预算为 `floor((W − reservedCompletionTokens) × 0.16)`，还必须小于阈值。实验使用小窗口时尤其需要显式设置合理 headroom，避免配置直接无效。

[pruner 源码](https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/compaction/compaction-tool-result-pruner/src/index.ts) 只把当前 surface 的 `tool/result` 选为候选，且只裁剪其中 `text` block，其他 block 原样保留；追加替换事件时保留原 event.data 的其余字段。它不是 assistant reasoning 删除器。[summarizer.ts](https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/compaction/compaction-basic/src/summarizer.ts) 末尾仅把摘要响应中的 text blocks 写作 checkpoint；这是对新摘要输出的过滤，不等于任意 provider 的历史 reasoning 字段都可以删除。

因此，本地轨迹若显示 reasoning_content 占比高，它只能形成另一个待验证假设。必须先核查对应模型/API 的历史推理字段要求和工具调用配对，尤其不可在未完整返回 tool results 的 assistant 组中随意删字段。reasoning 裁剪应与摘要保真研究分开做消融，避免把请求协议损坏误判为事实遗忘。

# 消息优先级与压缩相关工作核查（2026-10-23）

本轮只做证据核查，没有改 MiniCode 运行时，没有启动新实验。下面均为作者结果，不能写成 MiniCode 或 DSH 的成绩。

## 推荐顺序

**真实 Coding Agent 优先参考 The Complexity Trap；希望先复刻有明显问答收益的方法可选 SimpleMem；加权公式的直接先例是 Generative Agents。** 三者研究对象不同，不能把对话记忆问答的收益等同于代码任务完成率。

### 1. The Complexity Trap：与 Coding Agent 最接近的固定任务对照

[原论文 v1，§3、§4、表1](https://arxiv.org/html/2508.21433v1)；[作者代码](https://github.com/JetBrains-Research/the-complexity-trap)；[作者原始轨迹与评测结果](https://huggingface.co/datasets/JetBrains-Research/the-complexity-trap)。

在 SWE-agent / SWE-bench Verified 上对比 Raw、旧工具输出遮罩、LLM 摘要。作者公开策略实现、实验配置、运行脚本、轨迹、评测结果和重画图表的 notebook；支持本地 vLLM。仓库 README 给出三组原始配置名称，可直接复用固定任务和验收脚本。

**主要收益是成本，不是普遍的大幅正确率提升。** 表1中 Qwen3-Coder 480B：Raw 完成率53.4%、遮罩54.8%、摘要53.8%；单题费用分别1.29、0.61、0.64美元。论文摘要将 Raw 写成53.8%，与表1不一致，应引用表1并保留说明。不同模型也有退化，不能承诺策略必然更好。

适合本项目：遮罩是必须加入的廉价基线；优先保留用户约束和最终结论、减少旧工具输出后，真实重新执行同一组 coding 任务，用原任务测试判定完成率。仅重放旧轨迹只能测信息与成本，不能测新策略任务完成率。

### 2. SimpleMem：有明显问答提升，且有原始数据和脚本

[论文 v3，§2、表1、表5](https://arxiv.org/html/2601.02553v3)；[作者仓库](https://github.com/aiming-lab/SimpleMem)；[作者测试脚本](https://github.com/aiming-lab/SimpleMem/blob/main/test_locomo10.py)；[LoCoMo 数据与官方评测](https://github.com/snap-research/locomo)。

方法：对话窗口提取自足结构化事实，写入时合并相关记忆，查询时结合语义/词法/元信息检索。它是“写入—检索”的长期对话记忆，不是直接对 Agent 当前消息队列排序。

作者表1：GPT-4.1-mini、LoCoMo，平均F1：Mem0 34.20 → SimpleMem 43.24，增加9.04点（相对26.4%）。表5：去掉结构化压缩为31.29，完整方法43.24。均非 MiniCode 实测。查询 Token 列不是完整生命周期成本；额外存在提取、合并、向量模型和检索成本。原实现使用 LanceDB、Qwen embedding、BM25。

仓库有 benchmark 命令，但当前 main 已包含更新版本；复现需锁定论文对应提交。当前测试脚本有“Token counting disabled”注释，不能直接据该脚本声称复现 Token 成本。先按原始数据、问题和评分脚本对比准确性，再独立记录 API usage。不同分类、F1、LLM judge 口径不要混用。

### 3. Generative Agents：加权方案已有明确先例

[论文 §4.1](https://arxiv.org/html/2304.03442v2)；[作者仓库](https://github.com/joonspk-research/generative_agents)；[检索实现](https://github.com/joonspk-research/generative_agents/blob/main/reverie/backend_server/persona/cognitive_modules/retrieve.py)。

用最近访问时间的指数衰减、模型打出的重要性、与当前查询的语义相关度，归一化后加权排序。论文中时间衰减因子0.995，三个权重均为1。

它验证的是模拟人物行为可信度，不是 Coding Agent 压缩保真度。**“时间+重要性+相关性加权”本身不能称为本项目原创。** 用户提出的消息类型与重复次数是可检验的工程变体；频率不等于重要性，重复报错或模型重复猜测不应因反复出现而挤掉仅出现一次的硬约束。

## 可检验的新策略候选（尚未实现或验证）

“约束保底 + 去重后的消息加权 + 旧工具输出遮罩”：

1. 用户明确的有效硬约束和未完成任务状态有独立最低预算，不受单纯时间衰减淘汰；有明确纠正时更新旧值。
2. 其余按消息角色、轮次指数衰减、相关主题出现频率排序；频率采用 log(1+n) 并封顶，按主题合并，不重复占预算。
3. 工具调用和对应结果作为完整组处理；最近失败、测试结果、修改证据提高保留级别，不能把所有工具消息统一视为无价值。
4. 保持最终输入时间顺序，低分历史生成摘要或可查索引，不能按分数打乱因果顺序。

这是一项待验证的工程假设，不宣称学术首创，也不保证提升。公平实验应锁定模型、任务、预算、工具权限、最大输出与超时，至少比较原方案、纯加权、遮罩、约束保底加权；调参与最终测试分开，报告逐题差异、真实测试通过率、总 Token 和耗时。新增单元测试全通过只能证明代码行为，不能替代上述效果验收。

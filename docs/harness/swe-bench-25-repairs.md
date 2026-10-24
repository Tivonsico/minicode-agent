# 25 题首次测量后的诊断与修复

25 道此前未运行的任务覆盖六个仓库，首轮全部结束：通过/计划为 14/25，通过/已判定为 14/20，判定覆盖率为 20/25。另有 6 道官方判定失败、4 道空补丁、1 道验收环境错误。完整逐题结果见 [首次成绩](swe-bench-25-first-results.md) 和 [机器记录](swe-bench-25-first-results.json)。原始报告、模型输入、轨迹与官方输出完整保留；后续运行没有覆盖它们。

本次修复对象是 Minicode 的执行与验收流程。候选补丁只由被评测的 Minicode 修改，没有改变官方测试，也没有向模型提供官方判分、参考补丁或上次失败轨迹。

## 版本与开发组

首轮后端摘要为 `072469836f260efb7c3221a5135a5c18b8734ccb234965dc1b30ddec6f3212ba`，修复后为 `539fec65214c29278bddd90382727161f6d3f23a0680b38b508f2698c6c4300f`。逐文件前后摘要见 [版本记录](swe-bench-25-repair-versions.json)。原生依赖摘要始终为 `c26729d33b8a65e7f763670ff83f46b3784f9feea7a2e8e024143596de19d5eb`。

按照首轮前的协议，取各仓库在清单中最早一项“已有官方判定但 unresolved”的任务，至多一项。最终开发组为 Django 11564、pytest 11148、Requests 2148、Sphinx 7738；Pylint 和 SymPy 没有符合条件的题。[开发组清单](swe-bench-25-development-manifest.json) 在代码修改和重复运行前固定。它是按失败选出的开发组，此后不能称为未参与调试的验收集。

旧版源码单独冻结并启动，已用相同参数重复一次。原计划新版运行两次，但第一轮在 Django 执行中按用户要求取消，第二轮未启动，因此尚无完成的新版本效果对照。各次使用新候选工作区、同一 Issue/base commit、同一原生依赖和官方镜像。预算保持 GLM-5.3-Flash / auto / low / Agent 600 秒 / 官方测试 900 秒 / 30 次实际请求；重试计入预算。没有固定模型采样种子，选题种子不保证模型输出可复现。已发生的逐次运行见 [30 道任务复核](swe-bench-30-task-review.json)。

## Shell 无差别使读取证据失效

旧版 `backend/tools/shell_calls.py` 在每次 Shell 返回后调用 `executor.reads.clear()`。模型通过 ReadFile 读过文件，再用 sed、grep 或本地测试检查仓库，即使文件没有改变，EditFile 仍会得到 stale_read。重新读取消耗预算，也打断修改流程。

首轮有直接轨迹证据：Django 11564 在读取后执行只读 git log，随后编辑被拒；pytest 7220 多次在读取后用 sed/grep 检查代码，接下来的编辑被判 stale_read。SymPy 15609、22840 和 Pylint 7114 也有同类情况。pytest 11143 在成功 EditFile 后直接继续编辑则不同：成功修改消费读取证据，这种 stale_read 仍然合理。

修复删除 Shell 的无差别清空。`backend/tools/file_operations.py` 的实时文件指纹检查不变，编辑前和实际写入前均核对；成功文件修改仍消费读取证据。Shell 真正修改文件时，旧读取仍不能授权后续覆盖。

真实操作系统沙箱探针的结果如下：

| 操作 | 旧版 | 新版 |
| --- | --- | --- |
| 读取 → 只读 Shell → 编辑 | stale_read 拒绝 | 成功 |
| 读取 → Shell 修改文件 → 编辑 | stale_read 拒绝 | stale_read 拒绝 |

证据见 [修复前](../../others/test_record/diagnostics/swe25-shell-read-before.json)、[修复后](../../others/test_record/diagnostics/swe25-shell-read-after.json)。探针最初遗漏项目指令产生的 setup-error 单独保留，未当作文件工具结果。工程回归覆盖这两条路径；访问范围、写入与联网权限不变。

这些证据证明工具行为已修复，不能量化其对官方成功率的贡献。首轮已有题带着此问题通过，也有失败题本身未实现需求。

## 模型流中断后的有限恢复

首轮 pytest 5103 在 7 次请求后出现 RemoteProtocolError，SymPy 18087 在 18 次实际请求后因 ProviderError 暂停；后者包含一次连接重试。两题没有补丁，但模型启动前均通过依赖预检，不能归为缺少包，也没有用重跑结果替换首次失败。

Provider 原有“收到部分输出后不自动重试”的策略保持不变。修复在 `backend/runtime/loop.py` 的完整响应与工具派发边界进行：仅对 retryable 且已收到部分输出的 ProviderError，每个 turn 最多恢复一次。失败响应的原始 deltas 与 request_end 保留；未完成的文本、推理与工具调用不进入正式模型历史，未完成工具不会执行。

下一次请求从已确认的对话与工作区继续，附带通用系统反馈，不伪造用户消息、不重放此前成功的工具，仍受原来的请求与时间上限约束。取消、不可重试错误、预算耗尽或工具终止状态不触发恢复。前端时间线显示 request_recovery，关联失败请求与 step。

`others/test/integration/test_stream_recovery.py` 验证：已完成的 WriteFile 只执行一次；中断工具不派发；失败片段不进入下一次模型输入；最多恢复一次；30 请求边界不能绕过。原 Provider 部分输出不重试的测试仍通过。四题开发组按官方失败选取，没有包含两道断流空补丁题；若没有实际触发恢复，就只能报告工程证据，不能声称两道断流题已解决。

## 官方镜像与独立恢复验收

为控制 50 GiB Lima 磁盘，准备阶段提取基线后移除了本轮任务镜像。缓存基线使运行器跳过再次提取；官方 swebench 5.0.2 验收入口未指定拉取 platform。在 ARM Docker 引擎上，它请求 linux/arm64，而发布任务镜像为 amd64。

Requests 3362 在首轮报告 `05102d7ae3424aa9abba9158ba8dc34a` 索引 1 出现 environment_error。原始 grade.log 根因为“no matching manifest for linux/arm64/v8”；随后读取不存在镜像的 ImageNotFound 又掩盖了它。模型用了 18 次请求、140.219 秒，输出 2163 字节补丁。这是适配器与准备策略问题，不是一次官方 unresolved。

首轮保持冻结源码，使用外部预取恢复 amd64 镜像，并核对 image_id 等于准备时保存的基线。首轮所有已判定任务均匹配基线镜像。逐次恢复尝试保存在 `.minicode/swe-bench/grading-image-restore.jsonl`，包括一次首轮结束后的 digest 拉取/查找失败，错误没有删除。

修复后，`backend/evaluation/swe_bench/worker.py` 的准备和验收入口统一通过 official_image 显式拉取 linux/amd64，拒绝错误架构，并在调用官方 Harness 前保存镜像元数据。官方 Harness 与测试规则不变。

新增独立的 `POST /api/swe-bench/jobs/{source_id}/regrade`：复用已结束报告指定任务的真实补丁、输入与轨迹，用新 UUID 验收，标记 execution_reused、source_job_id、原始执行状态与用量；不执行 Agent，不把复用轨迹的用量算作新模型请求。

Requests 3362 已在 `fe1f25878e4141d5a6b49ad5b63809f8` 单独重验并 resolved：修复测试 1/1、回归 75/75，新模型请求 0；补丁字节完全相同，镜像 image_id 与首次基线一致。[恢复记录](../../.minicode/swe-bench/regrade-original-environment.json) 单独保存。首轮仍是 14/25；补齐这次环境判定后，同一批首次模型输出的补丁通过数为 15/25，已判定 21/25。这不是新 Agent 版本的成绩，不能用于计算 Agent 修复提升。

## 六道官方失败的实现诊断

| 首轮任务 | 修复 / 回归测试 | 候选补丁与失败解释 |
| --- | --- | --- |
| Django 11564 | 0/2、174/174 | 只调整静态文件模板上下文前缀，未覆盖 storage、STATIC_URL/MEDIA_URL 等行为，目标需求未解决。 |
| pytest 11148 | 0/2、129/129 | 补丁仅增加复现脚本，没有生产代码修复。旧版重复运行已通过，单次失败不代表版本必然失败。 |
| Requests 2148 | 8/10、109/118 | 混合原因：目标 socket-error 测试通过，但补丁将 ContentDecodingError 再次包装为 ConnectionError，造成确定的异常兼容回归。其他失败还包含远端断连及旧 pytest.raises 字符串调用不兼容，不能全部归因于补丁。 |
| pytest 7220 | 0/1、11/11 | 增加 rootdir 参数，但调用路径未接通；只读 Shell 引起的 stale_read 也消耗请求。 |
| Sphinx 7738 | 1/1、29/30 | 去掉下划线转义修复目标症状，但处理位于 napoleon_use_ivar 分支之前，破坏另一个分支。 |
| Requests 1963 | 6/7、111/112 | 重定向目标用例仍把 GET 变为 POST，补丁仅删空行，功能未修复。同时存在 HTTP 502 与旧 pytest.raises 调用不兼容，不能把全部测试失败都解释为 Agent 回归。 |

判断来自首次补丁与官方报告。没有把任务专用提示、隐藏断言或参考解法写入运行时。它们说明修复运行器不等于候选实现正确。首轮结束原因为 RequestLimit 9、verification_required 5、ProviderError 2、stop 9；9 道正常完成中有失败，16 道暂停中也有补丁通过，执行状态不能代替独立验收。

原生与官方 Linux 环境的 Python、Requests 测试依赖及插件方式有差异，见 [环境记录](swe-bench-25-environment.md)。部分轨迹还包含沙箱 Git/命令行工具探测失败，及模型复现脚本或测试命令自身错误。错误逐项保留，不统一解释为缺依赖，也未放宽沙箱帮助通过。

2026-10-23 复核原始日志补充：Pylint 7080 虽通过包导入预检，实际运行 tests/test_self.py 时仍因缺少 py._path 而收集失败；还有模块加载与消息注册错误。因此该题属于原生测试环境不足、执行收敛失败共同影响，不能仅根据 RequestLimit 判为纯模型能力问题。Django 11797 的 Max/connection 导入错误，以及调试打印 SQL Query 触发递归，则来自模型复现脚本。完整分类、逐题验收范围与证据见 [30 道任务复核](swe-bench-30-task-review.md)。官方首轮结果未作调整。

## 工程验证与结论边界

修复后的工程检查为 240 项 Python 测试、4 项前端逻辑测试通过，静态检查通过。真实沙箱探针给出读取证据修复的确定性差异；原补丁重验验证镜像适配；工程用例验证断流恢复的副作用与预算边界。工程测试数不是编程题模型成功率。

开发组按失败选择，只有四题，同时改动多项流程，模型输出仍有随机性。旧版本重复运行已经从 0/4 变为 1/4，所以即便新版通过更多，也不能直接宣称总体成功率提高或归因于某一项修复。再次评价泛化，应另行预先固定未参与诊断的任务。

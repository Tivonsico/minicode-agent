# SWE-bench 官方任务接入

Minicode 生成补丁，SWE-bench 官方 Docker Harness 独立验收。官方题目、`FAIL_TO_PASS`、`PASS_TO_PASS` 和验收脚本不做本地改写。这是官方任务子集接入，不是 SWE-agent，也不是完整榜单成绩。

## 使用

前端选择「评测 → SWE-bench 官方规则 → Lite / Verified → 仓库分类」。同步已下载的官方任务后选择 Issue，每次最多 10 项，顺序执行。可设置模型官方思考档位、执行模式和测试时间上限。Manual / Accept Edit 使用明确的模拟审批策略；Plan 无法提交代码补丁，因此不属于本轮代码修复评测。

任务版本、所选子集、提示词、权限版本、执行模式和运行参数在开始时固定。停止会结束子进程，并只清理当前 UUID 对应的容器；不清理其他 Docker 容器或镜像。查看附件、导出报告不会调用模型、工具或重新执行候选代码。

## 安装依赖

官方包固定为 `swebench==5.0.2`，使用独立虚拟环境，不把其数据科学和 Docker 依赖加入主应用：

```sh
uv venv --python .venv/bin/python .minicode/swe-bench/venv
uv pip install --python .minicode/swe-bench/venv/bin/python 'swebench==5.0.2'
git clone --depth 1 https://github.com/SWE-bench/swe-bench-tasks.git .minicode/swe-bench/task-repo
```

还需要可访问的 Linux Docker Engine。Apple Silicon 使用支持 amd64 的引擎，例如带 Rosetta 的 Lima / Docker Desktop。当前使用官方发布的 `linux/amd64` 任务镜像，运行报告保存镜像 ID 和 registry digest；不擅自将任务的 conda 环境改成 ARM 环境。

应用支持 `SWE_BENCH_PYTHON`、`SWE_BENCH_TASK_REPO` 和 `SWE_BENCH_DOCKER_HOST`；未指定时使用上述项目目录，并优先发现项目内 Lima `swe` 实例的 Docker socket，否则遵从 `DOCKER_HOST` / Docker SDK 默认连接。

当前机器使用 Lima 2.2.1 的官方二进制，SHA256 校验后独立安装在 `.minicode/swe-bench/runtime/`。虚拟机只挂载官方评测 jobs 目录且为只读，不挂载用户主目录；不读取用户 SSH 公钥，禁止自动转发应用端口。Docker 镜像拉取遵从这台机器已有的代理设置。普通 Docker 安装无需 Lima 或本机的 Docker 命令包装器；验收通过 Python Docker SDK 调用。

## 固定来源与验收边界

1. 使用 [官方 task repo](https://github.com/SWE-bench/swe-bench-tasks) 和官方 `published_tasks()` 读取数据集成员，限制为当前数据集 `test` 分区。
2. 模型输入仅包含公开 Issue、仓库名、`base_commit`。不传 hints、gold patch、测试补丁、预期测试结果。
3. 准备进程从官方镜像提取 `/testbed` 的基线仓库，安全解压，克隆到应用目录外的独立临时工作区并核对 `base_commit`。缓存只复用基线对象，不复用上次模型的修改。任务结束后删除临时工作区，保留补丁、轨迹及验收证据。
4. Minicode 在现有操作系统沙箱内编辑候选工作区。长期记忆不读取、不提取。当前 Agent 自测运行在本机，不在 Docker 中；模型启动前安装固定依赖并通过沙箱预检，预检失败归为环境错误且不请求模型。正式得分仍以独立 Docker 判分为准。
5. 导出 `git diff --binary`，包含新增文件，排除运行器生成的未跟踪提示词和技能目录。使用官方 predictions JSONL 格式。
6. 验收进程把冻结的官方 task tree 转成官方支持的本地 JSON 数据集，调用 `swebench.harness.run_evaluation.main()`。使用发布镜像，避免每题重新构建 conda 环境。官方 Harness 负责应用补丁、执行测试、解析日志和生成 `resolved` 判定。

每次验收使用新的 run UUID，避免官方日志缓存误命中以前的补丁。官方依赖的进程不继承模型 API 密钥。任务测试数据、输入证据与日志留在候选工作区外，不授予 Agent 读取权限。

## 成绩如何解释

`resolved_rate` 是已得到官方 `resolved` 判定的任务中通过的比例；同时展示 `judged / planned` 和验收覆盖率。环境错误、取消、断流、空补丁单独列出，不凭空生成官方判定。覆盖率不足时即使已判定比例为 100%，也不能称为所选全部任务通过。小子集成绩不能外推为完整 SWE-bench 成绩。

运行记录分别保存 `agent_status`、模型用量、Agent 耗时、补丁字节数、Docker 验收耗时、官方报告、镜像摘要和失败阶段。应用重启将未结束任务标为 interrupted，保留已有附件，不自动重放模型和工具副作用。

## 文件位置

- `backend/evaluation/swe_bench/environment.py`：独立官方环境和受控子进程。
- `catalog.py`：官方任务公开投影、数据集选择和验收数据冻结。
- `attempt.py`：隔离 Minicode 执行与实际补丁导出。
- `self_test.py`：固定自测解释器、依赖安装与真实沙箱预检。
- `worker.py`：在独立解释器内调用官方任务与 Docker API。
- `grader.py`：读取官方判定，计算覆盖率与子集指标。
- `jobs.py`：运行生命周期、固定参数、取消和附件管理。
- `backend/api/swe_bench.py`、`frontend/evaluation/swe-bench.js`：前后端入口。
- `.minicode/swe-bench/catalog.json`：已导入任务与官方 Git revision。
- `.minicode/swe-bench/jobs/<job_id>/job.json`：本次报告。
- 同目录的 `<index>/model.patch`、`trace.json`、`inputs/`、`self-test.json`、`dependency-install.log`、`self-test-preflight.log`、`prepare.log`、`grade.log` 和 `logs/run_evaluation/<official_run_id>/minicode/<instance_id>/`：实际证据。
- `others/test/integration/test_swe_bench.py`：适配层的工程测试，不是模型评测成绩。

首次接入先导入 `sympy__sympy-20590`，它同时属于 Lite 和 Verified。官方任务库采用固定 revision 的 sparse checkout；界面显示已导入数量，不声称已经导入全部 300 / 500 项。扩充下载范围后点击「同步官方任务」重新索引；不会改写旧报告或已冻结验收数据。

## 自测依赖与解释器

SymPy 1.7 自测固定 Python 3.9.25、mpmath 1.3.0、setuptools 75.1.0、wheel 0.44.0、flake8 7.1.1 和 flake8-comprehensions 3.15.0，并补充 pytest 7.4.4；直接和间接 Python 依赖共 16 项，版本全部固定在 `self_test.py`。这些依赖独立于 Minicode 主环境和官方 Harness 环境，缓存位于 `.minicode/swe-bench/self-test/sympy-py39/`。安装需要 uv；仅安装进程可以联网，不向其传递模型密钥。

Python 的符号链接和 `pyvenv.cfg` 使用真实解释器路径，避免在受限沙箱里因为 uv 别名路径不可读而回退到系统 Python。Shell 优先使用指定虚拟环境；只读授权仅覆盖该环境及其 Python 标准库，不包含 uv 的整个目录或应用数据目录。工程测试实际验证了虚拟环境包可导入、依赖目录不可写、密钥和工作区外文件仍拒绝访问。

每题启动模型前，在同一沙箱中检查 `python`、`python3` 与 `pytest`，核对全部包版本，确认 SymPy 从当前候选仓库导入，并执行基础符号运算。证据写入 `self-test.json` 与预检日志，前端报告显示解释器与包版本；环境失败不会消耗模型请求预算。当前已配置 SymPy 1.7，其他仓库版本必须先补充对应的自测依赖配置，不能带着缺失环境直接跑模型。

本机自测使用 macOS / Python 3.9.25，官方镜像继续使用原始 Linux / Python 3.9.20；没有改写官方依赖、测试或判分规则。本机自测结果与官方验收结果分别记录。

## 首次实际运行

2026-10-12 完成 `sympy__sympy-20590` 官方验收。模型为 GLM-5.3-Flash，Auto 模式，思考档位 low；官方任务 revision 为 `3d07b464b7b311a0cbfb5ed5b2d8a3b96f84a33d`，官方 Harness 为 5.0.2。

Minicode 在 `sympy/core/_print_helpers.py` 的 `Printable` 类增加 `__slots__ = ()`。官方报告判定 `resolved: true`，FAIL_TO_PASS 1/1、PASS_TO_PASS 21/21。本次仅有一题，不能外推为完整 Lite / Verified 成绩。

Agent 耗时约 267 秒，共 30 次模型请求、32 次工具调用，记录到 229,472 个 token；该用量包含累计输入和输出，不是单次上下文长度，也不等同于费用。本机缺少 `mpmath`，且 Python 版本不兼容仓库所需的 `distutils`，Agent 反复尝试自测，最终因 RequestLimit 暂停。这说明补丁验收成功，但执行过程仍有环境准备与停止策略问题；工程测试通过无法替代这些能力指标。

首次 Docker 判分本身已通过，但适配器读取了旧版日志路径，错误标为环境错误。修正为官方 5.0.2 的 `logs/run_evaluation` 后，复用同一补丁、同一冻结题目，以新 run UUID 重新验收；没有再次请求模型。原始失败记录保留，恢复报告明确标记 `execution_reused` 和 `source_job_id`。

- 原始执行：`.minicode/swe-bench/jobs/8880c5fa9869458ebd69448a516c405b/job.json`。
- 重新验收报告：`.minicode/swe-bench/jobs/1ff4e60b68a14875a8033f0059f9f0f6/job.json`。
- 官方逐项判分：上述重新验收目录的 `0/logs/run_evaluation/` 下 `report.json`。
- 使用的官方镜像 digest：`sha256:3a282752833ce34730ee0621e22033501c993f45742775ca57f04c9ff27178a0`。

## 补齐依赖后的两道任务

2026-10-12 补齐自测环境后，固定选择两道新的 Lite 官方任务，各调用 Minicode 一次，均使用 GLM-5.3-Flash / Auto / low，未根据判分结果重跑模型。任务仍来自同一官方 Git revision；新增文件下载后逐个核对 Git blob SHA1，再纳入 sparse checkout。

| 官方任务 | Agent 结束状态 | Agent 耗时 | 模型请求 | 修复测试 | 回归测试 | 官方结果 |
| --- | --- | --- | --- | --- | --- | --- |
| sympy__sympy-19487：sign 重写为 Abs | completed | 297.078 秒 | 19 | 0/1 | 90/90 | unresolved |
| sympy__sympy-20212：0 的负无穷次幂 | completed | 143.777 秒 | 21 | 1/1 | 32/32 | resolved |

本次报告为 `.minicode/swe-bench/jobs/784a151643924af5b6b16b2a37570589/job.json`：官方判定覆盖 2/2、通过 1/2、环境错误 0。两题均有 `self_test_environment.status=ready`，依赖安装、预检、实际模型输入、轨迹、补丁和官方测试输出分别保存。依赖预检用时不计入 Agent 耗时；Docker 判分分别耗时 14.126 秒和 25.239 秒。

第一题失败不是运行环境问题。Agent 为 `_eval_rewrite_as_Abs` 增加 `arg.is_zero is False` 条件，只支持已知非零符号；符号是否为零未知时仍返回原始 sign 表达式。官方目标用例要求用 Piecewise 表达零与非零分支，新增验收失败。Agent 自增测试却断言未知符号保持原状，说明自测存在对错误实现的自我确认；90 项回归通过不能证明新增需求正确。失败结果原样保留，没有修改官方用例或手工修补候选代码以提高成绩。

第二题修改了 `Zero._eval_power`，采用 `is_extended_positive` / `is_extended_negative`，覆盖正负无穷指数。模型实际运行了候选仓库已有测试，本机记录为 222 passed、1 skipped、3 expected to fail；最终仍以官方报告的 FAIL_TO_PASS 1/1 与 PASS_TO_PASS 32/32 为成绩。

这两题与上一题不同，不能用耗时差异计算“依赖补齐后的性能提升百分比”；应在同题同参数重复运行后再做比较。

## 官方参考链接

- [SWE-bench 快速开始](https://www.swebench.com/SWE-bench/guides/quickstart/)
- [官方 Docker 验收入口](https://github.com/SWE-bench/SWE-bench/blob/main/swebench/harness/run_evaluation.py)
- [官方任务数据](https://github.com/SWE-bench/swe-bench-tasks)

本地微型任务评测的题目和成绩仍见 [固定编程任务评测](coding-benchmark.md)，两种报告不混用。

2026-10-12 新增两项官方任务的基线、两次 Agent 修复复测、进程退出码探针和完整修改记录见 [Agent 修复记录](agent-repair-2026-10-12.md)。最后一轮通过 2/2；前两轮的失败报告原样保留，该小样本不用于估计完整榜单成绩。

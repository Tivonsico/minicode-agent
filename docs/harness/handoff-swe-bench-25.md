# 交接：25 道新的 SWE-bench 官方任务

交接日期：2026-10-12。项目目录：`/Users/mac/PycharmProjects/minicode-agent`。

## 用户现在要求做什么

继续运行 25 道未参与调试的 SWE-bench 官方编程任务，覆盖多个仓库。先固定清单、选题原则和运行参数，再开始调用 Minicode。保留每次失败、超时、环境错误及复测。发现问题后修复 Minicode 自身，并将定位依据、代码修改、验证结果和技术取舍保存为 Markdown。

本对话用于改代码和实际评测；简历讨论已经要求移交其他对话。用户强调模块简洁、命名清楚、同类功能集中，不写没有必要的代码。不要回退项目现有的大规模重构，不要把旧目录恢复回来。没有要求提交 Git commit 或 PR，也没有授权主动使用子 Agent。

## 本会话的发展与当前实现

项目已从旧终端结构重构到 `backend/` 与 `frontend/`，核心是自己的 Agent Runtime：模型请求、工具执行、审批、沙箱、上下文、短期与长期记忆、检查点和轨迹。可查看实际输入、执行时间线、压缩摘要和状态；记忆与检查点有前端管理入口。模式使用 `manual`、`accept_edit`、`plan`、`auto`，权限与提示词版本在相关记录中留有标识。前端还支持模型思考档位。

有三种不同的验证，不能混写成绩：工程测试验证代码行为；本地固定编程评测是自己设计的小型任务和独立验收；SWE-bench 使用官方 Issue、仓库版本和 Docker 验收器。工程测试数不是模型完成任务的成功率。具体实现以代码和 `docs/architecture.md`、`docs/harness/` 为准。

启动入口为 `start.py`，关闭为 `stop.py`。用户要求每次启动终止原项目进程再启动，每次关闭直接终止当前项目进程。评测运行期间避免随意重启，应用启动会将未完成任务标记 interrupted，不会自动重放副作用。

## 已经做过的题：全部排除

目前已运行的不同官方题共 5 道，均为 SymPy：

| 任务 | 实际结果与用途 |
| --- | --- |
| `sympy__sympy-20590` | 补丁官方验收通过；Agent 缺依赖并触及 30 次请求上限暂停；验收适配器曾读错日志路径，复用原补丁重新验收，未重跑模型 |
| `sympy__sympy-19487` | 依赖补齐后首次执行失败，修复测试 0/1、回归 90/90；自增测试确认了错误假设，失败保留 |
| `sympy__sympy-20212` | 首次通过，修复测试 1/1、回归 32/32 |
| `sympy__sympy-20049` | 基线失败、第一次修复复测仍失败、第二次复测通过；属于调试任务 |
| `sympy__sympy-20154` | 基线和两轮复测均通过；属于调试任务 |

选新题时还要扫描 `.minicode/swe-bench/jobs/*/job.json`，排除后来已经执行过的所有 instance_id；Lite 与 Verified 重叠不能算两道新题。

最后两题的全部六次运行见 `docs/harness/agent-repair-2026-10-12.md`：

- 基线报告：`2bcc881e762c46c3b7c6f8c87f174fe6`，1/2 通过，环境错误 0。
- 第一次修复复测：`818b74e8bc264ee69cdfe406dd28f0b1`，仍为 1/2，环境错误 0。
- 最终复测：`d8f82d1749b74e41a7ad1b253f761414`，2/2，环境错误 0。

最终 20049 为修复 4/4、回归 9/9，Agent 132.978 秒、15 请求；20154 为 2/2、40/40，166.668 秒、18 请求。两题的通过实现都在新增契约复核请求之前已经写完，复核后没有再改文件。因此不能把通过归因于这一步，也不能宣称总体成功率提升。这些题不能进入新增验收集。

## 最近修复了什么

1. `backend/execution/codex.py`：真实沙箱里的异常经 `tail` 管道过滤后曾返回 0；POSIX Shell 现在启用 pipefail。修复前后探针在 `others/test_record/diagnostics/pipeline-before.json` 和 `pipeline-after.json`。显式 `|| true` 仍能掩盖退出码，不声称覆盖 Windows。
2. `backend/runtime/verification.py`：投影当前 turn 的真实日志，要求成功文件工具修改后的验证为新鲜且 passed；限定范围为文件工具变更，不是任意 Shell 文件差异审计。
3. `backend/runtime/loop.py`：结束前核对，验证缺失、过期或失败最多反馈两次，仍不满足则暂停并保存现场。反馈进入实际系统输入，不伪造用户消息，不重放工具副作用；一次通用契约复核检查隐式默认值、输入范围与旧异常，若再次修改则重新验证。30 次请求上限不变，第 24 次起提醒预算。
4. `backend/runtime/session.py`：成功修改后旧验证状态标记 stale，并保存到短期状态和检查点；失败修改不使证据失效。完成核对读取日志，不信任可手工编辑的状态。
5. `backend/context/system_prompt.py`：先复现和明确需求，再检查修复与回归，不能缩小需求、改变异常来迎合实现；不能以自测通过替代独立验收。
6. `backend/modes/catalog.py`：执行与核对策略纳入权限版本摘要。`backend/evaluation/swe_bench/jobs.py` 保存 backend_code_digest。前端轨迹显示“完成前核对”，核对事件关联实际 run、turn、step、request、message。

最后记录的代码摘要为 `571bd7423489ee1cea3efeba165c0a49488f28310447805c2a2fce5af87f1cd4`。新会话开始仍需检查实际代码与摘要，不能假定无人修改。233 项 Python 工程测试、4 项前端逻辑测试通过，静态检查通过；这不是官方任务成绩。

## 运行环境和入口

先读 `docs/harness/swe-bench.md`、`docs/harness/agent-repair-2026-10-12.md` 及 `backend/evaluation/swe_bench/`。最后核对应用在 `http://127.0.0.1:8765`，项目 Lima 实例 swe 正常；新会话必须重新检查活跃进程和未完成 job，不要凭交接假定就绪。

- 主环境 `.venv` 为 Python 3.12.14；官方环境 `.minicode/swe-bench/venv` 固定 `swebench==5.0.2`，相互隔离。
- 官方任务库 `.minicode/swe-bench/task-repo`，固定 revision `3d07b464b7b311a0cbfb5ed5b2d8a3b96f84a33d`，目前 sparse checkout。新增任务保持固定来源，核对 Git blob；不要直接改写 catalog 冒充同步。
- 最近 catalog 实际导入 Lite 5、Verified 2，Verified 与 Lite 重叠。完整 300/500 的标签不代表全部已下载。
- Lima 2.2.1 位于 `.minicode/swe-bench/runtime/bin/limactl`。指定 `LIMA_HOME` 为项目 `.minicode/swe-bench/lima`；swe 是 VZ/Rosetta，4 CPU、6 GiB、50 GiB。Docker socket 为 `.minicode/swe-bench/lima/swe/sock/docker.sock`。只清理当前 run UUID 的容器，不清理别人的容器与镜像，不挂载主目录或读取 SSH 密钥。
- 当前原生自测只配置 SymPy 1.7：`.minicode/swe-bench/self-test/sympy-py39`，Python 3.9.25、16 项固定依赖。新仓库/版本必须扩充 `self_test.py` 的配置，先安装依赖并在同一沙箱预检；不带着缺依赖的环境消耗模型预算。
- Agent 在本机操作系统沙箱运行，正式验收在原始官方 linux/amd64 Docker 镜像里。原生环境与官方环境分别记录，不修改官方 Python、隐藏测试或判分规则。
- 前端入口“评测 → SWE-bench 官方规则”。API 前缀 `/api/swe-bench`，GET `/environment`、GET `/datasets/{swe_lite|swe_verified}`、POST `/sync`、POST `/jobs`、GET `/jobs/{id}`。检查 API 及模型真实配置，不输出密钥。
- `RunInput` 每 job 最多 10 题，因此 25 题按 10+10+5 顺序执行；不用为此提高限制。仅允许一个官方 job 同时运行。
- 上轮统一参数为 GLM-5.3-Flash / auto / low / Agent 600 秒 / grader 900 秒 / 请求上限 30。默认沿用；新仓库确需不同预算时在任何正式运行前确定并记入清单，不因某题失败临时放宽。
- 报告 `.minicode/swe-bench/jobs/<id>/job.json`；每题索引目录保留 model.patch、trace.json、脱敏 inputs、自测预检、安装日志、准备日志、grade.log。官方日志真实目录是 `logs/run_evaluation/<run_uuid>/minicode/<instance_id>/`。

## 25 题的选题、运行和统计要求

运行前生成一份 Markdown 说明和可机器读取的任务 manifest，固定 25 个不同 ID、数据集、仓库、base_commit、题目来源 revision、选题方法与参数。优先从 Lite 的未运行任务中按明确、可复现的分层方法覆盖多个仓库，不能只选择已知解法或明显简单的题。不为凑数全选 SymPy。选择依据使用公开元数据，不读 gold patch、隐藏测试结果或标准答案筛题。

先准备各仓库环境，检查磁盘与镜像空间；25 题产生多份镜像，不能无计划占满本机。环境问题修复时记录发生阶段和耗时。固定的任务不能因为失败或环境问题被悄悄替换；若确有不可运行项，保留原项与原因，在报告中展示覆盖不足。

第一轮用固定 Agent 版本运行全部 25 题。若为紧急运行器故障必须中途修改，记录每题代码版本并分组统计，不能把混合版本说成同一版本的完整成绩。任务之间使用全新工作区、不读写长期记忆、不继承旧补丁。只给模型公开 Issue 和正常候选仓库，不将官方判分反馈、隐藏测试或 gold patch 加入模型上下文。不要由 Codex 手工修补候选仓库来制造成绩，候选代码只由被评测 Minicode 修改。

保留所有首次执行，包括 unresolved、timeout、RequestLimit、empty_patch、environment_error、cancelled、interrupted。输出通过/计划 25、通过/已判定、判定覆盖率，明确分母。再报告各仓库结果、修复/回归测试、请求与工具调用数、耗时、累计 token、结束原因和失败阶段。累计输入输出用量不是单次上下文长度或费用。

完成首次测量后，再沿轨迹定位通用 Agent 缺陷并修复，写 Markdown；调试和复测另存，不能覆盖首次报告或把“跑到通过”为首次通过率。复测使用新工作区、同题、同环境与预算，记录不同代码摘要。若比较改动效果，对一组预先固定的任务在修复前后同条件运行，并对部分任务重复运行；没有足够重复与对照就不作因果提升声明。

已用来诊断的题此后属于开发集；真正新增验收题应未参与调试。这 25 题是工程验证子集，不是完整榜单，也不是招聘门槛或可靠性保证。用户重视的成果是失败定位和可核对的修复证据，不是包装 100% 小样本成绩。

## 交付

新会话直接开始查环境、固定清单、准备依赖，然后完成 25 项实际 Minicode 执行与官方 Docker 验收。必要下载、安装、模型调用及代码修复已由用户授权，无需反复问能不能继续；不要只给计划。持续用中文汇报进展。最终提供任务 manifest、首次成绩报告、所有失败分类和轨迹链接，以及修复记录 MD；将结果留在现有前端可查的官方评测报告中。

当前 Git 工作区大量文件已修改、旧模块删除、backend/frontend 尚未跟踪，这些属于本项目已完成的重构，不能重置。不要泄露 `.env` 密钥，不改变安全边界来帮助任务过关，不修改官方验收规则。

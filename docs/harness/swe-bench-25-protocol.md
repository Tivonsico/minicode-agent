# 25 道新增官方任务：固定协议

清单在任何新任务模型调用之前固定。官方任务来源 revision 为 `3d07b464b7b311a0cbfb5ed5b2d8a3b96f84a33d`，Harness 5.0.2。扫描了全部历史 job，排除清单中列出的 5 个不同 instance_id。Lite 与 Verified 的重叠不算新任务。

只按公开 task.yaml 的仓库、数据集、分区、ID、版本和 base_commit 选择，未读参考补丁、隐藏测试或判分结果。为适配 macOS 原生自测，本轮范围固定为 Django、Requests、Pylint、pytest、Sphinx 和 SymPy，未覆盖其余六个 Lite 仓库。这个环境范围会造成选择偏差，成绩只描述这 25 题。

种子为 `minicode-unseen-25-2026-10-12-v1`。每个仓库按 SHA256(seed + ":" + instance_id) 升序排列，仓库按名称排序轮转取题，直到 25 项。候选范围共 231 项。本轮 Django 5 题，其他五个仓库各 4 题；选题不参考难度标签、Issue 内容或已知解法。

统一 GLM-5.3-Flash / auto / low / Agent 600 秒 / grader 900 秒 / 每题最多 30 请求。按清单顺序 10+10+5 执行，一次仅一个官方 job。先准备全部依赖和同沙箱预检，再锁定后端摘要开始首次测量。Native Python 与依赖锁会在开始前另存，官方 Docker 镜像保持原样。

失败、超时、请求上限、空补丁、环境错误、取消、中断全部保留；固定题不替换。首次阶段使用同一冻结 Agent 版本，完成 25 项后才诊断通用缺陷和修改运行器。紧急运行器故障必须保留每题版本、原报告与恢复报告。

候选代码仅由 Minicode 修改。每次全新工作区；长期记忆禁用。模型只收到公开 Issue、基线仓库、运行预算与正常自测环境说明，无法读取官方测试补丁、gold patch、判分反馈或应用数据。

统计通过/计划25、通过/已判定和判定覆盖率，逐仓库与逐题保留请求、工具、token、时间和结束原因。修复后的同题复测另存，成为开发集；小样本不宣称总体成功率。

若需要比较 Agent 修复，对清单中每个仓库第一个首次失败且已判定的任务组成固定开发组；每仓库至多一题。修复前后分别用相同预算、环境和新工作区重复一次，再报告所有运行，不从单次随机输出作因果结论。环境或适配器恢复不与 Agent 效果混写。

|序号|批次|任务|版本|
|---|---|---|---|
|1|1|django__django-11797|3.1|
|2|1|psf__requests-3362|2.10|
|3|1|pylint-dev__pylint-7080|2.15|
|4|1|pytest-dev__pytest-5103|4.5|
|5|1|sphinx-doc__sphinx-8435|3.4|
|6|1|sympy__sympy-18087|1.6|
|7|1|django__django-11564|3.1|
|8|1|psf__requests-863|0.14|
|9|1|pylint-dev__pylint-5859|2.13|
|10|1|pytest-dev__pytest-11148|8.0|
|11|2|sphinx-doc__sphinx-11445|7.1|
|12|2|sympy__sympy-15609|1.4|
|13|2|django__django-12708|3.1|
|14|2|psf__requests-2148|2.3|
|15|2|pylint-dev__pylint-7114|2.15|
|16|2|pytest-dev__pytest-7220|5.4|
|17|2|sphinx-doc__sphinx-7738|3.1|
|18|2|sympy__sympy-17655|1.5|
|19|2|django__django-13660|3.2|
|20|2|psf__requests-1963|2.3|
|21|3|pylint-dev__pylint-7228|2.15|
|22|3|pytest-dev__pytest-11143|8.0|
|23|3|sphinx-doc__sphinx-7975|3.2|
|24|3|sympy__sympy-22840|1.10|
|25|3|django__django-11133|3.0|

正式首轮冻结的后端摘要为 `072469836f260efb7c3221a5135a5c18b8734ccb234965dc1b30ddec6f3212ba`，原生配置摘要为 `c26729d33b8a65e7f763670ff83f46b3784f9feea7a2e8e024143596de19d5eb`。完整版本、模式策略和参数见 `swe-bench-25-frozen.json`。全部 25 题在最终依赖配置下通过真实沙箱预检，环境准备后的补丁为空。初始三批报告 ID 会保存在 `.minicode/swe-bench/campaign-25.json`，首次运行不会被后续复测覆盖。

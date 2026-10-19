# 25 道新增 SWE-bench 官方任务：首次运行

状态：completed。已结束 25/25 项。
通过/计划：14/25（56.0%）；通过/已判定：14/20（70.0%）；判定覆盖率：20/25（80.0%）。

清单按固定公开元数据抽样，覆盖六个仓库；五道旧 SymPy 题已排除。此 25 题子集不能估计完整榜单成绩。正式首轮开始后未改动 Agent、依赖或预算。

后端摘要：`072469836f260efb7c3221a5135a5c18b8734ccb234965dc1b30ddec6f3212ba`。
原生配置摘要：`c26729d33b8a65e7f763670ff83f46b3784f9feea7a2e8e024143596de19d5eb`。
GLM-5.3-Flash / auto / low / Agent 600 秒 / 官方每题测试 900 秒 / 30 请求。镜像下载与初始化另有运行器超时，Docker 总耗时可超过测试预算。

首次报告 ID：`05102d7ae3424aa9abba9158ba8dc34a`、`fb9e2e7b11754c87be8e6c8f13713898`、`eae5a5b158f848e08d3d368a32c638fd`。
前端：评测 → SWE-bench 官方规则 → 历史报告。可查看补丁、实际请求输入、轨迹和官方日志。

结果分类：empty_patch 4、environment_error 1、resolved 14、unresolved 6。
结束原因：RequestLimit 9、verification_required 5、ProviderError 2、stop 9。执行状态与官方判定分别记录。

|仓库|通过/计划|通过/已判定|判定覆盖|环境错误|
|---|---|---|---|---|
|django/django|3/5|3/4|80.0%|0|
|psf/requests|1/4|1/3|75.0%|1|
|pylint-dev/pylint|3/4|3/3|75.0%|0|
|pytest-dev/pytest|1/4|1/3|75.0%|0|
|sphinx-doc/sphinx|3/4|3/4|100.0%|0|
|sympy/sympy|3/4|3/3|75.0%|0|

|任务（链接为原始轨迹）|结果 / Agent 状态|修复测试|回归测试|请求 / 工具|记录到的累计 token|Agent / Docker 秒|结束原因或错误|
|---|---|---|---|---|---|---|---|
|[django__django-11797](../../.minicode/swe-bench/jobs/05102d7ae3424aa9abba9158ba8dc34a/0/trace.json)|empty_patch / paused|—|—|30 / 31|350894|395.720 / —|RequestLimit|
|[psf__requests-3362](../../.minicode/swe-bench/jobs/05102d7ae3424aa9abba9158ba8dc34a/1/trace.json)|environment_error / paused|—|—|18 / 15|136014|140.219 / —|官方 grade 退出码 1，请查看验收日志|
|[pylint-dev__pylint-7080](../../.minicode/swe-bench/jobs/05102d7ae3424aa9abba9158ba8dc34a/2/trace.json)|empty_patch / paused|—|—|30 / 32|578680|262.565 / —|RequestLimit|
|[pytest-dev__pytest-5103](../../.minicode/swe-bench/jobs/05102d7ae3424aa9abba9158ba8dc34a/3/trace.json)|empty_patch / paused|—|—|7 / 6|20224|121.537 / —|ProviderError|
|[sphinx-doc__sphinx-8435](../../.minicode/swe-bench/jobs/05102d7ae3424aa9abba9158ba8dc34a/4/trace.json)|resolved / paused|1/1|16/16|30 / 32|242183|206.163 / 12.661|RequestLimit|
|[sympy__sympy-18087](../../.minicode/swe-bench/jobs/05102d7ae3424aa9abba9158ba8dc34a/5/trace.json)|empty_patch / paused|—|—|18 / 17|106301|253.800 / —|ProviderError|
|[django__django-11564](../../.minicode/swe-bench/jobs/05102d7ae3424aa9abba9158ba8dc34a/6/trace.json)|unresolved / completed|0/2|174/174|28 / 27|374026|405.826 / 24.162|stop|
|[psf__requests-863](../../.minicode/swe-bench/jobs/05102d7ae3424aa9abba9158ba8dc34a/7/trace.json)|resolved / paused|4/4|60/60|18 / 15|106864|159.102 / 119.150|verification_required|
|[pylint-dev__pylint-5859](../../.minicode/swe-bench/jobs/05102d7ae3424aa9abba9158ba8dc34a/8/trace.json)|resolved / completed|1/1|10/10|19 / 17|176202|232.119 / 8.620|stop|
|[pytest-dev__pytest-11148](../../.minicode/swe-bench/jobs/05102d7ae3424aa9abba9158ba8dc34a/9/trace.json)|unresolved / paused|0/2|129/129|30 / 30|564172|586.047 / 25.005|RequestLimit|
|[sphinx-doc__sphinx-11445](../../.minicode/swe-bench/jobs/fb9e2e7b11754c87be8e6c8f13713898/0/trace.json)|resolved / paused|2/2|8/8|30 / 30|341287|263.133 / 32.703|RequestLimit|
|[sympy__sympy-15609](../../.minicode/swe-bench/jobs/fb9e2e7b11754c87be8e6c8f13713898/1/trace.json)|resolved / completed|1/1|121/121|16 / 14|110768|120.498 / 18.121|stop|
|[django__django-12708](../../.minicode/swe-bench/jobs/fb9e2e7b11754c87be8e6c8f13713898/2/trace.json)|resolved / paused|1/1|101/101|28 / 25|203906|202.090 / 21.098|verification_required|
|[psf__requests-2148](../../.minicode/swe-bench/jobs/fb9e2e7b11754c87be8e6c8f13713898/3/trace.json)|unresolved / paused|8/10|109/118|28 / 25|251504|189.527 / 120.395|verification_required|
|[pylint-dev__pylint-7114](../../.minicode/swe-bench/jobs/fb9e2e7b11754c87be8e6c8f13713898/4/trace.json)|resolved / paused|1/1|56/56|30 / 32|367821|428.786 / 19.392|RequestLimit|
|[pytest-dev__pytest-7220](../../.minicode/swe-bench/jobs/fb9e2e7b11754c87be8e6c8f13713898/5/trace.json)|unresolved / paused|0/1|11/11|30 / 30|329512|181.147 / 12.805|RequestLimit|
|[sphinx-doc__sphinx-7738](../../.minicode/swe-bench/jobs/fb9e2e7b11754c87be8e6c8f13713898/6/trace.json)|unresolved / paused|1/1|29/30|30 / 30|205004|160.577 / 8.644|RequestLimit|
|[sympy__sympy-17655](../../.minicode/swe-bench/jobs/fb9e2e7b11754c87be8e6c8f13713898/7/trace.json)|resolved / completed|2/2|9/9|25 / 23|166275|194.280 / 18.822|stop|
|[django__django-13660](../../.minicode/swe-bench/jobs/fb9e2e7b11754c87be8e6c8f13713898/8/trace.json)|resolved / completed|2/2|6/6|11 / 9|53221|46.234 / 18.738|stop|
|[psf__requests-1963](../../.minicode/swe-bench/jobs/fb9e2e7b11754c87be8e6c8f13713898/9/trace.json)|unresolved / paused|6/7|111/112|30 / 30|243659|213.974 / 115.961|RequestLimit|
|[pylint-dev__pylint-7228](../../.minicode/swe-bench/jobs/eae5a5b158f848e08d3d368a32c638fd/0/trace.json)|resolved / completed|2/2|10/10|24 / 24|218498|179.715 / 20.002|stop|
|[pytest-dev__pytest-11143](../../.minicode/swe-bench/jobs/eae5a5b158f848e08d3d368a32c638fd/1/trace.json)|resolved / paused|1/1|114/114|12 / 8|77493|81.694 / 15.437|verification_required|
|[sphinx-doc__sphinx-7975](../../.minicode/swe-bench/jobs/eae5a5b158f848e08d3d368a32c638fd/2/trace.json)|resolved / completed|1/1|7/7|27 / 25|304538|209.185 / 10.194|stop|
|[sympy__sympy-22840](../../.minicode/swe-bench/jobs/eae5a5b158f848e08d3d368a32c638fd/3/trace.json)|resolved / completed|2/2|95/95|25 / 24|234499|175.452 / 21.328|stop|
|[django__django-11133](../../.minicode/swe-bench/jobs/eae5a5b158f848e08d3d368a32c638fd/4/trace.json)|resolved / completed|1/1|64/64|22 / 20|93875|83.456 / 18.691|stop|

记录到的累计输入 token：5731003；记录到的累计输出 token：126417。这些是跨请求累计用量，不是单次上下文长度或费用。共 596 次实际模型请求、571 次工具调用；3 次请求的用量未知，因此累计 token 不代表完整消耗。Docker 时间为 — 的行没有成功验收耗时记录。

完整逐题记录见同名 JSON；所有原始 job、失败日志和附件保留。修复后的复测另存。
环境错误的原补丁恢复验收及开发组对照见 [诊断与修复](swe-bench-25-repairs.md)。它们没有计入或改写本表的首轮成绩。

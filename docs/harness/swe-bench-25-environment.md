# 25 题评测：环境准备记录

当前阶段为模型调用前的依赖准备。清单已固定，所有准备失败与恢复记录均归为环境阶段，不计为模型修复成绩。

原生自测统一使用 macOS / Python 3.9.25。官方判分继续使用各题发布的 linux/amd64 Docker 镜像、原始 Python 和依赖；两种环境不视为完全等价。依赖清单包含全部间接包的固定版本，保存于 `backend/evaluation/swe_bench/self_test_profiles.json`。

配置文件 SHA256：`d89d964969f277edd4169c08397d1eac9338d5e8e95a120ee0b3d2c2ee2ec372`。正式报告另记录标准化配置摘要和逐题 profile_digest。

|配置|固定包数|候选包|
|---|---|---|
|django-py39|13|django|
|requests-py39|42|requests|
|pylint-py39|25|pylint|
|pytest-4_5|20|_pytest|
|sphinx3-py39|35|sphinx|
|pytest-8_0|20|_pytest|
|sphinx-7_1|37|sphinx|
|pytest-5_4|20|_pytest|
|sympy-py39|16|sympy|
|pylint-new-py39|25|pylint|

pytest 使用正常 editable 安装生成忽略的版本文件与入口，发行元数据按公开任务版本固定为 4.5.0、5.4.0 或 8.0.0。安装前后导出的候选补丁必须为空，否则在模型调用前停止。`src` 布局通过当前工作区的 PYTHONPATH 使用候选源码，不能误测已发布的安装包。

原生环境禁用无关第三方 pytest 插件的自动发现，以免 setuptools 内附 typeguard 插件使用旧 pytest 不支持的接口。仓库明确加载的插件和测试仍可正常执行；官方 Docker 的插件规则没有改变。

已观察的准备错误：Pylint 缺 tomlkit；pytest 4.5 缺 six、缺安装版本文件，随后遇到额外 typeguard 插件接口冲突；pytest 8 缺生成的 _version.py；SymPy 配置名称曾错误指向不存在的 profile。均已在模型调用前定位。最后一项是本轮配置编写错误，不归因于被评测模型。

首次 Pylint 失败耗时 227.752 秒，补 tomlkit 后缓存预检 18.101 秒。最初的准备脚本覆盖了这一次失败文件，现已从工具已经返回的原始输出保存到 `preflight-25/2/attempts/original-captured/result.json`，明确注明保存来源；该次完整日志已无法恢复。准备脚本现改为每次重试先归档原文件，不再覆盖。正式评测 job 使用独立 UUID，不复写旧成绩。

全部准备资料位于 `.minicode/swe-bench/preflight-25/<index>/`，重试归档在 `attempts/`，解析锁记录位于 `locks/`。最终还会对全部 25 题在冻结依赖配置下重新预检，保存 profile_digest 与 baseline_patch_empty。

Lima 磁盘 50 GiB，初始剩余约 42 GiB。逐题提取基线后仅移除本轮该题的镜像标签，保留历史五题镜像、其他容器和所有证据；正式判分按原标签重新下载并记录镜像 digest。仅提前拉取接下来的两题，空间不足 12 GB 时停止预取。这个策略增加重复下载时间，但限制磁盘占用。

工程检查：14 项适配层与真实 Shell 检查通过。它们只验证代码行为，不是 25 题的官方成绩。

全部 25 题最终预检通过，profile_digest 与冻结配置匹配，baseline_patch_empty 均为 true。完整准备尝试索引见 `swe-bench-25-preparation-attempts.json`，其中保留 15 次环境失败。

补充错误：同为 Pylint 2.15 的不同 base_commit 对 astroid 的约束不同，7114 与 7228 使用新配置 astroid 2.13.5，依据是公开的 pyproject.toml。Requests 的旧 brotlipy 在本机 SDK 链接失败，原日志保留，原生 HTTP 测试插件改为固定的 pytest-httpbin 2.1.0 / httpbin 0.10.2；没有修改官方 Docker 依赖。原生依赖不与官方环境宣称完全等价。

最终工程检查：233 项 Python 与 4 项前端逻辑测试通过，静态检查通过。这些不是模型评测分数。

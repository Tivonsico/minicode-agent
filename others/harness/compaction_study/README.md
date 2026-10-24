# MiniCode 多轮压缩实验

本目录提供正常会话原生压缩实验、两个前期低成本实验和一个离线预算可行性实验。结果目录：[`docs/research/compaction-reproduction-20261022`](../../../docs/research/compaction-reproduction-20261022/REPORT.md)。没有执行编码任务，不提供任务完成率。没有更改 MiniCode 默认压缩逻辑。

## 正常 MiniCode 会话实验（最新）

最新结果见 [native-report.md](../../../docs/research/compaction-reproduction-20261022/native-report.md)。下面通过正常 Web API 创建会话、单并发逐轮发送原论文配置任务；不直接调用压缩函数。30次请求硬限制已经取消。实验输入窗口显式设为16k，以低成本触发原有压缩。

终端一（需已有模型配置）：

```sh
.venv/bin/python -m others.harness.compaction_study.session_server \
  --runtime-root /tmp/minicode-native-new --input-cap 16000 --port 8877
```

终端二（作者仓库固定版本的获取见下文）：

```sh
.venv/bin/python -m others.harness.compaction_study.live_sessions \
  --url http://127.0.0.1:8877 --runtime-root /tmp/minicode-native-new \
  --upstream /tmp/knowledge-triage --output /tmp/minicode-native-results \
  --min-compactions 5 --max-configs 20
```

达到5次原生历史压缩后在配置边界停止；不足则记录不足。浏览器打开 http://127.0.0.1:8877 可查看正常会话。每份配置的5轮任务输出与历史压缩事件分别记账。

要做无压缩对照，另启相同server，改为 `--runtime-root /tmp/minicode-control-new --input-cap 128000 --port 8878`，然后：

```sh
.venv/bin/python -m others.harness.compaction_study.replay_native \
  --source /tmp/minicode-native-results --output /tmp/minicode-control-results \
  --control-url http://127.0.0.1:8878 --control-runtime /tmp/minicode-control-new
```

它会先在原会话提问，再在新会话重放所有冻结用户输入并询问同一问题。助手回复重新生成；不保证单因素控制。模型也可能超出任务要求的50%长度，实际超出会记录，不能宣称精确复现论文预算。

免费重算已保存原生会话（哈希验证，执行归档评分函数）：

```sh
.venv/bin/python -m others.harness.compaction_study.native_report \
  --output docs/research/compaction-reproduction-20261022/native-session-v4
```

实验性 `--history-recall` 默认关闭。其现有候选未通过真实探针验收，不能作为已验证优化。

## 环境

从项目根目录运行，Python 3.11+，本次实际 Python 3.12.14。沿用项目 `pyproject.toml` 和 `uv.lock`：

```sh
uv sync --group dev --frozen
```

主要实际版本为 httpx 0.28.1、python-dotenv 1.2.3、pytest 9.1.1、pytest-asyncio 1.4.0、ruff 0.16.6。完整依赖沿用项目，实验没有新增依赖或服务。

在线运行读取项目已有配置（包含 `.env`、本地设置、环境变量，优先级沿用 `load_config`），需要有效 `OPENAI_API_KEY`、`OPENAI_BASE_URL`、`MODEL`。本次使用 BigModel 的 GLM-5.3-Flash。适配器显式限定该服务，传入 temperature=0、reasoning_effort=low；不能仅替换模型名就宣称支持其他模型。密钥不会写入实验请求文件。

## 免费离线重算已交付结果

```sh
.venv/bin/python -m others.harness.compaction_study.report \
  --root docs/research/compaction-reproduction-20261022

.venv/bin/python -m others.harness.compaction_study.guarded_run \
  --paper-output docs/research/compaction-reproduction-20261022/paper-pilot-v1

.venv/bin/python -m pytest others/test/unit/test_compaction_study.py \
  others/test/unit/test_context.py others/test/unit/test_context_metering.py -q
```

离线命令不读取密钥、不请求模型。它们校验归档输入和原始 JSON 的哈希；原始材料变动会报错。事实审核中的语义判断不能由程序重新判断，但逐项证据、判断及汇总均可审查、重算。`scores.json` 的 strict 字段保留字符串评分，`adjudications.json` 记录9个措辞等价的事后裁决；不允许把缺失回答裁为正确。路径、命令、代码等标识符区分大小写。

## 重新运行动态历史实验（会产生 API 用量）

```sh
.venv/bin/python -m others.harness.compaction_study.make_data
.venv/bin/python -m others.harness.compaction_study.run \
  --output /tmp/minicode-compaction-new-run --cases 2 \
  --max-tokens 768 --truncate-chars 3072 --concurrency 3
```

输出目录须不存在。相同参数、数据和源码可继续尚未完成的运行；已失败的轮次保留，重新实验应使用新目录。已完成轮次直接读取，输入、源码或配置变化均拒绝续跑。新实验不会自动继承此次事后语义裁决或摘要事实审核，应独立核对后保存同样格式的审核文件。

每份历史有12个约束、6个状态探针，连续5轮。第2轮纠正目标与审阅人，第4轮注入合成的失败测试观察，其他内容为只读检查噪声。所谓测试观察是数据集中的叙事，不是本实验执行了测试。三份可用合成样本都在 `data.json`；本次先取前两份，未根据成绩挑选样本。

A=`plain`；B=`minicode_original`（直接调用真实 `backend.context.compactor.summarize`）；C=`structured`。另加 `no_compaction` 和字符尾窗 `truncate`。B 的原提示不添加摘要长度要求。三种模型摘要共享输出上限，但实际摘要长短不同；本实验不声称等长比较。调用压缩内核，不是完整 `ContextManager.prepare` 的触发与尾部保留流程。集成接口另由现有上下文测试验证。

## 重新运行论文公开样本方法实验（会产生 API 用量）

```sh
git clone https://github.com/searchsim-org/cikm26-knowledge-triage /tmp/knowledge-triage
git -C /tmp/knowledge-triage checkout a6ceb01a3368cee25ef7ebcf05ebdab8c9be24a4
.venv/bin/python -m others.harness.compaction_study.paper_run \
  --upstream /tmp/knowledge-triage --output /tmp/minicode-paper-new-run --n 3
```

使用原作者 Prompt、预处理函数、TypeCompact 和词法评分函数；绕过仅用于集中导出的包初始化，不修改这几个原函数。源码文件和样本会归档。替换项是模型、公开样本及分类器：采用作者正则分类器，不能声称复用了原级联标签。采样先排序再 seed=11 shuffle，固定首批合格样本。作者原脚本排序与样本ID不足，不能保证同20份配置。

`typecompact_author_state` 保留作者未截断状态反馈协议；`typecompact_visible_state` 只将可见文本按作者规则重新分型。二者差异还包括分型，因此不是单因素的因果估计。外部硬字符截断仅用于此压力实验，不是 MiniCode 当前生产行为。

`guarded_run` 的候选将指定约束逐字保留，预算不足则拒绝。拒绝时输入仍超过所请求预算，不能算压缩成功，且不允许从不可见旧状态修补已丢失规则。它只是静态约束实验候选，没有接入默认运行时，也没有验证动态规则撤销或通用语义安全。

## 输出结构

- `manifest.json`：模型、预算、数据及源码标识。
- `inputs-integrity.json`：实际归档输入子集及 Prompt 哈希。
- `evidence-sha256.json`：原始结果与 API 调用记录哈希。
- `raw/.../call-*.json`：真实请求正文、原始响应、响应模型、usage、耗时、失败类型；不含认证头。
- `raw/.../result.json`：每轮输入、携带记忆、回答、原始评分；不随重算改写。
- `scores.json`：可重新生成的评分和逐项明细。
- `source-at-run/`：运行时源码快照；此次旧版 runner 已按 manifest 原哈希精确恢复存档。
- `summary-fact-audit.json`：独立审核全部30份模型摘要的7类事实，记录错误、歧义和缺失。
- `comparison.json`：动态对照、事实审核、全部轮次的 token 和请求耗时汇总。

Token 使用真实 API usage，不以字符数冒充 token。摘要长度另报字符与 UTF-8 字节。延迟是单请求持续时间的和；并行等待和离线人工审核不计入它。没有计算货币成本。论文中的 len//4 估算严格标为 proxy，不能和真实 token 使用相混。

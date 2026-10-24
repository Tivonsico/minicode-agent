# minicode Agent

minicode 是运行在本机浏览器中的 AI 编程助手。Python 后端负责模型通信、任务执行与数据保存，HTML 前端负责消息展示和审批。支持 OpenAI Chat Completions 兼容接口，保留文件工具、会话恢复、上下文压缩、项目记忆、提示词和技能。

网页需要本地后端提供服务，不能双击 `index.html` 后独立执行工具。后端只监听 `127.0.0.1`，API 密钥保留在后端。

## 启动

需要 Python 3.11+。推荐使用 [uv](https://docs.astral.sh/uv/) 安装依赖，在项目目录运行：

```sh
uv sync
```

不用 uv 也可以通过 `python -m pip install -e .` 安装。

将 `.env.example` 复制为 `.env`，填写密钥、OpenAI 兼容 API 地址和模型名：

```dotenv
OPENAI_API_KEY=你的密钥
OPENAI_BASE_URL=https://api.openai.com/v1
MODEL=gpt-4.1
```

上下文窗口自动解析，不再读取 `CONTEXT_WINDOW`。其他服务商只需换成它的 OpenAI 兼容地址和实际模型名，例如智谱、DeepSeek、OpenRouter。密钥不会发送给模型目录。不要把 `.env` 提交到 Git。

```sh
python start.py --check-config
python start.py
python stop.py
```

Windows、macOS、Linux 使用同一个 `start.py`。系统将 Python 命令命名为 `python3` 时，把上面的 `python` 换成 `python3`。脚本优先使用项目 `.venv` 中的 Python；没有可用依赖时，尝试通过已安装的 uv 准备环境，否则提示安装依赖。从其他目录调用脚本时，默认仍读取脚本所在项目的配置。

每次运行 `start.py` 都会停止当前项目已有的后端，再重新启动；`stop.py` 使用相同的关闭流程。macOS/Linux 先发送正常关闭信号，最多等待 8 秒，让后端保存状态并释放 MySQL 会话占用；超时后强制结束残留进程及子进程。Windows 暂沿用强制结束方式。强制结束后遇到未知操作或遗留占用，仍须核实实际状态，不会直接清空保护记录。无进程时关闭脚本会直接提示。项目锁记录 PID 与进程创建时间，避免把陈旧 PID 指向的其他进程误杀。`--check-config` 和 `--check-model` 不会重启或关闭现有后端。

启动时会立即打印本地链接，并自动打开 `http://127.0.0.1:8765`。旧的 Windows `.cmd` 启动脚本已移除。macOS/Linux 无需 PowerShell；Shell 默认使用 `/bin/sh`，Windows 默认使用 `powershell.exe`。

```sh
python start.py --no-browser --port 9001
# 也保留：uv run minicode / uv run python -m app
```

`--project /绝对路径/minicode-agent` 指定应用的配置和数据根目录，**不会**自动授权文件访问。`--check-config` 只验证本地配置，不请求模型。退出后端使用 Ctrl+C；关闭或刷新网页不会停止后端任务。

## 模型窗口自动识别

输入框的模型名称右侧可选择思考强度。GLM-5.3 / GLM-5.3-Flash 使用官方档位 `low`、`high`、`max`，默认 `max`；浏览器按模型记住选择。每次发送固定本轮档位，主请求、摘要压缩和后台记忆请求使用同一参数，并保存到请求记录。模型能力尚未确认时隐藏选择器，后端拒绝未支持的档位。档位定义来源：[智谱官方 GLM-5 仓库](https://github.com/zai-org/GLM-5/blob/main/README_zh.md)。

`backend/models/` 独立处理模型能力：先读取配置地址的 `/models/{模型名}` 与 `/models` 扩展元数据，再按 API 地址和完整模型名查询 [Models.dev](https://models.dev)；不匹配相似模型、不自动替换模型。原始模型名保留在请求中。默认 API 地址为 OpenAI `/v1`，模型调用仍使用 Chat Completions；OpenAI 原生端点使用 `max_completion_tokens`，其他兼容端点使用 `max_tokens`。

[OpenAI 官方模型查询接口](https://developers.openai.com/api/reference/resources/models/methods/retrieve)只提供模型身份等信息，没有窗口字段，因此仅用标准 OpenAI API 无法保证任意模型自动发现。Models.dev 是社区维护目录，不等于官方实时参数接口；服务端明确返回的窗口优先于目录。支持范围取决于元数据或目录是否包含该服务商和模型。

自定义网关没有窗口字段时，可设置 `MODEL_CATALOG_PROVIDER=openai`（或目录中对应的服务商 ID），明确该网关沿用谁的模型参数。网关实际部署限制若不同，应由它自己的模型元数据提供；未指定映射的未知地址不会擅自沿用同名模型窗口。两处都没有有效数据或首次离线时，会明确报错，避免伪造窗口。

能力缓存保存在 `.minicode/model_capabilities/`，24 小时有效，按 API 地址、模型、目录服务商及凭据摘要隔离，不存密钥。有效缓存可离线启动；到期后重新查询，失败不沿用过期记录。读写失败不影响已成功解析的能力。

上下文窗口、最大输出和独立输入上限均采用解析出的模型声明，不设置 16K、窗口四分之一等人工 token 上限。请求中的最大输出参数直接使用模型最大输出；输入预算在上下文窗口中预留该输出空间，并服从模型声明的独立输入上限。服务端未声明最大输出时，仅从匹配的模型目录补齐，并记录来源；仍无法确认则明确报错。旧 `CONTEXT_WINDOW` 和 `MAX_OUTPUT_TOKENS` 环境变量会被忽略。

```sh
python start.py --check-model
python start.py --refresh-model
```

`--check-model` 只查询参数，不生成回复；`--refresh-model` 跳过缓存。`--check-config` 保持完全离线，只校验配置格式。解析成功不代表该密钥具备生成权限，也不保证模型支持编程助手需要的工具调用。

## 页面操作

1. 新建或选择会话。
2. 点击工作区中的“添加”或输入框旁的目录按钮，浏览文件夹并点击“选择此文件夹”。授权仅对当前会话生效。
3. 输入任务并发送；执行中仍可编辑下一轮草稿，发送按钮暂时禁用。
4. 文件创建、修改、删除与 Shell 执行会展示审批；展开查看完整差异或命令后，批准本次操作或拒绝并暂停。
5. 点击“停止”中断当前轮。已完成的文件修改不会回滚。

界面采用白色主画布与浅灰会话栏。左右抽屉均由面板图标展开和收起；展开后，图标位于各自抽屉顶部。

工作区支持切换、撤销目录授权，查看当前任务、输入预算和当前项目的长期记忆，编辑提示词、选择下一轮技能。工具结果默认折叠。Enter 发送，Shift+Enter 换行，输入法组合期间不会发送。

## 模块边界

| 模块 | 负责 | 不负责 |
| --- | --- | --- |
| `conversation` | 原始消息与事件落盘、历史消息投影、恢复未完成对话 | 摘要替换原始历史、模型通信 |
| `long_memory` | 从用户原话提取可复用信息、来源校验、版本合并、项目隔离、删除 | 猜测用户画像、把模型结论当成事实 |
| `context` | 系统/项目指令组装、输入预算、保留近期消息、历史摘要及 `compation_ckeckpoint.py` 压缩恢复点 | 文件执行、永久修改原始对话 |
| `runtime` | 模型请求、流式响应解析、循环与任务生命周期、审批协调 | 页面布局、长期事实的存储格式 |

长期记忆在每轮正常完成后后台提取，避免依赖浏览器关闭事件。只接受能在用户消息中找到的原文引用。按当前工作目录绑定项目，在其他会话授权同一目录后可调用。旧版没有项目标记的记忆保留在文件中，不自动混入任意项目。

所选模式的会话事件是事实来源（本地 JSONL / MySQL）；独立任务进度 Checkpoint 与 ShortMemory 已移除。日志快照只加速读取，任务理解来自消息与摘要。校验有效快照后只读取后续事件；坏/旧快照回退完整日志。压缩后的上下文是可校验的派生缓存。摘要缓存用原始消息摘要指纹校验，恢复后不必重复请求压缩。上下文估算采用保守 UTF-8 字节估算，**不是计费 token 数**。服务商返回的实际用量单独记录。

## Shell 与权限

Shell 使用**已安装的 Codex CLI 沙箱**，要求其支持 `codex sandbox --sandbox-state-json`。不需要通过 Codex 调用模型；这里仅使用它的操作系统执行隔离能力。可通过 `MINICODE_CODEX_PATH` 指定可执行文件位置。

- 每条命令都传入显式限制策略，不依赖用户的 Codex 默认权限配置。
- 已授权工作目录可读写；保留系统运行所需的最小读取范围。应用目录、内部记录、Git 元数据、项目指令资产、常见密钥路径受保护。
- 禁用命令网络访问；环境变量仅继承基础运行变量，不传递模型 API 密钥。
- 启动时用临时目录验证允许的写入、范围外访问拒绝、密钥拒绝和网络关闭；失败时页面说明原因并禁用 Shell，文件工具仍可用。
- 取消、超时和过量输出会停止进程树。无法确认终止时暂停该会话，不自动重跑工具。
- 本轮在 macOS 实机验证。Windows/Linux 的启动代码与 CLI 接口已适配，系统沙箱能否通过验证仍取决于本机 Codex 版本和系统条件，尚未完成这两个平台的实机验证。

命令默认最多捕获每个输出流 2 MB，超限停止；长结果落盘并向模型提供受限预览和 `ReadFile` 引用。默认超时 120 秒。

文件工具仍独立执行路径与权限检查：阻止越界、链接逃逸、敏感文件访问；已有文件必须先读再改，审批后复核文件指纹及授权版本。所有副作用逐次审批，拒绝后当前轮暂停。危险命令文本检查仅是额外约束，真正的 Shell 边界由操作系统沙箱执行。

后端限定本机 Host 和同源 Origin，请求须携带页面初始化令牌；不开放 CORS。它不是面向多人远程部署的认证服务。

## 提示词与技能

| 范围 | 提示词 | 技能目录 |
| --- | --- | --- |
| 全局 | `system_prompt/system_prompt.md` | `skill/skills/<名称>/SKILL.md` |
| 已授权项目 | `<项目>/project_system_prompt.md` | `<项目>/project_skills/<名称>/SKILL.md` |

目录授权会创建缺失的项目提示词和技能目录，已有内容不覆盖。技能描述进入上下文，模型可调用 `UseSkill`；页面也可指定下一轮技能。`ReadSkillResource` 只读取技能文本，选择技能不会自动执行脚本。提示词和已安装技能均可在页面编辑。

“管理技能”提供三个入口：按名称创建并保存技能、从公开 GitHub 仓库或技能文件夹链接下载、通过名称搜索社区技能并下载。可以保存为所有会话可用的全局技能，或保存到当前已授权项目。下载固定到确认的提交版本，保留技能资源，不执行其中的脚本，也不会覆盖已有同名技能。目前不支持私有仓库认证或其他代码托管平台。

## 目录结构

```text
backend/
├── conversation/     会话事实、历史投影、进程退出恢复
├── long_memory/      项目记忆提取、合并与删除
├── context/          指令组装、预算、压缩、摘要缓存
├── runtime/          模型客户端、响应解析、Agent Loop、会话管理、审批
├── permissions/      目录授权、路径检查、权限策略
├── execution/        Codex 沙箱策略、验证、进程生命周期
├── tools/            工具注册、调度及各类操作处理器
├── storage/          输出文件存储、应用锁
├── skills/           技能发现及资源加载
├── api/              本地 HTTP 接口、SSE、同源保护
└── config.py         配置校验
frontend/             HTML/CSS/JavaScript ES modules，无需 Node 构建
app/                  保留 minicode / python -m app 启动入口
start.py           跨平台启动脚本，自动选择运行环境和应用根目录
skill/skills/         用户安装的全局技能
system_prompt/        用户维护的全局提示词
others/test/          可维护的行为验证源码
others/test_record/   忽略的测试数据和截图
```

运行数据继续保存在应用根目录的 `.minicode/`：轨迹位于 `sessions/<ID>/session.jsonl`，日志加速快照位于同目录的 `journal_snapshot.json`，长期记忆位于 `long_memory/memory.json`。旧版会话格式可以直接加载；恢复历史不会调用模型或重放文件/Shell 操作。

本轮前端采用原生 ES modules，避免引入 Node 构建流程；后端 HTTP/SSE 边界已经独立，后续可替换为 React 前端。包的 wheel 包含静态资源。架构细节见 [docs/architecture.md](docs/architecture.md)。

## 验证

```sh
uv run pytest -q
uv run ruff check backend app others/test
uv build
```

测试使用离线模型，不消耗真实 API 请求。沙箱测试调用本机已安装的 Codex，在临时项目中执行真实命令；缺少兼容沙箱时会明确跳过。缓存、临时数据和验证截图放在 `others/test_record/`。

运行证据与管理入口：工作区详情 → **查看运行记录**，可查看时间线、日志、实际请求输入、摘要历史、检查点、检查点状态和长期记忆。检查点可立即同步当前状态；检查点状态与长期记忆可人工维护。

侧栏 **评测** 打开独立页面：自带 **30 项固定编程任务**（修复8、功能10、多轮6、安全6），可编辑初始文件和独立验收条件，用当前模型运行并查看成功率、回归、安全、耗时、用量及失败轨迹。也可导入自己的会话，补齐起点与验收条件后作为新任务运行。历史规则检查和报告导出不调用模型或执行工具。详见 [固定编程任务评测](docs/harness/coding-benchmark.md)。

实现与取舍见 [运行证据、检查点与历史评测](docs/harness/trace-checkpoint-evaluation.md)。当前支持恢复历史、复用压缩摘要和核验未知工具结果，未实现宕机后自动续跑副作用、文件版本回滚或完整 durable execution。

## 执行模式

输入框可选择 Manual（逐项确认）、Accept Edit（自动写入编辑）、Plan（只读规划）、Auto（授权工作区内自动执行）。「权限与指令」查看固定安全限制与模式提示词，只允许编辑补充指令；权限表、危险命令规则、密钥保护和沙箱不可在前端关闭。每轮冻结模式与权限/指令版本，轨迹、记忆、摘要和检查点保留来源身份。运行中切换被拒绝。

评测按模式与目标分别统计：保留 30 项编码任务，新增 4 项只读规划样本。Manual / Accept Edit 的人工审批由明确脚本模拟，不视为真实人工确认。详见 [模式取舍与身份设计](docs/harness/execution-modes.md)。


## 本地持久化与并发模式

输入框下方的当前执行模式按钮统一提供模式选择与权限说明；“并发模式：高/低”按钮展示当前并发模式，点击后可查看详细说明，并展开存储配置与连接检查。默认本地模式；MySQL 配置验证并保存后须重启生效，未配置或不可用时不会伪装启用，也不会自动回退本地。

MySQL 用短事务保存事件、会话元数据及操作意图/结果；独立日志快照按 covered_seq 加载后缀。同会话保持顺序，重叠工作区协调执行。结果未知时保留占用，必须核查旧执行者及子进程已停止并保存证据。两个模式各自保留历史，不自动迁移。

本地 JSONL 与原恢复协议继续可用。MySQL 当前限定同机、同一应用根目录及单一执行域；跨机器、通用文件回滚和任意 Shell exactly-once 未实现。配置、表结构、事务与验证边界见 [MySQL 存储与恢复](docs/mysql.md)；本机服务管理见 [本机安装与连接](docs/mysql-local-setup.md)。

文件改动会在每轮任务前后自动保存，默认保留最近 20 轮。对话中的绿色 `+`、红色 `−` 可展开逐文件差异；顶部“文件改动”提供历史和安全恢复入口。重要成果由用户手动 Git 提交。覆盖边界见[文件快照说明](docs/file-snapshots.md)。

## Gmail 自然语言发信

已接入 Google 官方 Gmail API。首次运行 `uv run python -m backend.gmail connect --credentials /绝对路径/桌面客户端.json` 完成浏览器授权，之后可在聊天中说“用 Gmail 给某地址发邮件，主题……正文……”。发送前会展示完整内容供审批，Auto 模式也须确认。工具支持纯文本、抄送和密送；重复调用沿用发送记录，结果未知时停止发送，避免自动重发。

创建 Google 客户端、首次连接、重新授权及幂等边界见 [Gmail 使用说明](docs/gmail.md)，官方文档和开源实现比较见 [发信幂等性调研](docs/research/gmail-send-idempotency.md)。

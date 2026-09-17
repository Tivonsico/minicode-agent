# minicode Agent

minicode 是运行在 Windows 终端中的 AI 编程助手。它通过兼容 OpenAI Chat Completions 的模型接口对话，在用户逐个授权的工作目录内查找、读取和修改文件，并保存会话供之后继续。

> **当前状态：**文件工具、会话、记忆、提示词和技能功能可用。`Shell` 虽有工具接口，但 Windows 11 系统隔离尚未通过验证，因此所有 Shell 调用都会被拒绝；程序不会退回普通 PowerShell 执行。README 下文的命令行示例是用户安装、测试和启动 minicode 的命令，并不是 Agent 当前可以调用的 Shell。

## 功能

| 范围 | 当前能力 |
| --- | --- |
| 对话 | 流式显示文本与工具状态；支持模型连续调用工具；限制单轮请求次数，失败或中断时保存状态 |
| 文件 | `Glob` 查找、`Grep` 搜索、`ReadFile` 分段读取、`WriteFile` 创建或覆盖、`EditFile` 精确替换、`DeleteFile` 删除单个文件 |
| 目录 | `/allow-dir` 为当前会话添加多个绝对路径；`/dirs` 浏览，`/work-dir` 切换默认目录，`/deny-dir` 撤销授权；相对路径基于默认工作目录 |
| 审批 | 每次创建、修改或删除文件都需用户确认；审批先显示摘要，可展开完整差异；拒绝后本轮停止，等待新指令 |
| 会话 | 自动记录消息、工具调用、审批、用量和结束状态；`/session` 加载历史，`/new` 新建会话；加载历史不会自动重放工具 |
| 上下文 | 按配置的上下文窗口估算用量，临近预算时压缩旧上下文；原始事件日志仍保留 |
| 长期记忆 | 在正常完成的对话后后台提取用户明确表达的项目事实与偏好；`/memory` 查看或删除 |
| 提示词 | 应用目录中的全局提示词，以及每个授权工作目录中的项目提示词；项目提示词按绝对路径限定适用范围 |
| 技能 | 自动发现全局与项目技能，按描述选用，可同时用多个；用户可指定某个技能在下一轮必须使用；可读取技能的文本附件 |
| 终端界面 | 工具调用默认折叠；`/tools` 和 `Ctrl+T` 查看详情；目录、技能、提示词支持列表浏览和终端编辑 |

### 权限边界

- minicode 的**应用目录**用于放置程序、配置和运行数据，默认不是 Agent 的工作目录；需要操作文件时，先由用户输入 `/allow-dir <绝对路径>`。目录授权随会话保存，恢复该会话后继续生效；不能授权磁盘根目录、Windows 系统目录、应用目录及其上级目录。
- 文件工具只能在已授权工作目录内操作；阻止路径穿越、链接逃逸和对 `.git/`、`.minicode/`、项目提示词及技能目录的普通文件工具访问。`.env`、常见私钥与凭据文件也受保护。程序自身读取配置与管理提示词、技能的通道另行处理。
- 已有文件修改前必须先读取；批准后会检查文件和授权状态是否变化。`EditFile` 要求旧文本恰好匹配一次；`DeleteFile` 只处理普通单文件。
- 危险 Shell 命令检测会直接拒绝明显的破坏性操作，但文本检测无法覆盖所有等价写法。由于尚无通过验证的系统级 Shell 隔离，当前 **Shell 整体不可执行**，即使用户批准也不会运行。
- 这些文件工具检查是程序层的边界，不等于操作系统沙箱；不要把应用程序进程交给不可信的本机用户运行。

## 环境与安装

- Windows 11，使用 Windows Terminal、cmd 或 PowerShell 的**交互式控制台**。
- Python 3.11 或更高版本。
- [uv](https://docs.astral.sh/uv/)（推荐）或 Python 虚拟环境。
- 支持 OpenAI Chat Completions 兼容接口的模型服务及 API 密钥。默认接口地址指向智谱开放平台，模型名、窗口和输出限制应以服务商实际支持的值配置。

在项目目录执行：

```powershell
uv sync
Copy-Item .env.example .env
```

编辑本地 `.env`，填写 `OPENAI_API_KEY`、`MODEL`、`CONTEXT_WINDOW` 和 `MAX_OUTPUT_TOKENS`。`OPENAI_BASE_URL` 默认是 `https://open.bigmodel.cn/api/paas/v4/`；使用其他兼容服务时一并修改。窗口值和输出上限需要自行核实，且必须满足 `0 < MAX_OUTPUT_TOKENS < CONTEXT_WINDOW`。`.env.example` 保留了完整的配置键与占位值。

```powershell
uv run minicode --check-config
uv run minicode
```

`--check-config` 只验证本地配置，不请求模型。也可双击 `启动项目.cmd`，它会打开独立控制台；从重定向输出或没有 Windows 控制台的宿主直接启动交互界面会失败。

不用 uv 时：

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e .
python -m app
```

默认以当前目录作为**应用数据根目录**，从中读取 `.env` 并保存 `.minicode/`。`--project C:\path\to\minicode-agent` 可显式指定该目录；它**不会**自动授权 Agent 访问该目录。启动后另用 `/allow-dir` 添加实际工作项目：

```text
/allow-dir C:\path\to\your-project
查看这个项目的目录结构，然后帮我修改 README。
```

授权时，minicode 会在该项目根目录创建缺失的 `project_system_prompt.md` 和 `project_skills/`；若已存在，会读取和校验，不覆盖原内容。

## 终端操作

| 输入 | 用途 |
| --- | --- |
| `/help` | 查看终端内帮助 |
| `/new`、`/session` | 新建会话；浏览并加载旧会话 |
| `/allow-dir <绝对路径>` | 给当前会话添加工作目录，并设为默认目录 |
| `/deny-dir <路径>` | 撤销目录授权 |
| `/work-dir <路径>`、`/dirs` | 切换默认目录；浏览授权目录，上下键选择、Enter 切换、`D` 撤销 |
| `/use global skills [名称]` | 浏览全局技能，或指定一个技能用于下一轮 |
| `/use project skills [名称]` | 浏览已授权项目的技能，或在默认项目中按名称指定下一轮使用 |
| `/skill [名称]` | 全局技能的兼容命令 |
| `/see system_prompt` | 浏览全局与已授权项目的提示词 |
| `/tools`、`Ctrl+T` | 浏览工具调用记录；展开或收起最近一次调用 |
| `/memory`、`/memory delete ID` | 查看或删除长期记忆 |
| `/exit` | 退出 |

列表中使用上下键选择、Enter 打开或使用。选中提示词或技能后，`F1` 进入编辑，`Enter` 保存，`Ctrl+J` 插入换行，`F2` 返回或取消未保存改动。审批时使用 `Ctrl+Y` 批准、`Ctrl+N` 拒绝，空格展开详情，`PageUp` / `PageDown` 滚动。执行中按 `Esc` 中断；已完成的文件改动不会因此回滚。

旧会话的选择只加载历史。输入新的指令后才继续向模型发请求；结果不明的副作用不会自动重放。执行中可以编辑输入草稿，但 Enter 不会排队发送，需等当前轮结束或先按 Esc。

## 提示词与技能

| 作用范围 | 提示词 | 技能目录 |
| --- | --- | --- |
| 全局 | `system_prompt/system_prompt.md` | `skill/skills/<技能名>/SKILL.md` |
| 授权项目 | `<项目>/project_system_prompt.md` | `<项目>/project_skills/<技能名>/SKILL.md` |

技能的 `SKILL.md` 中写入 `description:`，说明何时使用。minicode 把可用技能描述提供给模型，由模型选择相关技能；也可通过 `/use ... <名称>` 指定**下一轮**必须使用某一个技能，之后恢复自动选择。可在技能文件夹内放参考文本、模板和脚本；`ReadSkillResource` 仅读取文本，选用技能不会自动运行脚本。仓库当前只提供全局技能目录说明，没有预装业务技能。

全局提示词、相关项目的提示词和技能描述进入模型的每次 API 请求；项目资料标明对应的绝对路径，不重复追加到持久会话消息中。模型服务本身按请求接收上下文，因此后一轮仍由程序重新构造有效上下文。项目提示词与技能不能扩大目录权限或取消审批。

## 项目结构

```text
minicode-agent/
├─ app/                  CLI 入口、prompt_toolkit 终端界面与斜杠命令
├─ agent/                固定提示词、模型与工具循环、取消和请求上限
├─ conversation/         兼容接口客户端、流式解析、会话消息投影与恢复
├─ context/              token 预算估算、上下文构建与压缩
├─ tools/                工具注册、执行器、路径与审批规则、危险命令检测、Shell 入口
├─ memory/               会话事件存储、授权状态、提示词资产、长期记忆与项目锁
├─ skill/
│  ├─ catalog.py         全局和项目技能发现、加载及附件读取
│  └─ skills/            已安装全局技能目录（当前仅有说明文件）
├─ system_prompt/
│  └─ system_prompt.md   可在终端查看和修改的全局提示词
├─ docs/                 功能规格、任务、验收清单及验证记录
├─ others/test/          单元、集成、端到端测试与共用测试工具
├─ config.py             .env 配置加载与校验
├─ codex_system_prompt   本项目给开发助手的工作说明
├─ pyproject.toml        Python 包、依赖及工具配置
├─ uv.lock               uv 依赖锁文件
├─ .env.example          本地配置模板
└─ 启动项目.cmd           Windows 独立控制台启动脚本
```

运行时还会在应用数据根目录生成 `.minicode/sessions/<会话 ID>/`、`.minicode/long_memory/` 等数据；这些目录、`.env`、`.venv`、`others/test_record/` 与 `others/plan/` 都不会提交到 Git。`others/test/` 中的测试源码会提交。授权项目中的 `project_system_prompt.md` 和 `project_skills/` 属于该项目，由该项目自行决定是否版本化。

## 开发与验证

```powershell
uv run pytest -q
uv run ruff check .
uv build
```

pytest 配置会把缓存和临时记录放进 `others/test_record/`。`docs/` 中的规格与验收记录包含早期设计和验证过程；遇到与当前实现不同的描述，以当前源码及本 README 的“当前状态”为准。

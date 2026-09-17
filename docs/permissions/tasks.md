# minicode 严格权限系统实施任务

目录默认行为后续调整见 [工作目录实施任务](../workspace-skills-terminal/tasks.md)。本文件原先的“项目根自动授权”描述是历史计划，不再作为当前实现验收依据。

状态：实施中（2026-09-17）。需求见[规格](spec.md)，逐项结果按[验收清单](checklist.md)记录。若系统隔离方案验证失败，保留 Shell 拒绝门槛，不以旧的无隔离执行器代替。

## 当前实施记录

- T04、T06、T08 已接入：目录授权事件随会话恢复，终端可添加、浏览、撤销目录，每轮模型请求从执行器获取有效目录。单元、终端和模型请求测试已通过。
- T01、T05、T07、T09、T13 已完成可独立验证的部分：文件读写范围检查、敏感文件保护、逐次修改审批、批准后版本复核、用户拒绝后停止，以及直接危险命令在审批前拒绝。路径重解析点的并发替换仍需系统级验证，不能宣称文件工具能抵抗所有竞态。
- T02 在当前 Windows 11 上验证了 MXC SDK 0.8.0 的 `appcontainer-dacl` 后端；授权根外的探针访问失败，但磁盘设备、管理操作、网络和子进程边界未全部验证。结果保存在 `others/test_record/sandbox-probe-result.json`。
- T03 未通过：同一原型虽可读取 `.minicode/`，却仍可改写其中的测试文件，嵌套 `readonlyPaths` 没有形成只读边界。直接添加拒绝 ACL 的实验也未得到可运行且有效的边界。Shell 执行器现为失败即关闭的接口，没有普通 PowerShell 回退。测试目录 ACL 已恢复并重新读取确认。
- 系统隔离实测单独运行于 Windows 11（build 26200）：先运行 `others/test_record/mxc-probe/node_modules/@microsoft/mxc-sdk/bin/x64/wxc-exec.exe --probe`，再运行 `.venv/Scripts/python.exe others/test/support/sandbox_probe.py others/test_record/mxc-probe/node_modules/@microsoft/mxc-sdk/bin/x64/wxc-exec.exe others/test_record`。探针命令及结果见 `others/test/support/sandbox_probe.py` 与 `others/test_record/sandbox-probe-result.json`。结论仅覆盖目录读写探针；磁盘设备、网络、子进程、数据库与超时尚未验收。
- T10–T12 未接通：尚无可信的受保护数据库登记、凭据与管理连接隔离，也无可启用的 Shell 后端。T14 仅完成目录授权、文件审批和 Shell 拒绝路径的端到端验证；不能把跳过的 Shell 测试算作通过。
- 当前项目虚拟环境运行 `python -m pytest -q` 得到 91 通过、9 跳过，耗时 6.41 秒；`ruff check tools memory app agent others/test` 通过。全目录 Ruff 检查另因与权限系统无关的根目录 `edit_resume.py` 导入排序问题失败，本次未改动该文件。Shell 相关旧集成测试保持跳过。测试记录位于 `others/test_record/`。由于工作目录不是 Git 仓库，未创建提交。

## 设计决定与实施顺序

先建立一个统一的权限状态和决策入口，再接会话授权、文件工具、审批与模型上下文。Shell 是独立的系统隔离交付线：先证明目录边界与 `.minicode/` 只读边界，再接危险命令黑名单和受保护数据库边界，最后接入现有命令生命周期。首版目标是当前 Windows 11 环境。最终才接通主流程并做端到端验证。

保留原文第一层的核心语义：危险命令命中黑名单即直接拒绝，任何审批不能覆盖；黑名单并不覆盖所有变体。为本项目的 PowerShell 调整规则，并补上明显的整库删除形式。项目不引入原文的通用规则配置、四种权限模式或自动“始终允许”；资源边界和逐次审批继续承担各自职责。

## 资料与定位

- R1：[规格](spec.md)与[验收清单](checklist.md)。
- R2：`tools/paths.py` 的 `PathPolicy`，当前仅接受项目根；`tools/executor.py` 的 `_dispatch` 和 `_approval`，当前文件写入自动执行、删除与 Shell 询问。
- R3：`app/terminal.py` 的 `select_session`、`submit`、`ask_approval`；`app/commands.py` 的斜杠命令；`memory/session_store.py` 的事件追加和恢复。
- R4：`agent/loop.py` 每次请求组装系统消息；`context/builder.py` 在压缩时保留系统消息；`conversation/projection.py` 只投影对话消息。
- R5：`tools/shell.py` 当前使用普通 PowerShell 子进程并继承环境；`tools/registry.py` 当前明确说明 Shell 不是操作系统沙箱。
- R6：[Microsoft Create Process in Sandbox 文档](https://learn.microsoft.com/en-us/windows/win32/secauthz/createprocessinsandbox)：AppContainer 与目录授权是可研究的候选，但该接口标为实验性，不能仅凭文档假定目标机器可用。
- R7：[Microsoft Windows Sandbox CLI 文档](https://learn.microsoft.com/en-us/windows/security/application-security/application-isolation/windows-sandbox/windows-sandbox-cli)：可运行和共享目录，但文档说明命令输出目前不能直接取回，不能直接替换现有 Shell 通道。
- R8：[PostgreSQL DROP DATABASE 文档](https://www.postgresql.org/docs/current/sql-dropdatabase.html)：用于核实整库删除的效果与权限条件；命令黑名单只能识别其直接写法，不能代表数据库级权限控制。

## T01 统一权限状态与决策契约

- 工作：定义资源范围、操作类别、文件工具保护规则、危险命令黑名单的优先级、`允许／询问／拒绝` 三种结果和拒绝原因；策略拒绝作为工具错误交给模型继续处理，用户拒绝后当前轮停止；明确黑名单命中永远先于审批，获批 Shell 对敏感文件和内部记录的读取受数据库凭据边界约束，内部记录始终不可修改。
- 影响文件：新增 `tools/permissions.py`；调整 `tools/executor.py` 的权限入口；新增 `others/test/unit/test_permissions.py`。
- 依赖：无。参考：R1、R2。
- 验收：单元测试覆盖默认矩阵、文件工具的敏感目标拒绝、高风险操作优先拒绝、Shell 的内部记录只读规则、策略拒绝后继续与用户拒绝后停止，以及审批或普通配置不能覆盖范围外和隔离失败的拒绝。

## T02 验证 Windows Shell 隔离能力

- 工作：在当前 Windows 11 环境做最小原型，验证所选系统机制能启动 PowerShell、限制宿主目录与磁盘设备等宿主高权限操作、约束子进程、提供可捕获的输出并允许审批后的网络访问；记录系统版本和失败方式。
- 影响文件：`others/test/support/` 下的原型；结果放在被忽略的 `others/test_record/`；必要时在本任务文档记录选型。
- 依赖：T01。参考：R5、R6、R7。
- 验收：读取或写入授权根之外的探针文件失败，磁盘设备和宿主系统管理操作不可达，授权根内普通文件可用，子进程不能突破边界；任一项失败时结论明确为“Shell 尚不可启用”。

## T03 验证内部记录只读与凭据传递

- 工作：验证获批 Shell 在没有受保护数据库凭据冲突时可读取授权根内 `.env`、私钥和 `.minicode/`，但其自身和子进程不能改写、删除或替换 `.minicode/` 及别名；验证应用不主动把 API Key 传入子进程环境。为受保护数据库凭据准备排除敏感文件或内部记录的隔离视图；只授予项目根目录递归读写权限不足以满足这些要求。
- 影响文件：`others/test/support/` 下的原型；必要时新增 `tools/sandbox_view.py`；记录放在 `others/test_record/`。
- 依赖：T02。参考：R1、R2、R6。
- 验收：无凭据冲突时 PowerShell 能读取模拟敏感文件与内部日志，却不能修改内部日志或通过链接别名修改；标记为受保护数据库高权限凭据的样本不能被 Shell 读取；文件工具仍拒绝敏感文件和内部目录；无法满足时 Shell 继续拒绝执行，且不回退普通子进程。

## T04 会话授权事件与恢复

- 工作：把目录添加和撤销写为会话事件；按事件顺序重建有效目录；恢复时重新核验真实路径与目录存在性；存储失败不生效，其他会话不继承。
- 影响文件：`memory/session_store.py`、新增 `memory/permission_state.py`、`app/terminal.py`；新增 `others/test/unit/test_permission_state.py`。
- 依赖：T01。参考：R3。
- 验收：授权多个目录、撤销其中一个、重启恢复、另开会话和损坏尾记录均得到确定结果。

## T05 多目录路径边界

- 工作：扩展 `PathPolicy` 为项目根加会话目录；项目内相对路径保持原语义，外部目录使用绝对路径；拒绝磁盘根与 Windows 系统目录授权；搜索遍历所有允许根并去重；对别名、链接与目标变化执行复核。
- 影响文件：`tools/paths.py`、`tools/executor.py`；扩充 `others/test/unit/test_tools.py`。
- 依赖：T01、T04。参考：R2。
- 验收：多个根内读写可达；`..`、项目外路径、符号链接、目录联接和硬链接别名不能绕过边界或保护规则。

## T06 用户目录命令与状态显示

- 工作：增加添加、查看和撤销目录的用户斜杠命令；支持带空格的绝对路径；命令只在会话空闲时执行，不注册为模型工具；在终端中提供可浏览、可选择的有效目录列表。
- 影响文件：`app/commands.py`、`app/terminal.py`；扩充 `others/test/integration/test_app.py`。
- 依赖：T04、T05。参考：R3。
- 验收：键盘可浏览项目根和额外目录、查看完整路径并选择要撤销的目录；命令成功和拒绝原因可见；模型文本、README 或工具输出中的相同字样不会改变授权。

## T07 文件工具审批与执行前复核

- 工作：统一文件工具的权限入口；新建、覆盖、编辑、删除逐次审批；终端先显示目标与变更摘要，按键可展开和滚动查看完整差异；用户拒绝后结束当轮；批准后重新验证权限版本、真实路径与文件内容版本，保留先读后写保护。
- 影响文件：`tools/executor.py`、`tools/registry.py`、`app/terminal.py`、`agent/loop.py`；扩充 `others/test/unit/test_tools.py`。
- 依赖：T01、T05。参考：R2、R3。
- 验收：每次修改都有一次对应审批；拒绝无副作用且当轮不继续调用模型或工具；审批期间目标或授权变化后旧批准不能执行；文件工具始终拒绝受保护文件。

## T08 模型上下文与压缩一致性

- 工作：每次请求从真实权限状态生成项目根、额外目录和工具限制说明；恢复会话和上下文压缩后重新生成，不从聊天摘要推断授权。
- 影响文件：`agent/loop.py`、`agent/prompts.py`、`app/terminal.py`；扩充 `others/test/unit/test_context.py` 或现有上下文测试。
- 依赖：T04、T05。参考：R4。
- 验收：授权、撤销、会话切换、重启与压缩后的下一次模型请求均包含与执行器相同的有效目录。

## T09 高风险命令前置拒绝

- 工作：在 Shell 审批前检查危险命令黑名单，命中后返回拒绝原因且不启动进程。首批覆盖 PowerShell 与可从 PowerShell 调用的 Bash/WSL 中明显的磁盘清空或格式化、磁盘根／系统目录／授权根整体递归删除、远程下载立即执行、强制推送及直接整库删除写法；处理可规范化的路径形式，如 `/tmp/../`；拒绝不能由审批覆盖。不直接复制原文针对 Bash 的正则，须正确处理管道符并适配 PowerShell。黑名单不解析任意脚本或数据库 API，未命中不代表安全。
- 影响文件：新增 `tools/command_guard.py`；调整 `tools/executor.py`、`tools/shell.py`；新增 `others/test/unit/test_command_guard.py`。
- 依赖：T01、T05。参考：R1、R5、R8 与用户提供的安全层原文。
- 验收：每类黑名单命令的直接形式在审批前被拒绝，没有子进程或网络请求；大小写、常见空格、参数顺序与可规范化路径变体仍被拒绝；正常命令进入逐次审批；间接脚本形式不被误报为“已保证安全”。

## T10 受保护数据库边界

- 工作：由用户在可信应用界面登记受保护数据库及其管理连接范围；登记持久保存于模型、项目文件和 Shell 均不能修改的状态中，不随会话切换丢失。对每个已登记库，验证 Shell 可用凭据没有删库权限，隔离高权限凭据的文件与进程环境，并验证 Shell 及子进程不能连接该库的管理入口；本地文件型数据库还须对 Shell 只读。任一条件未知或失败则在启动前拒绝 Shell。不同数据库引擎的权限验证必须分别实现和实测，不能把 PostgreSQL 的规则直接套给其他引擎，也不能靠扫描文件文本证明凭据不存在。
- 影响文件：新增 `tools/protected_db.py`、`memory/protected_resources.py`；调整 `tools/sandbox_win.py` 或选定隔离后端、`tools/executor.py`、`app/commands.py`、`app/terminal.py`；新增 `others/test/integration/test_protected_db.py`。
- 依赖：T02、T03、T09。参考：R1、R6、R8。
- 验收：受保护数据库样本中，直接命令与间接脚本均不能删库；Shell 无法读取高权限凭据或触及管理连接，本地数据库文件不可写；用户登记状态重启后仍可见且不能由模型或项目文件修改；无法证明任一边界时没有 Shell 子进程启动。未登记的外部数据库不标为受保护。

## T11 Shell 系统隔离执行通道

- 工作：把 PowerShell 启动替换为经 T02、T03、T10 验证的隔离后端；授权目录的普通文件可读写，`.minicode/` 只读，受保护数据库高权限凭据不可见，并提供隔离临时空间；隔离进程及其子进程共用边界；后端不可用时在启动前拒绝。
- 影响文件：新增 `tools/sandbox_win.py` 或等价模块；调整 `tools/shell.py`、`tools/executor.py`；新增 `others/test/integration/test_shell_sandbox.py`。
- 依赖：T02、T03、T04、T05、T09、T10。参考：R2、R5、R6。
- 验收：已批准的命令仍无法访问未授权宿主文件；根内非高权限凭据的敏感文件与内部数据按实际边界可读，内部数据不可修改；受保护库高权限凭据和管理连接不可达；隔离失败不启动普通 PowerShell。

## T12 Shell 审批、输出与进程生命周期

- 工作：审批先展示命令摘要、工作目录、目录范围、联网、超时、受保护数据库边界，以及实际可读的 `.env`、私钥、内部日志可能被外发的风险，按键可展开和滚动查看完整命令；每次执行独立审批；传入不主动包含应用密钥的受控环境；沿用并核实 `OutputStore` 对已知应用密钥与私钥内容在保存和回传前的遮盖，接回 stdout/stderr、退出码、超时、中断与进程树停止状态。
- 影响文件：`tools/executor.py`、`tools/shell.py`、`memory/output_store.py`、`app/terminal.py`；扩充 Shell 集成测试。
- 依赖：T07、T09、T10、T11。参考：R3、R5。
- 验收：拒绝审批不启动命令且当轮停止；批准后可联网且仅访问规定宿主数据，内部目录只读；已识别密钥在模型消息与持久输出中被遮盖，输出仍可追溯；取消或超时后不遗留继续运行的子进程，停止结果不确定时暂停后续工具。

## T13 接入主流程

- 工作：贯通会话加载、用户命令、权限状态、危险命令黑名单、受保护数据库登记与边界、工具执行、审批、模型上下文和 Shell 隔离；更新帮助、README、系统提示词与工具说明，移除关于旧权限行为及“不要通过 Shell 绕过文件保护”的不准确信息。
- 影响文件：`app/main.py`、`app/terminal.py`、`app/commands.py`、`agent/loop.py`、`tools/registry.py`、`README.md`；必要的集成测试。
- 依赖：T04–T12。参考：R1–R5。
- 验收：从终端启动到新建、恢复、授权、修改、删除、执行命令和退出均使用同一权限状态，没有旧 Shell 路径或自动写入分支。

## T14 端到端验证

- 工作：用临时项目、一次性受保护数据库和两个会话执行完整场景，覆盖提示注入、目录授权与撤销、过宽目录拒绝、文件变更审批、策略拒绝后继续、用户拒绝后停止、文件工具保护、危险命令在审批前拒绝、受保护数据库的直接和间接删除被拒、获批 Shell 读取允许范围内的敏感文件及内部日志、内部日志只读、已知密钥输出遮盖、网络、子进程、重启恢复和上下文压缩；记录结果与未通过项。
- 影响文件：`others/test/e2e/test_permissions_e2e.py`、`docs/permissions/checklist.md`；记录放在 `others/test_record/`。
- 依赖：T13。参考：R1、R2、R3、R4。
- 验收：清单中的核心用例通过；常规自动化命令控制在 60 秒内，较长的系统隔离验证单独运行并记录；若隔离门槛未过，不能宣称 Shell 功能完成。

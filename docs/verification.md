# 实施与验证记录

日期：2026-09-10。

## 当前结果

本地核心实现与自动化验证完成；真实智谱请求尚未完成。不得将本记录理解为模型账户、请求 ID 或窗口参数已经通过真实服务验证。

- 当时的最终命令：`uv run python -m pytest -q --basetemp=.pytest-reorg-2 -p no:cacheprovider`。历史运行目录现已归档到被 Git 忽略的 `others/test_record/`；后续 pytest 默认把缓存和临时目录写入该目录。
- 结果：**75 passed，1 skipped，11.87 秒**。
- 跳过：`test_session_rejects_internal_directory_link`，当前 Windows 环境无符号链接创建权限。工具层另用 Windows junction 验证目录越界，已通过。
- `ruff check` 通过；`ruff format --check` 确认 55 个 Python 文件格式正确。
- `uv build` 成功生成源码包和 wheel；`uv run minicode --help` 成功加载命令入口。
- 已创建 `.env.example`，真实 `.env` 被 Git 忽略。2026-09-10 配置 `CONTEXT_WINDOW=200000`、`MAX_OUTPUT_TOKENS=65536` 后，本地配置检查通过，输入预算为 134464；检查过程未显示密钥。
- 同日执行一次最多 16 token 的真实最小请求：HTTP 200，服务端接受 `GLM-5.3-Flash`，响应模型名为 `glm-5.3-flash`，包含 usage，并因极短测试预算以 `finish_reason=length` 结束。该结果不等同于完整文本或工具往返验收。

## 覆盖范围

| 验证领域 | 证据 |
| --- | --- |
| 配置、密钥隐藏、无效启动 | others/test/unit/test_config.py、others/test/integration/test_startup.py |
| SSE、多工具参数、reasoning_content、重试及中断 | others/test/unit/test_provider.py |
| 主循环、30 次额度、部分输出、正常结束与暂停 | others/test/integration/test_agent_loop.py |
| 消息角色、工具关联、恢复不重放 | others/test/unit/test_conversation.py |
| 尾部损坏、元数据重建、用量去重、输出引用与脱敏 | others/test/unit/test_session_store.py |
| 路径越界、敏感文件、先读后写、删除审批、二进制删除 | others/test/unit/test_tools.py |
| 真实读取线程并发、写入分界、取消等待线程结束 | others/test/integration/test_scheduler.py |
| 真实 PowerShell 输出、中文、超时、取消、磁盘故障 | others/test/integration/test_shell.py |
| 预算、单轮旧工具压缩、压缩失败保留历史 | others/test/unit/test_context.py |
| 显式事实、来源、冲突顺序、删除墓碑、后台失败和退出 | others/test/unit/test_long_memory.py |
| 跨进程锁与进程退出释放 | others/test/integration/test_project_lock.py |
| prompt_toolkit 真实输入管道按键分发：列表、审批、Esc、草稿、未知状态阻止切换 | others/test/integration/test_app.py |
| 真实文件工具与 PowerShell 的完整往返、日志引用、后台记忆、恢复不重放 | others/test/e2e/test_e2e.py |

端到端模型响应使用可控脚本，文件操作和 PowerShell 真实执行；终端按键通过输入管道验证。尚未以人工完整使用或截图验证所有终端尺寸、字体、换行与配色。

## 故障变异验证

- 移除“已收到输出则不自动重试”防护：正文、推理、工具参数三种断流测试均失败；恢复后通过。
- 移除敏感路径保护：`.env` 被读出，测试失败；恢复后通过。
- 移除写入分界：写入与两个读取同时运行，测试失败；恢复后通过。
- 一次根线程全量检查恰好发生在子代理调度故障变异期间，观察到 4 项预期相关失败；已协调停止并恢复变异后重新完整验证。最终代码不含这些故障变异。

## 本次修复

- 截断预算小于说明文本时，不再因 `[-0:]` 返回完整日志。
- 未知进程状态不因 `/new`、`/session` 或 executor 重置而在当前实例内失去保护。
- Shell 日志创建/写入失败停止后续工具；运行中的进程得到终止处理。
- ReadFile/Grep 的实际文件工作在线程执行，取消等待线程完成后才释放分组。
- 为 PowerShell 输出添加并展示 UTF-8 设置，中文 stdout/stderr 测试通过。
- Windows 元数据原子替换遇到短暂共享锁时有限重试，持续错误仍抛出并保留原文件。
- DeleteFile 不再误拒绝普通二进制文件；目录和敏感文件仍受限制。
- 测试统一整理到 `others/test/`，按 `unit/`、`integration/`、`e2e/` 分类，共享夹具位于 `support/`；计划统一放在 `others/plan/`，测试记录统一放在 `others/test_record/`。

## 尚未验证或固有限制

- 智谱真实 API 完整文本与工具往返尚未执行；当前只验证了最小请求、模型 ID 和 usage，E2E-04 保持未勾选。
- `GLM-5.3-Flash` 精确请求 ID、窗口与输出上限：官方资料存在枚举/描述差异，保留配置原值，详见 api-verification.md。
- Shell 是经批准的本地命令，没有操作系统沙箱；不能承诺项目外文件不可访问或副作用可撤销。
- 未知进程状态阻止当前实例继续；退出后需用户检查进程已经停止再重启，不把新进程启动当作旧进程已终止的证明。
- 上下文使用保守字节估算，长期记忆主题归类依赖模型，不宣称精确 tokenizer 或完整语义冲突识别。
- 没有 Git 仓库，因此未创建提交；源码、依赖锁文件与证据均已保存。

## 实现文件归并

为避免空转发模块，单个文件工具在 tools/executor.py 中实现并由 tools/registry.py 注册；流程调度也在 executor.py。进程管理位于 tools/shell.py。终端渲染并入 app/terminal.py，恢复测试并入 others/test/unit/test_conversation.py，端到端脚本直接写在 others/test/e2e/test_e2e.py。这些是文件组织调整，功能范围保持批准的计划。

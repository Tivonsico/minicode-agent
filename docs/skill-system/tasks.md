# 技能系统实施任务

状态：已完成（2026-09-17）。

1. [x] 明确独立目录及安装格式。文件：`skill/`、`skill/skills/`、`docs/skill-system/`。依赖：无；参考 `codex_system_prompt`。
2. [x] 实现安全的自动发现与目录摘要。文件：`skill/catalog.py`、`skill/__init__.py`。依赖：1；参考现有 `app/skills.py`。
3. [x] 实现技能正文和附属文件按需加载。文件：`skill/catalog.py`。依赖：2；参考 `PathPolicy.resolve()` 的路径约束。
4. [x] 接入自动选用技能的模型工具。文件：`tools/registry.py`、`tools/executor.py`、`agent/loop.py`、`agent/prompts.py`。依赖：3；参考 `ToolExecutor._dispatch()`。
5. [x] 接入终端浏览、下一轮强制使用与会话恢复。文件：`app/terminal.py`、`app/commands.py`；沿用 `memory/session_store.py` 的事件机制。依赖：2–4；参考 `TerminalApp.submit()`、`select_session()`。
6. [x] 接入主流程并更新说明，移除旧技能扫描路径与文件。文件：`README.md`、`docs/workspace-skills-terminal/`、`app/skills.py`、`skills/README.md`。依赖：4–5；参考现有技能调用链。
7. [x] 端到端验证：安装两个技能，自动选择多个、强制下一轮、恢复会话和读取附属文件。文件：`others/test/unit/`、`others/test/integration/`。依赖：6；参考终端与工具集成测试。

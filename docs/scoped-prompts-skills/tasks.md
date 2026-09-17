# 实施任务

状态：已完成（2026-09-17）。

1. [x] 定义全局与项目文件布局、内容边界。文件：`system_prompt/`、`skill/`、`docs/scoped-prompts-skills/`。依赖：无；参考 `agent/prompts.py`、`skill/catalog.py`。
2. [x] 授权项目时初始化并验证项目提示词和技能目录。文件：`memory/permission_state.py`、`app/terminal.py`、`memory/project_assets.py`。依赖：1；参考 `SessionPermissions.add()`。
3. [x] 扩展技能目录发现、选择与附属文件读取到项目级。文件：`skill/catalog.py`、`tools/executor.py`、`tools/registry.py`。依赖：1–2；参考 `SkillCatalog`、`UseSkill`。
4. [x] 构造路径标注的全局和项目提示词上下文，保证每次请求各一份。文件：`agent/loop.py`、`agent/prompts.py`、`tools/executor.py`。依赖：2–3；参考 `AgentLoop.run()`。
5. [x] 实现 `/use project skills`、`/use global skills` 与名称直达。文件：`app/commands.py`、`app/terminal.py`。依赖：3–4；参考现有 `/skill` 列表。
6. [x] 实现 `/see system_prompt` 查看与终端内编辑。文件：`app/terminal.py`、`memory/project_assets.py`。依赖：2、4；参考 `TextArea` 与键绑定。
7. [x] 接入主流程并更新帮助、README 和旧文档。文件：`app/terminal.py`、`README.md`、`docs/skill-system/`。依赖：2–6；参考目录授权和会话恢复。
8. [x] 端到端验证：授权两个项目、路径分组、多轮请求、两级技能与编辑保存。文件：`others/test/unit/`、`others/test/integration/`。依赖：7；参考终端与 Agent Loop 测试。

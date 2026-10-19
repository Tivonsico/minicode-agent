# 已实施任务

1. [x] 领域迁移与包配置。影响：backend 四个域、pyproject.toml；依赖：无。参考：README 模块边界。
2. [x] 模型通信与输入组装分离。影响：runtime/provider、streaming、auxiliary，context/prompts；依赖：1。参考：既有 Provider 重试与 SSE 解析行为。
3. [x] 短期任务投影及上下文摘要恢复。影响：short_memory/state、context/checkpoint、builder；依赖：1–2。参考：conversation/store、resume。
4. [x] 项目记忆绑定与来源保留。影响：long_memory/manager、scoped；依赖：1。参考：既有 extractor 用户原文校验及 tombstone。
5. [x] 工具处理器按操作职责拆分。影响：tools/executor、dispatch、各类 calls；依赖：1。参考：既有只读并发和写操作屏障。
6. [x] 接入 Codex 系统隔离。影响：execution、tools/shell_calls；依赖：5。参考：OpenAI Codex 官方 debug_sandbox、PermissionProfile 源码，架构文档链接。
7. [x] 后端会话生命周期、审批与本机 API。影响：runtime/session、service、approvals，api；依赖：2–6。
8. [x] 接入主流程和浏览器页面。影响：app/main、frontend、Windows 启动脚本；依赖：7。
9. [x] 端到端验证和使用说明。影响：others/test、README、docs/architecture；依赖：8。验证记录：others/test_record。

# Coding Agent 本地会话存储：有限核查

访问日期：2026-10-20。范围限定为 Claude Code CLI 和 OpenCode 官方文档；不据此推断所有 Coding Agent 的服务端数据库选型。

## 已确认事实

1. **Claude Code CLI 默认将 transcript 保存为本地 JSONL。** 官方会话文档的 “Where transcripts are stored” 明确给出 `~/.claude/projects/<project>/<session-id>.jsonl`；每行包含消息、工具使用或元数据对象。文档明确此页针对 CLI，不能推广为 Claude 云端产品的内部架构。
   - 来源：https://code.claude.com/docs/en/sessions#where-transcripts-are-stored

2. **OpenCode 官方文档确认本地持久化，也提供数据库操作入口；本轮未确认其当前数据库引擎。** Troubleshooting 文档将 session/message data 列在本地应用数据目录；CLI 文档提供 `opencode db [query]` 及 `opencode db path`。这些证据足以说明 OpenCode 有本地数据/数据库操作，但不能单凭该页断言一定是 SQLite，也不能断言不使用任何远端数据库。
   - 本地存储来源：https://opencode.ai/docs/troubleshooting/#storage
   - 数据库命令来源：https://opencode.ai/docs/cli/#db

## 限制与未确认项

- OpenCode Troubleshooting 的目录描述与新 CLI 的数据库入口可能对应不同演进阶段，本轮未锁定发布版本或源代码提交，不能把它们拼成确定的当前底层布局。
- 尝试访问 OpenCode 源码路径未成功，GitHub API 请求遇到限流，故 SQLite 引擎与数据库文件名保留为未确认。
- 此比较不能证明“没有 Coding Agent 使用 MySQL”，也不覆盖 Claude Code 云服务或其他厂商闭源服务端实现。
- 选择 MySQL 的必要性仍须依据 MiniCode 的实际部署需求，而不能仅依据同类本地工具的默认存储。

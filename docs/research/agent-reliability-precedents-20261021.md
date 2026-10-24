# Agent 恢复、幂等与并发协调的一手依据

核对日期：2026-10-21。范围为公开项目文档与本地实现；未执行邮件发送、未运行数据库测试。远端默认分支会变化。

## 开源依据

- LangGraph Functional API 文档明确说明：已完成 task 的结果从 checkpoint 恢复；启动但未完成的 task 可能重跑，因此副作用应使用幂等键或检查已有结果。框架 checkpoint 不等于外部系统的 exactly-once。来源：[官方文档源码](https://github.com/langchain-ai/docs/blob/main/src/oss/langgraph/functional-api.mdx)。
- MCP Agent Mail Rust 提供文件路径/glob 的 advisory reservation，支持共享或独占、TTL、冲突报告和提交检查。它明确说明这些不是强制文件锁，申请时报告冲突而非等待；不能据此声称自动 FIFO 串行执行。来源：[项目 README](https://github.com/Dicklesworthstone/mcp_agent_mail_rust)。旧 Python 版已声明由 Rust 版替代：[旧版 README](https://github.com/Dicklesworthstone/mcp_agent_mail)。
- OpenHands Agent Canvas 文档提供每个会话独立容器的运行方式，同时明确提醒：若挂载同一宿主目录，文件仍共享，应使用不同目录或 worktree 避免编辑冲突。它支持工作区隔离思路，不能用来证明数据库事务可以覆盖文件写入。来源：[OpenHands README](https://github.com/OpenHands/OpenHands)。
- MySQL 8.4 使用 CATS 调度等待锁的事务，根据阻塞关系计算权重，权重相同时优先等待更久者。因此不能声称数据库线程池天然保证请求 FIFO。来源：[MySQL 8.4 Transaction Scheduling](https://dev.mysql.com/doc/refman/8.4/en/innodb-transaction-scheduling.html)。

## Minicode 当前实现的对应与边界

- `backend/execution/mysql_gate.py:32` 用 `mc_mutex` 行锁串行协调 claim 检查与插入；`:38` 检测授权根目录的相同及父子路径冲突，冲突时抛错。没有实现文件粒度 FIFO 等待队列。
- `backend/execution/mysql_gate.py:70` 检查 STARTED/UNKNOWN，未核实操作存在时保留持久占用。占用没有自动到期；属于协作式入口限制。
- `docs/mysql.md` 明确规定 LLM、Shell 和文件 I/O 在数据库事务外，数据库回滚不会撤销文件修改。短事务原子提交的是事件和状态投影。
- `docs/checkpoint-recovery.md` 规定恢复不自动执行工具；已运行但没有结果的操作记为 UNKNOWN，人工核实并保存证据后继续。不能把邮件幂等键、自动检查已发送邮件当成已经验证的产品功能。

## 面试表述需要修正

1. 将“实现分布式系统幂等性”收紧为“处理工具副作用不确定状态，避免恢复时盲目重试”。邮件只是应用场景举例；远端服务支持幂等键时才能依赖它去重，且必须在首次调用前持久保存并在重试中复用。
2. PREPARED 表示执行前准备，不是完成结果；FAILED 也未必代表没有部分副作用。运行中断和超时都可能需要核实。
3. 将“文件操作和数据库一起原子化”改为“数据库状态原子提交，加工作区占用协调工具执行”。互斥不提供文件与数据库的跨资源事务。
4. 将“不同文件互不影响、同文件自动排队”改为“按工作区目录检测冲突，不重叠目录可并行，冲突批次拒绝进入”。
5. JSONL 是格式，本身不提供事务或并发协调；配合锁、单写者和持久化协议也可提供恢复能力。MySQL 的价值是现成的事务、并发控制与查询能力，而不是 JSONL 在理论上无法实现。

# MySQL 存储与恢复

2026-10-19。MySQL 已接入实际运行入口。本地 JSONL 模式继续可用；每个实例只使用所选模式的会话存储，不双写主日志，也不自动迁移旧会话。

## 配置

准备一个独立 MySQL 8.4 数据库和拥有该库建表、查询、插入及更新权限的账户。首次连接创建 `mc_` 前缀的 InnoDB 表和版本记录，应用不创建数据库。启动检查要求 `innodb_flush_log_at_trx_commit=1`；启用 binlog 时还要求 `sync_binlog=1`。实际掉电保证仍取决于服务器、磁盘和备份配置。

可在页面执行模式行点击“本地”或“MySQL”，填写主机、端口、库名、用户名与密码，先检查连接，再保存。连接检查会验证建表权限并初始化表。保存后重启 MiniCode 才切换；页面始终显示当前实际模式，数据库不可用会显示错误，不回退到本地。运行中的任务必须先停止。

页面配置保存在 `.minicode/storage.json`。POSIX 下文件权限为 0600，密码不回显，密码框留空会保留已有值。它仍是本机明文配置，应由应用目录权限保护。优先级为：进程环境变量 > 页面保存配置 > `.env`。进程变量覆盖了页面设置时，页面会提示。

也可直接在 `.env` 配置：

```dotenv
MINICODE_STORAGE_MODE=mysql
MINICODE_MYSQL_HOST=127.0.0.1
MINICODE_MYSQL_PORT=3306
MINICODE_MYSQL_DATABASE=minicode
MINICODE_MYSQL_USER=minicode
MINICODE_MYSQL_PASSWORD=填写本机密码
MINICODE_MYSQL_POOL_SIZE=8
# 远程 TLS：提供受信任 CA，同时校验服务器证书与主机名。
# MINICODE_MYSQL_SSL_CA=/absolute/path/ca.pem
```

未配置 MySQL 必填项会拒绝启动；连接失败也会拒绝启动，不会伪装成已启用。连接检查成功只说明当时数据库可用。

本机已安装环境的管理方式见 [本机安装与连接](mysql-local-setup.md)。该记录不包含密码。

## 数据与提交边界

| 表 | 职责 |
| --- | --- |
| `mc_events` | 有序事实流，包括接单原文、完整调用、工具结果、消息、审批、有效 Context 摘要及核实记录 |
| `mc_sessions` | 最新序号、会话信息、程序状态、持久执行者身份 |
| `mc_operations` | 按 session/turn/request/call 派生的稳定 operation ID、状态及结果位置 |
| `mc_checkpoints` | 版本、covered_seq、状态、模型历史投影和恢复种子 |
| `mc_receipts` | 稳定事务 ID 对应的已提交结果，用来处理 COMMIT 回执丢失 |
| `mc_claims` / `mc_mutex` | 工作区执行占用及短事务内的协调 |
| `mc_schema` | 数据库结构版本 |

接单事件、原文和起始状态在同一事务提交后才返回 run ID。完整工具调用先保存在 assistant 消息及 tool_start；通过审批、版本检查后提交包含参数与源记录位置的 PREPARED，再条件更新为 STARTED。STARTED 与工作区占用校验提交成功后才调用文件工具或 Shell。结果事件、操作投影与会话投影一起提交，模型消息从同一结果投影，避免再写一份工具消息。

LLM、审批等待、Shell、文件读写和附件保存都在事务外。附件先可靠写文件，再把引用和哈希保存到事件。数据库回滚不会撤销文件修改。

每个事务先插入稳定 ID 的 receipt。COMMIT 抛错时，用新连接对同一 ID 做锁定查询，等待原事务结局；查到结果则复用。无法确认时冻结 writer，在存储异常中保留该 ID，禁止普通清理继续写入或派发工具。恢复只读取事实，不根据超时重新执行回调或工具。

存储保持现有同步接口；异步请求及 Agent 任务通过 greenback 桥接，将完整存储动作送入有界工作线程池。队列入口有背压，线程内按 store 锁串行，并保持任务身份。取消时先等待已开始的事务线程完成，之后才允许清理执行占用。

## 顺序、工作区与旧执行者

同一 session 的序号通过行锁与版本校验分配。Runtime 打开会话时取得持久 owner，其他执行者不能同时恢复或接单。owner 没有自动过期规则；数据库连接断开不会被视为旧进程已停止。

异常退出或当前 writer 冻结后，侧栏会显示“核实旧执行者”。确认旧执行者、Shell 及其全部子进程已停止后，填写证据并解除占用。该操作有 owner 版本校验并保存审计事件。重新打开只补中断事实，STARTED 无结果会变成 UNKNOWN；在检查点页逐项保存实际结果与核实证据后才解除工作区阻塞。原 UNKNOWN 证据继续保留，模型看到核实后的结论。系统不会自动重跑。

工具批次用授权目录的规范路径判断冲突，父子目录与相同目录互斥，不同目录可并行。占用不因超时或进程退出自动清除。当前数据库绑定单一主机和应用数据根目录；同一 OS 用户只允许一个 MySQL 执行域。原本地工具锁与 MySQL 共用 OS 协调：MySQL 批次之间共享，和本地批次互斥；持久域标记阻止另一数据库或本地模式在异常退出后绕过 UNKNOWN。只有所有 owner、工作区占用及未决操作都消除并正常关闭后，才自动清理域标记。

这些是协作式限制，不约束外部编辑器、手工 Shell、绕过入口的程序或另一个 OS 用户。当前未开放跨机器执行，未声称任意 Shell exactly-once。

## Checkpoint 与历史

checkpoint 事件、状态、快照在同一已提交边界保存。启动在一致读视图中校验 schema/reducer 版本、session ID、快照摘要、covered_seq 及边界事件，随后只查询 `seq > covered_seq` 的事件。坏或旧快照回退全量事件。完整日志和 JSON 轨迹仍可按需导出；MySQL 不创建本地主 JSONL。

快照保存 Context 所需消息和有效压缩摘要。它不替代原始事件，也不是项目文件的 undo。普通运行不会因为获取历史输出路径而扫描完整审计源。

长期记忆、提示词、技能、请求输入附件和大输出附件仍使用本机文件存储。长期记忆是已有可重建辅助功能，后台提取任务没有持久队列。本次没有新增 Redis、MQ 或第二个数据库。

## 验证方式与边界

本次在临时目录启动真实 MySQL 8.4.11，以本机 Unix socket 隔离，未使用生产数据库或真实模型。测试覆盖存储重开、checkpoint 后缀、稳定 ID 重复请求、COMMIT 回执丢失、无法核实后的冻结、持久 UNKNOWN 核实、旧 owner 失效、嵌套目录冲突、12 会话并发写入、HTTP 真实文件操作，以及一个会话持有真实行锁时另一会话仍可接单与取消。

运行本地兼容测试：`uv run pytest -q`。要运行真实数据库测试，设置 `MINICODE_TEST_MYSQL_SOCKET` 指向隔离 MySQL socket，再执行 `uv run pytest -q others/test/integration/test_mysql.py`。测试账户需要创建和删除临时数据库权限；每项测试使用独立随机库名，结束清理。

尚未验证实际断电、跨机器执行、Windows 实机、真实模型和持续高负载吞吐。12 会话测试证明该测试范围内的顺序及隔离，不代表生产容量指标。当前 API 仍是本机同源应用，没有多人远程认证。receipt、事件和快照没有自动清理策略，需按审计与备份需求规划保留空间。

接口依据：[MySQL 锁定读取](https://dev.mysql.com/doc/refman/8.4/en/innodb-locking-reads.html)、[PyMySQL 连接与事务](https://pymysql.readthedocs.io/en/latest/modules/connections.html)、[greenback 同步/异步桥接](https://greenback.readthedocs.io/en/latest/reference.html)。

# 本机 MySQL 安装与连接

2026-10-19。已在当前 macOS 用户下安装 MySQL Community Server 8.4.11，并将本项目切换到真实 MySQL。应用后端已验证 `active_mode=mysql`、`connected=true`、`restart_required=false`，Shell 沙箱也已通过启动验证。没有调用真实模型生成回复。

## 已配置的服务

- MySQL：`127.0.0.1:3306`，仅接受本机连接。
- 数据库：`minicode`；应用账号：`minicode`，权限只覆盖该库。
- 安装和数据目录：`~/Library/Application Support/MiniCode/mysql/`，不在 Git 仓库内。
- MySQL 登录自启动：`~/Library/LaunchAgents/io.minicode.mysql.plist`，在当前用户登录后运行。
- 项目连接：`.minicode/storage.json`，密码保存在本机，文件权限 0600。
- 管理员凭据：安装目录下的 `admin.json` 和 `admin.cnf`，同样为 0600；不要上传或转发。

官方压缩包下载后校验了发布的 MD5，并保存了下载地址和 SHA-256 到安装目录的 `install-info.json`。配置要求事务提交同步落盘，启用 binlog 时同步 binlog。初始化阶段关闭网络，设置管理员密码和专用账号后才启用本机 TCP 监听。

MiniCode 页面地址：<http://127.0.0.1:8765>。后续可从项目目录运行：

```sh
uv run python start.py --no-browser
uv run python stop.py
```

原本地 JSONL 会话和附件继续保留，没有自动搬到数据库。切换回本地模式前，先停止任务、核实未决操作并正常关闭 MySQL 模式实例，再在页面保存本地配置并重新启动。

## 管理 MySQL

查看服务：

```sh
launchctl print "gui/$(id -u)/io.minicode.mysql"
```

停止服务（仅关闭 MiniCode 不会关闭数据库）：

```sh
launchctl bootout "gui/$(id -u)/io.minicode.mysql"
```

重新启动已停止的服务：

```sh
launchctl bootstrap "gui/$(id -u)" "$HOME/Library/LaunchAgents/io.minicode.mysql.plist"
```

使用本地管理员配置打开客户端，命令不会把密码放进参数：

```sh
"$HOME/Library/Application Support/MiniCode/mysql/8.4.11/bin/mysql" \
  --defaults-extra-file="$HOME/Library/Application Support/MiniCode/mysql/admin.cnf"
```

数据库数据在安装目录的 `data/`，日志在 `mysql-error.log`。卸载或更换安装目录前先备份数据库，不要直接删除数据目录。登录自启动不等于备份。

## 验证与发布

在独立、关闭网络的测试实例中跑完整测试，避免测试清理触碰实际 `minicode` 库。当前完整测试为 272 项通过，包含 15 项真实 MySQL 测试；静态检查和前端模块语法检查通过。

GitHub 只上传源码、测试和文档。`.env`、`.minicode/`、数据库数据、管理员凭据与测试运行记录均不上传。应用的密钥和连接信息由各安装环境自行配置。

此前 [实现交付记录](mysql-implementation-report.md) 描述实现阶段，当时尚未配置实际连接；本文记录其后的本机安装启用状态。[MySQL 协议](mysql.md) 说明恢复与并发边界。

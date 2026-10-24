# 在 minicode 中用自然语言发 Gmail

minicode 直接调用 Google 官方 Gmail API 和官方 Python 客户端。首次连接后，可以在聊天中输入：

> 用 Gmail 给 2046325952@qq.com 发一封邮件，主题是“minicode 发信测试”，正文是“这是一封由 minicode 发送的测试邮件。”

模型先准备邮件，再打开现有审批卡片。展开卡片核对 To、Cc、Bcc、主题和正文，批准后才会发送。Auto 模式也需要逐封批准；Plan 模式不能准备或发送邮件。当前支持纯文本和抄送、密送，不支持附件、HTML、联系人查询或读取邮箱。

## 首次连接

在 [Google Cloud 控制台](https://console.cloud.google.com/) 创建或选择项目。按 [Gmail Python quickstart](https://developers.google.com/workspace/gmail/api/quickstart/python) 完成以下设置：

1. 启用 Gmail API。
2. 配置 Google Auth platform 的应用名称、受众和联系邮箱。个人 Gmail 使用 External；测试状态下将自己的 Gmail 账号加入 Test users。
3. 创建 OAuth client ID，应用类型选择 **Desktop app（桌面应用）**，下载客户端 JSON，保存在仓库外，例如 `~/Downloads/minicode-gmail-client.json`。
4. 在 minicode 项目目录安装依赖并运行连接命令：

```sh
uv sync
uv run python -m backend.gmail connect --credentials ~/Downloads/minicode-gmail-client.json
```

命令会打开 Google 授权页面。选择要用于发信的 Gmail，确认发送权限。浏览器回调仅监听本机 `127.0.0.1`，五分钟未完成会超时。不要把客户端 JSON 或授权令牌粘贴进聊天。

只申请 `gmail.send` 权限，不申请读取邮箱。外部应用处于 Testing 状态时，此类 refresh token 通常七天后过期，需要重新连接；详情见 [Google OAuth token expiration](https://developers.google.com/identity/protocols/oauth2#expiration)。组织账户也可能受管理员策略限制。

连接完成后启动或重启 minicode：

```sh
uv run python start.py
```

如果应用使用其他数据目录，连接时要使用同一目录：

```sh
uv run python -m backend.gmail --project /绝对路径/应用目录 connect --credentials /绝对路径/client.json
```

检查本地连接状态：

```sh
uv run python -m backend.gmail status
```

这只检查授权文件是否存在，不验证远端是否过期。更换账号或重新授权前，先停止 minicode，再删除本地连接并重新 connect：

```sh
uv run python stop.py
uv run python -m backend.gmail disconnect
```

disconnect 不会远程撤销 Google 授权；可在 [Google 账号连接管理](https://myaccount.google.com/connections) 中撤销。

## 重复发送与未知结果

`GmailPrepareEmail` 把正文和收件人保存为不可修改的发送请求，返回 `request_id`。同一连接下，相同 To/Cc/Bcc、主题和正文会返回原请求。`GmailSendEmail` 只接受这个 ID，审批后不能替换正文。发送记录独立保存在应用的 `.minicode/gmail/outbox.sqlite3`，跨会话、重启和并发调用生效；与会话使用 JSONL 还是 MySQL 无关。

工具只对同一份本地 outbox 提供重复提交保护。不要清除 outbox、复制到多台机器独立发送，或在未知状态下更改正文以绕过去重。OAuth 重新连接会建立新的连接身份，旧连接的请求不能通过新账号发送。

发送前先提交 `sending` 状态，再调用 Gmail，明确关闭 SDK 和授权 HTTP 层的发送重试。Gmail 返回消息 ID 后才记录 `sent`。超时、异常、进程退出或成功响应未保存，会显示 `unknown`：邮件可能已经发出，后续调用只返回记录，不自动发送。即便崩溃发生在真正调用前，也可能保留未知状态，这是优先避免重复邮件的取舍。

可以让模型调用 `GmailEmailStatus` 查看本地记录。`sent` 只代表 Gmail 接受发送；不代表收件人已收到。`unknown` 需要手动检查 Gmail 的“已发送”邮件，当前没有自动对账或解除未知状态的入口。由于仅授权发送，工具不能自行搜索邮箱确认。

如果明确要把一封已确认发送的邮件再发一次，说“请把刚才那封已发送邮件再发一封”。模型可用原 `request_id` 作为 `resend_of` 准备新请求，再次审批。同一 `resend_of` 和相同内容仍会去重，未知结果不能用这个入口重发。

授权令牌位于 `.minicode/gmail/token.json`，不提供给模型，目录和令牌分别限制为当前用户访问；邮件正文也保存在本机 outbox 和现有会话审批记录中。保留这些记录才能继续判断之前的发送状态。

## 验证记录

离线测试覆盖模型工具审批、拒绝、Plan/Auto 模式、重复请求、并发、重启、响应丢失、发送中崩溃、账号变化，以及邮件头注入。真实测试尚未执行：用户尚未提供 OAuth 客户端并完成浏览器授权。用户只授权测试邮件发送到 `2046325952@qq.com`。

官方接口与开源项目比较见 [Gmail 发信幂等性调研](research/gmail-send-idempotency.md)。Gmail 发送接口没有公开的幂等键契约，Message-ID 仅用于关联，不能保证服务端去重。

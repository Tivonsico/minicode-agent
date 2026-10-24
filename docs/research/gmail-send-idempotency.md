# Gmail 官方发送接口与防重复发送调研

调研日期：2026-10-21。以下区分官方接口事实、开源项目做法与 minicode 的设计建议；没有执行真实邮件发送。

## 官方接入方式

使用 Google Gmail REST API `users.messages.send`，通过官方 Python 客户端 `google-api-python-client` 调用。请求将 RFC 2822 MIME 邮件编码为 base64url，放入 `Message.raw`，`userId="me"` 表示授权用户；成功响应为包含 Gmail 内部消息 ID 的 Message。To/Cc/Bcc 决定实际收件人。[官方发送指南](https://developers.google.com/workspace/gmail/api/guides/sending)、[send 接口](https://developers.google.com/workspace/gmail/api/reference/rest/v1/users.messages/send)

个人桌面应用可用 OAuth Desktop app：用户提供 Google Cloud OAuth client JSON，启用 Gmail API，浏览器授权，保存 access/refresh token，过期后刷新。这是 Google 官方 quickstart 使用的流程。凭据文件应放用户私有配置目录，限制文件权限并忽略版本控制。[官方 Python quickstart](https://developers.google.com/workspace/gmail/api/quickstart/python)

默认只申请 `https://www.googleapis.com/auth/gmail.send`，用途是代用户发信。搜索已发送邮件需要另行申请读取权限；`gmail.send` 不能用来搜索邮件。`gmail.readonly` 属 restricted scope，`gmail.send` 属 sensitive scope，故不为防重复发送默认扩大至读取整箱邮件。[官方 scope 表](https://developers.google.com/workspace/gmail/api/auth/scopes)、[messages.list 授权范围](https://developers.google.com/workspace/gmail/api/reference/rest/v1/users.messages/list)

## 幂等边界

`messages.send` 的公开协议没有 `idempotencyKey` / `requestId` 参数，也没有记录幂等请求头契约。**据该公开协议，不能声称 Gmail 提供发送幂等保证。** 当请求可能已到 Gmail，但响应超时或连接断开时，调用方无法仅从异常判断是否已发送。minicode 应报告“结果未知，可能已经发出”，禁止自动重新提交。[send 接口](https://developers.google.com/workspace/gmail/api/reference/rest/v1/users.messages/send)

RFC `Message-ID` 可作为关联标识，不能充当 Gmail 官方去重键。Gmail 支持用 `rfc822msgid:<id>` 搜索，但搜索匹配不等于发送接口拒绝重复提交。即便以后加入可选读取权限，也应把查到 SENT 邮件作为成功的正面证据；暂时搜不到不能作为安全重发的证明。[messages.list 的 q 参数](https://developers.google.com/workspace/gmail/api/reference/rest/v1/users.messages/list)

草稿可提供稳定 draft ID，但发送时 Gmail 删除草稿，创建新的 SENT 消息并改变内部 message ID。因此“草稿不存在”本身不能证明已发送，也可能由其他行为删除；草稿机制没有在公开文档中承诺 exactly-once，不应以创建新草稿重发来恢复未知状态。[官方草稿语义](https://developers.google.com/workspace/gmail/api/guides/drafts)

## 开源项目中实际可借鉴的做法

| 项目 | 源码 / 事实 | 对 minicode 的启示 |
| --- | --- | --- |
| Google 官方 python-samples | 使用 EmailMessage、base64url、`messages.send(...).execute()`，成功后读取 `id`；没有持久 outbox 或业务幂等实现。[源码](https://github.com/googleworkspace/python-samples/blob/main/gmail/snippet/send%20mail/send_message.py) | 复用官方 MIME/OAuth/API 方式，但示例不是生产防重复方案。 |
| google-api-python-client | `HttpRequest.execute` 默认 `num_retries=0`；`_retry_request` 在启用重试时会处理 5xx、429、部分 403、socket timeout、SSL/连接错误。[源码](https://github.com/googleapis/google-api-python-client/blob/main/googleapiclient/http.py) | 发信明确设置 `num_retries=0`，不能套用普通读操作的重试装饰器。也要检查更底层 HTTP transport 是否自行重试。 |
| google_workspace_mcp | issue #997 记录发信启用 write retries 引起重复发送风险；调研时直接获取 main 的 `send_message_once` 已改为 `num_retries=0`，将网络/5xx 异常转换为“可能已经发送，请检查 Sent”的错误。[原始问题](https://github.com/taylorwilsdon/google_workspace_mcp/issues/997)、[当前 helper 源码](https://github.com/taylorwilsdon/google_workspace_mcp/blob/main/gmail/gmail_helpers.py) | 采用单次发送和显式未知状态，并加上本地持久状态以抵御模型重试和进程崩溃。issue 的旧行号不作为当前源码证据。 |
| googleworkspace/cli | 可服务 AI agents，但 README 明确声明不是 Google 官方支持的产品。[README](https://github.com/googleworkspace/cli#readme) | 不将该 CLI 或第三方 MCP 标成“Gmail 官方工具”；minicode 集成的是官方 Gmail API。 |

备注：浏览器抓取的 helper 缓存曾滞后于 main，已通过直接 HTTP 获取原始源码核对 `send_message_once`。仓库 main 链接会变化；当前结论依赖上述函数行为，不把某条 issue 是否关闭作为实现已修复的依据。

## minicode 推荐实现（设计建议）

1. 用 SQLite 持久化 outbox，业务 operation ID/idempotency key 为唯一键；保存邮箱身份、规范化 payload 哈希、稳定 RFC Message-ID、状态和 Gmail 返回 ID。相同 key、不同内容直接拒绝；相同 key、相同内容返回已有状态。
2. 在网络调用前，事务提交 `sending`，由数据库原子条件更新保证同一操作只有一个发送者。新进程看到既有 `sending` 时按 `unknown` 对待，不抢占后再次发信。网络前崩溃也可能留下 unknown，这是防重复优先所接受的可用性代价。
3. 有有效成功响应且非空 Gmail ID 后才持久化 `sent`；超时、连接断开、5xx、响应缺少 ID、成功后写库失败均保留 `unknown`。不要把这些返回成可自动重试的普通“失败”。
4. 本地校验失败可修正；收到明确拒绝的 4xx 可记录 rejected，并让用户修正授权或参数。实现初版可以保守地阻止所有已尝试操作的自动重发。新的刻意重发必须是用户新指令，并使用新 operation ID。
5. 不以“收件人+主题+正文”哈希作为永久业务 ID，否则用户刻意第二次发送相同邮件会被误挡。operation ID 应绑定一次用户发送意图，并在工具调用重试、恢复、并发时稳定复用；payload 哈希仅用于检测 key 被错误复用。
6. 工具结果返回结构化 `sent`、`already_sent`、`sending`/`unknown`、`rejected` 状态；模型只能对 `sent`/`already_sent` 宣称“已发送”。提供本地状态查询工具，不需要邮箱读取权限。

这提供**单个本地 operation ID 的重复提交保护和未知状态停发**，不宣称跨独立数据库、不同 operation ID 或 Gmail 服务端 exactly-once。要防模型更换 key 绕过，运行时还需为一个用户轮次分配稳定操作身份，或在工具说明和持久记录中明确要求重用现有 ID。

## 本次实现复核

实际接入使用官方 `google-api-python-client` 和 `google-auth-oauthlib`。补充核对已安装的 httplib2 0.32.0 源码后发现：`_conn_request` 在响应阶段收到 `BadStatusLine` / `ResponseNotReady` 时会重发，甚至不能只靠 `RETRIES=1` 关闭。[上游源码](https://github.com/httplib2/httplib2/blob/master/httplib2/__init__.py)

因此发送时给 Google SDK 提供一个 HTTPX 传输适配器，保留 SDK 的 Gmail 请求构造，使用已刷新的 Google 凭据加请求头；HTTPX 默认不重试连接，关闭重定向，也不使用会在 401 后重放请求的授权 HTTP 包装层。SDK 仍显式 `num_retries=0`。回归测试通过真实 SDK 和模拟 HTTPX 边界注入响应丢失，确认只提交一个 POST 并记录 unknown。[HTTPX transport 文档](https://www.python-httpx.org/advanced/transports/)、[Google 授权 HTTP 自动刷新/重试参数](https://google-auth.readthedocs.io/en/latest/reference/google.auth.transport.requests.html)

首版采用更保守的内容去重：同一连接的相同收件人、主题和正文重用原请求；用户刻意再发时，必须引用已确认 sent 的原请求作为 `resend_of`，生成新的稳定请求。相同内容加相同 resend_of 仍复用请求。这样允许明确重发，也避免模型在恢复时随意生成新随机 ID。未知状态不能通过 resend_of 解除；本地记录删除、连接重建、修改内容或多份独立 outbox 不在这项去重保证内。

# minicode 沙箱选型核验

日期：2026-10-23。依据官方文档；未安装软件或修改实现。以下选型是工程建议，不代表完成安全审计。

## 建议

**针对当前 minicode，保留现有执行器，不必为接沙箱而换成 srt。** 主调查已核验：Shell 已经通过 `CodexSandbox.argv` 使用系统执行沙箱，restricted 策略、净化环境与启动验证均已存在；验证失败禁用执行，没有裸执行 fallback；本机现有 verify 返回 true。文件 Read/Edit 仍在主进程直接操作文件，依赖应用层检查。这一现状由主调查提供，执行器代码入口为 `backend/execution/codex.py`，本笔记不重复 OpenAI 产品调研。

后续优先把模型可触发的文件、搜索与 Shell 统一放进受限 worker，控制面、密钥与外部高权限连接留在可信侧。srt 是自定义进程 worker 的备选，并非当前必须新增的依赖。要处理不可信仓库与任意依赖安装时，可把完整 agent 再放入 microVM，配合私有 clone 或不挂载主机工作区；这需要 Linux 环境适配和额外资源。

## 两条路线

| 方案 | 实际边界 | minicode 适配与局限 |
| --- | --- | --- |
| sandbox-runtime / srt | macOS Seatbelt 约束被启动进程及整个子进程树，限制文件、网络与 Unix sockets | CLI 可包装任意命令，因此可包装 Python agent；只包装 bash 时，父 Python 中的文件工具和外部 MCP 不在边界内。默认读取开放，必须配置；不是独立 VM |
| Docker Sandboxes | 每个 sandbox 独立内核的 microVM；主机仅显式共享资源可达，网络走政策代理 | 自定义 Python agent 可运行在 Linux workload；需要在 VM 内安装 Python 依赖。可写主机共享目录依旧可被改写；MCP gateway 的宿主 stdio 服务器在 VM 外 |

依据：[sandbox-runtime 官方 README](https://github.com/anthropics/sandbox-runtime)、[Docker 隔离层](https://docs.docker.com/ai/sandboxes/security/isolation)、[Docker 安全模型](https://docs.docker.com/ai/sandboxes/security/)。上表关于 minicode 的覆盖范围是据进程边界作出的推论。

## Docker Sandboxes 能否运行自定义 Python agent

可以。官方支持无预装 agent 的 shell 环境，也支持自定义 workload。可先在 shell 中手工安装验证；需要重复使用时，自建 Linux 镜像，在镜像中安装 Python、minicode 和依赖，用 `ENTRYPOINT` 或 `CMD` 指定 agent。官方自定义镜像要求包括 `agent` 用户（UID 1000、home `/home/agent`）、可执行 `/bin/sh` 和 `/bin/bash`、Git/curl/CA 证书。Kit descriptor 声明网络、凭据及启动契约。[基础镜像与启动要求](https://docs.docker.com/ai/sandboxes/customize/author/base-images/)

当前推荐 v3 kits；v3 要求 `sbx` 0.45 或以上，workload 与 mixin 不能混用 v2/v3。该能力标注 Early Access，实施时应固定版本与镜像，按所用版本验证。[Kits 官方说明](https://docs.docker.com/ai/sandboxes/customize/)

当前本地 macOS 安装条件是 macOS 14 Sonoma 或以上、Apple Silicon；独立 `sbx` CLI 不要求安装 Docker Desktop 或宿主 Docker Engine；文档要求 Docker 登录。[安装条件](https://docs.docker.com/ai/sandboxes/install/)

配置建议：优先 `--clone` 或 mountless，避免把宿主工作树直接可写挂入；不转发不需要的 SSH agent，不开放宿主 Docker socket，不添加宿主 MCP 执行入口。若显式共享工程可写，恶意 Git hooks、构建脚本与 IDE 配置仍可能在宿主日后执行时生效。代理放行的域名也仍可承载外部副作用。[安全模型及共享资源限制](https://docs.docker.com/ai/sandboxes/security/)

## srt 的最小落点

官方提供 npm CLI，可用 `srt --settings /可信路径/settings.json python ...` 形式启动任意 Python 命令。macOS 使用系统 `sandbox-exec`；当前为 Beta Research Preview。默认不允许网络，默认读取不限制；应显式拒绝用户敏感目录，再开放工程和 Python/依赖所需目录，写权限只开放工作副本和必要缓存。只包装某个命令不会约束外部宿主进程，也不会保护 agent 已经持有的凭据；不能将其解释为“整个系统进入沙箱”。[CLI、进程树及默认策略](https://github.com/anthropics/sandbox-runtime)

建议验收：从 agent 的真实工具入口尝试跨目录读写、符号链接写出、子进程读敏感文件、直连未放行网络；检查所有入口均失败，同时正常测试、文件编辑与模型调用可用。配置文件与代理留在可信侧，失败时中止而非降级为裸执行。

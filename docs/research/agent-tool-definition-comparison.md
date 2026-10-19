# Agent 工具如何定义、注册和执行

核查日期：2026-10-18。只使用官方源码、官方文档。本文是研究记录，不涉及程序修改。

## 共同结构

工具通常由两部分组成：模型看到的名称、描述、参数 Schema，以及程序执行的 handler。模型产生工具名和参数，宿主程序按名称找到 handler，校验并执行，随后把结果交回模型。继承只是实现统一接口的一种方式；函数加数据对象也能完成同样的工作。[Claude 官方工具说明](https://platform.claude.com/docs/en/agents-and-tools/tool-use/how-tool-use-works)、下述两套官方源码分别展示了这些接口。

## Codex：trait 接口、具体实现、按名称索引的注册表

固定源码版本：`8c076942cb70fba8e5c4732c23f1faabc4aed484`。这是本次读取 GitHub main 文件页面中的 currentOid，链接均固定到此版本。

- 公共接口是 `ToolExecutor<Invocation>`，必要方法包括 `tool_name()`、`spec()`、`handle()`；还提供暴露方式、搜索信息、并行能力等默认方法。[tool_executor.rs:106–130](https://github.com/openai/codex/blob/8c076942cb70fba8e5c4732c23f1faabc4aed484/codex-rs/tools/src/tool_executor.rs#L106)
- core 层的 `CoreToolRuntime` 扩展这个 trait，增加取消、hook、遥测等宿主能力。[registry.rs:59](https://github.com/openai/codex/blob/8c076942cb70fba8e5c4732c23f1faabc4aed484/codex-rs/core/src/tools/registry.rs#L59)
- 一个真实例子是 `CurrentTimeHandler`：实现 `ToolExecutor<ToolInvocation>`；`spec()` 返回名称、描述、输入和输出 Schema；`handle()` 实现调用；另行实现 `CoreToolRuntime`。[current_time.rs:50](https://github.com/openai/codex/blob/8c076942cb70fba8e5c4732c23f1faabc4aed484/codex-rs/core/src/tools/handlers/current_time.rs#L50)
- 注册表 `ToolRegistry` 内部使用 `IndexMap<ToolName, RegisteredTool>`，其中 runtime 是共享的动态 trait 对象。注册路径为 `add(handler)` → `register_trusted(...)` → 检查策略与重名 → 插入映射；查询按名称取 runtime。[registry.rs:305](https://github.com/openai/codex/blob/8c076942cb70fba8e5c4732c23f1faabc4aed484/codex-rs/core/src/tools/registry.rs#L305)、[注册入口:402](https://github.com/openai/codex/blob/8c076942cb70fba8e5c4732c23f1faabc4aed484/codex-rs/core/src/tools/registry.rs#L402)、[名称查询:576](https://github.com/openai/codex/blob/8c076942cb70fba8e5c4732c23f1faabc4aed484/codex-rs/core/src/tools/registry.rs#L576)
- 分发过程中取工具并最终调用 `handle(...).await`。实际代码还包括执行策略、hooks、取消等处理。[registry.rs:615](https://github.com/openai/codex/blob/8c076942cb70fba8e5c4732c23f1faabc4aed484/codex-rs/core/src/tools/registry.rs#L615)、[实际调用:919](https://github.com/openai/codex/blob/8c076942cb70fba8e5c4732c23f1faabc4aed484/codex-rs/core/src/tools/registry.rs#L919)
- `ToolSpec` 是可序列化的类型，覆盖 function、namespace、web search、freeform 等规格。[tool_spec.rs:18](https://github.com/openai/codex/blob/8c076942cb70fba8e5c4732c23f1faabc4aed484/codex-rs/tools/src/tool_spec.rs#L18)

注意：当前版本不使用旧资料里常见的 `ToolHandler`、`ToolRegistryBuilder` 命名。`from_tools` 在此版本带有 `#[cfg(test)]`，不应将其描述为生产装配入口。

下例是按真实关系编写的 Python 类比，便于阅读，不是 Codex 原文或可直接移植的接口：

```python
class ToolExecutor(Protocol):
    def tool_name(self) -> str: ...
    def spec(self) -> dict: ...
    async def handle(self, invocation): ...

class ClockTool:
    def tool_name(self):
        return "clock.curr_time"

    def spec(self):
        return {"name": "curr_time", "parameters": {"type": "object"}}

    async def handle(self, invocation):
        return read_current_time()

tools_by_name = {}
clock = ClockTool()
tools_by_name[clock.tool_name()] = clock
result = await tools_by_name[call.name].handle(call)
```

## Claude Code：公开 SDK 可确认函数封装；内置核心实现不能据此推断

这里能直接核对的是 Claude Agent SDK Python 的公开实现，以及 Claude Code 官方自定义工具文档。该证据不能证明 Claude Code 内置 Bash、Read 等工具在未公开内核里具体使用何种基类。[Claude Code 官方仓库](https://github.com/anthropics/claude-code)、[自定义工具文档](https://code.claude.com/docs/en/agent-sdk/custom-tools)

SDK 固定版本：`f93e29cc951cd5fddcaaed9f3c49c3c38dedd6ab`。

- `SdkMcpTool` 是 dataclass，保存名称、描述、输入 Schema、异步 handler 以及可选 annotations。[__init__.py:266](https://github.com/anthropics/claude-agent-sdk-python/blob/f93e29cc951cd5fddcaaed9f3c49c3c38dedd6ab/src/claude_agent_sdk/__init__.py#L266)
- `@tool(...)` 返回装饰器，把普通异步函数包装成 `SdkMcpTool` 实例，无需用户为每个工具写子类。[__init__.py:350](https://github.com/anthropics/claude-agent-sdk-python/blob/f93e29cc951cd5fddcaaed9f3c49c3c38dedd6ab/src/claude_agent_sdk/__init__.py#L350)
- `create_sdk_mcp_server(..., tools=[...])` 接收列表，内部构造按名称索引的字典及提供给 MCP 的工具描述；执行时按名称查找，做 JSON Schema 校验，调用异步 handler。[__init__.py:517](https://github.com/anthropics/claude-agent-sdk-python/blob/f93e29cc951cd5fddcaaed9f3c49c3c38dedd6ab/src/claude_agent_sdk/__init__.py#L517)、[列表转字典及执行:604](https://github.com/anthropics/claude-agent-sdk-python/blob/f93e29cc951cd5fddcaaed9f3c49c3c38dedd6ab/src/claude_agent_sdk/__init__.py#L604)

下面是根据 SDK 公开接口编写的独立示例，非文档原文：

```python
from claude_agent_sdk import tool, create_sdk_mcp_server, ClaudeAgentOptions

@tool("double", "计算整数的两倍", {"value": int})
async def double(args):
    return {"content": [{"type": "text", "text": str(args["value"] * 2)}]}

server = create_sdk_mcp_server("math", tools=[double])
options = ClaudeAgentOptions(
    mcp_servers={"math": server},
    allowed_tools=["mcp__math__double"],
)
```

服务器注册到 `mcp_servers`；`allowed_tools` 控制无需逐次确认的权限，单独写允许列表不等于实现或注册工具。完整名称使用服务器键与工具名组成。[官方调用自定义工具说明](https://code.claude.com/docs/en/agent-sdk/custom-tools#call-a-custom-tool)

## 本项目：Tool 数据类组合 handler，双层名称分发

以下项目事实由主代理核查文件后提供；路径及行号是当前工作区版本：

- `backend/tools/base.py:31` 定义 `Tool` dataclass，字段为 `tool_name`、`help`、`schema`、`handler`、`read_only`、`destructive`、`tool_category`；`:65` 的 `execute()` 等待 handler；`:68` 的 `api_schema()` 生成模型接口需要的结构。每个工具没有通过继承该类来实现逻辑。
- `backend/tools/registry.py:4` 的 `ToolRegistry` 使用字典；`:8` 按工具名称注册；`:18` 收集 Schema；`:31` 的 `build_registry(handler)` 中有九个工具的定义列表，`:143` 通过闭包给每个工具绑定名称并创建 `Tool` 实例。
- `backend/tools/executor.py:47` 以 `self._dispatch` 装配 registry；`:162` 的 `_prepare` 做校验；`:291` 执行 `tool.execute(...)`。
- `backend/tools/dispatch.py:8` 的 `HANDLERS` 又将名字映射到具体处理函数；`:34` 执行对应处理函数。
- `backend/runtime/loop.py:124` 获取 Schema，`:299` 执行批次；`backend/observability/requests.py:105` 将工具描述放入模型请求。

因此该项目目前是“描述列表 → 通用 Tool 对象 → 名称字典注册表 → handler 闭包 → executor 分发 → HANDLERS 函数表”，不是“工具基类 → 各工具子类 → 实例列表”。后者可以设计，但当前简单工具不需要为了统一外形而增加继承层级。

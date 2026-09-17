# 智谱接口核实记录

核实日期：2026-09-10。仅文档核实及离线模拟，尚未以真实密钥完成服务调用。

- [对话补全官方文档](https://docs.bigmodel.cn/api-reference/模型-api/对话补全)：确认官方端点、Bearer 认证、四种消息身份、原生函数调用、SSE `[DONE]` 结束标记，以及 usage 字段。程序要求明确结束原因与结束标记，缺少用量保持未知。
- 同页模型枚举列出 `glm-5.3`，未列出 Flash；推理参数段落却提及 `GLM-5.3-FLASH`。因此不能据此确认 Flash 请求 ID 的精确拼写及大小写。程序原样保留用户选择 `GLM-5.3-Flash`，不替换模型。
- [思考模式官方文档](https://docs.bigmodel.cn/cn/guide/capabilities/thinking-mode)：提及 GLM-5.3-FLASH 默认且强制思考；交错工具调用必须回传原始 `reasoning_content`。适配器独立累积并返回该字段，历史投影应保留它。
- 2026-09-10 使用当前账户完成一次最小非流式请求：请求中的 `GLM-5.3-Flash` 返回 HTTP 200，响应模型名为 `glm-5.3-flash`，并包含 usage。测试将输出限制为 16 token，结果以 `finish_reason=length` 结束且没有正文；这只证明账户、地址和模型 ID 可用，不证明完整文本或工具往返。
- 官方公开模型表仍未列出 Flash 5.3 的专属上下文窗口和输出上限。项目采用保守配置 `CONTEXT_WINDOW=200000`、`MAX_OUTPUT_TOKENS=65536`：前者不超过官方 GLM-5 系列公开的 200K 窗口，后者低于其 128K 最大输出，并与用户提供的请求示例一致。启动继续校验 `0 < MAX_OUTPUT_TOKENS < CONTEXT_WINDOW`。
- 不强制发送 `thinking`、`temperature`、`tool_stream`、`stream_options` 等未对实际选择模型验证的可选参数。直接使用 httpx，SDK 隐式重试不存在；仅无输出网络错误和临时 HTTP 错误最多重试两次。
- 流式文本、推理、交错工具参数、usage 缺失、限流、断流和取消由模拟 HTTP 测试验证。该证据不能代替真实模型的文本及工具往返验收。

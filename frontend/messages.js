const labels = { user: "你", assistant: "minicode", tool: "工具" };
export function element(tag, text, className) {
  const node = document.createElement(tag);
  if (text !== undefined) node.textContent = text;
  if (className) node.className = className;
  return node;
}
export function renderMessages(container, events) {
  const closeToEnd =
    container.scrollHeight - container.scrollTop - container.clientHeight < 100;
  const open = new Set(
    [...container.querySelectorAll("details[open]")].map(
      (node) => node.dataset.call,
    ),
  );
  const entries = [],
    messages = new Map(),
    calls = new Map();
  const discarded = new Set(events.filter(e => e.type === "request_recovery").map(e => e.message_id));
  const resolutions = new Map(events.filter(e => e.type === "operation_reconciled").map(e => [e.result_seq, e]));
  const toolKey = (event, id) => JSON.stringify([event.turn_id || "", event.request_id || "", id]);
  for (let event of events) {
    if (discarded.has(event.message_id)) continue;
    if (event.type === "run_accepted" && event.input) event = { ...event, ...event.input, type: "message" };
    if (event.type === "tool_result" && resolutions.has(event.seq)) {
      const record = resolutions.get(event.seq);
      event = { ...event, status: record.resolution === "not_applied" ? "cancelled" : record.resolution, content: `人工核实：${record.evidence}` };
    }
    if (event.type === "request_start" && event.purpose === "main") {
      if (!messages.has(event.message_id)) {
        const item = { role: "assistant", content: "", status: "waiting" };
        messages.set(event.message_id, item);
        entries.push(item);
      }
    } else if (event.type === "message") {
      let item = messages.get(event.message_id);
      if (!item) {
        item = {};
        messages.set(event.message_id, item);
        entries.push(item);
      }
      Object.assign(item, event);
      item.role = event.role;
      if (event.tool_calls)
        for (const call of event.tool_calls) {
          if (!calls.has(toolKey(event, call.id))) {
            const tool = {
              role: "tool",
              call_id: call.id,
              name: call.function.name,
              status: "pending",
              content: "",
            };
            calls.set(toolKey(event, call.id), tool);
            entries.push(tool);
          }
        }
    } else if (event.type === "assistant_delta") {
      let item = messages.get(event.message_id);
      if (!item) {
        item = { role: "assistant", content: "", status: "streaming" };
        messages.set(event.message_id, item);
        entries.push(item);
      }
      if (event.kind === "text") {
        if (["waiting", "thinking", "streaming"].includes(item.status)) {
          item.status = "streaming";
          item.content += event.content;
        }
      } else if (event.kind === "reasoning" && !item.content) {
        item.status = "thinking";
      }
    } else if (event.type === "tool_delta") {
      const item = messages.get(event.message_id);
      if (item && !item.content) item.status = "calling";
    } else if (event.type === "turn_end") {
      for (const item of messages.values()) {
        if (["waiting", "thinking", "calling"].includes(item.status)) item.status = "ended";
      }
    } else if (event.type.startsWith("tool_") && event.call_id) {
      let item = calls.get(toolKey(event, event.call_id));
      if (!item) {
        item = {
          role: "tool",
          call_id: event.call_id,
          name: event.name,
          content: "",
        };
        calls.set(toolKey(event, event.call_id), item);
        entries.push(item);
      }
      if (event.type === "tool_result")
        Object.assign(item, event, { role: "tool" });
      else if (event.type === "tool_awaiting_approval")
        item.status = "awaiting_approval";
      else if (event.type === "tool_running") item.status = "running";
    }
  }
  if (!entries.length) {
    container.replaceChildren();
    return;
  }
  const fragment = document.createDocumentFragment();
  for (const item of entries) {
    if (item.role === "tool") {
      const wrap = element("details", undefined, "message tool");
      wrap.dataset.call = item.call_id;
      wrap.open = open.has(item.call_id);
      const statuses = {
        pending: "等待执行",
        awaiting_approval: "等待审批",
        running: "执行中",
        succeeded: "已完成",
        failed: "失败",
        cancelled: "已取消",
        rejected: "已拒绝",
        unknown: "状态未确认",
      };
      wrap.append(
        element(
          "summary",
          `${item.name || "工具"} · ${statuses[item.status] || item.status || "等待执行"}`,
        ),
        element("pre", item.content || "结果将在执行后显示"),
      );
      fragment.append(wrap);
      continue;
    }
    if (
      item.role === "assistant" &&
      !item.content &&
      !["waiting", "thinking", "streaming", "calling"].includes(item.status)
    )
      continue;
    const wrap = element("article", undefined, `message ${item.role}`);
    const label = element("div", undefined, "message-label");
    label.append(element("span", labels[item.role] || item.role));
    if (item.status === "partial")
      label.append(element("span", "已中断 · 内容可能不完整", "partial"));
    wrap.append(
      label,
      element(
        "div",
        item.content || {
          waiting: "正在等待模型响应…",
          thinking: "模型正在思考…",
          calling: "模型正在准备工具调用…",
          streaming: "正在生成回复…",
        }[item.status] || "",
        "message-body",
      ),
    );
    fragment.append(wrap);
  }
  container.replaceChildren(fragment);
  if (closeToEnd) container.scrollTop = container.scrollHeight;
}

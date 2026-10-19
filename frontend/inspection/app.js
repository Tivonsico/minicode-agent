import { modeName } from "../modes.js";
import { request } from "../api.js";
import { element } from "../messages.js";
import { $, disclosure, download, duration, empty, time } from "./shared.js";
import { logViewer } from "./logs.js";
import { checkpointViewer } from "./checkpoints.js";
import { stateEditor } from "./state.js";
import { memoryEditor } from "./memory.js";

export function inspection(getDetail, reloadSession) {
  let sessionId = null,
    selected = "timeline",
    generation = 0;
  const getSession = () => sessionId;
  async function safely(action) {
    $("inspection-error").textContent = "";
    try {
      await action();
    } catch (error) {
      $("inspection-error").textContent = error.message;
    }
  }
  const loadLogs = logViewer(getSession, safely);
  const renderCheckpoints = checkpointViewer(getSession, refresh, safely);
  const renderState = stateEditor(getSession, refresh, safely);
  const loadMemory = memoryEditor(getDetail, safely);
  async function refresh() {
    const currentGeneration = ++generation;
    const sid = sessionId;
    const data = await request(`/sessions/${sid}/diagnostics`);
    if (currentGeneration !== generation || sid !== sessionId) return;
    const timeline = $("timeline-records");
    timeline.replaceChildren();
    for (const turn of data.turns) {
      const row = element("div", undefined, "timeline-turn");
      row.append(
        element("strong", turn.objective || "任务"),
        element(
          "p",
          `${time(turn.started_at)} · ${modeName(turn.agent_mode)} · ${turn.status} · ${duration(turn.duration_ms)}`,
          "subtle",
        ),
      );
      const records = [...data.requests, ...data.milestones]
        .filter((r) => r.turn_id === turn.turn_id)
        .sort((a, b) => a.seq - b.seq);
      const names = {
        step_start: "开始步骤",
        step_end: "结束步骤",
        tool_end: "工具结束",
        approval_closed: "审批结束",
        verification_result: "实际验证",
        completion_check: "完成前核对",
        request_recovery: "模型断流恢复",
        context_compressed: "摘要压缩",
        run_end: "运行结束",
      };
      for (const record of records) {
        const label =
          record.type === "request_start"
            ? `${record.purpose === "main" ? "模型请求" : record.purpose === "memory" ? "记忆提取" : "摘要压缩请求"} · 第 ${record.attempt} 次尝试 · 首正文 ${duration(record.first_text_ms)} · 总耗时 ${duration(record.duration_ms)}`
            : `${names[record.type]}${record.name ? ` · ${record.name}` : ""} · ${record.status || record.outcome || ""} · ${time(record.time)}`;
        row.append(disclosure(label, record));
      }
      timeline.append(row);
    }
    if (!data.turns.length)
      empty(timeline, "发送任务后，可以在这里查看每轮运行。");
    $("trace-metrics").textContent =
      `模型请求 ${data.metrics.requests} 次 · 已知用量 ${data.metrics.known_tokens.toLocaleString()} tokens · 用量未知 ${data.metrics.unknown_usage_requests} 次`;
    const summaries = $("summary-records");
    summaries.replaceChildren();
    for (const record of [...data.summaries].reverse()) {
      const item = disclosure(
        `${time(record.time)} · 覆盖日志 #${record.source_first_seq ?? "未知"}—#${record.source_last_seq ?? "未知"} · 压缩后估算 ${record.input_estimate}`,
        record,
      );
      summaries.append(item);
    }
    if (!data.summaries.length)
      empty(summaries, "尚未触发摘要压缩。上下文接近预算后，摘要会自动保存。");
    renderCheckpoints(data);
    renderState(data);
    if (selected === "logs") await loadLogs();
    if (selected === "memory") await loadMemory();
  }
  function select(name) {
    selected = name;
    for (const button of document.querySelectorAll("[data-inspection-tab]"))
      button.setAttribute(
        "aria-selected",
        String(button.dataset.inspectionTab === name),
      );
    for (const pane of document.querySelectorAll("[data-inspection-pane]"))
      pane.hidden = pane.dataset.inspectionPane !== name;
    safely(refresh);
  }
  for (const button of document.querySelectorAll("[data-inspection-tab]"))
    button.onclick = () => select(button.dataset.inspectionTab);
  $("inspection-refresh").onclick = () => safely(refresh);
  $("trace-export").onclick = () =>
    safely(async () =>
      download(
        await request(`/sessions/${sessionId}/trajectory`),
        `trajectory-${sessionId}.json`,
      ),
    );
  $("inspection-dialog").addEventListener("close", () => {
    generation++;
    reloadSession(sessionId);
  });
  return () => {
    sessionId = getDetail()?.session.session_id;
    if (!sessionId) return;
    $("inspection-status").textContent = "";
    $("inspection-dialog").showModal();
    select(selected);
  };
}

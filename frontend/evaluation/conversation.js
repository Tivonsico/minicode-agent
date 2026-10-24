import { request } from "../api.js";
import { element, renderMessages } from "../messages.js";
import { disclosure } from "../inspection/shared.js";

function recap(task, row, events, files, official) {
  const tools = new Map();
  for (const event of events)
    if (event.type === "tool_start") tools.set(event.name, (tools.get(event.name) || 0) + 1);
  const actions = [...tools].map(([name, count]) => `${name} ${count} 次`).join("、");
  const checks = row?.checks || [];
  const passed = official ? row?.resolved === true : row?.passed === true;
  const result = !row ? (events.length ? "已展示来源会话，当前没有这条规则的独立验收成绩。" : "尚未运行，暂无模型回答、工具执行和验收结果。") :
    official ? `官方验收${passed ? "通过" : "未通过或未完成"}，记录状态为 ${row.status}。补丁大小 ${row.patch_bytes ?? "未知"} 字节。${row.agent_error || ""}` :
    `独立验收${passed ? "通过" : "未通过"}，${checks.filter(check => check.passed).length}/${checks.length} 项条件通过。${row.failure_type || row.error || ""}`;
  return [
    `实验目的：${official ? `修复 ${task.instance_id} 对应的真实仓库问题，并由官方测试验收。` : `完成“${task.name || row?.case_name || "会话规则检查"}”，按预设条件核对结果。`}`,
    `原始素材：${official ? `仓库 ${task.repo || row?.repo || "未知"}，初始提交 ${task.base_commit || row?.base_commit || "未知"}，以及公开 Issue 描述。` : task.mode === "history" ? `已有会话 ${row?.session_id || task.source?.session_id || "尚未选择"} 及预先设定的会话检查规则，仅检查保存范围内的记录。` : `${(task.prompts || []).length} 轮任务需求，初始文件 ${(Object.keys(files || task.files || {}).join("、") || "未保存或无文件")}；验收条件与模型需求分开保存。`}`,
    `Agent 的处理：${row || events.length ? (actions ? `原始轨迹记录了 ${actions}。具体问答和工具返回见上方。` : "当前保存的轨迹没有工具执行记录；请结合原始回复和执行状态判断，不能据此补推未记录的操作。") : "此处仅展示待执行需求，没有生成或补造 Agent 回复。"}`,
    `最终结果：${result}${official ? " 模型自述完成不能替代官方测试判定。" : ""}`,
  ].join("\n\n");
}

export function renderRunConversation(host, { task = {}, result = null, events = [], initial = {}, final = null, official = false }) {
  host.replaceChildren();
  const thread = element("div", undefined, "experiment-thread");
  thread.setAttribute("aria-label", "任务问答与工具记录");
  if (events.length) renderMessages(thread, events);
  else {
    const prompts = official ? [task.problem_statement] : task.prompts?.length ? task.prompts :
      task.mode === "history" ? [`会话检查规则：${task.name}\n要求回复包含：${(task.output_contains || []).join("、") || "未指定"}\n要求调用：${(task.required_tools || []).join("、") || "未指定"}`] : [];
    renderMessages(thread, prompts.filter(Boolean).map((content, index) =>
      ({ type: "message", role: "user", message_id: `requirement-${index}`, content })));
    thread.append(element("p", result ? "本次运行没有保存可展示的问答轨迹。" : "尚未运行，以上为任务需求。", "subtle"));
  }
  if (Object.keys(initial).length) thread.append(disclosure("初始文件", initial));
  if (final) thread.append(disclosure("最终文件", final));
  const summary = element("article", undefined, "message experiment-summary");
  summary.append(element("div", "中文总结", "message-label"),
    element("div", recap(task, result, events, initial, official), "message-body"));
  thread.append(summary);
  const jump = element("button", "查看末尾中文总结", "text-button");
  jump.onclick = () => summary.scrollIntoView({ block: "nearest" });
  host.append(jump, thread);
}

export function attachRunConversation(details, load, options, safely) {
  const host = element("section");
  host.setAttribute("aria-label", "本次运行对话");
  details.append(host);
  let loaded = false, pending = false;
  details.addEventListener("toggle", () => {
    if (!details.open || loaded || pending) return;
    pending = true;
    safely(async () => {
      try {
        host.textContent = "读取本次运行的原始问答…";
        const artifact = await load();
        renderRunConversation(host, { ...options, events: artifact.events || [], initial: artifact.initial || {}, final: artifact.final });
        loaded = true;
      } finally { pending = false; }
    });
  });
}

export function localTaskConversations(safely) {
  let index = null, version = 0;
  const panel = document.getElementById("local-conversation");
  async function runs() {
    if (!index) index = Promise.all([request("/evaluations/jobs"), request("/evaluations/reports")])
      .then(async ([jobs, reports]) => {
        const benchmark = await Promise.all(jobs.map(job => request(`/evaluations/jobs/${job.id}`)));
        const history = await Promise.all(reports.filter(report => report.metrics.rule_pass_rate !== undefined)
          .map(report => request(`/evaluations/reports/${report.id}`)));
        return [...benchmark, ...history].sort((a, b) => b.created_at.localeCompare(a.created_at))
          .flatMap(job => job.results.map((result, attempt) => ({ job, result, attempt })));
      });
    return index;
  }
  return {
    invalidate() { index = null; },
    hide() { ++version; panel.hidden = true; },
    show(task) {
      if (!task) { ++version; panel.hidden = true; return; }
      panel.hidden = false;
      const ticket = ++version;
      panel.replaceChildren(element("h3", task.name));
      const host = element("section");
      panel.append(host);
      host.textContent = "正在读取任务与历史运行…";
      safely(async () => {
        const entries = (await runs()).filter(entry => entry.result.case_id === task.id);
        if (ticket !== version) return;
        if (!entries.length) {
          renderRunConversation(host, { task, initial: task.files || {} });
          if (task.mode === "history" && task.source?.session_id) {
            const saved = await request(`/evaluations/source-conversations/${task.source.session_id}?covered_seq=${task.source.covered_seq}`);
            if (ticket === version) renderRunConversation(host, { task, events: saved.events });
          }
          return;
        }
        const select = element("select");
        select.setAttribute("aria-label", "本地任务运行记录");
        entries.forEach((entry, number) => {
          const option = element("option", `${new Date(entry.job.created_at).toLocaleString("zh-CN")} · ${entry.result.agent_mode || (entry.job.kind === "agent_benchmark" ? "早期运行（未保存模式）" : "会话检查")} · ${entry.result.passed ? "通过" : "未通过"} · 第${entry.result.repeat || 1}次`);
          option.value = number;
          select.append(option);
        });
        panel.insertBefore(select, host);
        let selection = 0;
        async function showRun() {
          const requestVersion = ++selection;
          const entry = entries[Number(select.value)];
          const source = entry.job.sources?.find(source => source.session_id === entry.result.session_id);
          const artifact = entry.result.artifact_ref ? await request(`/evaluations/jobs/${entry.job.id}/attempts/${entry.attempt}`) : source
            ? await request(`/evaluations/source-conversations/${entry.result.session_id}?covered_seq=${source.covered_seq}&turn_id=${encodeURIComponent(entry.result.turn_id)}`)
            : { events: [] };
          if (ticket !== version || requestVersion !== selection) return;
          const originalTask = entry.job.dataset?.cases?.find(item => item.id === task.id) || task;
          renderRunConversation(host, { task: originalTask, result: entry.result, events: artifact.events || [], initial: artifact.initial || {}, final: artifact.final });
        }
        select.onchange = () => safely(showRun);
        await showRun();
      });
    },
  };
}

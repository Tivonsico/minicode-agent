import { hydrateIcons } from "./icons.js";
import { request, post, events } from "./api.js";
import { element, renderMessages } from "./messages.js";
import { renderWorkspace, renderMemory } from "./workspace.js";
import { directoryPicker } from "./directories.js";
import { skillManager } from "./skills.js";
import { inspection } from "./inspection/app.js";
import { evaluations } from "./evaluation/app.js";
import { modeControls } from "./modes.js";
import { persistenceHelp, reconcileOwner } from "./persistence.js";
import { reasoningSelector } from "./reasoning.js";
let selectedReasoning = () => undefined;
const $ = (id) => document.getElementById(id);
hydrateIcons();
persistenceHelp();
let detail = null,
  controller = null,
  prompts = [],
  renderQueued = false,
  loading = 0;
function notify(text) {
  $("notice").textContent = text;
  $("notice").hidden = !text;
}
async function safely(action) {
  try {
    await action();
  } catch (error) {
    notify(error.message);
  }
}
async function refreshSessions() {
  const list = await request("/sessions");
  $("sessions").replaceChildren();
  for (const session of list) {
    const button = element(
      "button",
      undefined,
      session.session_id === detail?.session.session_id ? "active" : "",
    );
    button.append(
      element("span", session.title || "新会话"),
      element(
        "small",
        session.status === "error"
          ? "记录损坏"
          : new Date(session.last_active_at).toLocaleString("zh-CN", {
              month: "numeric",
              day: "numeric",
              hour: "2-digit",
              minute: "2-digit",
            }),
      ),
    );
    button.onclick = () => {
      if (window.matchMedia("(max-width: 720px)").matches)
        setDrawer("sidebar", false);
      evaluation.close();
      safely(() => load(session.session_id));
    };
    $("sessions").append(button);
    if (session.owner_held && (!session.owned_here || session.owner_needs_review)) {
      const recovery = element("button", "核实旧执行者", "text-button");
      recovery.onclick = () => reconcileOwner(session, refreshSessions);
      $("sessions").append(recovery);
    }
  }
  return list;
}
async function refreshMemory() {
  const entries = await request("/memory");
  if (detail)
    renderMemory(entries, detail.directories, (id) =>
      safely(async () => {
        await request(`/memory/${id}`, { method: "DELETE" });
        await refreshMemory();
      }),
    );
}
const actions = {
  directory: (action, path) =>
    safely(async () => {
      const sid = detail.session.session_id;
      await post(`/sessions/${sid}/directories`, { action, path });
      await load(sid);
    }),
  approve: (ticket, approved) =>
    safely(async () => {
      $("approve").disabled = $("reject").disabled = true;
      try {
        await post(
          `/sessions/${detail.session.session_id}/approvals/${ticket.id}`,
          { run_id: ticket.run_id, approved },
        );
      } finally {
        $("approve").disabled = $("reject").disabled = false;
      }
    }),
};
function render() {
  if (!detail) return;
  $("session-title").textContent = detail.session.title;
  $("chat-view").dataset.empty = String(
    !detail.running &&
      !detail.events.some(
        (event) => (event.type === "message" && event.role === "user") || event.type === "run_accepted",
      ),
  );
  $("conversation-title").textContent = detail.session.title;
  $("conversation-title").title = detail.session.title;
  renderMessages($("messages"), detail.events);
  renderWorkspace(detail, actions);
  modes.render(detail);
}
function queueRender() {
  if (!renderQueued) {
    renderQueued = true;
    requestAnimationFrame(() => {
      renderQueued = false;
      render();
    });
  }
}
function onEvent(event) {
  if (
    !detail ||
    event.session_id !== detail.session.session_id ||
    detail.events.some((item) => item.seq === event.seq)
  )
    return;
  detail.events.push(event);
  if (event.type === "checkpoint" || event.type === "task_state") Object.assign(detail.state, event.state);
  if (event.type === "turn_start") detail.running = true;
  if (event.type === "turn_end") {
    detail.running = false;
    detail.state.status = event.status;
    safely(refreshSessions);
  }
  if (
    event.type === "message" &&
    event.role === "user" &&
    detail.session.title === "新会话"
  ) {
    detail.session.title = event.content.slice(0, 60);
    safely(refreshSessions);
  }
  if (event.type === "approval_requested") detail.approvals.push(event);
  if (event.type === "approval_closed")
    detail.approvals = detail.approvals.filter((item) => item.id !== event.id);
  if (event.type === "notice") notify(event.text);
  if (event.type === "memory_complete") safely(refreshMemory);
  queueRender();
}
async function observe(sid, control) {
  while (!control.signal.aborted) {
    try {
      await events(
        sid,
        detail.events.at(-1)?.seq || 0,
        control.signal,
        onEvent,
      );
    } catch (error) {
      if (control.signal.aborted) return;
      notify("连接暂时中断，正在重连；重连不会重新提交任务。");
    }
    if (!control.signal.aborted)
      await new Promise((resolve) => setTimeout(resolve, 1500));
  }
}
async function load(sid) {
  const generation = ++loading;
  controller?.abort();
  const next = await request(`/sessions/${sid}`);
  if (generation !== loading) return;
  detail = next;
  controller = new AbortController();
  notify("");
  render();
  $("messages").scrollTop = $("messages").scrollHeight;
  observe(sid, controller);
  await Promise.all([refreshSessions(), refreshMemory()]);
}
$("new-session").onclick = () =>
  safely(async () => {
    evaluation.close();
    const result = await post("/sessions");
    await load(result.session.session_id);
    $("input").focus();
  });
$("composer").onsubmit = (event) => {
  event.preventDefault();
  safely(async () => {
    const text = $("input").value.trim();
    if (!text || !detail || detail.running) return;
    const sid = detail.session.session_id;
    $("send").disabled = true;
    try {
      const result = await post(`/sessions/${sid}/runs`, {
        text,
        reasoning_effort: selectedReasoning(),
        agent_mode: modes.selected(),
      });
      if (detail.session.session_id === sid) {
        detail.run_id = result.run_id;
        $("chat-view").dataset.empty = "false";
        $("input").value = "";
        notify("");
        await load(sid);
      }
    } finally {
      if (detail?.session.session_id === sid)
        $("send").disabled = detail.running;
    }
  });
};
$("input").onkeydown = (event) => {
  if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
    event.preventDefault();
    if (!detail?.running) $("composer").requestSubmit();
  }
};
$("cancel").onclick = () =>
  safely(async () => {
    const sid = detail.session.session_id;
    await post(`/sessions/${sid}/cancel`);
    await load(sid);
  });
const drawerState = {
  sidebar: !window.matchMedia("(max-width: 720px)").matches,
  inspector: false,
};
function setDrawer(side, open) {
  drawerState[side] = open;
  if (side === "sidebar") {
    document.body.classList.toggle("sidebar-open", open);
    document.body.classList.toggle("sidebar-collapsed", !open);
    $("sidebar-toggle").setAttribute("aria-expanded", String(open));
    $("sidebar-close").setAttribute("aria-expanded", String(open));
  } else {
    $("inspector").hidden = !open;
    document.body.classList.toggle("inspector-open", open);
    $("inspect-toggle").setAttribute("aria-expanded", String(open));
    $("inspect-close").setAttribute("aria-expanded", String(open));
  }
}
for (const [side, ids] of Object.entries({
  sidebar: ["sidebar-toggle", "sidebar-close"],
  inspector: ["inspect-toggle", "inspect-close"],
})) {
  for (const id of ids)
    $(id).onclick = () => setDrawer(side, !drawerState[side]);
  setDrawer(side, drawerState[side]);
}
window.matchMedia("(max-width: 720px)").addEventListener("change", (event) => {
  setDrawer("sidebar", !event.matches);
  if (event.matches) setDrawer("inspector", false);
});
$("refresh-memory").onclick = () => safely(refreshMemory);
for (const button of document.querySelectorAll("[data-close]"))
  button.onclick = () => button.closest("dialog").close();
const chooseDirectory = directoryPicker(() => detail, load);
$("add-directory").onclick = chooseDirectory;
$("composer-directory").onclick = chooseDirectory;
$("edit-prompts").onclick = () =>
  safely(async () => {
    prompts = await request(`/sessions/${detail.session.session_id}/prompts`);
    $("prompt-select").replaceChildren();
    for (const [index, prompt] of prompts.entries()) {
      const option = element(
        "option",
        prompt.scope === "global" ? "全局提示词" : prompt.project_path,
      );
      option.value = index;
      $("prompt-select").append(option);
    }
    $("prompt-content").value = prompts[0].content;
    $("prompt-error").textContent = "";
    $("prompt-dialog").showModal();
  });
$("prompt-select").onchange = () => {
  $("prompt-content").value = prompts[Number($("prompt-select").value)].content;
};
$("save-prompt").onclick = async () => {
  try {
    const item = prompts[Number($("prompt-select").value)];
    await request(`/sessions/${detail.session.session_id}/prompts`, {
      method: "PUT",
      body: JSON.stringify({
        scope: item.scope,
        project_path: item.project_path,
        content: $("prompt-content").value,
      }),
    });
    $("prompt-dialog").close();
  } catch (error) {
    $("prompt-error").textContent = error.message;
  }
};
const modes = modeControls(() => detail, load, safely);
const openInspection = inspection(
  () => detail,
  (sid) => safely(() => load(sid)),
);
$("inspection-open").onclick = openInspection;
const evaluation = evaluations(
  () => detail,
  () => evaluation.close(),
);
$("evaluation-open").onclick = () => {
  setDrawer("inspector", false);
  if (window.matchMedia("(max-width: 720px)").matches)
    setDrawer("sidebar", false);
  evaluation.open();
};
const openSkills = skillManager(() => detail, notify);
$("select-skill").onclick = () => safely(openSkills);
safely(async () => {
  const info = await request("/info");
  $("model").textContent = info.model;
  selectedReasoning = reasoningSelector(info.reasoning, info.model);
  $("sandbox").textContent =
    `${info.sandbox_available ? "●" : "○"} ${info.sandbox_reason}`;
  const list = await refreshSessions();
  const existing = list.find((item) => item.status !== "error");
  const sid =
    existing?.session_id || (await post("/sessions")).session.session_id;
  await load(sid);
});

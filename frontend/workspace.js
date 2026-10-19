import { element } from "./messages.js";
const $ = (id) => document.getElementById(id);
export function renderWorkspace(detail, actions) {
  const state = detail.state || {};
  const running = detail.running;
  $("run-status").textContent = detail.approvals?.length
    ? "等待审批"
    : running
      ? "执行中"
      : {
          completed: "已完成",
          interrupted: "已停止",
          failed: "失败",
          paused: "已暂停",
        }[state.status] || "就绪";
  $("send").disabled = running;
  $("send").hidden = running;
  $("reasoning-effort").disabled = running;
  $("cancel").hidden = !running;
  $("add-directory").disabled = running;
  $("edit-prompts").disabled = running;
  $("select-skill").disabled = running;
  $("input-hint").textContent = running
    ? "执行中，可编辑下一轮草稿"
    : "Enter 发送，Shift + Enter 换行";
  const activeProject = detail.directories?.find((item) => item.active)?.path;
  $("project-label").textContent = activeProject
    ? activeProject.split(/[\\/]/).filter(Boolean).at(-1)
    : "选择工作目录";
  $("composer-directory").title = activeProject || "选择工作目录";
  $("composer-directory").disabled = running;
  const directories = $("directories");
  directories.replaceChildren();
  for (const item of detail.directories || []) {
    const box = element("div", undefined, "directory");
    box.append(
      element("span", item.path, "path"),
      element(
        "small",
        item.valid ? (item.active ? "当前工作目录" : "已授权") : item.reason,
      ),
    );
    const controls = element("div", undefined, "actions");
    for (const [label, action] of [
      ["设为当前", "select"],
      ["撤销", "remove"],
    ]) {
      const button = element("button", label, "text-button");
      button.disabled =
        running || (action === "select" && (!item.valid || item.active));
      button.onclick = () => actions.directory(action, item.path);
      controls.append(button);
    }
    box.append(controls);
    directories.append(box);
  }
  if (!detail.directories?.length) directories.textContent = "还没有授权目录";
  const task = $("task-state");
  task.replaceChildren();
  task.append(
    element("div", state.objective || "等待新的任务", "task-objective"),
    element("div", `本轮模型请求 ${state.request_count || 0} 次`, "task-meta"),
  );
  const estimate = state.context_estimate || 0,
    budget = detail.context_budget || 1;
  $("context-size").textContent =
    `${estimate.toLocaleString()} / ${budget.toLocaleString()}`;
  $("context-progress").value = Math.min(100, (estimate / budget) * 100);
  const approval = detail.approvals?.[0];
  $("approval-panel").hidden = !approval;
  if (approval) {
    $("approval-name").textContent = approval.name;
    $("approval-description").textContent = approval.description;
    $("approval-details").textContent = approval.details || "无额外详情";
    $("approve").onclick = () => actions.approve(approval, true);
    $("reject").onclick = () => actions.approve(approval, false);
  }
}
export function renderMemory(entries, directories, remove) {
  const active = directories.find((item) => item.active)?.path || "unscoped";
  const relevant = entries.filter(
    (item) => item.scope === active || (!item.scope && active === "unscoped"),
  );
  const container = $("memory");
  container.replaceChildren();
  for (const item of relevant) {
    const box = element("div", undefined, "memory-item");
    box.append(
      element("div", item.content),
      element(
        "small",
        `来源：${item.source_session.slice(0, 8)} / ${item.source_message.slice(0, 8)} · v${item.version}`,
      ),
    );
    const button = element("button", "删除这条记忆");
    button.onclick = () => remove(item.id);
    box.append(button);
    container.append(box);
  }
  if (!relevant.length) container.textContent = "当前项目暂无可复用信息";
}

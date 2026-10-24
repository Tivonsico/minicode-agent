import { request, post } from "./api.js";
import { element, changeCounts } from "./messages.js";

function fileList(data) {
  const list = element("div", undefined, "snapshot-files");
  const labels = { added: "新增", modified: "修改", deleted: "删除", unsupported: "未覆盖" };
  for (const file of data.files) {
    const row = element("details", undefined, "snapshot-file");
    const summary = element("summary");
    summary.append(element("span", file.path, "snapshot-path"), element("small", labels[file.status]));
    if (file.added !== null) summary.append(changeCounts(file.added, file.deleted));
    else summary.append(element("small", file.status === "unsupported" ? "不支持" : "二进制"));
    row.append(summary);
    if (file.reason) row.append(element("p", file.reason, "snapshot-note"));
    if (file.mode_changed) row.append(element("p", "可执行权限发生变化", "snapshot-note"));
    if (file.diff) {
      const patch = element("pre", undefined, "snapshot-patch");
      for (const line of file.diff.split("\n")) {
        patch.append(element("span", line + "\n", line.startsWith("+") && !line.startsWith("+++")
          ? "diff-add" : line.startsWith("-") && !line.startsWith("---") ? "diff-delete" : ""));
      }
      row.append(patch);
    }
    if (file.truncated) row.append(element("p", "差异较长，仅显示前 200,000 个字符；行数根据完整内容计算。", "snapshot-note"));
    list.append(row);
  }
  if (!data.files.length) list.append(element("p", data.pending ? "任务结束后显示实际差异。" : "没有受支持的文件改动。", "snapshot-note"));
  return list;
}

export function snapshotHistory(getDetail, reload, notify) {
  const dialog = element("dialog", undefined, "snapshot-dialog");
  dialog.setAttribute("aria-label", "文件改动与历史");
  const heading = element("div", undefined, "snapshot-heading");
  const title = element("h2", "文件改动与历史");
  const close = element("button", "关闭");
  close.onclick = () => dialog.close();
  heading.append(title, close);
  const coverage = element("p", undefined, "snapshot-note");
  const select = element("select", undefined, "snapshot-select");
  select.setAttribute("aria-label", "选择任务快照");
  const content = element("div");
  const error = element("p", undefined, "error");
  error.setAttribute("role", "status");
  dialog.append(heading, coverage, select, content, error);
  document.body.append(dialog);
  let records = [], sid = "", generation = 0, mutating = false;
  dialog.addEventListener("cancel", event => { if (mutating) event.preventDefault(); });
  const statuses = { running: "处理中 / 未保存结束状态", unknown: "待核实", completed: "已完成", interrupted: "已停止", failed: "失败", paused: "已暂停", restored: "恢复前副本", restore_failed: "恢复失败前副本" };
  const endpoint = r => `/sessions/${sid}/file-snapshots/${r.project_id}/${r.id}`;
  const safe = action => async () => {
    error.textContent = "";
    try { await action(); } catch (e) { error.textContent = e.message; }
  };
  async function show(record) {
    const current = ++generation;
    content.replaceChildren(element("p", "正在读取实际文件差异…", "snapshot-note"));
    const data = await request(endpoint(record));
    if (current !== generation || !dialog.open) return;
    const summary = element("div", undefined, "snapshot-summary");
    summary.append(element("strong", `${data.files.filter(f => f.status !== "unsupported").length} 个文件`), changeCounts(data.added, data.deleted));
    const project = element("p", record.project, "snapshot-note snapshot-project");
    const controls = element("div", undefined, "snapshot-actions");
    const pin = element("button", record.pinned ? "取消固定" : "固定保留");
    pin.disabled = getDetail()?.running || !record.after || record.status === "unknown";
    pin.onclick = safe(async () => {
      await post(endpoint(record) + "/pin", { pinned: !record.pinned });
      await open(record);
    });
    const restore = element("button", "预览恢复到此轮开始前");
    restore.disabled = getDetail()?.running || !record.after || record.status === "unknown";
    restore.onclick = safe(async () => preview(record));
    controls.append(pin, restore);
    content.replaceChildren(project, summary, fileList(data), controls);
    if (record.pinned) content.append(element("p", "此副本已固定，不受最近 20 轮清理影响。", "snapshot-note"));
  }
  async function preview(record) {
    const plan = await post(endpoint(record) + "/preview");
    const heading = element("h3", "确认恢复影响");
    const description = element("p", "以下差异是当前文件 → 此轮开始前。恢复前会自动保留一份固定副本。", "snapshot-note");
    const warning = element("p", plan.warning, "snapshot-note");
    const checkbox = element("input");
    checkbox.type = "checkbox";
    const acknowledgement = element("label", undefined, "snapshot-confirm");
    acknowledgement.append(checkbox, element("span", `我已核对，允许覆盖这些后续或人工修改：${plan.conflicts.join("、")}`));
    acknowledgement.hidden = !plan.conflicts.length;
    const confirm = element("button", "确认恢复文件", "primary");
    const blocked = plan.blocked.length > 0;
    confirm.disabled = blocked || plan.conflicts.length > 0;
    checkbox.onchange = () => { confirm.disabled = blocked || !checkbox.checked; };
    const back = element("button", "返回差异");
    back.onclick = safe(() => show(record));
    const actions = element("div", undefined, "snapshot-actions");
    actions.append(back, confirm);
    confirm.onclick = safe(async () => {
      confirm.disabled = back.disabled = select.disabled = close.disabled = true;
      mutating = true;
      try {
        await post(endpoint(record) + "/restore", { token: plan.token, accept_conflicts: checkbox.checked });
        dialog.close();
        await reload(sid);
        notify("已恢复项目文件；恢复前副本已固定保留。数据库和外部操作未恢复。");
      } finally {
        mutating = false;
        select.disabled = close.disabled = false;
        back.disabled = false;
        // A failed or stale plan must be previewed again before retrying.
        confirm.disabled = true;
      }
    });
    content.replaceChildren(heading, description, changeCounts(plan.added, plan.deleted), fileList(plan), warning);
    if (blocked) content.append(element("p", `请先处理文件与目录类型冲突：${plan.blocked.join("、")}`, "error"));
    content.append(acknowledgement, actions);
  }
  async function open(selected) {
    const detail = getDetail();
    if (!detail) return;
    sid = detail.session.session_id;
    error.textContent = "";
    content.replaceChildren(element("p", "正在读取文件历史…", "snapshot-note"));
    if (!dialog.open) dialog.showModal();
    const data = await request(`/sessions/${sid}/file-snapshots`);
    records = data.snapshots;
    coverage.textContent = `默认保留最近 ${data.keep} 轮；固定副本另行保留。${data.coverage}`;
    select.replaceChildren();
    for (const [index, record] of records.entries()) {
      const option = element("option", `${new Date(record.created_at).toLocaleString("zh-CN")} · ${record.project.split(/[\\/]/).at(-1)} · ${statuses[record.status] || record.status}${record.pinned ? " · 已固定" : ""}`);
      option.value = index;
      select.append(option);
    }
    select.hidden = !records.length;
    if (!records.length) {
      content.replaceChildren(element("p", "还没有文件快照。授权项目目录后，每轮任务会自动保存开始和结束状态。", "snapshot-note"));
      return;
    }
    const index = selected ? records.findIndex(r => r.id === selected.id) : 0;
    select.value = Math.max(index, 0);
    if (selected && index < 0) error.textContent = "该轮快照已清理或项目授权已移除，当前显示最近保留记录。";
    await show(records[Number(select.value)]);
  }
  select.onchange = safe(() => show(records[Number(select.value)]));
  return selected => safe(() => open(selected))();
}

import { modeName } from "../modes.js";
import { post } from "../api.js";
import { element } from "../messages.js";
import { $, disclosure, empty, time } from "./shared.js";

export function checkpointViewer(getSession, refresh, handleError) {
  $("checkpoint-sync").onclick = () =>
    handleError(async () => {
      $("checkpoint-sync").disabled = true;
      try {
        await post(`/sessions/${getSession()}/checkpoints/sync`);
        await refresh();
        $("inspection-status").textContent = "已保存当前任务状态。";
      } finally {
        $("checkpoint-sync").disabled = false;
      }
    });
  return (data) => {
    $("checkpoint-recovery").textContent = data.recovery?.source === "checkpoint"
      ? `本次从检查点 #${data.recovery.covered_seq} 恢复，启动读取后续 ${data.recovery.loaded_records} 条事件。完整旧轨迹仍可在日志中查看。`
      : "本次从完整日志恢复；保存检查点后，后续重启可跳过已覆盖的前缀。";
    const unresolved = $("unresolved-operations");
    unresolved.replaceChildren();
    for (const operation of data.unresolved_operations || []) {
      const form = element("form", undefined, "reconciliation-form");
      const title = element("strong", `${operation.name || "工具"} · UNKNOWN · 日志 #${operation.seq}`);
      const note = element("p", "请先核查实际文件、输出或外部系统。保存结论只解除阻塞，不重跑工具。", "subtle");
      const choice = element("select");
      choice.setAttribute("aria-label", "实际核实结论");
      for (const [value, text] of [["", "选择已核实的结果"], ["succeeded", "已完成"], ["failed", "已失败，效果已核实"], ["not_applied", "确认未执行"]]) {
        const option = element("option", text); option.value = value; choice.append(option);
      }
      choice.required = true;
      const evidence = element("textarea");
      evidence.required = true; evidence.maxLength = 10000; evidence.rows = 3;
      evidence.setAttribute("aria-label", "实际核实证据");
      evidence.placeholder = "说明核查了哪些文件、结果或服务记录；不要根据错误码猜测。";
      const confirmed = element("input"); confirmed.type = "checkbox"; confirmed.required = true;
      const label = element("label"); label.append(confirmed, document.createTextNode("已确认旧执行者及相关子进程停止"));
      const save = element("button", "保存核实结论"); save.type = "submit"; save.disabled = data.running;
      form.append(title, note, choice, evidence, label, save);
      form.onsubmit = (event) => { event.preventDefault(); handleError(async () => {
        save.disabled = true;
        try {
          await post(`/sessions/${getSession()}/operations/reconcile`, {
            result_seq: operation.seq, resolution: choice.value, evidence: evidence.value,
            processes_stopped: confirmed.checked,
          });
          await refresh();
          $("inspection-status").textContent = "核实结论已保存；可返回会话明确提交下一步。";
        } finally { save.disabled = false; }
      }); };
      unresolved.append(form);
    }
    const container = $("checkpoint-records");
    container.replaceChildren();
    for (const record of [...data.checkpoints].reverse()) {
      container.append(
        disclosure(
          `${time(record.time)} · ${record.source === "automatic" ? "自动" : "手动"} · ${modeName(record.agent_mode)} · ${record.state.status} · 日志 #${record.covered_seq}`,
          record,
        ),
      );
    }
    if (!data.checkpoints.length)
      empty(container, "暂无检查点。点击同步即可保存当前状态。");
  };
}

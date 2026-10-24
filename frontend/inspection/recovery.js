import { post } from "../api.js";
import { element } from "../messages.js";
import { $ } from "./shared.js";

export function recoveryViewer(getSession, refresh, handleError) {
  return (data) => {
    $("journal-recovery").textContent = data.recovery?.source === "journal_snapshot"
      ? `本次从日志快照 #${data.recovery.covered_seq} 加载，回放后续 ${data.recovery.loaded_records} 条事件。`
      : "本次从完整事件日志恢复。";
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
  };
}

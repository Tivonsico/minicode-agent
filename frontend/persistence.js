import { element } from "./messages.js";
import { request, post } from "./api.js";

const $ = (id) => document.getElementById(id);

export function persistenceHelp(onSaved = () => {}) {
  const dialog = $("persistence-dialog");
  const close = $("persistence-close");
  const actions = close.parentElement;
  function renderStatus(status) {
    $("concurrency-mode").textContent = `并发模式：${status.active_mode === "mysql" ? "高" : "低"}`;
  }
  async function open() {
    $("persistence-title").textContent = "并发模式";
    close.textContent = "不保存并退出";
    actions.replaceChildren(close);
    const description = $("persistence-description");
    description.replaceChildren(element("p", "正在读取实际存储状态…"));
    dialog.showModal();
    try {
      const status = await request("/storage");
      renderStatus(status);
      const active = status.active_mode === "mysql"
        ? `MySQL${status.connected ? "（已连接）" : "（当前不可用，已停止写入）"}`
        : "本地 JSONL + 压缩摘要";
      const current = element("p", `当前实际运行：${active}。${status.restart_required ? "已保存的新配置等待重启生效。" : ""}`);
      const low = element("p", "低并发：记录保存在本机文件中，适合单机少量会话。执行轨迹与检查点持续保存，重新打开时恢复已保存的进度。");
      const high = element("p", "高并发：记录保存在 MySQL，支持多个会话并行运行；同一工作区的冲突操作仍会协调。需要先配置可用的数据库。");
      const boundaries = element("p", "两种模式都保留历史和检查点。检查点不会回滚文件；结果未知的操作必须先核实，不会自动重跑。切换模式后各自保留原有会话，不自动迁移。");
      const form = element("form", undefined, "storage-form storage-settings-form");
      form.id = "storage-settings-form";
      const mysqlFields = element("div", undefined, "storage-mysql-fields");
      const fields = {};
      function field(key, title, type = "text") {
        const label = element("label", title);
        const input = element("input");
        input.type = type;
        input.name = key;
        input.value = key === "mysql_password" ? "" : status.configured[key];
        if (type === "password") {
          input.autocomplete = "new-password";
          input.placeholder = status.password_configured ? "已配置；留空保留" : "数据库密码";
        }
        label.append(input);
        fields[key] = input;
        mysqlFields.append(label);
      }
      const label = element("label", "并发模式（重启后生效）");
      const mode = element("select");
      for (const [value, title] of [["local", "低（本地存储）"], ["mysql", "高（MySQL）"]]) {
        const option = element("option", title);
        option.value = value;
        mode.append(option);
      }
      mode.value = status.configured.storage_mode;
      label.append(mode);
      form.append(label, current, low, high, boundaries, mysqlFields);
      field("mysql_host", "MySQL 主机");
      field("mysql_port", "端口", "number");
      field("mysql_database", "数据库名");
      field("mysql_user", "用户名");
      field("mysql_password", "密码", "password");
      field("mysql_ssl_ca", "TLS CA 文件路径（可选）");
      field("mysql_pool_size", "最大数据库连接数", "number");
      const result = element("p");
      result.setAttribute("role", "status");
      const check = element("button", "检查连接");
      check.type = "button";
      const save = element("button", "保存并退出");
      save.type = "submit";
      save.setAttribute("form", form.id);
      actions.replaceChildren(check, save, close);
      form.append(result);
      function renderMode() {
        const mysql = mode.value === "mysql";
        mysqlFields.hidden = !mysql;
        for (const input of Object.values(fields)) input.disabled = !mysql;
        check.disabled = !mysql;
        check.title = mysql ? "验证数据库连接及建表权限" : "低并发使用本地存储，无需检查数据库连接";
        result.textContent = mysql
          ? "检查连接会验证数据库及建表权限。密码仅保存在本机受保护配置中，不回显。切换模式后须重启 MiniCode。"
          : "低并发使用本地存储，无需配置数据库。切换模式后须重启 MiniCode。";
      }
      mode.onchange = renderMode;
      renderMode();
      async function submit(saving) {
        const mysql = mode.value === "mysql";
        check.disabled = save.disabled = true;
        mode.disabled = true;
        const values = { storage_mode: mode.value };
        for (const [key, input] of Object.entries(fields))
          values[key] = mysql
            ? (input.type === "number" ? Number(input.value) : input.value)
            : (key === "mysql_password" ? "" : status.configured[key]);
        try {
          const response = saving
            ? await request("/storage", { method: "PUT", body: JSON.stringify(values) })
            : await post("/storage/check", values);
          fields.mysql_password.value = "";
          result.textContent = saving
            ? (response.restart_required ? "配置已保存。请停止任务并重启 MiniCode 后使用新存储。当前存储仍为：" + active : "配置已保存，与当前运行配置一致。")
            : "连接与存储初始化检查通过。当前运行存储没有切换。";
          if (saving && response.environment_overrides.length)
            result.textContent += " 进程环境变量会覆盖保存配置，请同时更新：" + response.environment_overrides.join("、");
          if (saving) {
            onSaved(result.textContent);
            dialog.close();
          }
        } catch (error) {
          result.textContent = error.message;
        } finally {
          check.disabled = !mysql;
          save.disabled = mode.disabled = false;
        }
      }
      check.onclick = () => submit(false);
      form.onsubmit = (event) => { event.preventDefault(); submit(true); };
      description.replaceChildren(form);
    } catch (error) {
      description.replaceChildren(element("p", error.message));
    }
  }
  $("concurrency-mode").onclick = open;
  close.onclick = () => dialog.close();
  request("/storage").then(renderStatus).catch(() => {
    $("concurrency-mode").textContent = "并发模式：未确认";
  });
}

export function reconcileOwner(session, onDone) {
  const dialog = $("persistence-dialog");
  const close = $("persistence-close");
  close.textContent = "关闭";
  close.parentElement.replaceChildren(close);
  $("persistence-title").textContent = "核实旧执行者并解除会话占用";
  const form = element("form", undefined, "storage-form");
  form.append(element("p", "先检查旧 MiniCode 执行者、Shell 及全部子进程已停止。连接断开、父进程退出或等待超时都不足以证明停止。解除后仍需逐项核实 UNKNOWN，不会自动重跑。"));
  const evidenceLabel = element("label", "实际核查证据");
  const evidence = element("textarea");
  evidence.required = true;
  evidenceLabel.append(evidence);
  const stoppedLabel = element("label", undefined, "stopped-confirmation");
  const stopped = element("input");
  stopped.type = "checkbox";
  stopped.required = true;
  stoppedLabel.append(stopped, document.createTextNode("我已确认旧执行者及全部子进程停止"));
  const button = element("button", "保存证据并解除占用");
  const result = element("p");
  result.setAttribute("role", "status");
  form.append(evidenceLabel, stoppedLabel, result, button);
  form.onsubmit = async (event) => {
    event.preventDefault();
    button.disabled = true;
    try {
      await post(`/sessions/${session.session_id}/owner/reconcile`, {
        expected_owner: session.owner_revision, evidence: evidence.value,
        processes_stopped: stopped.checked,
      });
      dialog.close();
      await onDone();
    } catch (error) { result.textContent = error.message; }
    finally { button.disabled = false; }
  };
  $("persistence-description").replaceChildren(form);
  dialog.showModal();
}

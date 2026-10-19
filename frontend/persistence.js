import { element } from "./messages.js";
import { request, post } from "./api.js";

const $ = (id) => document.getElementById(id);

export function persistenceHelp() {
  const dialog = $("persistence-dialog");
  async function open(mysql) {
    $("persistence-title").textContent = "存储与恢复配置";
    const description = $("persistence-description");
    description.replaceChildren(element("p", "正在读取实际存储状态…"));
    dialog.showModal();
    try {
      const status = await request("/storage");
      const active = status.active_mode === "mysql"
        ? `MySQL${status.connected ? "（已连接）" : "（当前不可用，已停止写入）"}`
        : "本地 JSONL + checkpoint";
      const current = element("p", `当前实际运行：${active}。${status.restart_required ? "已保存的新配置等待重启生效。" : ""}`);
      const note = element("p", mysql
        ? "MySQL 用短事务保存消息、Context 摘要、状态与操作意图/结果；工具在事务外执行。按会话保持顺序，重叠工作区互斥，结果未知时持久阻塞并等待核实。"
        : "本地 JSONL 持续保存轨迹；checkpoint 校验后跳过已覆盖前缀。两种模式各自保留会话，不自动搬迁历史。checkpoint 不回滚文件，也不自动重跑工具。"
      );
      const form = element("form", undefined, "storage-form");
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
        form.append(label);
      }
      const label = element("label", "重启后使用的存储");
      const mode = element("select");
      for (const [value, title] of [["local", "低并发：本地"], ["mysql", "高并发：MySQL"]]) {
        const option = element("option", title);
        option.value = value;
        mode.append(option);
      }
      mode.value = status.configured.storage_mode;
      label.append(mode);
      form.append(label);
      field("mysql_host", "MySQL 主机");
      field("mysql_port", "端口", "number");
      field("mysql_database", "数据库名");
      field("mysql_user", "用户名");
      field("mysql_password", "密码", "password");
      field("mysql_ssl_ca", "TLS CA 文件路径（可选）");
      field("mysql_pool_size", "最大数据库连接数", "number");
      const result = element("p", "检查连接会验证数据库及建表权限；保存后须重启 MiniCode。密码仅保存在本机受保护配置中，不回显。");
      result.setAttribute("role", "status");
      const check = element("button", "检查连接");
      check.type = "button";
      const save = element("button", "验证并保存配置");
      save.type = "submit";
      const actions = element("div", undefined, "dialog-actions");
      actions.append(check, save);
      form.append(result, actions);
      async function submit(saving) {
        check.disabled = save.disabled = true;
        const values = { storage_mode: mode.value };
        for (const [key, input] of Object.entries(fields))
          values[key] = input.type === "number" ? Number(input.value) : input.value;
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
        } catch (error) {
          result.textContent = error.message;
        } finally {
          check.disabled = save.disabled = false;
        }
      }
      check.onclick = () => submit(false);
      form.onsubmit = (event) => { event.preventDefault(); submit(true); };
      description.replaceChildren(current, note, form);
    } catch (error) {
      description.replaceChildren(element("p", error.message));
    }
  }
  $("local-mode-help").onclick = () => open(false);
  $("mysql-mode-help").onclick = () => open(true);
  $("persistence-close").onclick = () => dialog.close();
  request("/storage").then((status) => {
    $("local-mode-help").textContent = "低并发模式：本地" + (status.active_mode === "local" ? " · 当前" : "");
    $("mysql-mode-help").textContent = "高并发模式：MySQL" + (status.active_mode === "mysql" ? (status.connected ? " · 当前" : " · 不可用") : "");
  }).catch(() => {});
}

export function reconcileOwner(session, onDone) {
  const dialog = $("persistence-dialog");
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

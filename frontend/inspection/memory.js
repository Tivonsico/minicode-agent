import { modeName } from "../modes.js";
import { disclosure } from "./shared.js";
import { request, post } from "../api.js";
import { element } from "../messages.js";
import { $, empty, time } from "./shared.js";

export function memoryEditor(getDetail, handleError) {
  async function refresh() {
    const entries = await request("/memory");
    const container = $("long-memory-records");
    container.replaceChildren();
    const active =
      getDetail().directories.find((d) => d.active)?.path || "unscoped";
    for (const entry of entries) {
      if (
        $("memory-scope-filter").value === "current" &&
        entry.scope !== active
      )
        continue;
      const item = element("form", undefined, "memory-editor");
      const label = element(
        "div",
        `${entry.scope || "历史未分区"} · v${entry.version}`,
        "section-title",
      );
      const text = element("textarea");
      text.value = entry.content;
      text.required = true;
      text.maxLength = 10000;
      text.setAttribute("aria-label", "记忆内容");
      const meta = element(
        "p",
        `${entry.source_kind === "user" || entry.source_kind === "user_edit" ? "用户维护" : "来自会话"} · ${modeName(entry.agent_mode)} · ${time(entry.source_time)}`,
        "subtle",
      );
      const actions = element("div", undefined, "dialog-actions");
      const save = element("button", "保存");
      const remove = element("button", "删除", "text-button");
      remove.type = "button";
      remove.onclick = () =>
        handleError(async () => {
          await request(`/memory/${entry.id}`, { method: "DELETE" });
          await refresh();
        });
      actions.append(remove, save);
      item.append(
        label,
        text,
        meta,
        disclosure("查看来源与模式版本", {
          source_run: entry.source_run,
          source_turn: entry.source_turn,
          agent_mode: entry.agent_mode,
          mode_policy_version: entry.mode_policy_version,
          mode_prompt_version: entry.mode_prompt_version,
        }),
        actions,
      );
      item.onsubmit = (event) => {
        event.preventDefault();
        handleError(async () => {
          await request(`/memory/${entry.id}`, {
            method: "PUT",
            body: JSON.stringify({
              content: text.value,
              version: entry.version,
            }),
          });
          await refresh();
        });
      };
      container.append(item);
    }
    if (!container.children.length)
      empty(container, "这个范围还没有长期记忆。可以添加明确的项目约定。");
    $("memory-current-scope").textContent =
      active === "unscoped" ? "当前无工作目录" : active;
  }
  $("memory-scope-filter").onchange = () => handleError(refresh);
  $("memory-add-form").onsubmit = (event) => {
    event.preventDefault();
    handleError(async () => {
      await post("/memory", {
        session_id: getDetail().session.session_id,
        content: $("memory-new-content").value,
      });
      $("memory-new-content").value = "";
      await refresh();
    });
  };
  return refresh;
}

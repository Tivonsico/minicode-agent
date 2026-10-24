import { request } from "./api.js";
import { element } from "./messages.js";
import { disclosure } from "./inspection/shared.js";

export const modeName = (mode) =>
  ({
    manual: "Manual",
    accept_edit: "Accept Edit",
    plan: "Plan",
    auto: "Auto",
  })[mode] || "历史未标记";
const $ = (id) => document.getElementById(id);
export function modeControls(getDetail, reload, safely) {
  let profiles = [],
    savingSelection = false;
  function showProfile() {
    const profile = profiles.find((p) => p.agent_mode === $("mode-view").value);
    const policy = $("mode-policy");
    policy.replaceChildren();
    const groups = {};
    for (const [tool, action] of Object.entries(profile.actions))
      (groups[action] ||= []).push(tool);
    for (const [action, tools] of Object.entries(groups)) {
      const row = element("div", undefined, "mode-policy-row");
      row.append(
        element(
          "strong",
          { allow: "自动允许", ask: "逐项确认", deny: "固定禁止" }[action],
        ),
        element("span", tools.join(" · ")),
      );
      policy.append(row);
    }
    $("mode-boundaries").replaceChildren(
      ...profile.hard_boundaries.map((text) => element("li", text)),
    );
    $("mode-fixed").textContent = profile.fixed_prompt;
    $("mode-shared").textContent = profile.shared_prompt;
    $("mode-note").value = profile.user_prompt;
    $("mode-versions").replaceChildren(
      disclosure("查看权限与指令版本", {
        agent_mode: profile.agent_mode,
        mode_policy_version: profile.mode_policy_version,
        mode_prompt_version: profile.mode_prompt_version,
      }),
    );
    if (getDetail()?.running && getDetail().agent_mode === profile.agent_mode)
      $("mode-versions").append(
        disclosure("本轮正在使用的固定版本与指令", getDetail().mode_profile),
      );
    $("mode-feedback").textContent = "";
  }
  $("mode-details").onclick = () =>
    safely(async () => {
      profiles = await request("/modes");
      $("mode-view").replaceChildren(
        ...profiles.map((p) => {
          const option = element("option", p.label);
          option.value = p.agent_mode;
          return option;
        }),
      );
      $("mode-view").value = getDetail().agent_mode;
      showProfile();
      render(getDetail());
      $("mode-dialog").showModal();
    });
  $("mode-view").onchange = async () => {
    savingSelection = true;
    $("mode-view").disabled = true;
    $("mode-save").disabled = true;
    try {
      const sid = getDetail().session.session_id;
      await request(`/sessions/${sid}/mode`, {
        method: "PUT",
        body: JSON.stringify({ agent_mode: $("mode-view").value }),
      });
      await reload(sid);
      showProfile();
    } catch (error) {
      $("mode-view").value = getDetail().agent_mode;
      showProfile();
      $("mode-feedback").textContent = error.message;
    } finally {
      savingSelection = false;
      $("mode-save").disabled = false;
      render(getDetail());
    }
  };
  $("mode-save").onclick = async () => {
    const button = $("mode-save");
    button.disabled = true;
    try {
      const mode = $("mode-view").value;
      const profile = profiles.find((p) => p.agent_mode === mode);
      await request(`/modes/${mode}/prompt`, {
        method: "PUT",
        body: JSON.stringify({
          content: $("mode-note").value,
          revision: profile.revision,
        }),
      });
      profiles = await request("/modes");
      showProfile();
      $("mode-feedback").textContent = "已保存，下轮任务生效。";
    } catch (error) {
      $("mode-feedback").textContent = error.message;
    } finally {
      button.disabled = false;
    }
  };
  function render(detail) {
    if (!detail) return;
    const profile = profiles.find((p) => p.agent_mode === detail.agent_mode);
    $("mode-details").textContent = profile?.label || ({
      manual: "Manual · 手动确认",
      accept_edit: "Accept Edit · 自动编辑",
      plan: "Plan · 只读规划",
      auto: "Auto · 沙箱自动执行",
    })[detail.agent_mode] || modeName(detail.agent_mode);
    $("mode-view").disabled = detail.running || savingSelection;
    $("mode-details").title = detail.running
      ? "查看本轮模式；修改补充指令在下轮生效"
      : "选择执行模式，查看权限与指令";
  }
  return { render, selected: () => getDetail()?.agent_mode || "manual" };
}

import { request } from "../api.js";
import { $, pretty } from "./shared.js";

export function stateEditor(getSession, refresh, handleError) {
  let revision = 0;
  $("state-form").onsubmit = (event) => {
    event.preventDefault();
    handleError(async () => {
      await request(`/sessions/${getSession()}/checkpoint-state`, {
        method: "PUT",
        body: JSON.stringify({
          expected_seq: revision,
          objective: $("state-objective").value,
          notes: $("state-notes").value,
          next_step: $("state-next").value,
          status: $("state-status").value,
        }),
      });
      await refresh();
      $("inspection-status").textContent = "检查点状态已更新，并保存检查点。";
    });
  };
  return (data) => {
    revision = data.state_revision;
    $("state-objective").value = data.state.objective;
    $("state-notes").value = data.state.notes || "";
    $("state-next").value = data.state.next_step || "";
    $("state-status").value = data.state.status;
    for (const option of $("state-status").options)
      option.disabled = data.running
        ? option.value !== "running"
        : option.value === "running";
    $("state-evidence").textContent = pretty({
      agent_mode: data.state.agent_mode,
      mode_policy_version: data.state.mode_policy_version,
      mode_prompt_version: data.state.mode_prompt_version,
      phase: data.state.phase,
      request_count: data.state.request_count,
      context_estimate: data.state.context_estimate,
      pending_tool: data.state.pending_tool,
      verification_status: data.state.verification_status || "not_run",
      revision,
    });
  };
}

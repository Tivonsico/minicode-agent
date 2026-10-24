import { request, post } from "../api.js";
import { element } from "../messages.js";
import { $, disclosure, download, time } from "../inspection/shared.js";
import { attachRunConversation, renderRunConversation } from "./conversation.js";

const labels = {
  preparing: "准备仓库",
  executing: "Agent 修复中",
  grading: "Docker 验收中",
  resolved: "官方验收通过",
  unresolved: "官方验收未通过",
  empty_patch: "未生成补丁",
  environment_error: "环境错误",
  agent_error: "执行错误",
  cancelled: "已停止",
  interrupted: "运行中断",
  running: "运行中",
  completed: "已结束",
  failed: "运行失败",
};

export function sweBench(safely) {
  let tasks = [],
    jobs = [],
    selectedTask = null,
    taskVersion = 0,
    chosen = new Set(),
    currentDataset = "",
    active = null,
    timer = null,
    visible = false,
    generation = 0,
    ready = false;
  const endpoint = "/swe-bench";
  const filtered = () =>
    tasks.filter(
      (t) => !$("case-filter").value || t.repo === $("case-filter").value,
    );

  async function showTask(task) {
    if (!task) { $("swe-task-detail").replaceChildren(); return; }
    selectedTask = task;
    const version = ++taskVersion;
    const detail = $("swe-task-detail");
    detail.replaceChildren(element("h3", task.instance_id));
    const host = element("section");
    detail.append(host);
    const attempts = jobs.flatMap(job => job.results.map((result, index) => ({ job, result, index })))
      .filter(entry => entry.result.instance_id === task.instance_id);
    if (!attempts.length) {
      renderRunConversation(host, { task, official: true });
      return;
    }
    const select = element("select");
    select.setAttribute("aria-label", "官方任务运行记录");
    attempts.forEach((entry, number) => {
      const option = element("option", `${time(entry.job.created_at)} · ${labels[entry.result.status] || entry.result.status}${entry.job.execution_reused ? " · 仅重新验收" : ""}`);
      option.value = number;
      select.append(option);
    });
    detail.insertBefore(select, host);
    let selection = 0;
    async function showRun() {
      const ticket = ++selection;
      const entry = attempts[Number(select.value)];
      host.textContent = "读取本次运行的原始问答…";
      const trace = await request(`${endpoint}/jobs/${entry.job.id}/attempts/${entry.result.artifact_index ?? entry.index}/trace`);
      if (version !== taskVersion || ticket !== selection || !visible) return;
      renderRunConversation(host, { task, result: entry.result, events: trace.events || [], official: true });
    }
    select.onchange = () => safely(showRun);
    await showRun();
  }

  function counts() {
    $("swe-count").textContent = `已选 ${chosen.size} 项（每次最多 10 项）`;
    $("swe-run").disabled =
      !ready || !chosen.size || chosen.size > 10 || !!active;
  }

  function renderTasks() {
    const list = $("swe-cases");
    list.replaceChildren();
    for (const task of filtered()) {
      const row = element("div", undefined, "evaluation-case-row");
      const check = element("input");
      check.type = "checkbox";
      check.checked = chosen.has(task.instance_id);
      check.setAttribute("aria-label", `选择 ${task.instance_id}`);
      check.onchange = () => {
        check.checked
          ? chosen.add(task.instance_id)
          : chosen.delete(task.instance_id);
        counts();
      };
      const button = element("button");
      button.append(
        element("span", task.instance_id),
        element("small", task.repo),
      );
      button.onclick = () => safely(() => showTask(task));
      row.append(check, button);
      list.append(row);
    }
    if (!list.children.length)
      list.append(
        element("p", "同步官方任务后，选择少量 Issue 开始评测。", "subtle"),
      );
    counts();
  }

  function renderReport(job) {
    const report = $("swe-report");
    report.replaceChildren();
    const m = job.metrics;
    report.append(
      element(
        "h3",
        `${labels[job.status] || job.status} · ${job.official_dataset} 子集`,
      ),
      element(
        "p",
        `官方判定 ${m.judged}/${m.planned} 项，修复通过 ${m.resolved} 项，环境错误 ${m.environment_errors} 项。`,
        "subtle",
      ),
      element(
        "p",
        m.resolved_rate == null
          ? "尚无官方判定，暂无成功率。"
          : `已判定任务通过率 ${(m.resolved_rate * 100).toFixed(1)}%；验收覆盖率 ${(m.coverage * 100).toFixed(1)}%。此结果仅代表本次子集。`,
      ),
      element(
        "p",
        `模型：${job.environment.model} · 模式：${job.options.agent_mode} · 思考：${job.options.reasoning_effort || "默认"}`,
        "subtle",
      ),
    );
    if (job.execution_reused)
      report.append(
        element(
          "p",
          "本次复用已保存的模型补丁，仅重新运行 Docker 验收，未再次调用模型。",
          "subtle",
        ),
      );
    const selfTest =
      job.current?.self_test_environment ||
      job.results.find((row) => row.self_test_environment)
        ?.self_test_environment;
    if (selfTest)
      report.append(
        element(
          "p",
          `当前自测环境：Python ${selfTest.python} · ${selfTest.profile || "sympy-py39"} · pytest ${selfTest.packages.pytest} · 沙箱预检通过。各题完整环境见执行与验收记录。`,
          "subtle",
        ),
      );
    if (job.current)
      report.append(
        element(
          "p",
          `${job.current.instance_id} · ${labels[job.current.status] || job.current.status}`,
          "subtle",
        ),
      );
    const exportButton = element("button", "导出本次报告");
    exportButton.onclick = () => download(job, `swe-bench-${job.id}.json`);
    report.append(exportButton);
    for (const row of job.results) {
      const details = element("details", undefined, "record-item");
      details.append(
        element(
          "summary",
          `${row.instance_id} · ${labels[row.status] || row.status}`,
        ),
        disclosure("执行与验收记录", row),
      );
      const actions = element("div", undefined, "inline-actions");
      for (const [name, title] of [
        ["patch", "查看补丁"],
        ["trace", "查看轨迹"],
        ["log", "验收日志"],
        ["tests", "测试输出"],
      ]) {
        const button = element("button", title);
        button.onclick = () =>
          safely(async () => {
            const value = await request(
              `${endpoint}/jobs/${job.id}/attempts/${row.artifact_index}/${name}`,
            );
            const content = disclosure(
              title,
              name === "trace" ? value : value.text,
            );
            content.open = true;
            details.append(content);
            if (name === "trace") {
              const refs = [
                ...new Set(
                  (value.events || [])
                    .map((event) => event.input_ref)
                    .filter(Boolean),
                ),
              ];
              for (const [index, ref] of refs.entries()) {
                const id = /^inputs\/([a-f0-9]{32})\.json$/.exec(ref)?.[1];
                if (!id) continue;
                const input = element(
                  "button",
                  `查看实际模型输入 ${index + 1}`,
                );
                input.onclick = () =>
                  safely(async () => {
                    const payload = await request(
                      `${endpoint}/jobs/${job.id}/attempts/${row.artifact_index}/inputs/${id}`,
                    );
                    const item = disclosure("脱敏请求输入", payload);
                    item.open = true;
                    details.append(item);
                  });
                details.append(input);
              }
            }
          });
        actions.append(button);
      }
      details.append(actions);
      const task = tasks.find(task => task.instance_id === row.instance_id) || row;
      attachRunConversation(details,
        () => request(`${endpoint}/jobs/${job.id}/attempts/${row.artifact_index}/trace`),
        { task, result: row, official: true }, safely);
      report.append(details);
    }
  }

  async function loadJobs() {
    jobs = await request(endpoint + "/jobs");
    const list = $("swe-reports");
    list.replaceChildren();
    for (const job of jobs) {
      const button = element(
        "button",
        `${time(job.created_at)} · ${job.metrics.resolved}/${job.planned} 项通过 · ${labels[job.status] || job.status}`,
        "report-link",
      );
      button.onclick = () =>
        safely(async () =>
          renderReport(await request(`${endpoint}/jobs/${job.id}`)),
        );
      list.append(button);
    }
    if (jobs.length) renderReport(jobs[0]);
    const available = filtered();
    const task = available.find(task => task.instance_id === selectedTask?.instance_id) ||
      available.find(task => jobs.some(job => job.results.some(row => row.instance_id === task.instance_id))) || available[0];
    if (visible) await showTask(task);
    const running = jobs.find((job) => job.status === "running");
    active = running?.id || null;
    $("swe-cancel").hidden = !active;
    counts();
    if (active) poll();
  }

  async function poll() {
    clearTimeout(timer);
    if (!visible || !active) return;
    try {
      const job = await request(`${endpoint}/jobs/${active}`);
      renderReport(job);
      if (job.status !== "running") {
        active = null;
        $("swe-cancel").hidden = true;
        await loadJobs();
      } else timer = setTimeout(poll, 3000);
    } catch (error) {
      $("evaluation-error").textContent = error.message;
      timer = setTimeout(poll, 5000);
    }
    counts();
  }

  async function open(dataset, info) {
    visible = true;
    const version = ++generation;
    if (info) {
      $("swe-model").textContent = info.model;
      const effort = $("swe-effort");
      effort.replaceChildren();
      for (const level of info.reasoning.levels) {
        const option = element("option", level.label);
        option.value = level.value;
        effort.append(option);
      }
      effort.value = info.reasoning.default || "";
      effort.disabled = !info.reasoning.levels.length;
    }
    if (currentDataset !== dataset) {
      chosen.clear();
      currentDataset = dataset;
    }
    $("swe-state").textContent = "检查环境与任务…";
    const [environment, catalog] = await Promise.all([
      request(endpoint + "/environment"),
      request(`${endpoint}/datasets/swe_${dataset}`),
    ]);
    if (!visible || version !== generation) return;
    ready = environment.ready;
    tasks = catalog.tasks;
    $("swe-state").textContent = ready
      ? `官方验收器 ${environment.harness_version} · Docker ${environment.docker_version}`
      : `环境尚未就绪：${environment.reason || "请同步官方任务"}`;
    $("dataset-revision").textContent =
      `已导入 ${tasks.length} 项 · 官方 test 集`;
    $("evaluation-scope").textContent = catalog.revision
      ? `已固定官方任务版本 ${catalog.revision.slice(0, 12)}`
      : "任务尚未同步。";
    const select = $("case-filter");
    const previous = select.value;
    select.replaceChildren();
    for (const [key, name] of [
      ["", "全部仓库"],
      ...[...new Set(tasks.map((t) => t.repo))].sort().map((r) => [r, r]),
    ]) {
      const option = element("option", name);
      option.value = key;
      select.append(option);
    }
    select.value = [...select.options].some((o) => o.value === previous)
      ? previous
      : "";
    select.disabled = !tasks.length;
    renderTasks();
    await loadJobs();
  }

  $("swe-sync").onclick = () =>
    safely(async () => {
      $("swe-sync").disabled = true;
      $("swe-state").textContent = "同步官方任务中，首次下载可能需要几分钟…";
      try {
        await post(endpoint + "/sync");
        await open(currentDataset);
      } finally {
        $("swe-sync").disabled = false;
      }
    });
  $("swe-clear").onclick = () => {
    chosen.clear();
    renderTasks();
  };
  $("swe-run").onclick = () =>
    safely(async () => {
      $("swe-run").disabled = true;
      try {
        const job = await post(endpoint + "/jobs", {
          dataset_id: "swe_" + currentDataset,
          instance_ids: [...chosen],
          agent_mode: $("swe-mode").value,
          approval_behavior: $("swe-approval").value,
          reasoning_effort: $("swe-effort").disabled
            ? null
            : $("swe-effort").value,
          agent_timeout_seconds: Number($("swe-agent-timeout").value),
          grader_timeout_seconds: Number($("swe-grader-timeout").value),
        });
        active = job.id;
        $("swe-cancel").hidden = false;
        renderReport(job);
        poll();
      } finally {
        counts();
      }
    });
  $("swe-cancel").onclick = () =>
    safely(async () => {
      if (active) {
        await post(`${endpoint}/jobs/${active}/cancel`);
        await poll();
      }
    });
  return {
    open,
    filter: () => {
      chosen.clear();
      renderTasks();
      safely(() => showTask(filtered()[0]));
    },
    close: () => {
      visible = false;
      generation++;
      taskVersion++;
      clearTimeout(timer);
    },
  };
}

import { request, post } from "../api.js";
import { element } from "../messages.js";
import { $, empty, time } from "../inspection/shared.js";
import { sweBench } from "./swe-bench.js";
import { taskEditor } from "./editor.js";
import { reports, rate } from "./reports.js";
import {
  frameworks,
  datasets,
  categories,
  historyCategories,
  casesIn,
  sourceSessions,
  subcategory,
} from "./catalog.js";

function options(id, choices, value) {
  const select = $(id);
  select.replaceChildren();
  for (const [key, name] of choices) {
    const option = element("option", name);
    option.value = key;
    select.append(option);
  }
  select.value = choices.some(([key]) => key === value)
    ? value
    : choices[0]?.[0] || "";
}

export function evaluations(getDetail, showChat) {
  let dataset = null,
    sessions = [],
    selected = -1,
    chosen = new Set(),
    active = null,
    timer = null,
    visible = false,
    reportVersion = "",
    reasoningSupported = false;
  const editor = taskEditor();
  async function safely(action) {
    $("evaluation-error").textContent = "";
    try {
      await action();
    } catch (error) {
      $("evaluation-error").textContent = error.message;
      if ($("evaluation-import-dialog").open)
        $("evaluation-import-error").textContent = error.message;
    }
  }
  const officialView = sweBench(safely);
  let modelInfo = null;
  const view = reports(safely, loadReports);
  options(
    "evaluation-framework",
    frameworks.map((f) => [f.id, f.name]),
    "local",
  );
  const local = () => $("evaluation-framework").value === "local";
  const datasetId = () => $("evaluation-dataset").value;
  const visibleCases = () =>
    casesIn(dataset?.cases || [], datasetId(), $("case-filter").value);
  const chosenCases = () => visibleCases().filter((c) => chosen.has(c.id));
  function collect() {
    if (dataset && !$("case-form").hidden)
      editor.collect(dataset.cases[selected]);
  }
  function selectors() {
    const choices = datasets[$("evaluation-framework").value];
    options(
      "evaluation-dataset",
      choices.map((d) => [
        d.id,
        `${d.name}（${local() ? casesIn(dataset.cases, d.id).length : d.count} 项）`,
      ]),
      datasetId(),
    );
    const group = datasetId() === "history" ? historyCategories : categories;
    const available = new Set(
      casesIn(dataset.cases, datasetId()).map((c) =>
        subcategory(c, datasetId()),
      ),
    );
    options(
      "case-filter",
      local()
        ? [
            ["", "全部分类"],
            ...Object.entries(group).filter(([key]) => available.has(key)),
          ]
        : [["", "官方任务导入后，按仓库分类"]],
      $("case-filter").value,
    );
    $("case-filter").disabled = !local();
  }
  function render() {
    selectors();
    $("local-library").hidden = !local();
    $("swe-bench-setup").hidden = local();
    $("swe-library").hidden = local();
    if (local()) officialView.close();
    $("benchmark-controls").hidden = !local() || datasetId() === "history";
    $("history-controls").hidden = !local() || datasetId() !== "history";
    $("evaluation-report").hidden =
      !local() || !$("report-heading").textContent;
    if (!local()) {
      const official = datasets.swe_bench.find((d) => d.id === datasetId());
      editor.render(null);
      $("evaluation-empty").hidden = true;
      $("dataset-revision").textContent = "官方 test 集";
      $("evaluation-scope").textContent = "加载官方任务…";
      $("swe-dataset-name").textContent = official.name;
      $("swe-dataset-description").textContent = official.description;
      $("swe-dataset-link").href = official.url;
      safely(() => officialView.open(datasetId(), modelInfo));
      return;
    }
    const cases = visibleCases();
    const ids = new Set(cases.map((c) => c.id));
    chosen = new Set([...chosen].filter((id) => ids.has(id)));
    if (!ids.has(dataset.cases[selected]?.id))
      selected = dataset.cases.findIndex((c) => ids.has(c.id));
    $("dataset-revision").textContent =
      `v${dataset.revision} · ${cases.length}项`;
    $("evaluation-scope").textContent =
      `当前分类 ${cases.length} 项。选择和运行仅作用于这里的评测项。`;
    $("case-add").textContent =
      datasetId() === "history" ? "新增规则" : "新增任务";
    const container = $("evaluation-cases");
    container.replaceChildren();
    dataset.cases.forEach((case_, index) => {
      if (!ids.has(case_.id)) return;
      const row = element("div", undefined, "evaluation-case-row");
      const checkbox = element("input");
      checkbox.type = "checkbox";
      checkbox.checked = chosen.has(case_.id);
      checkbox.setAttribute("aria-label", `选择 ${case_.name}`);
      checkbox.onchange = () => {
        checkbox.checked ? chosen.add(case_.id) : chosen.delete(case_.id);
        counts();
      };
      const button = element(
        "button",
        undefined,
        index === selected ? "active" : "",
      );
      button.append(
        element("span", case_.name),
        element(
          "small",
          case_.mode === "task"
            ? `${categories[case_.category]} · ${case_.prompts.length}轮 / ${case_.assertions.length}条件`
            : historyCategories[subcategory(case_, "history")],
        ),
      );
      button.onclick = () =>
        safely(async () => {
          collect();
          selected = index;
          render();
        });
      row.append(checkbox, button);
      container.append(row);
    });
    if (!cases.length) empty(container, "当前分类暂无评测项。");
    $("evaluation-empty").hidden = !!cases.length;
    editor.render(dataset.cases[selected]);
    counts();
  }
  const modeInputs = [
    ...document.querySelectorAll(".benchmark-mode-options input"),
  ];
  const selectedModes = () =>
    modeInputs.filter((i) => i.checked).map((i) => i.value);
  modeInputs.forEach((i) => (i.onchange = counts));
  $("benchmark-repeats").onchange = counts;
  function counts() {
    const modes = selectedModes();
    const cases = local() ? chosenCases() : [];
    const pairs = cases
      .filter((c) => c.mode === "task")
      .reduce(
        (n, c) =>
          n +
          modes.filter((m) => (c.goal === "planning") === (m === "plan"))
            .length,
        0,
      );
    $("benchmark-count").textContent =
      `${cases.length}项 · ${pairs * Number($("benchmark-repeats").value)}次运行`;
    $("history-count").textContent = `${cases.length} 条规则`;
    $("evaluation-run").disabled =
      !cases.length || !sourceSessions(cases, sessions).length;
    modeInputs.forEach((i) => (i.disabled = !!active));
    $("benchmark-approval").disabled = !!active;
    $("benchmark-run").disabled = !pairs || !!active;
    $("benchmark-effort").disabled = !!active || !reasoningSupported;
    $("benchmark-repeats").disabled = !!active;
    $("benchmark-timeout").disabled = !!active;
  }
  async function save() {
    if (!local()) throw new Error("官方评测集不能在本地规则编辑器中修改。");
    collect();
    dataset = await request("/evaluations/dataset", {
      method: "PUT",
      body: JSON.stringify(dataset),
    });
    $("evaluation-status").textContent = "评测集已保存。";
    render();
  }
  async function loadReports() {
    const [saved, jobs] = await Promise.all([
      request("/evaluations/reports"),
      request("/evaluations/jobs"),
    ]);
    const container = $("evaluation-reports");
    container.replaceChildren();
    for (const job of jobs.filter((j) => j.status !== "completed")) {
      const button = element(
        "button",
        `${job.status === "running" ? "运行中" : job.status === "cancelled" ? "已停止" : "未完成"} · ${job.metrics.finished}/${job.planned}项 · ${time(job.created_at)}`,
        "report-link",
      );
      button.onclick = () =>
        safely(async () => {
          const value = await request(`/evaluations/jobs/${job.id}`);
          if (local()) view.render(value);
          if (value.status === "running") {
            active = value.id;
            poll();
          }
        });
      container.append(button);
    }
    for (const report of saved) {
      const benchmark = report.metrics.task_success_rate !== undefined;
      const button = element(
        "button",
        `${benchmark ? "任务" : "会话"} · ${time(report.created_at)} · ${rate(benchmark ? report.metrics.task_success_rate : report.metrics.rule_pass_rate)}`,
        "report-link",
      );
      button.onclick = () =>
        safely(async () =>
          view.render(await request(`/evaluations/reports/${report.id}`)),
        );
      container.append(button);
    }
    if (!saved.length && !jobs.length)
      empty(container, "运行任务或检查会话后，报告会保存在这里。");
    const running = jobs.find((j) => j.status === "running");
    if (running && !active) {
      active = running.id;
      poll();
    }
  }
  async function poll() {
    clearTimeout(timer);
    if (!active || !visible) return;
    try {
      const job = await request(`/evaluations/jobs/${active}`);
      $("benchmark-effort").value = job.options.reasoning_effort || "";
      $("benchmark-repeats").value = job.options.repeats;
      $("benchmark-timeout").value = job.options.timeout_seconds;
      modeInputs.forEach(
        (i) => (i.checked = (job.options.agent_modes || []).includes(i.value)),
      );
      $("benchmark-approval").value =
        job.options.approval_behavior || "approve";
      $("benchmark-progress").textContent =
        job.status === "running"
          ? `已结束 ${job.metrics.finished}/${job.planned} 次 · 当前：${job.current?.name || "准备工作区"} · ${job.current?.agent_mode || "历史未标记"}（第${job.current?.repeat || 1}次）`
          : `评测${job.status === "completed" ? "完成" : "已停止"} · 已结束 ${job.metrics.finished}/${job.planned} 次`;
      $("benchmark-cancel").hidden = job.status !== "running";
      const version = `${job.id}:${job.results.length}:${job.status}`;
      if (version !== reportVersion) {
        if (local()) view.render(job);
        reportVersion = version;
      }
      if (job.status !== "running") {
        active = null;
        counts();
        await loadReports();
        return;
      }
      counts();
      timer = setTimeout(poll, 2000);
    } catch (error) {
      $("evaluation-error").textContent = error.message;
      timer = setTimeout(poll, 5000);
    }
  }
  function selectScope() {
    selectors();
    chosen = new Set(local() ? visibleCases().map((c) => c.id) : []);
    selected = -1;
    render();
  }
  function defaultModes() {
    if (!local() || active) return;
    const mode = datasetId() === "planning" ? "plan" : "auto";
    modeInputs.forEach((input) => (input.checked = input.value === mode));
    counts();
  }
  $("evaluation-framework").onchange = () =>
    safely(async () => {
      collect();
      $("case-filter").value = "";
      selectScope();
      defaultModes();
    });
  $("evaluation-dataset").onchange = () =>
    safely(async () => {
      collect();
      $("case-filter").value = "";
      selectScope();
      defaultModes();
    });
  $("case-filter").onchange = () =>
    safely(async () => {
      if (!local()) {
        officialView.filter();
        return;
      }
      collect();
      selectScope();
    });
  $("case-select-all").onclick = () =>
    safely(async () => {
      collect();
      chosen = new Set(visibleCases().map((c) => c.id));
      render();
    });
  $("case-select-none").onclick = () =>
    safely(async () => {
      collect();
      chosen.clear();
      render();
    });
  $("case-add").onclick = () =>
    safely(async () => {
      collect();
      const history = datasetId() === "history";
      dataset.cases.push({
        id: crypto.randomUUID(),
        name: history ? "新会话规则" : "新编程任务",
        mode: history ? "history" : "task",
        category: history ? "history" : "feature",
        goal: datasetId() === "planning" ? "planning" : "implementation",
        prompts: ["请实现需要的功能"],
        files: { "solution.py": "" },
        assertions: [],
        protected_files: [],
        source: { type: "custom" },
        input_contains: [],
        output_contains: [],
        required_tools: [],
        forbidden_tools: [],
        expected_status: history ? "completed" : null,
        require_verification: false,
      });
      selected = dataset.cases.length - 1;
      $("case-filter").value = "";
      render();
    });
  $("case-remove").onclick = () => {
    if (selected < 0) return;
    const removed = dataset.cases.splice(selected, 1)[0];
    if (removed) chosen.delete(removed.id);
    selected = Math.max(0, selected - 1);
    render();
  };
  $("case-form").onsubmit = (event) => {
    event.preventDefault();
    safely(save);
  };
  $("dataset-save").onclick = () => safely(save);
  const sessionIds = () =>
    [...$("evaluation-sessions").querySelectorAll("input:checked")].map(
      (e) => e.value,
    );
  $("session-import-open").onclick = () => {
    $("evaluation-import-error").textContent = "";
    $("evaluation-import-dialog").showModal();
  };
  $("session-import").onclick = () =>
    safely(async () => {
      const ids = sessionIds();
      if (!ids.length) throw new Error("请先选择要加入的会话。");
      $("evaluation-import-error").textContent = "";
      $("session-import").disabled = true;
      try {
        await save();
        for (const id of ids)
          dataset = await post("/evaluations/imports", {
            session_id: id,
            revision: dataset.revision,
          });
        $("evaluation-import-dialog").close();
      } finally {
        $("session-import").disabled = false;
        $("evaluation-dataset").value = "history";
        $("case-filter").value = "";
        chosen = new Set(casesIn(dataset.cases, "history").map((c) => c.id));
        selected = dataset.cases.length - 1;
        render();
      }
      $("evaluation-status").textContent =
        "会话已加入「会话检查规则」。可直接检查，或补齐文件和验收条件后转为编程任务。";
    });
  $("evaluation-run").onclick = () =>
    safely(async () => {
      await save();
      const cases = chosenCases();
      const ids = sourceSessions(cases, sessions);
      if (!cases.length || !ids.length)
        throw new Error("请选择规则，并导入可检查的会话。");
      view.render(
        await post("/evaluations/runs", {
          session_ids: ids,
          case_ids: cases.map((c) => c.id),
        }),
      );
      await loadReports();
    });
  $("benchmark-run").onclick = () =>
    safely(async () => {
      await save();
      const options = {
        case_ids: chosenCases()
          .filter((c) => c.mode === "task")
          .map((c) => c.id),
        agent_modes: selectedModes(),
        approval_behavior: $("benchmark-approval").value,
        repeats: Number($("benchmark-repeats").value),
        timeout_seconds: Number($("benchmark-timeout").value),
      };
      if (!$("benchmark-effort").disabled)
        options.reasoning_effort = $("benchmark-effort").value;
      const job = await post("/evaluations/jobs", options);
      active = job.id;
      counts();
      await poll();
    });
  $("benchmark-cancel").onclick = () =>
    safely(async () => {
      if (active) {
        await post(`/evaluations/jobs/${active}/cancel`);
        await poll();
      }
    });
  $("evaluation-back").onclick = showChat;
  return {
    open: () =>
      safely(async () => {
        visible = true;
        $("chat-view").hidden = true;
        $("evaluation-view").hidden = false;
        document.body.classList.add("evaluation-open");
        for (const id of [
          "evaluation-framework",
          "evaluation-dataset",
          "case-filter",
        ])
          $(id).disabled = true;
        const [next, availableSessions, info] = await Promise.all([
          request("/evaluations/dataset"),
          request("/sessions"),
          request("/info"),
        ]);
        modelInfo = info;
        dataset = next;
        sessions = availableSessions;
        selected = -1;
        $("evaluation-framework").value = "local";
        $("evaluation-dataset").replaceChildren();
        $("case-filter").replaceChildren();
        $("case-form").hidden = true;
        $("evaluation-status").textContent = "";
        reportVersion = "";
        $("evaluation-framework").disabled = false;
        $("evaluation-dataset").disabled = false;
        selectScope();
        defaultModes();
        $("benchmark-model").textContent = info.model;
        const effort = $("benchmark-effort");
        effort.replaceChildren();
        for (const level of info.reasoning.levels) {
          const option = element("option", level.label);
          option.value = level.value;
          effort.append(option);
        }
        effort.value = info.reasoning.default || "";
        reasoningSupported = !!info.reasoning.levels.length;
        effort.disabled = !reasoningSupported;
        const container = $("evaluation-sessions");
        container.replaceChildren();
        for (const session of sessions.filter((s) => s.status !== "error")) {
          const label = element("label", undefined, "session-check");
          const input = element("input");
          input.type = "checkbox";
          input.value = session.session_id;
          input.checked =
            session.session_id === getDetail()?.session.session_id;
          label.append(input, element("span", session.title || "新会话"));
          container.append(label);
        }
        if (!container.children.length)
          empty(container, "暂无可导入的会话。先返回会话发送一条消息。");
        counts();
        await loadReports();
        if (active) await poll();
      }),
    close: () => {
      visible = false;
      officialView.close();
      clearTimeout(timer);
      $("chat-view").hidden = false;
      $("evaluation-view").hidden = true;
      document.body.classList.remove("evaluation-open");
    },
  };
}

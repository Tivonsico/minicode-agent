import { request } from "../api.js";
import { element, renderMessages } from "../messages.js";
import { $, disclosure, empty } from "../inspection/shared.js";

const statuses = { passed: "通过", failed: "未通过", unknown: "待核实", error: "执行错误", observed: "观测记录" };
const unique = values => [...new Set(values)];
export function experimentArchive(safely) {
  let records = [], selected = "", category = "", version = 0;
  const cache = new Map();
  const filtered = () => records.filter(record => record.category === category &&
    ($("evaluation-dataset").value === "all" || record.source_key === $("evaluation-dataset").value) &&
    (!$("case-filter").value || record.status === $("case-filter").value));
  function note(label, content, className = "") {
    const article = element("article", undefined, `message experiment-note ${className}`);
    article.append(element("div", label, "message-label"), element("div", content, "message-body"));
    return article;
  }
  function selector(host, label, choices, value, changed) {
    const wrapper = element("label", label);
    const select = element("select");
    select.setAttribute("aria-label", label);
    for (const [key, text] of choices) {
      const option = element("option", text);
      option.value = key;
      select.append(option);
    }
    select.value = value;
    select.onchange = () => safely(() => changed(select.value));
    wrapper.append(select);
    host.append(wrapper);
  }
  async function show(taskKey, chosenVariant, chosenCondition, chosenRun) {
    const ticket = ++version;
    selected = taskKey;
    // Keep all conditions available within a matching task, including comparison controls.
    const runs = records.filter(r => r.category === category && r.task_key === taskKey);
    const variants = unique(runs.map(r => r.variant_key));
    const variant = variants.includes(chosenVariant) ? chosenVariant :
      variants.find(v => v.startsWith("本项目改进后")) || variants.find(v => v.startsWith("论文原方案")) || variants[0];
    const methods = runs.filter(r => r.variant_key === variant);
    const conditions = unique(methods.map(r => r.condition_key));
    const condition = conditions.includes(chosenCondition) ? chosenCondition :
      conditions.find(c => c === "请求已执行，但响应丢失") || conditions[0];
    const trials = methods.filter(r => r.condition_key === condition);
    const picked = trials.find(r => r.id === chosenRun) || trials[0];
    const detail = $("experiment-detail");
    detail.replaceChildren(element("h3", picked.task_title), element("p", picked.source_label, "subtle"));
    const controls = element("div", undefined, "experiment-controls");
    selector(controls, "比较方案", variants.map(v => [v, v]), variant, v => show(taskKey, v, condition));
    selector(controls, "测试情况", conditions.map(c => [c, c]), condition, c => show(taskKey, variant, c));
    detail.append(controls, element("p", category === "recovery"
      ? "“未发生中断”和“响应丢失”是测试情况。优化前后请看“比较方案”；论文中的不同方案属于方法对比。测试情况表示预设场景，是否实际触发以末尾验收为准。"
      : "在“比较方案”中切换原方案、对照方案或改进尝试；在“测试情况”中选择运行条件。改进尝试是否有效，以末尾验收结果为准。", "subtle"));
    if (trials.length > 1) {
      const older = element("details");
      older.append(element("summary", `同条件的其他运行（共 ${trials.length} 次，默认显示最近一次）`));
      selector(older, "历史运行", trials.map((r, index) => [r.id, `${index === 0 ? "最近一次" : `较早运行 ${index}`} · ${r.created_at ? new Date(r.created_at).toLocaleString("zh-CN") : "未记录时间"} · ${statuses[r.status]}`]), picked.id,
        id => show(taskKey, variant, condition, id));
      detail.append(older);
    }
    const target = element("div", "正在读取原始实验记录…");
    detail.append(target);
    for (const button of $("experiment-cases").querySelectorAll("button"))
      button.classList.toggle("active", button.dataset.id === taskKey);
    const record = cache.get(picked.id) || await request(`/evaluations/experiments/${picked.id}`);
    if (ticket !== version) return;
    cache.set(record.id, record);
    target.replaceChildren(element("p", `本次结果：${statuses[record.status]}`, "subtle"));
    const jump = element("button", "查看末尾中文总结", "text-button");
    target.append(jump);
    const thread = element("div", undefined, "experiment-thread");
    thread.setAttribute("aria-label", "实验对话与复盘");
    if (record.notes && record.notes !== "按时间顺序展示用户任务、模型回复与工具结果。") thread.append(note("记录来源", record.notes));
    if (record.source_key === "paper") thread.append(note("任务来源", "这里展示论文或开源项目素材在本项目中的适配与对比运行，不代表已完成原论文全量复现。"));
    for (const content of record.annotations || []) {
      if (content === "检查工具执行成功但响应或记录缺失时，恢复是否会重复操作，并核对最终外部状态。") continue;
      thread.append(note("实验说明", content.replaceAll(record.title, `${record.task_title}；${record.variant_label}；${record.condition_label}`)));
    }
    const original = element("section");
    original.setAttribute("aria-label", "原始对话记录");
    let events = record.events;
    if (record.focus_last_turn) {
      const last = [...events].reverse().find(e => e.type === "message" && e.role === "user");
      if (last?.turn_id) {
        const previous = events.filter(e => e.turn_id !== last.turn_id);
        const history = element("details");
        history.append(element("summary", "展开共用的此前对话（默认只显示本次验证）"));
        let loaded = false;
        history.ontoggle = () => { if (history.open && !loaded) { loaded = true; const body = element("div"); renderMessages(body, previous); history.append(body); } };
        thread.append(history);
        events = events.filter(e => e.turn_id === last.turn_id);
      }
    }
    renderMessages(original, events);
    if (record.origin === "request_archive") {
      for (const label of original.querySelectorAll(".message-label > span:first-child"))
        label.textContent = label.textContent === "minicode" ? "模型（原始响应）" : "原始请求输入";
    }
    thread.append(original);
    if (record.evidence) thread.append(disclosure("独立验收原始证据", record.evidence));
    const summary = note("中文总结", record.summary, "experiment-summary");
    thread.append(summary);
    target.append(thread);
    jump.onclick = () => summary.scrollIntoView({ block: "nearest" });
  }
  return {
    setRecords(value) { records = value; },
    selectors(nextCategory, options) {
      category = nextCategory;
      const matching = records.filter(r => r.category === category);
      const count = rows => unique(rows.map(r => r.task_key)).length;
      options("evaluation-dataset", [["all", `全部任务（${count(matching)} 项）`],
        ...unique(matching.map(r => r.source_key)).map(key => [key,
          `${matching.find(r => r.source_key === key).source_label}（${count(matching.filter(r => r.source_key === key))} 项）`])], $("evaluation-dataset").value);
      options("case-filter", [["", "全部结果"], ...Object.entries(statuses).map(([key, label]) => [key, key === "observed" ? "有观测记录" : `有${label}记录`])], $("case-filter").value);
      $("case-filter").disabled = false;
    },
    open() {
      $("experiment-library").hidden = false;
      $("experiment-workbench").hidden = false;
      const items = filtered();
      const tasks = unique(items.map(r => r.task_key));
      $("dataset-revision").textContent = `${tasks.length} 项任务`;
      $("evaluation-scope").textContent = "每个任务只列一次。点开后选择方案与测试情况；历史运行折叠保留，不会重新执行或消耗模型用量。";
      const container = $("experiment-cases");
      container.replaceChildren();
      for (const key of tasks) {
        const record = items.find(r => r.task_key === key);
        const allRuns = records.filter(r => r.task_key === key);
        const button = element("button", undefined, key === selected ? "active" : "");
        button.dataset.id = key;
        button.append(element("span", record.task_title), element("small", `${record.source_label} · ${unique(allRuns.map(r => r.variant_key)).length} 种方案`));
        button.onclick = () => safely(() => show(key));
        container.append(button);
      }
      if (!tasks.length) {
        ++version;
        empty(container, "当前筛选没有任务。");
        empty($("experiment-detail"), "请选择其他来源或结果。");
      } else safely(() => show(tasks.includes(selected) ? selected : tasks[0]));
    },
    close() {
      ++version;
      $("experiment-library").hidden = true;
      $("experiment-workbench").hidden = true;
    },
  };
}

import { element } from "../messages.js";
import { $ } from "../inspection/shared.js";

const list = (text) =>
  text
    .split("\n")
    .map((s) => s.trim())
    .filter(Boolean);
const input = (label, value, multiline = false) => {
  const wrapper = element("label", undefined, "editor-field");
  const field = element(multiline ? "textarea" : "input");
  field.value = value;
  if (multiline) field.rows = 3;
  wrapper.append(element("span", label, "field-label"), field);
  return { wrapper, field };
};
const select = (label, choices, value) => {
  const wrapper = element("label", undefined, "editor-field");
  const field = element("select");
  choices.forEach(([key, name]) => {
    const option = element("option", name);
    option.value = key;
    field.append(option);
  });
  field.value = value;
  wrapper.append(element("span", label, "field-label"), field);
  return { wrapper, field };
};

export function taskEditor() {
  let fileRows = [],
    checkRows = [];
  function addFile(path = "", text = "") {
    const row = element("div", undefined, "task-file");
    const name = input("文件路径", path),
      content = input("初始内容", text, true);
    const remove = element("button", "移除文件", "text-button");
    remove.type = "button";
    const item = { row, name: name.field, content: content.field };
    remove.onclick = () => {
      row.remove();
      fileRows = fileRows.filter((r) => r !== item);
    };
    row.append(name.wrapper, content.wrapper, remove);
    $("case-files").append(row);
    fileRows.push(item);
  }
  function addCheck(
    check = {
      name: "新验收条件",
      kind: "call",
      path: "solution.py",
      symbol: "",
      args: [],
      kwargs: {},
      expected: null,
      raises: null,
      phase: "target",
    },
  ) {
    const row = element("details", undefined, "task-check");
    row.open = checkRows.length === 0;
    row.append(element("summary", check.name));
    const fields = {
      name: input("条件名称", check.name),
      kind: select(
        "验收方式",
        [
          ["call", "函数返回值 / 异常"],
          ["file_contains", "文件包含文本"],
          ["file_equals", "文件内容一致"],
          ["file_absent", "文件不存在"],
        ],
        check.kind,
      ),
      phase: select(
        "检查目的",
        [
          ["target", "新需求 / 修复"],
          ["regression", "原有行为回归"],
          ["constraint", "安全与约束"],
        ],
        check.phase,
      ),
      path: input("文件路径", check.path),
      symbol: input("函数名", check.symbol || ""),
      args: input("位置参数（JSON数组）", JSON.stringify(check.args || [])),
      kwargs: input("命名参数（JSON对象）", JSON.stringify(check.kwargs || {})),
      expected: input("预期结果（JSON）", JSON.stringify(check.expected)),
      raises: input("预期异常（留空检查返回值）", check.raises || ""),
    };
    const grid = element("div", undefined, "assertion-fields");
    Object.values(fields).forEach((f) => grid.append(f.wrapper));
    const remove = element("button", "移除条件", "text-button");
    remove.type = "button";
    const item = { row, fields };
    remove.onclick = () => {
      row.remove();
      checkRows = checkRows.filter((r) => r !== item);
    };
    row.append(grid, remove);
    $("case-assertions").append(row);
    checkRows.push(item);
  }
  $("file-add").onclick = () => addFile();
  $("assertion-add").onclick = () => addCheck();
  function mode() {
    $("task-fields").hidden = $("case-mode").value !== "task";
    $("history-fields").hidden = $("case-mode").value !== "history";
    $("planning-fields").hidden = $("case-goal").value !== "planning";
  }
  $("case-goal").onchange = mode;
  $("case-mode").onchange = () => {
    if (
      $("case-mode").value === "task" &&
      $("case-category").value === "history"
    )
      $("case-category").value = "feature";
    mode();
  };
  return {
    render(case_) {
      $("case-form").hidden = !case_;
      $("case-editor-panel").hidden = !case_;
      if (!case_) return;
      $("case-name").value = case_.name;
      $("case-mode").value = case_.mode;
      $("case-category").value = case_.category;
      $("case-goal").value = case_.goal || "implementation";
      $("case-agent-mode").value = case_.agent_mode || "";
      $("case-plan-output").value = (case_.output_contains || []).join("\n");
      $("case-prompts").value = (case_.prompts || []).join("\n---\n");
      $("case-protected").value = (case_.protected_files || []).join("\n");
      for (const [key, id] of [
        ["input_contains", "case-input"],
        ["output_contains", "case-output"],
        ["required_tools", "case-tools"],
        ["forbidden_tools", "case-forbidden"],
      ])
        $(id).value = (case_[key] || []).join("\n");
      $("case-status").value = case_.expected_status || "";
      $("case-verification").checked = case_.require_verification;
      $("case-source").textContent = case_.source?.url
        ? `${case_.source.upstream_id || "参考方法"} · ${case_.source.url} · ${case_.source.method || ""}`
        : case_.source?.session_id
          ? `来自会话 ${case_.source.session_id.slice(0, 8)} · 日志截至 ${case_.source.covered_seq}`
          : "自定义任务";
      $("case-files").replaceChildren();
      $("case-assertions").replaceChildren();
      fileRows = [];
      checkRows = [];
      Object.entries(case_.files || {}).forEach(([path, text]) =>
        addFile(path, text),
      );
      (case_.assertions || []).forEach(addCheck);
      mode();
    },
    collect(case_) {
      if (!case_) return;
      case_.name = $("case-name").value;
      case_.mode = $("case-mode").value;
      case_.category = $("case-category").value;
      case_.goal = $("case-goal").value;
      case_.agent_mode = $("case-agent-mode").value || null;
      case_.prompts = $("case-prompts")
        .value.split(/^---\s*$/m)
        .map((v) => v.trim())
        .filter(Boolean);
      case_.protected_files = list($("case-protected").value);
      case_.files = {};
      for (const row of fileRows) {
        if (Object.hasOwn(case_.files, row.name.value))
          throw new Error("初始文件路径不能重复。");
        case_.files[row.name.value] = row.content.value;
      }
      try {
        case_.assertions = checkRows.map(({ fields: f }) => ({
          name: f.name.field.value,
          kind: f.kind.field.value,
          phase: f.phase.field.value,
          path: f.path.field.value,
          symbol: f.symbol.field.value,
          args: JSON.parse(f.args.field.value),
          kwargs: JSON.parse(f.kwargs.field.value),
          expected: JSON.parse(f.expected.field.value),
          raises: f.raises.field.value || null,
        }));
      } catch {
        throw new Error("验收条件中的参数和预期结果必须是有效JSON。");
      }
      for (const [key, id] of [
        ["input_contains", "case-input"],
        ["output_contains", "case-output"],
        ["required_tools", "case-tools"],
        ["forbidden_tools", "case-forbidden"],
      ])
        case_[key] = list($(id).value);
      if (case_.mode === "task" && case_.goal === "planning")
        case_.output_contains = list($("case-plan-output").value);
      case_.expected_status = $("case-status").value || null;
      case_.require_verification = $("case-verification").checked;
    },
  };
}

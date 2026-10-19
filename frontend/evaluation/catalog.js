// Frameworks describe how to grade; datasets describe which cases to grade.
export const frameworks = [
  { id: "local", name: "本地会话规则" },
  { id: "swe_bench", name: "SWE-bench 官方规则" },
];

export const categories = {
  repair: "问题修复",
  feature: "功能实现",
  multiturn: "多轮需求",
  security: "安全场景",
  history: "会话规则",
};

export const datasets = {
  local: [
    { id: "coding", name: "本地编程任务" },
    { id: "planning", name: "只读规划任务" },
    { id: "history", name: "会话检查规则" },
  ],
  swe_bench: [
    {
      id: "lite",
      name: "SWE-bench Lite",
      count: 300,
      description: "300 项真实仓库问题，适合先跑通少量任务的接入流程。",
      url: "https://huggingface.co/datasets/SWE-bench/SWE-bench_Lite",
    },
    {
      id: "verified",
      name: "SWE-bench Verified",
      count: 500,
      description: "500 项经人工核验的真实仓库问题。",
      url: "https://huggingface.co/datasets/SWE-bench/SWE-bench_Verified",
    },
  ],
};

export function belongs(case_, datasetId) {
  if (datasetId === "history") return case_.mode === "history";
  if (case_.mode !== "task") return false;
  if (datasetId === "planning") return case_.goal === "planning";
  return datasetId === "coding" && case_.goal !== "planning";
}

export function subcategory(case_, datasetId) {
  if (datasetId !== "history") return case_.category;
  if (case_.source?.type === "session") return "imported";
  return case_.source?.type === "custom" ? "custom" : "builtin";
}

export const historyCategories = {
  builtin: "系统规则",
  imported: "导入会话",
  custom: "自定义规则",
};

export function casesIn(cases, datasetId, filter = "") {
  return cases.filter(
    (case_) =>
      belongs(case_, datasetId) &&
      (!filter || subcategory(case_, datasetId) === filter),
  );
}

export function sourceSessions(cases, sessions) {
  // Generic rules inspect all eligible conversations. Imported rules retain their origin.
  if (cases.some((case_) => !case_.source?.session_id))
    return sessions
      .filter((s) => s.status !== "error")
      .map((s) => s.session_id);
  return [...new Set(cases.map((case_) => case_.source.session_id))];
}

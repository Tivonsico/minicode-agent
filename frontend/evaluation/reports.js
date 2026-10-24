import { request, post } from "../api.js";
import { modeName } from "../modes.js";
import { element } from "../messages.js";
import { attachRunConversation } from "./conversation.js";
import {
  $,
  disclosure,
  download,
  duration,
  empty,
  time,
} from "../inspection/shared.js";

export const rate = (value) =>
  value == null ? "暂无样本" : `${(value * 100).toFixed(1)}%`;
export function reports(safely, refreshReports) {
  let current = null;
  $("report-export").onclick = () => {
    if (current) download(current, `evaluation-${current.id}.json`);
  };
  $("report-regrade").onclick = () =>
    safely(async () => {
      const button = $("report-regrade");
      button.disabled = true;
      $("evaluation-status").textContent =
        "正在沙箱中重新验收保存产物，不调用模型或重放工具。";
      try {
        const report = await post(
          `/evaluations/jobs/${current.job_id || current.id}/regrade`,
        );
        render(report);
        await refreshReports();
        $("evaluation-status").textContent = "新的验收报告已保存，原报告保留。";
      } finally {
        button.disabled = false;
      }
    });
  const metric = (label, text) => {
    const item = element("div");
    item.append(
      element("span", label, "subtle"),
      element("strong", String(text)),
    );
    $("report-metrics").append(item);
  };
  function render(report) {
    current = report;
    const m = report.metrics,
      benchmark = report.kind === "agent_benchmark";
    $("evaluation-report").hidden = false;
    $("report-heading").textContent =
      `${benchmark ? "任务运行" : "历史会话检查"} · ${report.dataset_name} · 版本${report.dataset_revision} · ${time(report.created_at)}${benchmark ? ` · ${report.environment.model} / ${report.environment.reasoning_effort || "默认"}` : ""}`;
    $("report-description").textContent = benchmark
      ? `${report.regraded_from ? "本报告重新验收已保存的最终代码，中间阶段仅重判已记录值；保留原运行耗时与用量，未再次运行Agent。" : "任务成绩来自独立验收。"}实现与规划按目标分别统计；实现成功率排除环境错误，整体成功率包含已结束的错误与取消；覆盖率显示已结束/计划次数。重复运行至少成功一次的比例是本次实验结果，不是无偏pass@k估计。`
      : "这些结果来自已有会话规则检查，不能作为重新运行代码的验收成功率。";
    $("report-regrade").hidden = !benchmark || report.status === "running";
    $("report-metrics").replaceChildren();
    const values = benchmark
      ? [
          ["代码实现成功率", rate(m.task_success_rate)],
          ["规划验收通过率", rate(m.planning_success_rate)],
          [
            "实现 / 规划已判分",
            `${m.implementation_judged ?? m.judged} / ${m.planning_judged || 0}`,
          ],
          ["整体成功率", rate(m.overall_success_rate)],
          ["已结束 / 计划次数", `${m.finished} / ${m.planned}`],
          ["首轮成功率", rate(m.pass_at_1)],
          ["回归条件通过率", rate(m.regression_pass_rate)],
          ["已暴露攻击成功率", rate(m.attack_success_rate)],
          ["安全场景功能通过率", rate(m.security_utility_rate)],
          ["已暴露安全样本", m.attack_exposed],
          [
            "耗时 P50 / P95",
            `${duration(m.duration_p50_ms)} / ${duration(m.duration_p95_ms)}`,
          ],
          ["请求首正文 P50", duration(m.first_text_p50_ms)],
          ["已知 tokens", m.known_tokens.toLocaleString()],
          ["请求 / 重试", `${m.requests} / ${m.retries}`],
          ["用量未知请求", m.unknown_usage_requests],
          ["工具失败", m.tool_failures],
          [
            "成功率 95% 区间",
            m.task_success_ci95?.map(rate).join(" – ") || "暂无样本",
          ],
          ["重复运行至少成功一次", rate(m.any_success_rate)],
        ]
      : [
          ["规则通过率", rate(m.rule_pass_rate)],
          ["通过 / 已评测", `${m.passed} / ${m.evaluated}`],
          ["规则覆盖率", rate(m.case_coverage)],
          [
            "耗时 P50 / P95",
            `${duration(m.turn_duration_p50_ms)} / ${duration(m.turn_duration_p95_ms)}`,
          ],
          ["请求首正文 P50", duration(m.first_text_p50_ms)],
          ["已知 tokens", m.known_tokens.toLocaleString()],
          ["用量未知请求", m.unknown_usage_requests],
          ["请求 / 重试", `${m.requests} / ${m.retries}`],
          ["工具失败 / 结果", `${m.tool_failures} / ${m.tool_calls}`],
        ];
    values.forEach(([label, value]) => metric(label, value));
    for (const [mode, stats] of Object.entries(m.mode_metrics || {})) {
      metric(
        `${modeName(mode)} · ${benchmark ? "验收通过率" : "历史规则通过率"}`,
        rate(
          stats.task_success_rate ??
            stats.planning_success_rate ??
            stats.rule_pass_rate,
        ),
      );
      if (benchmark)
        metric(
          `${modeName(mode)} · 审批 / 耗时 P50`,
          `${stats.approvals} / ${duration(stats.duration_p50_ms)}`,
        );
    }
    const results = $("report-results");
    results.replaceChildren();
    report.results.forEach((result, index) => {
      const item = element("details", undefined, "record-item");
      item.append(
        element(
          "summary",
          `${result.passed ? "通过" : result.status === "error" ? "环境错误" : "未通过"} · ${result.case_name} · ${modeName(result.agent_mode)}${benchmark ? ` · 第${result.repeat}次` : ""}${result.failure_type ? ` · ${result.failure_type}` : ""}`,
        ),
      );
      result.checks?.forEach((check, i) => {
        const before = result.baseline_checks?.[i];
        item.append(
          element(
            "p",
            `${check.passed ? "✓" : "×"} ${check.name}${before ? ` · 初始${before.passed ? "通过" : "失败"} → 最终${check.passed ? "通过" : "失败"}` : ""}`,
            "subtle",
          ),
        );
        if (benchmark)
          item.append(
            disclosure("预期与实际", {
              expected: check.expected,
              actual: check.actual,
              phase: check.phase,
            }),
          );
      });
      if (benchmark) {
        const task = report.dataset?.cases?.find(case_ => case_.id === result.case_id) || { name: result.case_name };
        attachRunConversation(item, () => result.artifact_ref
          ? request(`/evaluations/jobs/${report.job_id || report.id}/attempts/${index}`)
          : Promise.resolve({ events: [] }), { task, result }, safely);
        item.append(
          disclosure("阶段与运行指标", {
            agent_mode: result.agent_mode,
            mode_policy_version: result.mode_policy_version,
            mode_prompt_version: result.mode_prompt_version,
            goal: result.goal,
            approvals: result.approvals,
            approval_behavior: result.approval_behavior,
            stages: result.stages,
            duration_ms: result.duration_ms,
            known_tokens: result.known_tokens,
            requests: result.requests,
            attack_exposed: result.attack_exposed,
            attack_success: result.attack_success,
            protected_changed: result.protected_changed,
          }),
        );
        if (result.artifact_ref) {
          const button = element(
            "button",
            "查看文件与原始请求附件",
            "text-button",
          );
          button.onclick = () =>
            safely(async () => {
              button.disabled = true;
              try {
                const job = report.job_id || report.id;
                const artifact = await request(
                  `/evaluations/jobs/${job}/attempts/${index}`,
                );
                item.append(
                  disclosure("初始文件", artifact.initial),
                  disclosure("最终文件", artifact.final),
                  disclosure("原始事件附件", artifact.events),
                );
                const inputs = artifact.events.filter(
                  (e) => e.type === "request_start" && e.input_ref,
                );
                for (const entry of inputs) {
                  const b = element(
                    "button",
                    `查看实际请求 ${entry.request_id.slice(0, 8)}`,
                    "text-button",
                  );
                  b.onclick = () =>
                    safely(async () => {
                      item.append(
                        disclosure(
                          "实际模型输入",
                          await request(
                            `/evaluations/jobs/${job}/attempts/${index}/input?ref=${encodeURIComponent(entry.input_ref)}`,
                          ),
                        ),
                      );
                      b.remove();
                    });
                  item.append(b);
                }
                button.remove();
              } finally {
                button.disabled = false;
              }
            });
          item.append(button);
        }
      } else if (result.session_id) {
        const source = report.sources?.find(entry => entry.session_id === result.session_id);
        const task = report.dataset?.cases?.find(case_ => case_.id === result.case_id) || { name: result.case_name, mode: "history" };
        attachRunConversation(item, () => source
          ? request(`/evaluations/source-conversations/${result.session_id}?covered_seq=${source.covered_seq}&turn_id=${encodeURIComponent(result.turn_id)}`)
          : Promise.resolve({ events: [] }), { task, result }, safely);
      }
      results.append(item);
    });
    if (!report.results.length)
      empty(
        results,
        benchmark
          ? "正在运行，完成第一项后显示验收结果。"
          : "没有匹配的历史会话，请导入或调整规则。",
      );
    if (report.unmatched_cases?.length)
      results.append(
        element(
          "p",
          `未匹配规则：${report.unmatched_cases.join("、")}`,
          "subtle",
        ),
      );
    results.append(
      disclosure("分类成绩、失败分类与实验来源", {
        category_rates: m.category_rates,
        mode_metrics: m.mode_metrics,
        goal_metrics: m.goal_metrics,
        mode_profiles: report.mode_profiles,
        failure_types: m.failure_types,
        environment: report.environment,
        dataset_digest: report.dataset_digest,
        regraded_from: report.regraded_from,
        sources: report.sources,
        options: report.options,
        cost: m.cost,
      }),
    );
  }
  return { render };
}

import { request, post } from "./api.js";
import { element } from "./messages.js";

const $ = (id) => document.getElementById(id);

export function skillManager(getDetail, notify) {
  let sid, scopes = [], editing = null, busy = false, activeTab = "create";
  const base = () => `/sessions/${sid}`;
  const target = () => scopes[Number($("skill-scope").value)];
  function tab(name) {
    activeTab = name;
    $("skill-scope").disabled = Boolean(editing) && name === "create";
    for (const button of document.querySelectorAll("[data-skill-tab]")) {
      const selected = button.dataset.skillTab === name;
      button.setAttribute("aria-selected", String(selected));
      button.tabIndex = selected ? 0 : -1;
      $("skill-pane-" + button.dataset.skillTab).hidden = !selected;
    }
    $("skill-error").textContent = "";
  }
  for (const button of document.querySelectorAll("[data-skill-tab]")) {
    button.onclick = () => tab(button.dataset.skillTab);
    button.onkeydown = (event) => {
      if (!["ArrowLeft", "ArrowRight"].includes(event.key)) return;
      event.preventDefault();
      const buttons = [...document.querySelectorAll("[data-skill-tab]")];
      const next = buttons[(buttons.indexOf(button) + (event.key === "ArrowRight" ? 1 : 2)) % 3];
      tab(next.dataset.skillTab);
      next.focus();
    };
  }
  function resetEditor() {
    editing = null;
    $("skill-create-form").reset();
    $("skill-name").disabled = false;
    $("skill-scope").disabled = false;
    $("skill-description-field").hidden = false;
    $("skill-description").required = true;
    $("skill-save").textContent = "保存技能";
    $("skill-new").hidden = true;
  }
  async function action(callback) {
    if (busy) return;
    busy = true;
    $("skill-error").textContent = "";
    $("skill-dialog").setAttribute("aria-busy", "true");
    const controls = [...$("skill-dialog").querySelectorAll("button, input, select, textarea")];
    const states = controls.map((node) => node.disabled);
    controls.forEach((node) => { node.disabled = true; });
    try { await callback(); }
    catch (error) { $("skill-error").textContent = error.message; $("skill-status").textContent = ""; }
    finally {
      controls.forEach((node, index) => { node.disabled = states[index]; });
      $("skill-name").disabled = Boolean(editing);
      $("skill-scope").disabled = Boolean(editing) && activeTab === "create";
      $("skill-dialog").setAttribute("aria-busy", "false");
      busy = false;
    }
  }
  async function refreshInstalled() {
    const skills = await request(base() + "/skills");
    $("skill-count").textContent = skills.length;
    const list = $("skill-list");
    list.replaceChildren();
    for (const item of skills) {
      const row = element("div", undefined, "installed-skill");
      row.append(element("strong", item.name), element("small", item.description || "暂无用途说明"), element("small", item.scope === "global" ? "所有会话" : "当前项目"));
      const buttons = element("div", undefined, "skill-row-actions");
      const use = element("button", "用于下一轮");
      use.onclick = () => action(async () => {
        await post(base() + "/skills", { name: item.name, scope: item.scope, project_path: item.project_path });
        $("skill-dialog").close();
        notify(`下一轮已选择技能：${item.name}`);
      });
      const edit = element("button", "编辑");
      edit.onclick = () => action(async () => {
        const params = new URLSearchParams({ scope: item.scope });
        if (item.project_path) params.set("project_path", item.project_path);
        const result = await request(base() + `/skill-library/${encodeURIComponent(item.name)}?${params}`);
        editing = item;
        $("skill-name").value = item.name;
        $("skill-content").value = result.content;
        $("skill-description-field").hidden = true;
        $("skill-description").required = false;
        $("skill-save").textContent = "保存修改";
        $("skill-new").hidden = false;
        $("skill-scope").value = scopes.findIndex((scope) => scope.scope === item.scope && scope.project_path === item.project_path);
        tab("create");
        $("skill-status").textContent = "正在编辑已安装技能的完整 SKILL.md。";
      });
      buttons.append(use, edit);
      row.append(buttons);
      list.append(row);
    }
    if (!skills.length) list.append(element("p", "还没有技能。可以在右侧创建，或下载社区技能。", "subtle"));
  }
  $("skill-new").onclick = () => { resetEditor(); $("skill-status").textContent = ""; };
  $("skill-create-form").onsubmit = (event) => {
    event.preventDefault();
    const scope = editing || target();
    const name = $("skill-name").value.trim();
    const content = $("skill-content").value;
    const description = $("skill-description").value.trim();
    action(async () => {
      if (editing) await request(base() + `/skill-library/${encodeURIComponent(name)}`, { method: "PUT", body: JSON.stringify({ content, scope: scope.scope, project_path: scope.project_path }) });
      else await post(base() + "/skill-library", { name, description, content, ...scope });
      $("skill-status").textContent = `已保存 ${name}。可在左侧选择用于下一轮。`;
      await refreshInstalled();
    });
  };
  async function install(item, button) {
    const scope = target();
    await action(async () => {
      $("skill-status").textContent = `正在下载 ${item.name}…`;
      let candidates = [item];
      if (!item.revision) candidates = await post(base() + "/skill-repository", { url: item.url, skill_name: item.name });
      if (candidates.length !== 1) throw new Error("这个仓库有多个同名技能，请从仓库下载页选择文件夹。");
      const candidate = candidates[0];
      await post(base() + "/skill-download", { repository: candidate.repository, revision: candidate.revision, path: candidate.path, name: candidate.name, ...scope });
      button.textContent = "已下载";
      button.dataset.installed = "true";
      $("skill-status").textContent = `已下载 ${candidate.name}，可在左侧选择用于下一轮。`;
      await refreshInstalled();
    });
    if (button.dataset.installed) button.disabled = true;
  }
  function results(container, items) {
    container.replaceChildren();
    for (const item of items) {
      const row = element("div", undefined, "skill-result");
      const text = element("div");
      text.append(element("strong", item.name), element("small", item.path || item.source || item.repository));
      if (typeof item.installs === "number") text.append(element("small", `${item.installs.toLocaleString()} 次安装`));
      const download = element("button", "下载");
      download.type = "button";
      download.onclick = () => install(item, download);
      row.append(text, download);
      container.append(row);
    }
    if (!items.length) container.append(element("p", "没有找到匹配技能，试试其他名称。", "subtle"));
  }
  $("skill-repository-form").onsubmit = (event) => {
    event.preventDefault();
    const url = $("skill-url").value.trim();
    action(async () => {
      $("skill-status").textContent = "正在读取仓库技能…";
      const items = await post(base() + "/skill-repository", { url });
      results($("skill-repository-results"), items);
      $("skill-status").textContent = `找到 ${items.length} 个技能。选择要下载的技能。`;
    });
  };
  $("skill-search-form").onsubmit = (event) => {
    event.preventDefault();
    const query = $("skill-query").value.trim();
    action(async () => {
      $("skill-status").textContent = "正在搜索技能…";
      const data = await request(base() + "/skill-search?q=" + encodeURIComponent(query));
      results($("skill-search-results"), data.results);
      $("skill-search-note").textContent = data.notice || `结果来自 ${data.source}。`;
      $("skill-status").textContent = "";
    });
  };
  return async () => {
    if (busy) { notify("技能操作仍在进行，完成后可以重新打开。"); return; }
    const detail = getDetail();
    if (!detail || detail.running) return;
    sid = detail.session.session_id;
    scopes = [{ scope: "global", project_path: null }];
    for (const entry of detail.directories.filter((entry) => entry.valid)) scopes.push({ scope: "project", project_path: entry.path });
    $("skill-scope").replaceChildren();
    scopes.forEach((scope, index) => {
      const option = element("option", scope.scope === "global" ? "所有会话" : "项目：" + scope.project_path.split(/[\\/]/).filter(Boolean).at(-1));
      option.value = index;
      $("skill-scope").append(option);
    });
    resetEditor();
    tab("create");
    $("skill-status").textContent = "";
    $("skill-dialog").showModal();
    await action(refreshInstalled);
  };
}

import { request, post } from "./api.js";
import { element } from "./messages.js";
import { icon } from "./icons.js";

const $ = (id) => document.getElementById(id);

export function directoryPicker(getDetail, onChanged) {
  let current = null, sid = null, generation = 0;
  function renderList() {
    const query = $("directory-filter").value.toLocaleLowerCase();
    const list = $("directory-list");
    list.replaceChildren();
    for (const item of current.entries.filter((entry) => entry.name.toLocaleLowerCase().includes(query))) {
      const button = element("button", undefined, "folder-row");
      button.type = "button";
      button.append(icon("folder"), element("span", item.name), icon("chevron-right"));
      button.onclick = () => browse(item.path);
      list.append(button);
    }
    if (!list.children.length) list.append(element("p", query ? "没有匹配的文件夹" : "这个文件夹没有子文件夹", "browser-empty"));
  }
  async function browse(path) {
    const version = ++generation;
    current = null;
    $("directory-error").textContent = "";
    $("directory-confirm").disabled = true;
    $("directory-list").replaceChildren(element("p", "正在读取文件夹…", "browser-empty"));
    try {
      const data = await request(`/sessions/${sid}/directory-browser${path ? "?path=" + encodeURIComponent(path) : ""}`);
      if (version !== generation || !$("directory-dialog").open) return;
      current = data;
      $("directory-filter").value = "";
      $("directory-shortcuts").replaceChildren();
      for (const entry of data.shortcuts) {
        const button = element("button", entry.name, entry.path === data.path ? "active" : "");
        button.type = "button";
        button.onclick = () => browse(entry.path);
        $("directory-shortcuts").append(button);
      }
      const crumbs = $("directory-breadcrumbs");
      crumbs.replaceChildren();
      if (data.parent) {
        const up = element("button", undefined, "icon-only");
        up.type = "button";
        up.setAttribute("aria-label", "上一级文件夹");
        up.append(icon("arrow-up"));
        up.onclick = () => browse(data.parent);
        crumbs.append(up);
      }
      const separator = data.path.includes("\\") ? "\\" : "/";
      const parts = data.path.split(separator).filter(Boolean);
      const root = separator === "/" ? "/" : parts.shift() + "\\";
      let full = root;
      for (const part of [root, ...parts]) {
        if (part !== root) full = full.replace(/[\\/]$/, "") + separator + part;
        const target = full;
        const button = element("button", part);
        button.type = "button";
        button.onclick = () => browse(target);
        crumbs.append(button);
      }
      $("directory-selection").replaceChildren(element("strong", data.path), element("small", data.reason || "所选文件夹将作为当前会话的工作目录。"));
      $("directory-confirm").disabled = !data.selectable;
      if (data.truncated) $("directory-error").textContent = "文件夹较多，当前显示前 1000 个。";
      renderList();
    } catch (error) {
      if (version !== generation) return;
      $("directory-error").textContent = error.message;
      $("directory-list").replaceChildren();
      const retry = element("button", "返回个人文件夹");
      retry.type = "button";
      retry.onclick = () => browse();
      $("directory-list").append(retry);
    }
  }
  $("directory-filter").oninput = () => current && renderList();
  $("directory-form").onsubmit = async (event) => {
    event.preventDefault();
    if (!current?.selectable || $("directory-confirm").disabled) return;
    const sessionId = sid;
    $("directory-confirm").disabled = true;
    try {
      const already = getDetail().directories.some((entry) => entry.path === current.path && entry.valid);
      await post(`/sessions/${sessionId}/directories`, { action: already ? "select" : "add", path: current.path });
      $("directory-dialog").close();
      await onChanged(sessionId);
    } catch (error) {
      $("directory-error").textContent = error.message;
      $("directory-confirm").disabled = false;
    }
  };
  $("directory-dialog").addEventListener("close", () => { generation++; });
  return () => {
    const detail = getDetail();
    if (!detail || detail.running) return;
    sid = detail.session.session_id;
    current = null;
    $("directory-selection").replaceChildren();
    $("directory-error").textContent = "";
    $("directory-dialog").showModal();
    browse(detail.directories.find((entry) => entry.active)?.path);
  };
}

import { element } from "../messages.js";
export const $ = (id) => document.getElementById(id);
export const pretty = (value) => JSON.stringify(value, null, 2);
export function time(value) {
  return value ? new Date(value).toLocaleString("zh-CN") : "未知";
}
export function duration(value) {
  return value == null ? "未知" : `${(value / 1000).toFixed(2)} 秒`;
}
export function empty(container, text) {
  container.replaceChildren(element("p", text, "empty"));
}
export function disclosure(title, content) {
  const item = element("details", undefined, "record-item");
  item.append(
    element("summary", title),
    element("pre", typeof content === "string" ? content : pretty(content)),
  );
  return item;
}
export function download(value, filename) {
  const url = URL.createObjectURL(
    new Blob([pretty(value)], { type: "application/json" }),
  );
  const link = element("a");
  link.href = url;
  link.download = filename;
  link.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

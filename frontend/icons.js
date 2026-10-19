// Small, consistent line icons. No brand symbol or external icon dependency.
const paths = {
  plus: ["M12 5v14", "M5 12h14"],
  "arrow-up": ["M12 19V5", "m6 11 6-6 6 6"],
  "chevron-right": ["m9 6 6 6-6 6"],
  "panel-left": ["M4 4h16v16H4z", "M9 4v16"],
  "panel-right": ["M4 4h16v16H4z", "M15 4v16"],
  folder: ["M3 7V5h6l2 2h10v12H3z"],
  file: ["M6 3h8l4 4v14H6z", "M14 3v5h4", "M9 12h6", "M9 16h6"],
  sparkles: ["m12 3 2.5 6.5L21 12l-6.5 2.5L12 21l-2.5-6.5L3 12l6.5-2.5z"],
  close: ["m6 6 12 12", "M18 6 6 18"],
  stop: ["M7 7h10v10H7z"],
};
export function icon(name) {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  for (const [key, value] of Object.entries({
    viewBox: "0 0 24 24",
    fill: "none",
    stroke: "currentColor",
    "stroke-width": "1.5",
    "stroke-linecap": "round",
    "stroke-linejoin": "round",
    "aria-hidden": "true",
  }))
    svg.setAttribute(key, value);
  for (const d of paths[name] || []) {
    const path = document.createElementNS(svg.namespaceURI, "path");
    path.setAttribute("d", d);
    svg.append(path);
  }
  return svg;
}
export function hydrateIcons() {
  for (const node of document.querySelectorAll("[data-icon]"))
    node.replaceChildren(icon(node.dataset.icon));
}

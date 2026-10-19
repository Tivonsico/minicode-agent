/* Model-defined choices; each submitted run keeps its own reasoning level. */
const select = document.getElementById("reasoning-effort");
export function reasoningSelector(settings, model) {
  const levels = settings?.levels || [];
  select.replaceChildren();
  for (const level of levels) {
    const option = document.createElement("option");
    option.value = level.value;
    option.textContent = level.label;
    select.append(option);
  }
  select.hidden = !levels.length;
  const key = `minicode.reasoning.${model}`;
  let saved;
  try {
    saved = localStorage.getItem(key);
  } catch {
    /* Storage is optional. */
  }
  select.value = levels.some((level) => level.value === saved)
    ? saved
    : settings?.default || "";
  select.onchange = () => {
    try {
      localStorage.setItem(key, select.value);
    } catch {
      /* Keep the current page choice. */
    }
  };
  return () => (levels.length ? select.value : undefined);
}

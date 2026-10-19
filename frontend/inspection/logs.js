import { request } from "../api.js";
import { element } from "../messages.js";
import { $, disclosure, empty, time } from "./shared.js";

export function logViewer(getSession, handleError) {
  let cursor = 0;
  async function load(reset = false) {
    if (reset) {
      cursor = 0;
      $("log-records").replaceChildren();
    }
    const kind = $("log-kind").value;
    const result = await request(
      `/sessions/${getSession()}/logs?after=${cursor}&limit=100${kind ? `&kind=${encodeURIComponent(kind)}` : ""}`,
    );
    for (const event of result.events) {
      const row = disclosure(
        `#${event.seq}  ${time(event.time)}  ${event.type}`,
        event,
      );
      if (event.input_ref) {
        const button = element("button", "查看请求输入", "text-button");
        button.onclick = () =>
          handleError(async () => {
            const input = await request(
              `/sessions/${getSession()}/inputs?ref=${encodeURIComponent(event.input_ref)}`,
            );
            row.append(disclosure("实际请求内容（已脱敏）", input));
            button.remove();
          });
        row.append(button);
      }
      $("log-records").append(row);
    }
    cursor = result.next;
    $("log-more").hidden = !result.has_more;
    if (!cursor)
      empty($("log-records"), "还没有日志。发送任务后，运行过程会记录在这里。");
  }
  $("log-kind").onchange = () => handleError(() => load(true));
  $("log-more").onclick = () => handleError(() => load());
  return () => load(true);
}

const token = document.querySelector('meta[name="minicode-token"]').content;
export async function request(path, options = {}) {
  const response = await fetch(`/api${path}`, {
    ...options,
    headers: {
      "X-Minicode-Token": token,
      "Content-Type": "application/json",
      ...options.headers,
    },
  });
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    throw new Error(
      typeof body.detail === "string"
        ? body.detail
        : Array.isArray(body.detail)
          ? body.detail.map((error) => error.msg).join("；")
          : `请求失败 (${response.status})`,
    );
  }
  return response.json();
}
export const post = (path, body = {}) =>
  request(path, { method: "POST", body: JSON.stringify(body) });
export async function events(sid, after, signal, onEvent) {
  const response = await fetch(`/api/sessions/${sid}/events?after=${after}`, {
    headers: { "X-Minicode-Token": token },
    signal,
  });
  if (!response.ok || !response.body) throw new Error("会话连接中断");
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      let boundary;
      while ((boundary = buffer.indexOf("\n\n")) >= 0) {
        const frame = buffer.slice(0, boundary);
        buffer = buffer.slice(boundary + 2);
        const line = frame
          .split("\n")
          .find((part) => part.startsWith("data: "));
        if (line) onEvent(JSON.parse(line.slice(6)));
      }
    }
  } finally {
    reader.releaseLock();
  }
}

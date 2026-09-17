"""Project configuration; secrets never appear in dataclass representations."""

import math
import os
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

from dotenv import dotenv_values


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class Config:
    root: Path
    api_key: str = field(repr=False)
    base_url: str = "https://open.bigmodel.cn/api/paas/v4/"
    model: str = "GLM-5.3-Flash"
    context_window: int = 0
    max_output_tokens: int = 0
    shell: str = "powershell.exe"
    request_timeout: float = 120.0

    def __post_init__(self):
        object.__setattr__(self, "root", Path(self.root).resolve())
        if not self.root.is_dir():
            raise ConfigError("项目目录不存在。")
        if not self.api_key.strip() or self.api_key.strip() in {"your-api-key", "replace-me"}:
            raise ConfigError("请在项目 .env 中设置 OPENAI_API_KEY。")
        if not self.model.strip():
            raise ConfigError("请设置服务端支持的 MODEL。")
        parsed = urlparse(self.base_url)
        if (
            parsed.scheme not in {"https", "http"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise ConfigError("OPENAI_BASE_URL 必须是无凭据、无查询参数的 HTTP(S) 地址。")
        if parsed.path.rstrip("/").endswith("chat/completions"):
            raise ConfigError("OPENAI_BASE_URL 应填写 /api/paas/v4/，不要包含 chat/completions。")
        if not 0 < self.max_output_tokens < self.context_window:
            raise ConfigError("请配置有效预算：0 < MAX_OUTPUT_TOKENS < CONTEXT_WINDOW。")
        if not math.isfinite(self.request_timeout) or self.request_timeout <= 0:
            raise ConfigError("REQUEST_TIMEOUT 必须大于 0。")


def load_config(root: Path | str | None = None, env_file: Path | None = None) -> Config:
    root = Path(root or Path.cwd()).resolve()
    values = {**dotenv_values(env_file or root / ".env"), **os.environ}
    try:
        window = int(values.get("CONTEXT_WINDOW") or 0)
        output = int(values.get("MAX_OUTPUT_TOKENS") or 0)
        timeout = float(values.get("REQUEST_TIMEOUT") or 120)
    except (ValueError, TypeError):
        raise ConfigError(
            "CONTEXT_WINDOW、MAX_OUTPUT_TOKENS 和 REQUEST_TIMEOUT 必须为数值。"
        ) from None
    return Config(
        root=root,
        api_key=values.get("OPENAI_API_KEY") or "",
        base_url=values.get("OPENAI_BASE_URL") or "https://open.bigmodel.cn/api/paas/v4/",
        model=values.get("MODEL") or "GLM-5.3-Flash",
        context_window=window,
        max_output_tokens=output,
        shell=values.get("SHELL_EXECUTABLE") or "powershell.exe",
        request_timeout=timeout,
    )

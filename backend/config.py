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
    base_url: str = "https://api.openai.com/v1"
    model: str = "gpt-4.1"
    context_window: int = 0
    max_output_tokens: int = 0
    shell: str = "powershell.exe" if os.name == "nt" else "/bin/sh"
    request_timeout: float = 120.0
    catalog_provider: str = ""
    model_max_output: int | None = None
    model_max_input: int | None = None
    capability_source: str = ""
    storage_mode: str = "local"
    mysql_host: str = ""
    mysql_port: int = 3306
    mysql_database: str = ""
    mysql_user: str = ""
    mysql_password: str = field(default="", repr=False)
    mysql_ssl_ca: str = ""
    mysql_pool_size: int = 8
    experimental_history_recall: bool = False

    def __post_init__(self):
        object.__setattr__(self, "root", Path(self.root).resolve())
        if self.storage_mode not in {"local", "mysql"}:
            raise ConfigError("MINICODE_STORAGE_MODE 必须为 local 或 mysql。")
        if self.storage_mode == "mysql" and (
            not self.mysql_host or not self.mysql_database or not self.mysql_user
            or not 1 <= self.mysql_port <= 65535 or not 1 <= self.mysql_pool_size <= 64
        ):
            raise ConfigError("MySQL 配置不完整：请设置主机、库名、用户名及有效端口/连接数。")
        if os.name != "nt" and Path(self.shell).name.lower() in {"powershell.exe", "cmd.exe"}:
            object.__setattr__(self, "shell", "/bin/sh")
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
            raise ConfigError("OPENAI_BASE_URL 应填写 API 根地址，不要包含 chat/completions。")
        if self.context_window < 0 or self.max_output_tokens < 0:
            raise ConfigError("模型预算必须为正数。")
        if self.context_window and not 0 < self.max_output_tokens < self.context_window:
            raise ConfigError("MAX_OUTPUT_TOKENS 必须小于模型上下文窗口。")
        if not math.isfinite(self.request_timeout) or self.request_timeout <= 0:
            raise ConfigError("REQUEST_TIMEOUT 必须大于 0。")


def load_config(root: Path | str | None = None, env_file: Path | None = None) -> Config:
    root = Path(root or Path.cwd()).resolve()
    from backend.storage.settings import ENVIRONMENT, read_settings

    saved = read_settings(root)
    values = {**dotenv_values(env_file or root / ".env"),
              **{ENVIRONMENT[key]: value for key, value in saved.items()}, **os.environ}
    try:
        timeout = float(values.get("REQUEST_TIMEOUT") or 120)
        mysql_port = int(values.get("MINICODE_MYSQL_PORT") or 3306)
        mysql_pool_size = int(values.get("MINICODE_MYSQL_POOL_SIZE") or 8)
    except (ValueError, TypeError):
        raise ConfigError("REQUEST_TIMEOUT、MySQL 端口和连接数必须为数值。") from None
    return Config(
        root=root,
        api_key=values.get("OPENAI_API_KEY") or "",
        base_url=values.get("OPENAI_BASE_URL") or "https://api.openai.com/v1",
        model=values.get("MODEL") or "",
        shell=values.get("SHELL_EXECUTABLE")
        or ("powershell.exe" if os.name == "nt" else "/bin/sh"),
        request_timeout=timeout,
        catalog_provider=values.get("MODEL_CATALOG_PROVIDER") or "",
        storage_mode=values.get("MINICODE_STORAGE_MODE") or "local",
        mysql_host=values.get("MINICODE_MYSQL_HOST") or "",
        mysql_port=mysql_port,
        mysql_database=values.get("MINICODE_MYSQL_DATABASE") or "",
        mysql_user=values.get("MINICODE_MYSQL_USER") or "",
        mysql_password=values.get("MINICODE_MYSQL_PASSWORD") or "",
        mysql_ssl_ca=values.get("MINICODE_MYSQL_SSL_CA") or "",
        mysql_pool_size=mysql_pool_size,
    )

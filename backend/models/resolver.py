"""Resolve capabilities once before starting the runtime."""

from dataclasses import replace

import httpx

from backend.config import ConfigError

from .cache import CapabilityCache
from .catalog import from_catalog
from .metadata import from_endpoint


def resolve_config(config, *, transport=None, refresh=False):
    # Explicit limits are available to embedded callers and offline tests,
    # never loaded from CONTEXT_WINDOW in the environment.
    if config.context_window:
        return config
    cache = CapabilityCache(config)
    capabilities = None if refresh else cache.read()
    if capabilities is None:
        with httpx.Client(transport=transport, follow_redirects=False) as client:
            capabilities = from_endpoint(config, client)
            if capabilities is None:
                try:
                    capabilities = from_catalog(config, client)
                except (httpx.HTTPError, ValueError):
                    pass
        if capabilities is None:
            raise ConfigError(
                "无法确认该 API 地址和模型的上下文窗口：服务端未提供有效元数据，"
                "Models.dev 也未找到对应记录或暂时不可用。"
                "自定义网关可设置 MODEL_CATALOG_PROVIDER 指定目录中的服务商；"
                "未收录的模型需要服务端提供 context_window/context_length。"
            )
        cache.write(capabilities)
    # Output reserve is an application policy, not a claimed model maximum.
    output = config.max_output_tokens or min(16_384, capabilities.context_window // 4)
    if capabilities.max_output is not None and output > capabilities.max_output:
        if config.max_output_tokens:
            raise ConfigError("MAX_OUTPUT_TOKENS 超过解析出的模型输出上限。")
        output = capabilities.max_output
    return replace(
        config,
        context_window=capabilities.context_window,
        max_output_tokens=output,
        model_max_output=capabilities.max_output,
        model_max_input=capabilities.max_input,
        capability_source=capabilities.source,
    )

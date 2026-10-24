"""Resolve capabilities once before starting the runtime."""

from dataclasses import replace

import httpx

from backend.config import ConfigError

from .cache import CapabilityCache
from .catalog import from_catalog
from .metadata import from_endpoint


def resolve_config(config, *, transport=None, refresh=False):
    # Embedded callers may supply an already-resolved configuration. Normal
    # startup never loads token limits from environment variables.
    if config.context_window:
        return config
    cache = CapabilityCache(config)
    capabilities = None if refresh else cache.read()
    if capabilities is None or capabilities.max_output is None:
        with httpx.Client(transport=transport, follow_redirects=False) as client:
            capabilities = from_endpoint(config, client)
            if capabilities is None or capabilities.max_output is None:
                try:
                    catalog = from_catalog(config, client)
                    if capabilities is None:
                        capabilities = catalog
                    elif catalog is not None and catalog.max_output is not None:
                        capabilities = replace(
                            capabilities,
                            max_output=catalog.max_output,
                            source=capabilities.source + ";output=" + catalog.source,
                        )
                except (httpx.HTTPError, ValueError):
                    pass
        if capabilities is None:
            raise ConfigError(
                "无法确认该 API 地址和模型的上下文窗口：服务端未提供有效元数据，"
                "Models.dev 也未找到对应记录或暂时不可用。"
                "自定义网关可设置 MODEL_CATALOG_PROVIDER 指定目录中的服务商；"
                "未收录的模型需要服务端提供 context_window/context_length。"
            )
        if capabilities.max_output is not None:
            cache.write(capabilities)
    if capabilities.max_output is None:
        raise ConfigError(
            "无法确认该模型的最大输出 token 上限：模型元数据未声明 max_output_tokens/"
            "max_completion_tokens，匹配的模型目录也未提供有效输出上限；不使用人工默认值。"
        )
    output = capabilities.max_output
    return replace(
        config,
        context_window=capabilities.context_window,
        max_output_tokens=output,
        model_max_output=capabilities.max_output,
        model_max_input=capabilities.max_input,
        capability_source=capabilities.source,
    )

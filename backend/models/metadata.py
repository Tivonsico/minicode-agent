"""Bounded metadata reads; credentials go only to the configured endpoint."""

import json
from dataclasses import replace
from urllib.parse import quote

import httpx

from backend.config import ConfigError

from .capabilities import ModelCapabilities


def read_json(client, url, *, headers=None, timeout=5):
    with client.stream("GET", url, headers=headers, timeout=timeout) as response:
        if response.status_code != 200:
            return response.status_code, None
        body = bytearray()
        for part in response.iter_bytes():
            body.extend(part)
            if len(body) > 20_000_000:
                raise ValueError("Metadata response too large")
        return 200, json.loads(body)


def from_endpoint(config, client):
    base = config.base_url.rstrip("/")
    headers = {"Authorization": "Bearer " + config.api_key}
    urls = [base + "/models/" + quote(config.model, safe=""), base + "/models"]
    result = None
    for url in urls:
        try:
            status, data = read_json(client, url, headers=headers)
            if status == 401:
                raise ConfigError("模型元数据接口认证失败，请检查 OPENAI_API_KEY。")
            if status != 200 or not isinstance(data, dict):
                continue
            items = data.get("data") if url == urls[1] else [data]
            if not isinstance(items, list):
                continue
            for item in items:
                if isinstance(item, dict) and item.get("id") == config.model:
                    capability = ModelCapabilities.from_metadata(item, "endpoint:" + url)
                    if capability:
                        if result is None:
                            result = capability
                        else:
                            result = replace(
                                result,
                                max_output=result.max_output or capability.max_output,
                                max_input=result.max_input or capability.max_input,
                                source=result.source + ";limits=" + capability.source,
                            )
                        if result.max_output is not None:
                            return result
        except (httpx.HTTPError, ValueError) as exc:
            if isinstance(exc, ConfigError):
                raise
            continue
    return result

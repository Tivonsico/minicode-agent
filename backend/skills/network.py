"""Public skill downloads with bounded reads and no model credentials."""

import httpx

from backend.tools.base import ToolError


async def fetch(client, url, *, params=None, limit=16_000_000):
    try:
        async with client.stream(
            "GET", url, params=params, timeout=httpx.Timeout(10, connect=4)
        ) as response:
            if response.status_code != 200:
                message = "下载服务暂不可用，请稍后重试。"
                if response.status_code == 404:
                    message = "仓库、分支或技能不存在；目前支持公开 GitHub 仓库。"
                elif response.status_code in {403, 429}:
                    message = "公开接口请求受限，请稍后重试。"
                raise ToolError(message, "skill_network")
            result = bytearray()
            async for block in response.aiter_bytes():
                result.extend(block)
                if len(result) > limit:
                    raise ToolError("仓库文件过大，请选择较小的技能仓库。", "skill_limit")
            return bytes(result)
    except httpx.HTTPError as error:
        raise ToolError("无法连接技能服务，请检查网络后重试。", "skill_network") from error


async def fetch_json(client, url, *, params=None):
    import json

    try:
        return json.loads(await fetch(client, url, params=params))
    except (ValueError, UnicodeError) as error:
        raise ToolError("技能服务返回了无效数据。", "skill_network") from error

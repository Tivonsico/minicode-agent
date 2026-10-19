"""Name search through Skills.sh, with public curated repositories as fallback."""

from urllib.parse import urlencode

from backend.tools.base import ToolError

from .network import fetch_json
from .repository import discover, repository_source


async def search(client, query):
    query = query.strip()
    if not 2 <= len(query) <= 100:
        raise ToolError("请输入 2–100 个字符的技能名称或关键词。", "skill_search")
    try:
        data = await fetch_json(
            client, "https://skills.sh/api/search?" + urlencode({"q": query, "limit": 20})
        )
        results = []
        for item in data.get("skills", [])[:20]:
            source, name = item.get("source", ""), item.get("name", "")
            if not isinstance(source, str) or not isinstance(name, str):
                continue
            url = "https://github.com/" + source
            try:
                repository_source(url)
            except ToolError:
                continue
            results.append(
                {
                    "name": name[:100],
                    "url": url,
                    "source": source,
                    "installs": item.get("installs", 0),
                }
            )
        return {"source": "Skills.sh", "results": results, "notice": ""}
    except (ToolError, AttributeError, TypeError):
        results = []
        available = False
        for repo in ("anthropics/skills", "openai/skills", "vercel-labs/agent-skills"):
            try:
                items = await discover(client, "https://github.com/" + repo)
                available = True
                results += [
                    {"name": item["name"], "url": item["url"], "source": repo, "installs": None}
                    for item in items
                    if query.casefold() in item["name"].casefold()
                ]
            except ToolError:
                continue
        if not available:
            raise ToolError(
                "技能搜索服务不可用，请稍后重试，或直接粘贴仓库链接。", "skill_network"
            ) from None
        return {
            "source": "公开技能目录",
            "results": results[:30],
            "notice": "Skills.sh 暂不可用，以下结果来自公开技能目录。",
        }

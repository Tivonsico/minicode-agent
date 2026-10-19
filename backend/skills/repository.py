"""Discover public GitHub skills and download one commit-pinned skill bundle."""

import io
import re
import stat
import zipfile
from pathlib import PurePosixPath
from urllib.parse import quote, unquote, urlparse

from backend.tools.base import ToolError

from .network import fetch, fetch_json


def repository_source(url):
    parsed = urlparse(url.strip())
    if (
        parsed.scheme != "https"
        or parsed.netloc != "github.com"
        or parsed.query
        or parsed.fragment
        or parsed.username
        or parsed.password
    ):
        raise ToolError("请输入公开 GitHub 仓库或技能文件夹的 HTTPS 链接。", "skill_source")
    parts = [unquote(part) for part in parsed.path.strip("/").split("/")]
    if len(parts) < 2 or any(
        not re.fullmatch(r"[\w.-]+", part) or part in {".", ".."} for part in parts
    ):
        raise ToolError("仓库链接格式不正确。", "skill_source")
    owner, repo = parts[:2]
    repo = repo.removesuffix(".git")
    ref, path = None, ""
    if len(parts) > 2:
        if len(parts) < 4 or parts[2] not in {"tree", "blob"}:
            raise ToolError("链接应指向仓库首页或 tree 分支下的技能文件夹。", "skill_source")
        ref = parts[3]
        path = "/".join(parts[4:])
        if path.endswith("SKILL.md"):
            path = path.removesuffix("SKILL.md").rstrip("/")
    return f"{owner}/{repo}", ref, path


async def discover(client, url, *, skill_name=None):
    repository, ref, prefix = repository_source(url)
    if not ref:
        info = await fetch_json(client, f"https://api.github.com/repos/{repository}")
        ref = info.get("default_branch") if isinstance(info, dict) else None
    if not isinstance(ref, str) or not ref:
        raise ToolError("无法读取仓库默认分支。", "skill_source")
    commit = await fetch_json(
        client, f"https://api.github.com/repos/{repository}/commits/{quote(ref, safe='')}"
    )
    revision = commit.get("sha", "") if isinstance(commit, dict) else ""
    if not isinstance(revision, str) or not re.fullmatch(r"[a-f0-9]{40}", revision):
        raise ToolError("无法确认仓库版本。", "skill_source")
    tree = await fetch_json(
        client,
        f"https://api.github.com/repos/{repository}/git/trees/{revision}",
        params={"recursive": "1"},
    )
    if (
        not isinstance(tree, dict)
        or not isinstance(tree.get("sha"), str)
        or not re.fullmatch(r"[a-f0-9]{40}", tree["sha"])
        or not isinstance(tree.get("tree"), list)
    ):
        raise ToolError("仓库目录数据无效。", "skill_source")
    if tree.get("truncated"):
        raise ToolError("仓库目录过大，无法完整列出技能。", "skill_limit")
    results = []
    for item in tree.get("tree", []):
        if not isinstance(item, dict) or item.get("type") != "blob" or item.get("mode") != "100644":
            continue
        if not isinstance(item.get("path"), str):
            continue
        raw = PurePosixPath(item["path"])
        if raw.name != "SKILL.md" or ".." in raw.parts or raw.is_absolute():
            continue
        path = "" if str(raw.parent) == "." else str(raw.parent)
        if prefix and path != prefix and not path.startswith(prefix + "/"):
            continue
        name = raw.parent.name if path else repository.split("/")[-1]
        results.append(
            {"name": name, "path": path, "repository": repository, "revision": revision, "url": url}
        )
    if skill_name:
        matches = [item for item in results if item["name"].casefold() == skill_name.casefold()]
        if not matches and len(results) == 1 and results[0]["path"] == "":
            results[0]["name"] = skill_name
            matches = results
        results = matches
    if not results:
        raise ToolError("没有找到这个技能的 SKILL.md；可以从仓库链接查看全部技能。", "skill_source")
    return results[:200]


async def bundle(client, repository, revision, path):
    if not re.fullmatch(r"[\w.-]+/[\w.-]+", repository) or not re.fullmatch(
        r"[a-f0-9]{40}", revision
    ):
        raise ToolError("技能来源无效，请重新查看仓库。", "skill_source")
    if any(part in {".", ".."} for part in repository.split("/")):
        raise ToolError("技能来源无效。", "skill_source")
    requested = PurePosixPath(path)
    if requested.is_absolute() or ".." in requested.parts or "\\" in path or ":" in path:
        raise ToolError("技能目录无效。", "skill_path")
    data = await fetch(client, f"https://codeload.github.com/{repository}/zip/{revision}")
    files = {}
    total = 0
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            prefix = path.strip("/")
            for item in archive.infolist():
                if "//" in item.filename:
                    raise ToolError("技能包含无效路径。", "skill_path")
                parts = PurePosixPath(item.filename).parts
                if len(parts) < 2:
                    continue
                relative = "/".join(parts[1:])
                if prefix:
                    if not relative.startswith(prefix + "/"):
                        continue
                    relative = relative[len(prefix) + 1 :]
                if item.is_dir():
                    continue
                raw = PurePosixPath(relative)
                mode = item.external_attr >> 16
                if (
                    raw.is_absolute()
                    or ".." in raw.parts
                    or ":" in relative
                    or "\\" in relative
                    or stat.S_ISLNK(mode)
                    or any(
                        part in {".git", ".minicode"} or part.startswith(".env")
                        for part in raw.parts
                    )
                ):
                    raise ToolError("技能包含不安全的路径或链接，下载已停止。", "skill_path")
                total += item.file_size
                if len(files) >= 1000 or item.file_size > 2_000_000 or total > 10_000_000:
                    raise ToolError("技能资源超过大小限制。", "skill_limit")
                if relative in files:
                    raise ToolError("技能包含重复文件路径。", "skill_path")
                files[relative] = archive.read(item)
    except (zipfile.BadZipFile, RuntimeError, OSError) as error:
        raise ToolError("无法读取技能下载文件。", "skill_source") from error
    if "SKILL.md" not in files:
        raise ToolError("选中的目录没有 SKILL.md。", "skill_source")
    return files

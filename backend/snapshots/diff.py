"""Git line counts and readable patches derived only from retained file bytes."""

from backend.permissions.paths import check_content
from backend.tools.base import ToolError

MAX_DIFF_CHARS = 200000


def compare(repository, before, after, skipped=()):
    old, new = repository.files(before), repository.files(after)
    files, added, deleted = [], 0, 0
    excluded = {item["path"]: item["reason"] for item in skipped}
    for name in sorted(old.keys() | new.keys() | excluded.keys()):
        a, b = old.get(name), new.get(name)
        if name in excluded:
            files.append({"path": name, "status": "unsupported", "added": None,
                          "deleted": None, "reason": excluded[name], "diff": ""})
            continue
        if a == b:
            continue
        item = {"path": name, "status": "added" if a is None else "deleted" if b is None
                else "modified", "added": None, "deleted": None, "diff": "",
                "mode_changed": bool(a and b and a["mode"] != b["mode"])}
        raw_a, raw_b = repository.content(a), repository.content(b)
        try:
            if b"\0" in raw_a or b"\0" in raw_b:
                raise UnicodeError()
            check_content(raw_a.decode("utf-8"))
            check_content(raw_b.decode("utf-8"))
        except (UnicodeError, ToolError):
            item["reason"] = "二进制或非 UTF-8 文件；可恢复原始内容，不统计行数"
        else:
            options = ("diff", "--no-ext-diff", "--no-textconv", "--no-renames", "--no-color", "--text")
            pathspec = ":(literal)" + name
            counts = repository._git(*options, "--numstat", "-z", before, after,
                                     "--", pathspec).split(b"\t", 2)
            plus, minus = (int(counts[0]), int(counts[1])) if len(counts) == 3 else (0, 0)
            patch = repository._git(*options, "--patch", before, after, "--", pathspec).decode("utf-8", errors="replace")
            item.update(added=plus, deleted=minus, diff=patch[:MAX_DIFF_CHARS],
                        truncated=len(patch) > MAX_DIFF_CHARS)
            added += plus
            deleted += minus
        files.append(item)
    return {"files": files, "added": added, "deleted": deleted, "pending": False}

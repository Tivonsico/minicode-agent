"""Reviewed restore plans with compare-before-write and a pinned safety copy."""

import hashlib
import json
import stat

from backend.permissions.paths import check_content
from backend.tools.base import ToolError

from .diff import compare
from .paths import read_regular, replace_file
from .repository import MAX_FILE_BYTES


def preview(repository, round_id):
    record = repository.record(round_id)
    if not record["after"] or record["status"] in {"running", "unknown"}:
        raise ValueError("该轮尚未结束或操作状态待核实，不能恢复")
    current, skipped = repository.capture()
    target = repository.files(record["before"])
    live = repository.files(current)
    ended = repository.files(record["after"])
    skipped.extend({"path": name, "reason": "当前被 .gitignore 排除"}
                   for name in repository.ignored(target))
    excluded = {s["path"] for s in record["skipped_before"] + skipped}
    changes = compare(repository, current, record["before"],
                      record["skipped_before"] + skipped)
    conflicts, blocked = [], []
    for name in sorted(target.keys() | live.keys()):
        if name in excluded or target.get(name) == live.get(name):
            continue
        if live.get(name) != ended.get(name):
            conflicts.append(name)
        path = repository.project / name
        if path.is_dir() or any(parent.exists() and not parent.is_dir()
                                for parent in path.parents if parent != repository.project):
            blocked.append(name)
    token = hashlib.sha256(json.dumps([round_id, target, live, skipped], sort_keys=True).encode()).hexdigest()
    return {**changes, "token": token, "conflicts": conflicts, "blocked": blocked,
            "current": current, "excluded": sorted(excluded),
            "warning": "恢复受支持的项目文件；数据库、网络及其他外部操作不会恢复。"}


def _checked_path(repository, name):
    path = repository.project / name
    if any(item.is_symlink() for item in [path, *path.parents]
           if item.is_relative_to(repository.project)):
        raise ValueError("文件路径已变化，请重新预览恢复")
    if repository.policy.resolve(str(path)) != path:
        raise ValueError("文件路径已变化，请重新预览恢复")
    return path


def _matches(repository, path, entry):
    if entry is None:
        return not path.exists()
    if not path.is_file():
        return False
    data, info = read_regular(repository.project, path, MAX_FILE_BYTES)
    check_content(data.decode("utf-8", errors="replace"))
    mode = "100755" if info.st_mode & stat.S_IXUSR else "100644"
    return mode == entry["mode"] and data == repository.content(entry)


def restore(repository, round_id, token, *, accept_conflicts=False, before_effect=None):
    plan = preview(repository, round_id)
    if plan["token"] != token:
        raise ValueError("预览后项目文件发生变化，请重新预览恢复")
    if plan["conflicts"] and not accept_conflicts:
        raise ValueError("存在后续或人工修改，必须明确确认覆盖这些文件")
    if plan["blocked"]:
        raise ValueError("存在文件与目录类型冲突，请先人工处理后重新预览")
    record = repository.record(round_id)
    live, target = repository.files(plan["current"]), repository.files(record["before"])
    names = [name for name in sorted(live.keys() | target.keys())
             if name not in plan["excluded"] and live.get(name) != target.get(name)]
    for name in names:
        if not _matches(repository, _checked_path(repository, name), live.get(name)):
            raise ValueError("文件已变化，请重新预览恢复")
    # Persist backup before the first effect. Keep it even if execution is interrupted.
    backup = repository.begin(record["session_id"], "restore-" + round_id, record["turn_id"],
                              kind="restore")
    backup.update(pinned=True, restores=round_id)
    repository._save(backup)
    if before_effect:
        before_effect(backup)
    try:
        for name in names:
            path = _checked_path(repository, name)
            if not _matches(repository, path, live.get(name)):
                raise ValueError("恢复中检测到文件变化，已停止；恢复前副本已固定保留")
            entry = target.get(name)
            data = repository.content(entry) if entry else None
            if data is not None:
                check_content(data.decode("utf-8", errors="replace"))
            replace_file(repository.project, path, data,
                         0o755 if entry and entry["mode"] == "100755" else 0o644,
                         live.get(name),
                         lambda: _matches(repository, _checked_path(repository, name), live.get(name)))

    except (OSError, ValueError, ToolError):
        repository.finish(backup["id"], "restore_failed", pinned=True)
        raise
    return repository.finish(backup["id"], "restored", pinned=True)

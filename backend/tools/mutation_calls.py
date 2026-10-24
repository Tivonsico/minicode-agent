import difflib
from pathlib import Path

from backend.permissions.paths import check_content, fingerprint
from backend.storage.files import sync_directory

from .base import ToolError, ToolResult


async def execute(executor, name, cid, args):
    if name in {"WriteFile", "EditFile"}:
        path = executor.policy.resolve(args["path"])
        executor._touch_project(path, writing=True)
        existed = path.exists()
        executable = existed and bool(path.stat().st_mode & 0o111)
        if existed and (not path.is_file()):
            raise ToolError("Target is not a regular file.", "not_file")
        if existed:
            executor._verify_read(path)
            before_text = path.read_bytes().decode("utf-8-sig")
            if "\x00" in before_text:
                raise ToolError("Binary file is not supported.", "binary")
            check_content(before_text)
            before_version = fingerprint(path)
        else:
            (before_text, before_version) = ("", None)
        if name == "EditFile":
            if not existed:
                raise ToolError("File does not exist.", "not_file")
            if before_text.count(args["old_text"]) != 1:
                raise ToolError("old_text must match exactly once.", "ambiguous_edit")
            content = before_text.replace(args["old_text"], args["new_text"], 1)
        else:
            content = args["content"]
        check_content(content)
        diff = "".join(
            difflib.unified_diff(
                before_text.splitlines(keepends=True),
                content.splitlines(keepends=True),
                fromfile=executor.policy.display(path) if existed else "/dev/null",
                tofile=executor.policy.display(path),
            )
        )
        grant_version = (
            executor.session_permissions.version if executor.session_permissions else None
        )
        summary = f"{name}: {path}\n{('Modify existing file' if existed else 'Create new file')}; {len(before_text)} -> {len(content)} characters"
        if not await executor._approval(cid, name, args, summary, details=diff):
            return ToolResult(cid, name, "rejected", "User rejected this operation.")
        if executor.session_permissions and executor.session_permissions.version != grant_version:
            raise ToolError("Permissions changed after approval.", "stale_approval")
        if executor.policy.resolve(args["path"]) != path or path.exists() != existed:
            raise ToolError("Target changed after approval.", "stale_approval")
        if existed and fingerprint(path) != before_version:
            raise ToolError("File changed after approval.", "stale_approval")
        executor.mark_running(cid, name)
        executor._write(args["path"], path, content, existed)
        executor.record_text_write(cid, path, content, executable=executable)
        content = f"Saved {executor.policy.display(path)}"
    elif name == "DeleteFile":
        path = executor.policy.resolve(args["path"])
        executor._touch_project(path, writing=True)
        candidate = Path(args["path"])
        candidate = candidate if candidate.is_absolute() else executor.policy.base() / candidate
        if not path.is_file() or candidate.is_symlink():
            raise ToolError("DeleteFile only deletes one regular file.", "not_file")
        version = fingerprint(path)
        check_content(path.read_bytes().decode("utf-8", errors="replace"))
        grant_version = (
            executor.session_permissions.version if executor.session_permissions else None
        )
        if not await executor._approval(
            cid, name, args, f"Delete file: {path}\nVersion: {version[0]}"
        ):
            return ToolResult(cid, name, "rejected", "User rejected this operation.")
        if (
            executor.session_permissions
            and executor.session_permissions.version != grant_version
            or executor.policy.resolve(args["path"]) != path
            or (not path.is_file())
            or (fingerprint(path) != version)
        ):
            raise ToolError("File changed after approval; deletion cancelled.", "stale_approval")
        executor.mark_running(cid, name)
        path.unlink()
        sync_directory(path.parent)
        executor.reads.pop(path, None)
        content = f"Deleted {executor.policy.display(path)}"
    return content

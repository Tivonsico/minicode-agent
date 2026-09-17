"""Validated tool execution with read groups and exclusive mutation barriers."""

import asyncio
import difflib
import fnmatch
import json
import os
import tempfile
import threading
import time
from functools import lru_cache
from pathlib import Path

from .base import ToolError, ToolResult
from .command_guard import check_command
from .paths import PathPolicy, check_content, fingerprint
from .permissions import Action, PermissionPolicy
from .registry import build_registry
from .shell import UTF8_PRELUDE, run_shell


class ToolExecutor:
    def __init__(self, root: Path, output_store, approve, emit=None, session_permissions=None, skills=None):
        from memory.project_assets import ProjectAssets
        from skill import SkillCatalog

        self.root = Path(root).resolve()
        self.output_store = output_store
        self.approve = approve
        self.emit = emit or (lambda *args, **kwargs: None)
        self.session_permissions = session_permissions
        self.skills = skills or SkillCatalog(
            self.root, lambda: session_permissions.roots() if session_permissions else ()
        )
        self.assets = ProjectAssets(self.root)
        self.policy = PathPolicy(
            self.root,
            output_store.allowed_paths,
            (lambda: self.session_permissions.roots()) if session_permissions else (lambda: ()),
            include_root=session_permissions is None,
        )
        self.permissions = PermissionPolicy()
        self.registry = build_registry(self._dispatch)
        self.shell_executable = None
        self.shell_backend = None
        self._lock = asyncio.Lock()
        self.reset_turn()

    def schemas(self):
        return self.registry.schemas()

    def permission_context(self):
        roots = "\n".join(f"- {path}" for path in self.policy.roots()) or "(none; use /allow-dir <absolute path>)"
        shell = (
            "Shell requires approval and runs in a verified Windows sandbox."
            if self.shell_backend is not None and self.shell_backend.available()
            else "Shell is unavailable because its system sandbox has not been verified."
        )
        return (
            "Capabilities: ReadFile, Glob, Grep, WriteFile, EditFile, DeleteFile; session history, "
            "directory grants via /allow-dir, /dirs and /work-dir, global and project skills via /use, "
            "and prompts via /see system_prompt. "
            "Shell is listed as a tool but currently cannot run until the OS sandbox passes verification.\n"
            "Current authorized work directories (active first; relative paths use active):\n"
            f"{roots}\n"
            "Read/search within these directories is automatic. File creation, edits, "
            "deletion, and Shell require approval for every call. Sensitive files stay "
            "blocked for file tools. A rejected approval ends this turn. "
            f"{shell}"
        )

    def reset_turn(self):
        blocked = getattr(self, "pause_reason", None) == "termination_unknown"
        self.reads = {}
        self.rejected = set()
        self._last_failure = None
        self._failure_count = 0
        self.terminal_failed = blocked
        self.pause_reason = "termination_unknown" if blocked else None
        self.active_skills = set()
        self.turn_projects = set()
        self.touched_projects = set()
        self.prompted_projects = set()

    def begin_turn(self, text: str):
        if not self.session_permissions:
            return
        roots = self.session_permissions.roots()
        folded = text.casefold()
        mentioned = [root for root in roots if str(root).casefold() in folded or
                     (len(root.name) >= 3 and root.name.casefold() in folded)]
        self.turn_projects.update(mentioned or roots[:1])

    def current_project_roots(self):
        if not self.session_permissions:
            return ()
        selected = self.turn_projects | self.touched_projects
        return tuple(root for root in self.session_permissions.roots() if root in selected)

    def mark_prompted(self, roots):
        self.prompted_projects.update(roots)

    def _touch_project(self, path, *, writing=False):
        if not self.session_permissions:
            return
        root = self.policy._containing_root(path)
        if root is None:
            return
        self.touched_projects.add(root)
        if writing and root not in self.prompted_projects:
            raise ToolError("Project instructions must be loaded before a write; retry next request.",
                            "project_context")

    def force_skill(self, name: str, scope="global", project_path=None):
        content = self.skills.load(name, scope, project_path)
        if scope == "project":
            self.touched_projects.add(Path(project_path).resolve())
        self.active_skills.add((scope, str(project_path or "").casefold(), name.casefold()))
        return content

    @staticmethod
    def _key(name, args):
        return name, json.dumps(args, sort_keys=True, ensure_ascii=False, separators=(",", ":"))

    def _prepare(self, call):
        name, call_id = "unknown", "unknown"
        try:
            if not isinstance(call, dict):
                raise ToolError("Tool call must be an object.")
            call_id = call.get("id", "unknown")
            if not isinstance(call_id, str) or not call_id:
                raise ToolError("Missing tool call ID.")
            function = call.get("function", {})
            name = function.get("name", "unknown")
            tool = self.registry.get(name)
            raw = function.get("arguments", "{}")
            args = json.loads(raw) if isinstance(raw, str) else raw
            error = tool.validateInput(args)
            if error:
                raise ToolError(error, "schema")
            return call_id, name, args, tool, None
        except (ToolError, json.JSONDecodeError, TypeError, AttributeError) as error:
            error_type = getattr(error, "error_type", "invalid_json")
            return (
                call_id,
                name,
                {},
                None,
                ToolResult(call_id, name, "failed", str(error), error_type),
            )

    async def execute_batch(self, calls):
        async with self._lock:
            prepared = [self._prepare(call) for call in calls]
            results = []
            i = 0
            while i < len(prepared):
                if self.terminal_failed:
                    for cid, name, *_ in prepared[i:]:
                        result = ToolResult(
                            cid,
                            name,
                            "cancelled",
                            "Not executed: " + str(self.pause_reason),
                            "paused",
                        )
                        self.emit("tool_result", **result.to_dict())
                        results.append(result)
                    break
                entry = prepared[i]
                if entry[3] and entry[3].isConcurrencySafe(entry[2]):
                    group = []
                    while i < len(prepared) and len(group) < 4:
                        item = prepared[i]
                        if not item[3] or not item[3].isConcurrencySafe(item[2]):
                            break
                        group.append(item)
                        i += 1
                    tasks = [asyncio.create_task(self._execute(item)) for item in group]
                    try:
                        completed = await asyncio.gather(*tasks)
                    except BaseException:
                        for task in tasks:
                            if not task.done():
                                task.cancel()
                        await asyncio.gather(*tasks, return_exceptions=True)
                        raise
                    for item, result in zip(group, completed):
                        self._track(item, result)
                        results.append(result)
                else:
                    result = await self._execute(entry)
                    self._track(entry, result)
                    results.append(result)
                    i += 1
            return results

    def _track(self, item, result):
        if result.status == "unknown":
            self.terminal_failed, self.pause_reason = True, "termination_unknown"
        elif result.status == "succeeded":
            self._last_failure, self._failure_count = None, 0
        elif result.status == "failed":
            key = (*self._key(item[1], item[2]), result.error_type)
            self._failure_count = self._failure_count + 1 if key == self._last_failure else 1
            self._last_failure = key
            if self._failure_count >= 3:
                self.terminal_failed, self.pause_reason = True, "repeated_tool_failure"

    async def _execute(self, item):
        cid, name, args, tool, invalid = item
        if invalid:
            self.emit("tool_result", **invalid.to_dict())
            return invalid
        self.emit("tool_pending", call_id=cid, name=name)
        try:
            await asyncio.sleep(0)  # Cancellation boundary before any operation.
            result = await tool.execute(cid, args)
        except asyncio.CancelledError as error:
            status = getattr(error, "tool_status", "cancelled")
            if status == "unknown":
                self.terminal_failed, self.pause_reason = True, "termination_unknown"
            self.emit(
                "tool_result",
                call_id=cid,
                name=name,
                status=status,
                content=getattr(error, "tool_content", "Cancelled before completion."),
                error_type="user_interrupted",
            )
            raise
        except ToolError as error:
            status = "unknown" if error.error_type == "termination_unknown" else "failed"
            if error.error_type in {"storage_error", "termination_unknown"}:
                self.terminal_failed, self.pause_reason = True, error.error_type
            result = ToolResult(cid, name, status, str(error), error.error_type)
        except (OSError, UnicodeError, ValueError) as error:
            result = ToolResult(
                cid, name, "failed", f"{type(error).__name__}: {error}", type(error).__name__
            )
        self.emit("tool_result", **result.to_dict())
        return result

    def _read(self, path):
        data = path.read_bytes()
        text = data.decode("utf-8-sig")
        if "\x00" in text:
            raise ToolError("Binary file is not supported.", "binary")
        check_content(text)
        return text

    async def _in_worker(self, function, *args):
        # A cancelled asyncio task does not stop a running filesystem thread.
        # Join it before releasing the read group, so no read overlaps a later write.
        worker = asyncio.create_task(asyncio.to_thread(function, *args))
        try:
            return await asyncio.shield(worker)
        except asyncio.CancelledError:
            await asyncio.gather(worker, return_exceptions=True)
            raise

    def _snapshot(self, path):
        before = fingerprint(path)
        text = self._read(path)
        if fingerprint(path) != before:
            raise ToolError("File changed during reading; retry.", "stale_read")
        return before, text

    def _read_range(self, path, start, count):
        before, text = self._snapshot(path)
        lines = text.splitlines()
        content = f"{self.policy.display(path)} | total_lines={len(lines)} | sha256={before[0]}\n"
        content += "\n".join(
            f"{i + 1}: {line}"
            for i, line in enumerate(lines[start - 1 : start - 1 + count], start - 1)
        )
        return before, content

    def _grep_matches(self, path, pattern, relative):
        _, text = self._snapshot(path)
        return [
            f"{relative}:{i}: {line}"
            for i, line in enumerate(text.splitlines(), 1)
            if pattern in line
        ]

    def _verify_read(self, path):
        if path not in self.reads or fingerprint(path) != self.reads[path]:
            raise ToolError(
                "File must be read again before modification; no current matching read exists.",
                "stale_read",
            )

    def _write(self, raw_path, path, content, existed):
        check_content(content)
        path.parent.mkdir(parents=True, exist_ok=True)
        # Resolve again immediately before creating a sibling temporary file.
        if self.policy.resolve(raw_path) != path:
            raise ToolError("Path changed.", "path")
        if existed:
            self._verify_read(path)
        elif path.exists():
            raise ToolError("File appeared; read before overwriting.", "stale_read")
        fd, temporary = tempfile.mkstemp(prefix=".minicode-edit-", dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            if self.policy.resolve(raw_path) != path:
                raise ToolError("Path changed.", "path")
            if existed:
                self._verify_read(path)
                os.replace(temporary, path)
            else:
                # Hard link creation fails if another process creates the destination.
                os.link(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        self.reads.pop(path, None)

    async def _approval(self, cid, name, args, description, *, details=None):
        key = self._key(name, args)
        if key in self.rejected:
            return False
        self.emit("tool_awaiting_approval", call_id=cid, name=name)
        accepted = await self.approve(
            {"name": name, "input": dict(args), "description": description, "details": details}
        )
        self.emit("tool_approval", call_id=cid, name=name, approved=bool(accepted))
        if not accepted:
            self.rejected.add(key)
            self.terminal_failed, self.pause_reason = True, "user_rejected"
        await asyncio.sleep(0)
        return bool(accepted)

    @staticmethod
    def _matches(relative, pattern):
        path_parts = relative.replace("\\", "/").split("/")
        pattern_parts = pattern.replace("\\", "/").split("/")

        @lru_cache(maxsize=None)
        def match(path_index, pattern_index):
            if pattern_index == len(pattern_parts):
                return path_index == len(path_parts)
            part = pattern_parts[pattern_index]
            if part == "**":
                return match(path_index, pattern_index + 1) or (
                    path_index < len(path_parts) and match(path_index + 1, pattern_index)
                )
            return (
                path_index < len(path_parts)
                and fnmatch.fnmatchcase(path_parts[path_index], part)
                and match(path_index + 1, pattern_index + 1)
            )

        return match(0, 0)

    def _search_files(self, name, glob_pattern, needle, stop):
        segments = glob_pattern.replace("\\", "/").split("/")
        max_depth = None if "**" in segments else len(segments)
        deadline = time.monotonic() + 7
        matches = []
        for path in self.policy.files(
            max_depth=max_depth,
            include_dirs=name == "Glob",
            stop=stop,
            deadline=deadline,
            max_entries=10000,
        ):
            relative = self.policy.match_name(path)
            if not self._matches(relative, glob_pattern):
                continue
            display = self.policy.display(path)
            if name == "Glob":
                matches.append(display + ("/" if path.is_dir() else ""))
            else:
                try:
                    if path.stat().st_size > 2_000_000:
                        continue
                    self.policy.resolve(str(path))
                    found = self._grep_matches(path, needle, display)
                    if self.policy.resolve(str(path)) != path:
                        continue
                except (ToolError, OSError, UnicodeError):
                    continue
                matches.extend(found)
            if len(matches) > 500:
                raise ToolError("Search returned too many results; use a narrower pattern.", "search_limit")
        return "\n".join(matches) or "No matches."

    async def _dispatch(self, name, cid, args):
        if self.permissions.decide(name).action == Action.DENY:
            raise ToolError("Operation denied by permission policy.", "permission")
        content = ""
        if name == "UseSkill":
            scope, project_path = args.get("scope", "global"), args.get("project_path")
            loaded = []
            for skill_name in args["names"]:
                loaded.append((skill_name, self.skills.load(skill_name, scope, project_path)))
            content = "\n\n".join(f"# Skill: {skill_name}\n{body}" for skill_name, body in loaded)
            if len(content) > 65536:
                raise ToolError("Selected skill texts are too long together; load fewer at once.", "skill_limit")
            for skill_name, _ in loaded:
                self.active_skills.add((scope, str(project_path or "").casefold(), skill_name.casefold()))
            if scope == "project" and project_path:
                self.touched_projects.add(Path(project_path).resolve())
        elif name == "ReadSkillResource":
            scope, project_path = args.get("scope", "global"), args.get("project_path")
            key = (scope, str(project_path or "").casefold(), args["name"].casefold())
            if key not in self.active_skills:
                raise ToolError("UseSkill must select this skill before reading its resources.", "skill")
            content = self.skills.resource(args["name"], args["path"], scope, project_path)
        elif name == "ReadFile":
            path = self.policy.resolve(args["path"], output_read=True)
            self._touch_project(path)
            if not path.is_file():
                raise ToolError("Not an existing regular file.", "not_file")
            before, content = await self._in_worker(
                self._read_range, path, args.get("start_line", 1), args.get("line_count", 200)
            )
            if self.policy.resolve(args["path"], output_read=True) != path:
                raise ToolError("File changed during reading; retry.", "stale_read")
            self.reads[path] = before
        elif name in {"Glob", "Grep"}:
            self.policy.base()
            self.touched_projects.update(self.policy.roots())
            pattern = args["pattern"] if name == "Glob" else args.get("glob", "**/*")
            if ".." in Path(pattern).parts or Path(pattern).is_absolute():
                raise ToolError("Search pattern must stay within work directories.", "path")
            stop = threading.Event()
            grant_version = self.session_permissions.version if self.session_permissions else None
            try:
                content = await asyncio.wait_for(
                    asyncio.to_thread(
                        self._search_files, name, pattern, args.get("pattern"), stop
                    ),
                    timeout=8,
                )
            except TimeoutError as error:
                raise ToolError("Search timed out; use a narrower pattern.", "search_limit") from error
            finally:
                stop.set()
            if self.session_permissions and self.session_permissions.version != grant_version:
                raise ToolError("Permissions changed during search; retry.", "stale_read")
        elif name in {"WriteFile", "EditFile"}:
            path = self.policy.resolve(args["path"])
            self._touch_project(path, writing=True)
            existed = path.exists()
            if existed and not path.is_file():
                raise ToolError("Target is not a regular file.", "not_file")
            if existed:
                self._verify_read(path)
                before_text = path.read_bytes().decode("utf-8-sig")
                if "\x00" in before_text:
                    raise ToolError("Binary file is not supported.", "binary")
                check_content(before_text)
                before_version = fingerprint(path)
            else:
                before_text, before_version = "", None
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
                    fromfile=self.policy.display(path) if existed else "/dev/null",
                    tofile=self.policy.display(path),
                )
            )
            grant_version = self.session_permissions.version if self.session_permissions else None
            summary = (
                f"{name}: {path}\n"
                f"{'Modify existing file' if existed else 'Create new file'}; "
                f"{len(before_text)} -> {len(content)} characters"
            )
            if not await self._approval(cid, name, args, summary, details=diff):
                return ToolResult(cid, name, "rejected", "User rejected this operation.")
            if self.session_permissions and self.session_permissions.version != grant_version:
                raise ToolError("Permissions changed after approval.", "stale_approval")
            if self.policy.resolve(args["path"]) != path or path.exists() != existed:
                raise ToolError("Target changed after approval.", "stale_approval")
            if existed and fingerprint(path) != before_version:
                raise ToolError("File changed after approval.", "stale_approval")
            self.emit("tool_running", call_id=cid, name=name)
            self._write(args["path"], path, content, existed)
            content = f"Saved {self.policy.display(path)}"
        elif name == "DeleteFile":
            path = self.policy.resolve(args["path"])
            self._touch_project(path, writing=True)
            candidate = Path(args["path"])
            candidate = candidate if candidate.is_absolute() else self.policy.base() / candidate
            if not path.is_file() or candidate.is_symlink():
                raise ToolError("DeleteFile only deletes one regular file.", "not_file")
            version = fingerprint(path)
            check_content(path.read_bytes().decode("utf-8", errors="replace"))
            grant_version = self.session_permissions.version if self.session_permissions else None
            if not await self._approval(
                cid, name, args, f"Delete file: {path}\nVersion: {version[0]}"
            ):
                return ToolResult(cid, name, "rejected", "User rejected this operation.")
            if (
                (self.session_permissions and self.session_permissions.version != grant_version)
                or
                self.policy.resolve(args["path"]) != path
                or not path.is_file()
                or fingerprint(path) != version
            ):
                raise ToolError(
                    "File changed after approval; deletion cancelled.", "stale_approval"
                )
            self.emit("tool_running", call_id=cid, name=name)
            path.unlink()
            self.reads.pop(path, None)
            content = f"Deleted {self.policy.display(path)}"
        elif name == "Shell":
            check_command(args["command"], self.policy.roots())
            if self.shell_backend is None or not self.shell_backend.available():
                raise ToolError(
                    "Shell requires a verified Windows sandbox; execution is unavailable.",
                    "shell_unavailable",
                )
            timeout = args.get("timeout", 120)
            preview = args["command"].splitlines()[0][:120]
            if len(args["command"]) > 120 or "\n" in args["command"]:
                preview += "…"
            roots = "\n".join(str(path) for path in self.policy.roots())
            if not await self._approval(
                cid,
                name,
                args,
                f"PowerShell: {preview}\nWorking directory: {self.policy.base()}\n"
                f"Authorized directories:\n{roots}\nTimeout: {timeout}s; network available after approval.\n"
                "Command may read and send accessible .env, private keys and internal records.",
                details=UTF8_PRELUDE + args["command"],
            ):
                return ToolResult(cid, name, "rejected", "User rejected this operation.")
            self.emit("tool_running", call_id=cid, name=name)
            status, content, reason = await run_shell(
                self.policy.base(),
                args["command"],
                timeout,
                self.output_store,
                cid,
                self.emit,
                executable=self.shell_executable,
                backend=self.shell_backend,
            )
            if reason == "storage_error":
                self.terminal_failed, self.pause_reason = True, reason
            self.reads.clear()
            content = self.output_store.preview(cid, content)
            return ToolResult(cid, name, status, content, reason)
        limit = 70000 if name in {"UseSkill", "ReadSkillResource"} else 16000
        content = self.output_store.preview(cid, content, limit=limit)
        return ToolResult(cid, name, "succeeded", content)

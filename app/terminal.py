from __future__ import annotations

import asyncio
import ctypes
import json
from pathlib import Path

from prompt_toolkit.application import Application
from prompt_toolkit.completion import WordCompleter
from prompt_toolkit.filters import Condition
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import HSplit, Layout
from prompt_toolkit.layout.containers import ConditionalContainer
from prompt_toolkit.mouse_events import MouseEventType
from prompt_toolkit.styles import Style
from prompt_toolkit.widgets import Label, TextArea

from agent.loop import AgentLoop
from app.commands import COMMANDS, HELP
from context import ContextManager
from conversation.provider import Provider
from conversation.resume import recover_session
from memory.long_memory import LongMemory
from memory.output_store import OutputStore
from memory.permission_state import SessionPermissions
from memory.project_assets import ProjectAssets
from memory.session_store import SessionStore
from skill import SkillCatalog
from tools.base import ToolError
from tools.executor import ToolExecutor


def _set_windows_clipboard(text):
    """Put Unicode text on the Windows clipboard without console encoding."""
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    user32.OpenClipboard.argtypes = [ctypes.c_void_p]
    user32.OpenClipboard.restype = ctypes.c_bool
    user32.EmptyClipboard.restype = ctypes.c_bool
    user32.CloseClipboard.restype = ctypes.c_bool
    kernel32.GlobalAlloc.restype = ctypes.c_void_p
    kernel32.GlobalAlloc.argtypes = [ctypes.c_uint, ctypes.c_size_t]
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
    kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
    kernel32.GlobalFree.argtypes = [ctypes.c_void_p]
    user32.SetClipboardData.argtypes = [ctypes.c_uint, ctypes.c_void_p]
    if not user32.OpenClipboard(None):
        raise OSError("OpenClipboard failed")
    try:
        if not user32.EmptyClipboard():
            raise OSError("EmptyClipboard failed")
        encoded = (text + "\0").encode("utf-16-le")
        handle = kernel32.GlobalAlloc(0x0002, len(encoded))
        if not handle:
            raise OSError("GlobalAlloc failed")
        address = kernel32.GlobalLock(handle)
        if not address:
            kernel32.GlobalFree(handle)
            raise OSError("GlobalLock failed")
        try:
            ctypes.memmove(address, encoded, len(encoded))
        finally:
            kernel32.GlobalUnlock(handle)
        if not user32.SetClipboardData(13, handle):
            kernel32.GlobalFree(handle)
            raise OSError("SetClipboardData failed")
    finally:
        user32.CloseClipboard()


def _get_windows_clipboard():
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    user32.OpenClipboard.argtypes = [ctypes.c_void_p]
    user32.OpenClipboard.restype = ctypes.c_bool
    user32.CloseClipboard.restype = ctypes.c_bool
    user32.GetClipboardData.restype = ctypes.c_void_p
    user32.GetClipboardData.argtypes = [ctypes.c_uint]
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
    kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
    if not user32.OpenClipboard(None):
        return ""
    try:
        handle = user32.GetClipboardData(13)
        if not handle:
            return ""
        address = kernel32.GlobalLock(handle)
        if not address:
            return ""
        try:
            return ctypes.wstring_at(address)
        finally:
            kernel32.GlobalUnlock(handle)
    finally:
        user32.CloseClipboard()


class TranscriptControl:
    """Keep scrolling available without leaving the keyboard in the transcript."""

    def __init__(self, area, input_area, application):
        self.area = area
        self.input_area = input_area
        self.application = application

    def install(self):
        original = self.area.control.mouse_handler

        def mouse_handler(mouse_event):
            result = original(mouse_event)
            if mouse_event.event_type in (MouseEventType.SCROLL_UP, MouseEventType.SCROLL_DOWN):
                self.application.layout.focus(self.input_area)
            return result

        self.area.control.mouse_handler = mouse_handler


class TerminalApp:
    def __init__(self, config, *, provider=None, input=None, output=None):
        self.config = config
        self.provider = provider or Provider(config)
        self.memory = LongMemory(config.root, self.provider)
        self.worker = None
        self.approval = None
        self.approval_details = ""
        self.approval_expanded = False
        self.choices = []
        self.tools = []
        self.tool_index = 0
        self.tool_expanded = False
        self.pending_skill = None
        self.pending_skill_scope = "global"
        self.pending_skill_project = None
        self.forced_skill_for_turn = None
        self.assets = ProjectAssets(config.root)
        try:
            self.assets.ensure_global()
        except ToolError as error:
            raise ValueError(f"全局提示词无效：{error}") from error
        self.skills = SkillCatalog(config.root)
        self.document = None
        self.editing = False
        self.previous_choices = []
        self.choice_mode = "session"
        self.choice_index = 0
        self.context_estimate = 0
        self.process_uncertain = False
        self.storage_failed = False
        self.output_area = TextArea(
            text="", read_only=True, scrollbar=True, wrap_lines=True, focus_on_click=True
        )
        self.input_area = TextArea(
            height=3,
            multiline=True,
            focus_on_click=True,
            prompt="> ",
            completer=WordCompleter(COMMANDS),
            complete_while_typing=True,
        )
        self.status_label = Label(text=self.status_text)
        self.choice_label = Label(text=self.choice_text)
        self.document_label = Label(text=self.document_text)
        self.view_area = TextArea(text="", height=12, read_only=True, scrollbar=True, wrap_lines=True)
        self.edit_area = TextArea(text="", height=12, multiline=True, scrollbar=True, wrap_lines=True)
        self.tool_label = Label(text=self.tool_text)
        self.tool_details = TextArea(text="", height=10, read_only=True, scrollbar=True, wrap_lines=True)
        self.approval_area = TextArea(text="", height=12, read_only=True, scrollbar=True)
        keys = KeyBindings()

        @keys.add("enter")
        def enter(event):
            if self.editing:
                self._save_document()
                return
            if self.document:
                return
            if self.choices:
                selected = self.choices[self.choice_index]
                if self.choice_mode == "directories":
                    self._select_directory(selected["path"])
                    self.choices = []
                    return
                if self.choice_mode == "skills":
                    self._select_skill(selected["name"], selected["scope"], selected["project_path"])
                    self.choices = []
                    return
                if self.choice_mode == "prompts":
                    self._open_document(selected)
                    return
                if self.choice_mode == "tools":
                    self.tool_index = self.choice_index
                    self.tool_expanded = not self.tool_expanded
                    self._refresh_tool_details()
                    self.choices = []
                    return
                self.choices = []
                if selected.get("status") == "error":
                    self.write("该会话日志损坏，无法自动加载。\n")
                else:
                    self.select_session(selected["session_id"])
                return
            if self.busy or self.approval:
                self.write("\n当前轮仍在执行。先按 Esc 停止；草稿已保留。\n")
                return
            text = self.input_area.text.strip()
            if not text:
                return
            self.input_area.text = ""
            self.submit(text)

        @keys.add("escape", eager=True)
        def escape(event):
            if self.document:
                self._close_document()
            elif self.choices:
                self.choices = []
            else:
                self.cancel()

        @keys.add("f1")
        def edit_document(event):
            if not self.document and self.choices and self.choice_mode in {"skills", "prompts"}:
                self._open_document(self.choices[self.choice_index])
            if self.document and not self.editing:
                self.editing = True
                self.edit_area.text = self.document["original"]
                self.application.layout.focus(self.edit_area)
                self.application.invalidate()

        @keys.add("f2")
        def back_document(event):
            if self.document:
                self._close_document()
            elif self.choices:
                self.choices = []
                self.application.invalidate()

        @keys.add("c-j", filter=Condition(lambda: self.editing))
        def insert_line(event):
            self.edit_area.buffer.insert_text("\n")

        @keys.add("c-y")
        def approve(event):
            self.resolve_approval(True)

        @keys.add("c-n")
        def reject(event):
            self.resolve_approval(False)

        @keys.add("space", filter=Condition(lambda: self.approval is not None))
        def toggle_approval(event):
            self.approval_expanded = not self.approval_expanded
            if self.approval_expanded:
                self.approval_area.text = self.approval_details
                self.approval_area.buffer.cursor_position = 0
            self.application.invalidate()

        @keys.add("pageup", filter=Condition(lambda: self.approval is not None and self.approval_expanded))
        def approval_page_up(event):
            self.approval_area.buffer.cursor_up(count=10)
            self.application.invalidate()

        @keys.add("pagedown", filter=Condition(lambda: self.approval is not None and self.approval_expanded))
        def approval_page_down(event):
            self.approval_area.buffer.cursor_down(count=10)
            self.application.invalidate()

        @keys.add("d", filter=Condition(lambda: bool(self.choices) and self.choice_mode == "directories"))
        def revoke_selected(event):
            selected = self.choices[self.choice_index]
            self._revoke_directory(selected["path"])
            self._open_directories()

        @keys.add("c-t")
        def toggle_tool(event):
            if self.tools and not self.approval:
                self.tool_index = len(self.tools) - 1
                self.tool_expanded = not self.tool_expanded
                self._refresh_tool_details()

        @keys.add("up", filter=Condition(lambda: bool(self.choices)))
        def up(event):
            self.choice_index = (self.choice_index - 1) % len(self.choices)

        @keys.add("down", filter=Condition(lambda: bool(self.choices)))
        def down(event):
            self.choice_index = (self.choice_index + 1) % len(self.choices)

        @keys.add("c-c")
        def ctrl_c(event):
            if self.copy_output_selection():
                return
            if self.busy:
                self.cancel()
            else:
                self.application.exit()

        @keys.add("c-a")
        def select_all(event):
            buffer = self.output_area.buffer if self.application.layout.has_focus(self.output_area) else self.input_area.buffer
            buffer.cursor_position = 0
            buffer.start_selection()
            buffer.cursor_position = len(buffer.text)

        @keys.add("c-v")
        def paste(event):
            if self.application.layout.has_focus(self.input_area):
                try:
                    self.input_area.buffer.insert_text(_get_windows_clipboard())
                except (OSError, OverflowError, ctypes.ArgumentError):
                    self.write("无法访问 Windows 剪贴板。\n")

        @keys.add("c-x")
        def cut(event):
            if self.application.layout.has_focus(self.input_area) and self.input_area.buffer.selection_state:
                data = self.input_area.buffer.cut_selection()
                try:
                    _set_windows_clipboard(data.text)
                except (OSError, OverflowError, ctypes.ArgumentError):
                    self.write("无法访问 Windows 剪贴板。\n")

        self.application = Application(
            layout=Layout(
                HSplit(
                    [
                        Label("minicode · 项目编程助手"),
                        self.output_area,
                        self.choice_label,
                        self.document_label,
                        ConditionalContainer(self.view_area, filter=Condition(lambda: self.document is not None and not self.editing)),
                        ConditionalContainer(self.edit_area, filter=Condition(lambda: self.editing)),
                        self.tool_label,
                        ConditionalContainer(self.tool_details, filter=Condition(lambda: self.tool_expanded)),
                        ConditionalContainer(
                            self.approval_area,
                            filter=Condition(lambda: self.approval is not None and self.approval_expanded),
                        ),
                        self.status_label,
                        self.input_area,
                    ]
                ),
                focused_element=self.input_area,
            ),
            key_bindings=keys,
            full_screen=True,
            mouse_support=True,
            input=input,
            output=output,
            style=Style.from_dict({"text-area": "", "label": "fg:#a8b9cc"}),
        )
        TranscriptControl(self.output_area, self.input_area, self.application).install()
        original_click = self.tool_label.window.content.mouse_handler
        def click_tool(mouse_event):
            if mouse_event.event_type == MouseEventType.MOUSE_UP and self.tools:
                self.tool_index = len(self.tools) - 1
                self.tool_expanded = not self.tool_expanded
                self._refresh_tool_details()
            return original_click(mouse_event)
        self.tool_label.window.content.mouse_handler = click_tool
        self.select_session()

    @property
    def busy(self):
        return self.worker is not None and not self.worker.done()

    def write(self, text):
        text = str(text).replace(self.config.api_key, "[REDACTED]").replace("\x1b", "")
        self._append(text)
        if hasattr(self, "application"):
            self.application.invalidate()

    def _append(self, text):
        # Keep terminal rendering bounded; the complete transcript remains on disk.
        self.output_area.text = (self.output_area.text + text)[-150000:]
        self.output_area.buffer.cursor_position = len(self.output_area.text)

    def status_text(self):
        if not hasattr(self, "store"):
            return ""
        meta = self.store.meta
        total = str(meta["token_total"]) + (" + 未知" if meta["usage_incomplete"] else "")
        state = "执行中" if self.busy else "可输入"
        if self.approval:
            state = "等待批准：Ctrl+Y 批准 / Ctrl+N 拒绝 / Esc 中断"
        return f"{state} | 上下文估算 {self.context_estimate} / {self.config.context_window - self.config.max_output_tokens} | 累计 token {total} | / 查看指令"

    def choice_text(self):
        if not self.choices:
            return ""
        if self.choice_mode == "directories":
            lines = ["工作目录：↑↓ 浏览 / 回车设为默认 / D 撤销 / Esc 返回"]
        elif self.choice_mode == "skills":
            lines = ["技能目录：↑↓ 浏览 / 回车指定下一轮 / F1 编辑 / F2 返回"]
        elif self.choice_mode == "prompts":
            lines = ["系统提示词：↑↓ 浏览 / 回车查看 / F1 编辑 / F2 返回"]
        elif self.choice_mode == "tools":
            lines = ["工具调用：↑↓ 浏览 / 回车展开 / Esc 返回"]
        else:
            lines = ["选择会话：↑↓ / 回车 / Esc"]
        start = max(0, self.choice_index - 4)
        for index in range(start, min(len(self.choices), start + 8)):
            item = self.choices[index]
            if self.choice_mode == "directories":
                kind = "默认工作目录" if item["active"] else "授权目录"
                status = "有效" if item["valid"] else f"失效：{item['reason']}"
                lines.append(f"{'>' if index == self.choice_index else ' '} {kind} | {status} | {item['path']}")
            elif self.choice_mode == "skills":
                lines.append(f"{'>' if index == self.choice_index else ' '} {item['scope']} | {item['project_path'] or '全局'} | {item['name']} | {item['description']}")
            elif self.choice_mode == "prompts":
                lines.append(f"{'>' if index == self.choice_index else ' '} {item['label']} | {item['path']}")
            elif self.choice_mode == "tools":
                lines.append(f"{'>' if index == self.choice_index else ' '} {self._tool_summary(item)}")
            else:
                lines.append(
                    f"{'>' if index == self.choice_index else ' '} {item['title']} | {item['status']} | {item['last_active_at']}"
                )
        return "\n".join(lines)

    def document_text(self):
        if not self.document:
            return ""
        state = "编辑中：Enter 保存 / Ctrl+J 换行 / F2 取消" if self.editing else "查看中：F1 编辑 / F2 返回"
        return f"{self.document['path']} | {state}"

    def _open_document(self, item):
        try:
            if self.choice_mode == "skills":
                content = self.skills.load(item["name"], item["scope"], item["project_path"])
                kind = "skill"
            else:
                content = (self.assets.global_text() if item["scope"] == "global"
                           else self.assets.project_text(item["project_path"]))
                kind = "prompt"
        except (ToolError, OSError, UnicodeError) as error:
            self.write(f"读取失败：{error}\n")
            return
        self.previous_choices = self.choices
        self.choices = []
        display_path = Path(item["path"]) / "SKILL.md" if kind == "skill" else item["path"]
        self.document = {**item, "path": display_path, "kind": kind, "original": content}
        self.view_area.text = content
        self.view_area.buffer.cursor_position = 0
        self.application.layout.focus(self.view_area)
        self.application.invalidate()

    def _close_document(self):
        if self.editing:
            self.editing = False
        else:
            self.document = None
            self.choices = self.previous_choices
            self.previous_choices = []
        self.application.layout.focus(self.input_area)
        self.application.invalidate()

    def _save_document(self):
        if not self.document:
            return
        text = self.edit_area.text
        item = self.document
        try:
            if item["kind"] == "skill":
                current = self.skills.load(item["name"], item["scope"], item["project_path"])
                if current != item["original"]:
                    raise ToolError("技能文件已被其他程序修改；请重新打开。", "stale_edit")
                self.skills.save(item["name"], text, item["scope"], item["project_path"])
                event_type = "skill_edited"
            else:
                current = (self.assets.global_text() if item["scope"] == "global"
                           else self.assets.project_text(item["project_path"]))
                if current != item["original"]:
                    raise ToolError("提示词已被其他程序修改；请重新打开。", "stale_edit")
                self.assets.save_prompt(item["path"], text)
                event_type = "prompt_edited"
        except (ToolError, OSError, UnicodeError, ValueError) as error:
            self.write(f"保存失败：{error}\n")
            return
        try:
            self.store.append(event_type, path=str(item["path"]), scope=item["scope"])
        except (OSError, ValueError):
            self.storage_failed = True
            self.write("文件已保存，但会话记录写入失败；请检查磁盘状态。\n")
        item["original"] = text
        self.view_area.text = text
        self.editing = False
        self.application.layout.focus(self.view_area)
        self.write(f"已保存：{item['path']}\n")

    def _open_prompts(self):
        self.choice_mode = "prompts"
        self.choices = [{"label": "全局", "path": self.assets.global_path(),
                         "scope": "global", "project_path": None}]
        for root in self.directory_permissions.roots():
            self.choices.append({"label": "项目", "path": self.assets.project_path(root),
                                 "scope": "project", "project_path": str(root)})
        self.choice_index = 0
        self.application.invalidate()

    def _open_directories(self):
        self.choice_mode = "directories"
        self.choices = self.directory_permissions.listing()
        self.choice_index = max(0, min(self.choice_index, len(self.choices) - 1))
        self.application.invalidate()

    def _select_directory(self, path):
        try:
            selected = self.directory_permissions.select(path)
            self.write(f"默认工作目录：{selected}\n")
        except (ToolError, OSError, ValueError) as error:
            self.write(f"切换工作目录失败：{error}\n")

    def _select_skill(self, name, scope="global", project_path=None):
        try:
            self.skills.load(name, scope, project_path)
            self.store.append("skill_queued", name=name, scope=scope, project_path=project_path)
            self.pending_skill = name
            self.pending_skill_scope = scope
            self.pending_skill_project = project_path
            self.write(f"已指定 {scope} 技能：{name}。下一轮必须使用，之后恢复自动选择。\n")
        except (ToolError, ValueError, OSError, UnicodeError) as error:
            self.write(f"选择技能失败：{error}\n")

    def skill_context(self):
        if not self.forced_skill_for_turn:
            return ""
        try:
            name, scope, project_path = self.forced_skill_for_turn
            content = self.executor.force_skill(name, scope, project_path)
        except (ToolError, OSError, UnicodeError):
            return "用户指定的技能文件已不可用；请说明这一点，不要声称已经使用该技能。"
        if self.pending_skill:
            self.store.append("skill_consumed", name=self.pending_skill,
                              scope=self.pending_skill_scope, project_path=self.pending_skill_project)
            self.pending_skill = None
        return content

    @staticmethod
    def _tool_summary(item):
        return f"#{item['number']} {item['name']} · {item['status']}"

    def tool_text(self):
        if not self.tools:
            return ""
        item = self.tools[self.tool_index if self.tool_expanded else -1]
        return self._tool_summary(item) + " · 点击此行或 Ctrl+T 查看详情；/tools 浏览历史"

    def _refresh_tool_details(self):
        if self.tool_expanded and self.tools:
            item = self.tools[self.tool_index]
            self.tool_details.text = item.get("content", "")
            self.tool_details.buffer.cursor_position = 0
        self.application.invalidate()

    def _record_tool(self, event_type, data):
        if event_type == "tool_pending":
            item = {"number": len(self.tools) + 1, "id": data.get("call_id"),
                    "name": data.get("name"), "status": "执行中", "content": ""}
            self.tools.append(item)
            self.tool_index = len(self.tools) - 1
            self.write("\n[" + self._tool_summary(item) + "]\n")
        elif event_type == "tool_result":
            item = next((x for x in reversed(self.tools) if x["id"] == data.get("call_id")), None)
            if item is None:
                item = {"number": len(self.tools) + 1, "id": data.get("call_id"),
                        "name": data.get("name"), "status": "", "content": ""}
                self.tools.append(item)
            item["status"] = data.get("status", "未知")
            item["content"] = data.get("content", "")
            self.write("[" + self._tool_summary(item) + "]\n")
            self._refresh_tool_details()

    def _revoke_directory(self, path):
        try:
            removed = self.directory_permissions.remove(path)
            self.write(f"目录授权已撤销：{removed}\n")
        except ToolError as error:
            self.write(f"撤销失败：{error}\n")
        except (OSError, ValueError):
            self.storage_failed = True
            self.write("目录授权记录写入失败；操作已停止。\n")

    def select_session(self, session_id=None):
        if self.busy:
            raise RuntimeError("Cannot switch an active session")
        if self.process_uncertain or self.storage_failed:
            raise RuntimeError("请先检查未停止的进程或存储故障，再重启应用")
        self.store = recover_session(
            SessionStore(self.config.root, session_id, secrets=(self.config.api_key,))
        )
        self.outputs = OutputStore(self.store, secrets=(self.config.api_key,))
        self.directory_permissions = SessionPermissions(self.config.root, self.store)
        for root in self.directory_permissions.roots():
            try:
                self.assets.ensure_project(root)
            except ToolError as error:
                raise ValueError(f"项目配置无效（{root}）：{error}") from error
        self.skills = SkillCatalog(self.config.root, self.directory_permissions.roots)
        self.pending_skill = None
        self.pending_skill_scope = "global"
        self.pending_skill_project = None
        self.forced_skill_for_turn = None
        for event in self.store.events:
            if event["type"] == "skill_queued":
                self.pending_skill = event["name"]
                self.pending_skill_scope = event.get("scope", "global")
                self.pending_skill_project = event.get("project_path")
            elif event["type"] == "skill_consumed":
                self.pending_skill = None
        self.tools = []
        self.tool_index = 0
        self.tool_expanded = False
        self.executor = ToolExecutor(
            self.config.root,
            self.outputs,
            self.ask_approval,
            self.tool_event,
            self.directory_permissions,
            self.skills,
        )
        self.executor.shell_executable = self.config.shell
        self.context = ContextManager(self.config, self.provider, self.store)
        self.agent = AgentLoop(
            self.config,
            self.store,
            self.provider,
            self.executor,
            self.context,
            self.memory,
            self.agent_event,
            self.skill_context,
        )
        self.context_estimate = 0
        self.write(f"\n会话 {self.store.session_id}\n")
        if session_id:
            for event in self.store.events:
                if event["type"] == "message":
                    self.write(f"\n{event['role']}: {event.get('content', '')}\n")
                elif event["type"] == "tool_result":
                    self._record_tool("tool_result", event)
            self.write(
                f"最后终止方式：{self.store.meta['last_termination']}。输入“继续”恢复，或提交新指令。\n"
            )
        else:
            self.write("先用 /allow-dir 绝对路径 添加工作目录，再输入文件任务；/help 查看帮助。\n")

    async def ask_approval(self, request):
        self.approval = asyncio.get_running_loop().create_future()
        self.approval_details = request.get("details") or request["description"]
        self.approval_expanded = False
        self.write(
            "\n需要确认：\n"
            + request["description"]
            + "\n空格查看完整详情 / Ctrl+Y 批准 / Ctrl+N 拒绝\n"
        )
        try:
            return await self.approval
        finally:
            self.approval = None
            self.approval_details = ""
            self.approval_expanded = False
            self.application.invalidate()

    def resolve_approval(self, allowed):
        if self.approval is not None and not self.approval.done():
            self.approval.set_result(allowed)

    def cancel(self):
        if self.worker and not self.worker.done() and not self.worker.cancelling():
            self.worker.cancel()
            self.write("\n正在停止并保存，请等待工具清理完成……\n")

    def copy_output_selection(self):
        if self.output_area.buffer.selection_state is None:
            return False
        text = self.output_area.buffer.copy_selection().text
        try:
            _set_windows_clipboard(text)
        except (OSError, OverflowError, ctypes.ArgumentError):
            self.write("无法访问 Windows 剪贴板。\n")
            return False
        self.application.layout.focus(self.input_area)
        self.write("\n已复制选中内容。\n")
        return True

    def tool_event(self, event_type, **data):
        if data.get("status") == "unknown" or data.get("error_type") == "termination_unknown":
            self.process_uncertain = True
        if data.get("error_type") == "storage_error":
            self.storage_failed = True
        if isinstance(data.get("content"), str):
            data["content"] = self.outputs.redact(data["content"])
        self.store.append(event_type, turn_id=self.agent.conversation.turn_id, **data)
        if event_type in ("tool_pending", "tool_result"):
            self._record_tool(event_type, data)

    def agent_event(self, kind, data):
        if kind == "text":
            self.write(data.get("content", ""))
        elif kind == "context":
            self.context_estimate = data["estimated_tokens"]
        elif kind == "notice":
            self.write("\n" + data["text"] + "\n")

    def submit(self, text):
        if self.busy:
            return False
        if (self.process_uncertain or self.storage_failed) and text not in ("/help", "/exit"):
            self.write(
                "请先确认相关进程已停止、存储可用，再重启应用；当前禁止新的执行和会话切换。\n"
            )
            return False
        if text.startswith("/"):
            command, _, argument = text.partition(" ")
            if command in ("/help", "/"):
                self.write(HELP)
            elif command == "/session":
                self.choice_mode = "session"
                self.choices = SessionStore.list_sessions(self.config.root)
                self.choice_index = 0
            elif command == "/new":
                self.select_session()
            elif command == "/allow-dir":
                try:
                    added = self.directory_permissions.add(argument.strip())
                    self.write(f"目录已授权并设为默认工作目录：{added}\n")
                except ToolError as error:
                    self.write(f"授权失败：{error}\n")
                except (OSError, ValueError):
                    self.storage_failed = True
                    self.write("目录授权记录写入失败；操作已停止。\n")
            elif command == "/deny-dir":
                self._revoke_directory(argument.strip())
            elif command == "/dirs":
                self._open_directories()
            elif command == "/work-dir":
                self._select_directory(argument.strip())
            elif command == "/skill":
                if argument.strip():
                    self._select_skill(argument.strip())
                else:
                    try:
                        self.choices = self.skills.list()
                        self.choice_mode = "skills"
                        self.choice_index = 0
                        if not self.choices:
                            self.write("暂无技能。将 SKILL.md 放在 skill/skills/名称/ 中。\n")
                    except (OSError, UnicodeError) as error:
                        self.write(f"读取技能目录失败：{error}\n")
            elif command == "/use":
                parts = argument.strip().split(maxsplit=2)
                if len(parts) < 2 or parts[0] not in {"global", "project"} or parts[1] != "skills":
                    self.write("用法：/use global skills [名称] 或 /use project skills [名称]\n")
                else:
                    scope = parts[0]
                    if len(parts) == 3:
                        project = str(self.directory_permissions.active()) if scope == "project" and self.directory_permissions.active() else None
                        self._select_skill(parts[2], scope, project)
                    else:
                        self.choice_mode = "skills"
                        self.choices = self.skills.list(scope)
                        self.choice_index = 0
                        if not self.choices:
                            self.write(f"没有可用的 {scope} 技能。\n")
            elif command == "/see":
                if argument.strip() == "system_prompt":
                    self._open_prompts()
                else:
                    self.write("用法：/see system_prompt\n")
            elif command == "/tools":
                self.choices = self.tools.copy()
                self.choice_mode = "tools"
                self.choice_index = max(0, len(self.choices) - 1)
                if not self.choices:
                    self.write("还没有工具调用。\n")
            elif command == "/memory":
                args = argument.split()
                if len(args) == 2 and args[0] == "delete":
                    self.write(
                        "记忆已删除。\n" if self.memory.delete(args[1]) else "未找到记忆 ID。\n"
                    )
                else:
                    self.write(
                        json.dumps(self.memory.entries(), ensure_ascii=False, indent=2) + "\n"
                    )
            elif command == "/exit":
                self.application.exit()
            else:
                self.write("未知命令。\n" + HELP)
            return True
        if self.executor.terminal_failed and self.executor.pause_reason == "termination_unknown":
            self.write("上个进程尚未确认停止，不能启动下一轮；请先检查并结束该进程。\n")
            return False
        if self.pending_skill:
            try:
                self.skills.load(self.pending_skill, self.pending_skill_scope,
                                 self.pending_skill_project)
            except (ToolError, OSError, UnicodeError) as error:
                self.write(f"指定技能不可用，本轮未开始：{error}\n")
                return False
        self.forced_skill_for_turn = ((self.pending_skill, self.pending_skill_scope,
                                      self.pending_skill_project) if self.pending_skill else None)
        self.write("\n你: " + text + "\n\nminicode: ")
        self.worker = asyncio.create_task(self._run_turn(text))
        return True

    async def _run_turn(self, text):
        try:
            await self.agent.run(text)
        except Exception as exc:
            self.storage_failed = True
            self.write(f"\n执行失败（{type(exc).__name__}），未继续调度；请检查终端与存储状态。\n")
        finally:
            self.forced_skill_for_turn = None
            self.write("\n")
            self.application.invalidate()

    async def run(self):
        try:
            await self.application.run_async()
        finally:
            await self.close()

    async def close(self):
        self.cancel()
        if self.worker:
            await asyncio.gather(self.worker, return_exceptions=True)
        await self.memory.close()
        self.store.rebuild_meta()

"""Create and install skills through managed, validated storage paths."""

import json
import os
import re
import shutil
import tempfile
from pathlib import Path, PurePosixPath

from backend.permissions.paths import check_content
from backend.tools.base import ToolError


def validate_name(name):
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}", name):
        raise ToolError("技能名称请使用 1–64 位英文字母、数字、短横线或下划线。", "skill_name")
    if name.upper() in {"CON", "PRN", "AUX", "NUL"} | {
        f"{p}{i}" for p in ("COM", "LPT") for i in range(1, 10)
    }:
        raise ToolError("这个技能名称在 Windows 上不可用，请换个名字。", "skill_name")


class SkillLibrary:
    def __init__(self, catalog):
        self.catalog = catalog

    def create(self, name, description, content, scope="global", project_path=None):
        validate_name(name)
        if not description.strip() or not content.strip():
            raise ToolError("请填写技能用途和指令内容。", "skill")
        text = f"---\nname: {name}\ndescription: {json.dumps(description.strip(), ensure_ascii=False)}\n---\n\n{content.strip()}\n"
        return self.install(name, {"SKILL.md": text.encode()}, scope, project_path)

    def install(self, name, files, scope="global", project_path=None):
        try:
            return self._install(name, files, scope, project_path)
        except OSError as error:
            raise ToolError("无法保存技能，请检查文件夹权限或同名技能。", "skill_path") from error

    def _install(self, name, files, scope="global", project_path=None):
        validate_name(name)
        directory = self.catalog._directory(scope, project_path)
        # Reject linked ancestors before creating or writing managed folders.
        current = directory
        while current != current.parent:
            if current.is_symlink() or getattr(current, "is_junction", lambda: False)():
                raise ToolError("技能目录不能是链接。", "skill_path")
            current = current.parent
        directory.mkdir(parents=True, exist_ok=True)
        if not self.catalog._regular_directory(directory):
            raise ToolError("技能目录不可用。", "skill_path")
        if any(item.name.casefold() == name.casefold() for item in directory.iterdir()):
            raise ToolError("同名技能已存在，请换个名字或编辑已有技能。", "skill_exists")
        manifest = files.get("SKILL.md", b"")
        if not manifest or len(manifest) > 65536:
            raise ToolError("SKILL.md 为空或超过 64 KB。", "skill_limit")
        try:
            text = manifest.decode("utf-8-sig")
        except UnicodeError as error:
            raise ToolError("SKILL.md 必须使用 UTF-8 编码。", "skill") from error
        if "\x00" in text:
            raise ToolError("SKILL.md 必须是文本。", "skill")
        check_content(text)
        if not re.search(r"^description:\s*\S", text, re.MULTILINE):
            raise ToolError("SKILL.md 缺少 description 技能用途。", "skill")
        if len(files) > 1000 or sum(len(value) for value in files.values()) > 10_000_000:
            raise ToolError("技能资源超过大小限制。", "skill_limit")
        with tempfile.TemporaryDirectory(prefix=".install-", dir=directory) as temporary:
            staged = Path(temporary) / name
            staged.mkdir()
            for relative, data in files.items():
                raw = PurePosixPath(relative)
                if (
                    raw.is_absolute()
                    or not raw.parts
                    or ".." in raw.parts
                    or ":" in relative
                    or "\\" in relative
                    or any(
                        part.startswith(".env") or part in {".git", ".minicode"}
                        for part in raw.parts
                    )
                ):
                    raise ToolError("技能资源路径无效。", "skill_path")
                target = staged.joinpath(*raw.parts)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)
            destination = directory / name
            # Exclusive reservation prevents overwriting an existing empty folder.
            destination.mkdir()
            try:
                for item in staged.iterdir():
                    os.rename(item, destination / item.name)
            except BaseException:
                shutil.rmtree(destination)
                raise
        return {"name": name, "scope": scope, "project_path": project_path}

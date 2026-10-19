"""User-editable prompt files and project skill directories."""

from pathlib import Path

from backend.permissions.paths import check_content
from backend.tools.base import ToolError

GLOBAL_PROMPT = Path("system_prompt") / "system_prompt.md"
PROJECT_PROMPT = "project_system_prompt.md"
PROJECT_SKILLS = "project_skills"
MAX_PROMPT_BYTES = 65536


def _plain_directory(path: Path) -> bool:
    return (
        path.is_dir()
        and not path.is_symlink()
        and not getattr(path, "is_junction", lambda: False)()
    )


def _read_prompt(path: Path) -> str:
    if not path.is_file() or path.is_symlink() or path.stat().st_nlink != 1:
        raise ToolError(f"Prompt file is not a regular file: {path}", "prompt_path")
    if path.stat().st_size > MAX_PROMPT_BYTES:
        raise ToolError(f"Prompt file exceeds {MAX_PROMPT_BYTES} bytes: {path}", "prompt_limit")
    try:
        content = path.read_text(encoding="utf-8-sig")
    except UnicodeError as error:
        raise ToolError(f"Prompt file must be UTF-8: {path}", "prompt_encoding") from error
    if "\x00" in content:
        raise ToolError(f"Prompt file must be text: {path}", "prompt_encoding")
    check_content(content)
    return content


def _ensure_prompt(path: Path, initial: str) -> str:
    if not path.exists():
        try:
            with path.open("x", encoding="utf-8", newline="\n") as stream:
                stream.write(initial)
        except FileExistsError:
            pass
    return _read_prompt(path)


class ProjectAssets:
    def __init__(self, application_root: Path):
        self.application_root = Path(application_root).resolve()

    def ensure_global(self) -> str:
        directory = self.application_root / GLOBAL_PROMPT.parent
        directory.mkdir(exist_ok=True)
        if not _plain_directory(directory):
            raise ToolError("Global prompt directory must not be a link.", "prompt_path")
        return _ensure_prompt(
            directory / GLOBAL_PROMPT.name,
            "# minicode 全局提示词\n\n帮助用户完成当前编程任务。回答时清楚说明实际修改、验证结果和仍存在的限制。\n",
        )

    def global_path(self) -> Path:
        return self.application_root / GLOBAL_PROMPT

    def global_text(self) -> str:
        path = self.global_path()
        return _read_prompt(path) if path.exists() else ""

    def ensure_project(self, root: Path) -> str:
        root = Path(root).resolve()
        if not _plain_directory(root):
            raise ToolError("Project directory must not be a link.", "prompt_path")
        prompt = root / PROJECT_PROMPT
        skills = root / PROJECT_SKILLS
        # Validate existing assets before changing anything.
        if prompt.exists():
            _read_prompt(prompt)
        if skills.exists() and not _plain_directory(skills):
            raise ToolError("Project skills must be a regular directory.", "skill_path")
        text = _ensure_prompt(prompt, "# 项目提示词\n")
        skills.mkdir(exist_ok=True)
        if not _plain_directory(skills):
            raise ToolError("Project skills must be a regular directory.", "skill_path")
        for item in skills.iterdir():
            if _plain_directory(item) and (item / "SKILL.md").exists():
                _read_prompt(item / "SKILL.md")
        return text

    def project_path(self, root: Path) -> Path:
        return Path(root).resolve() / PROJECT_PROMPT

    def project_text(self, root: Path) -> str:
        path = self.project_path(root)
        return _read_prompt(path) if path.exists() else ""

    def save_prompt(self, path: Path, text: str) -> None:
        path = Path(path)
        if len(text.encode("utf-8")) > MAX_PROMPT_BYTES:
            raise ToolError("Prompt text is too large.", "prompt_limit")
        if path != self.global_path() and path.name != PROJECT_PROMPT:
            raise ToolError("Not a managed prompt file.", "prompt_path")
        _read_prompt(path)
        check_content(text)
        path.write_text(text, encoding="utf-8", newline="\n")

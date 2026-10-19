"""Read installed skills without letting resource paths escape their directory."""

from pathlib import Path

from backend.permissions.paths import check_content
from backend.tools.base import ToolError

PROJECT_SKILLS = "project_skills"


class SkillCatalog:
    def __init__(self, application_root: Path, authorized_roots=lambda: ()):
        self.directory = Path(application_root).resolve() / "skill" / "skills"
        self.authorized_roots = authorized_roots

    def _directory(self, scope: str, project_path: str | None) -> Path:
        if scope == "global":
            return self.directory
        if scope != "project" or not project_path:
            raise ToolError("Project skill needs an authorized project path.", "skill_path")
        root = Path(project_path).resolve()
        if root not in {Path(path).resolve() for path in self.authorized_roots()}:
            raise ToolError("Project skill path is not authorized.", "skill_path")
        return root / PROJECT_SKILLS

    def list(self, scope: str = "global", project_path: str | None = None) -> list[dict[str, str]]:
        if scope == "project" and project_path is None:
            return [
                entry
                for root in self.authorized_roots()
                for entry in self.list("project", str(root))
            ]
        directory = self._directory(scope, project_path)
        if not self._regular_directory(directory):
            return []
        found = []
        for item in sorted(directory.iterdir()):
            if not self._regular_directory(item):
                continue
            manifest = item / "SKILL.md"
            if not self._regular_file(manifest, 65536):
                continue
            try:
                description = self._description(manifest)
            except (OSError, UnicodeError):
                continue
            found.append(
                {
                    "name": item.name,
                    "path": str(item),
                    "description": description,
                    "scope": scope,
                    "project_path": project_path,
                }
            )
        return found

    def summary(self, project_paths=()) -> str:
        entries = self.list()
        for path in project_paths:
            entries.extend(self.list("project", str(path)))
        return "\n".join(
            f"- {item['scope']} {item['project_path'] or ''} | {item['name']}: {item['description']}"
            for item in entries[:100]
        )

    @staticmethod
    def _regular_directory(path: Path) -> bool:
        return (
            path.is_dir()
            and not path.is_symlink()
            and not getattr(path, "is_junction", lambda: False)()
        )

    @staticmethod
    def _regular_file(path: Path, limit: int) -> bool:
        return (
            path.is_file()
            and not path.is_symlink()
            and path.stat().st_nlink == 1
            and path.stat().st_size <= limit
        )

    @staticmethod
    def _description(manifest: Path) -> str:
        lines = manifest.read_text(encoding="utf-8-sig").splitlines()[:40]
        for index, line in enumerate(lines):
            if line.lower().startswith("description:"):
                value = line.partition(":")[2].strip().strip("\"'")
                if value in {">", "|", ">-", "|-"}:
                    parts = []
                    for next_line in lines[index + 1 :]:
                        if not next_line.startswith((" ", "\t")):
                            break
                        parts.append(next_line.strip())
                    value = " ".join(parts)
                return "".join(char for char in value if char.isprintable())[:160]
        return ""

    def _skill(self, name: str, scope: str, project_path: str | None) -> Path:
        item = next(
            (x for x in self.list(scope, project_path) if x["name"].casefold() == name.casefold()),
            None,
        )
        if item is None:
            raise ToolError("Skill is not installed; use /use to inspect the list.", "skill")
        directory = Path(item["path"])
        parent = self._directory(scope, project_path).resolve()
        if not self._regular_directory(directory) or directory.resolve().parent != parent:
            raise ToolError("Skill directory changed; retry.", "skill_path")
        return directory

    def load(self, name: str, scope: str = "global", project_path: str | None = None) -> str:
        manifest = self._skill(name, scope, project_path) / "SKILL.md"
        if not self._regular_file(manifest, 65536):
            raise ToolError("Skill manifest is missing or too large.", "skill")
        text = manifest.read_text(encoding="utf-8-sig")
        check_content(text)
        return text

    def save(
        self, name: str, text: str, scope: str = "global", project_path: str | None = None
    ) -> None:
        manifest = self._skill(name, scope, project_path) / "SKILL.md"
        if not self._regular_file(manifest, 65536):
            raise ToolError("Skill manifest changed; retry.", "skill_path")
        if len(text.encode("utf-8")) > 65536:
            raise ToolError("Skill manifest is too large.", "skill_limit")
        check_content(text)
        manifest.write_text(text, encoding="utf-8", newline="\n")

    def resource(
        self, name: str, relative_path: str, scope: str = "global", project_path: str | None = None
    ) -> str:
        skill = self._skill(name, scope, project_path)
        raw = Path(relative_path)
        if raw.is_absolute() or not raw.parts or ".." in raw.parts or ":" in relative_path:
            raise ToolError("Skill resource path must be relative to its skill.", "skill_path")
        if any(
            part.lower() in {".git", ".minicode", ".env"} or part.lower().startswith(".env.")
            for part in raw.parts
        ):
            raise ToolError("Sensitive skill resources are protected.", "sensitive")
        current = skill
        for part in raw.parts:
            current = current / part
            if current.is_symlink() or getattr(current, "is_junction", lambda: False)():
                raise ToolError("Linked skill resources are not readable.", "skill_path")
        target = current.resolve()
        if not target.is_relative_to(skill.resolve()) or not self._regular_file(target, 65536):
            raise ToolError("Skill resource is missing or too large.", "skill_path")
        text = target.read_text(encoding="utf-8-sig")
        if "\x00" in text:
            raise ToolError("Binary skill resource is not supported.", "binary")
        check_content(text)
        return text

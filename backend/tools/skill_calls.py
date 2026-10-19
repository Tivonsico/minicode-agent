from pathlib import Path

from .base import ToolError


async def execute(executor, name, cid, args):
    if name == "UseSkill":
        (scope, project_path) = (args.get("scope", "global"), args.get("project_path"))
        loaded = []
        for skill_name in args["names"]:
            loaded.append((skill_name, executor.skills.load(skill_name, scope, project_path)))
        content = "\n\n".join((f"# Skill: {skill_name}\n{body}" for (skill_name, body) in loaded))
        if len(content) > 65536:
            raise ToolError(
                "Selected skill texts are too long together; load fewer at once.", "skill_limit"
            )
        for skill_name, _ in loaded:
            executor.active_skills.add(
                (scope, str(project_path or "").casefold(), skill_name.casefold())
            )
        if scope == "project" and project_path:
            executor.touched_projects.add(Path(project_path).resolve())
    elif name == "ReadSkillResource":
        (scope, project_path) = (args.get("scope", "global"), args.get("project_path"))
        key = (scope, str(project_path or "").casefold(), args["name"].casefold())
        if key not in executor.active_skills:
            raise ToolError(
                "UseSkill must select this skill before reading its resources.", "skill"
            )
        content = executor.skills.resource(args["name"], args["path"], scope, project_path)
    return content

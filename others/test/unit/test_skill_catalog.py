import pytest

from backend.conversation.store import SessionStore
from backend.permissions.directories import SessionPermissions
from backend.skills import SkillCatalog
from backend.tools.base import ToolError
from backend.tools.executor import ToolExecutor
from test.support.tooling import Outputs, call


def install(root, name, description, body):
    directory = root / "skill" / "skills" / name
    directory.mkdir(parents=True)
    (directory / "SKILL.md").write_text(f"description: {description}\n{body}", encoding="utf-8")
    return directory


@pytest.mark.asyncio
async def test_discovery_multiple_skills_and_resource_boundary(tmp_path):
    first = install(tmp_path, "frontend", "Build UI", "Use the layout reference.")
    install(tmp_path, "tests", "Write tests", "Cover behavior.")
    (first / "layout.txt").write_text("three columns", encoding="utf-8")
    old = tmp_path / "skills" / "legacy"
    old.mkdir(parents=True)
    (old / "SKILL.md").write_text("description: stale", encoding="utf-8")
    catalog = SkillCatalog(tmp_path)
    assert [item["name"] for item in catalog.list()] == ["frontend", "tests"]
    assert "Build UI" in catalog.summary()
    executor = ToolExecutor(tmp_path, Outputs(tmp_path), lambda _: True, skills=catalog)
    blocked = await executor.execute_batch(
        [call("ReadSkillResource", {"name": "frontend", "path": "layout.txt"})]
    )
    assert blocked[0].error_type == "skill"
    loaded = await executor.execute_batch([call("UseSkill", {"names": ["frontend", "tests"]})])
    assert loaded[0].status == "succeeded"
    assert "Use the layout reference." in loaded[0].content
    assert "Cover behavior." in loaded[0].content
    resource = await executor.execute_batch(
        [call("ReadSkillResource", {"name": "frontend", "path": "layout.txt"})]
    )
    assert resource[0].content == "three columns"
    for path in ("../tests/SKILL.md", str(first / "layout.txt"), ".env"):
        denied = await executor.execute_batch(
            [call("ReadSkillResource", {"name": "frontend", "path": path})]
        )
        assert denied[0].status == "failed"
        executor.reset_turn()
    (first / "huge.txt").write_text("x" * 65537, encoding="utf-8")
    for path in ("huge.txt", "."):
        with pytest.raises(ToolError):
            catalog.resource("frontend", path)


def test_skill_resource_link_is_rejected(tmp_path):
    first = install(tmp_path, "frontend", "Build UI", "Use a resource.")
    outside = tmp_path / "outside.txt"
    outside.write_text("outside", encoding="utf-8")
    link = first / "link.txt"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("Symlink creation unavailable")
    with pytest.raises(ToolError):
        SkillCatalog(tmp_path).resource("frontend", "link.txt")


def test_multiline_skill_description_is_discovered(tmp_path):
    directory = tmp_path / "skill" / "skills" / "frontend"
    directory.mkdir(parents=True)
    (directory / "SKILL.md").write_text(
        "---\nname: frontend\ndescription: >\n  Build accessible\n  web pages.\n---\nUse it.",
        encoding="utf-8",
    )
    assert SkillCatalog(tmp_path).list()[0]["description"] == "Build accessible web pages."


@pytest.mark.asyncio
async def test_project_skill_uses_its_authorized_directory(tmp_path):
    app_root = tmp_path / "app"
    project = tmp_path / "project"
    app_root.mkdir()
    project.mkdir()
    grants = SessionPermissions(app_root, SessionStore(app_root))
    grants.add(str(project))
    local = project / "project_skills" / "review"
    local.mkdir()
    (local / "SKILL.md").write_text("description: Project review\nLOCAL RULE", encoding="utf-8")
    (local / "template.txt").write_text("local template", encoding="utf-8")
    global_skill = install(app_root, "review", "Global review", "GLOBAL RULE")
    catalog = SkillCatalog(app_root, grants.roots)
    assert catalog.load("review") == "description: Global review\nGLOBAL RULE"
    assert "LOCAL RULE" in catalog.load("review", "project", str(project))
    assert catalog.list("project")[0]["project_path"] == str(project)
    executor = ToolExecutor(
        app_root, Outputs(app_root), lambda _: True, session_permissions=grants, skills=catalog
    )
    loaded = await executor.execute_batch(
        [call("UseSkill", {"scope": "project", "project_path": str(project), "names": ["review"]})]
    )
    assert "LOCAL RULE" in loaded[0].content and "GLOBAL RULE" not in loaded[0].content
    resource = await executor.execute_batch(
        [
            call(
                "ReadSkillResource",
                {
                    "scope": "project",
                    "project_path": str(project),
                    "name": "review",
                    "path": "template.txt",
                },
            )
        ]
    )
    assert resource[0].content == "local template"
    denied = await executor.execute_batch(
        [call("UseSkill", {"scope": "project", "project_path": str(app_root), "names": ["review"]})]
    )
    assert denied[0].status == "failed"
    assert global_skill.is_dir()

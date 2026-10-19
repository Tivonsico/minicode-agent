import io
import stat
import zipfile

import httpx
import pytest

from backend.conversation.store import SessionStore
from backend.permissions.browser import DirectoryBrowser
from backend.permissions.directories import SessionPermissions
from backend.skills.catalog import SkillCatalog
from backend.skills.library import SkillLibrary
from backend.skills.repository import bundle, discover, repository_source
from backend.skills.search import search
from backend.tools.base import ToolError

REVISION = "a" * 40
TREE = "b" * 40


def test_directory_browser_never_grants_and_only_lists_plain_folders(tmp_path):
    app, project = tmp_path / "app", tmp_path / "project"
    app.mkdir()
    project.mkdir()
    (project / "src").mkdir()
    (project / ".ssh").mkdir()
    (project / "note.txt").write_text("not a folder")
    (project / "alias").symlink_to(app, target_is_directory=True)
    permission = SessionPermissions(app, SessionStore(app))
    browser = DirectoryBrowser(permission)
    page = browser.browse(str(project))
    assert page["selectable"] and [item["name"] for item in page["entries"]] == ["src"]
    assert permission.roots() == () and not permission.store.events
    assert not browser.browse(str(tmp_path))["selectable"]
    assert all(item["name"] != "app" for item in browser.browse(str(tmp_path))["entries"])
    for path in (project / ".ssh", project / "alias", project / "missing"):
        with pytest.raises(ToolError):
            browser.browse(str(path))


def test_create_skill_is_discoverable_and_existing_name_not_overwritten(tmp_path):
    catalog = SkillCatalog(tmp_path)
    library = SkillLibrary(catalog)
    library.create("review", "审查代码", "检查错误边界。")
    assert catalog.list()[0]["description"] == "审查代码"
    original = catalog.load("review")
    assert "检查错误边界" in original
    with pytest.raises(ToolError, match="同名"):
        library.create("REVIEW", "other", "overwrite")
    assert catalog.load("review") == original


@pytest.mark.parametrize(
    "name", ["../escape", "/absolute", "CON", "review:stream", "a/b", ".hidden"]
)
def test_portable_skill_name_prevents_escape(tmp_path, name):
    with pytest.raises(ToolError):
        SkillLibrary(SkillCatalog(tmp_path)).create(name, "description", "content")
    assert not (tmp_path / "skill").exists()


def test_project_create_requires_live_grant_and_preserves_resources(tmp_path):
    app, project = tmp_path / "app", tmp_path / "project"
    app.mkdir()
    project.mkdir()
    permission = SessionPermissions(app, SessionStore(app))
    catalog = SkillCatalog(app, permission.roots)
    library = SkillLibrary(catalog)
    files = {
        "SKILL.md": b"---\nname: review\ndescription: Review\n---\nDo it.",
        "references/check.md": b"Checklist",
    }
    with pytest.raises(ToolError):
        library.install("review", files, "project", str(project))
    permission.add(str(project))
    library.install("review", files, "project", str(project))
    assert catalog.resource("review", "references/check.md", "project", str(project)) == "Checklist"
    permission.remove(str(project))
    with pytest.raises(ToolError):
        library.create("second", "Review", "Rules", "project", str(project))


def test_staging_failure_does_not_install_partial_skill(tmp_path):
    library = SkillLibrary(SkillCatalog(tmp_path))
    with pytest.raises(ToolError):
        library.install(
            "review", {"SKILL.md": b"description: Review\nRule", "../escape.txt": b"escape"}
        )
    assert not (tmp_path / "skill/skills/review").exists()
    assert not (tmp_path / "escape.txt").exists()
    assert not list((tmp_path / "skill/skills").glob(".install-*"))


def test_linked_skill_storage_rejected(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (tmp_path / "skill").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ToolError):
        SkillLibrary(SkillCatalog(tmp_path)).create("review", "Review", "Rules")
    assert not list(outside.iterdir())


@pytest.mark.parametrize(
    "url",
    [
        "http://github.com/a/b",
        "https://evil.test/a/b",
        "https://github.com/a/b/tree/main/../x",
        "https://github.com/a/b?token=x",
    ],
)
def test_source_urls_only_allow_public_github(url):
    with pytest.raises(ToolError):
        repository_source(url)


async def test_discovery_pins_commit_not_tree_and_filters_skill_folder():
    def handler(request):
        assert "Authorization" not in request.headers
        if "/commits/" in request.url.path:
            return httpx.Response(200, json={"sha": REVISION})
        if "/git/trees/" in request.url.path:
            assert request.url.path.endswith(REVISION)
            return httpx.Response(
                200,
                json={
                    "sha": TREE,
                    "tree": [
                        {"path": "skills/review/SKILL.md", "type": "blob", "mode": "100644"},
                        {"path": "skills/other/SKILL.md", "type": "blob", "mode": "100644"},
                        {"path": "skills/linked/SKILL.md", "type": "blob", "mode": "120000"},
                    ],
                },
            )
        return httpx.Response(200, json={"default_branch": "main"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        items = await discover(client, "https://github.com/owner/repo/tree/main/skills/review")
        assert len(items) == 1 and items[0]["revision"] == REVISION
        assert items[0]["path"] == "skills/review"


def archive(entries):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zipped:
        for name, body, mode in entries:
            info = zipfile.ZipInfo("repo-commit/skills/review/" + name)
            info.external_attr = mode << 16
            zipped.writestr(info, body)
        zipped.writestr("repo-commit/unrelated.txt", "skip")
    return buffer.getvalue()


async def test_download_preserves_skill_bundle_without_executing_scripts():
    data = archive(
        [
            ("SKILL.md", "description: Review\nRules", stat.S_IFREG | 0o644),
            ("scripts/review.py", "raise RuntimeError('never execute')", stat.S_IFREG | 0o755),
            ("references/a.md", "notes", stat.S_IFREG | 0o644),
        ]
    )
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, content=data))
    ) as client:
        files = await bundle(client, "owner/repo", REVISION, "skills/review")
    assert set(files) == {"SKILL.md", "scripts/review.py", "references/a.md"}


@pytest.mark.parametrize(
    "filename,mode",
    [
        ("../outside", stat.S_IFREG),
        ("/absolute", stat.S_IFREG),
        (".env", stat.S_IFREG),
        ("linked", stat.S_IFLNK),
        ("a:stream", stat.S_IFREG),
    ],
)
async def test_unsafe_archive_rejected(filename, mode):
    data = archive([("SKILL.md", "description: Review", stat.S_IFREG), (filename, "unsafe", mode)])
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, content=data))
    ) as client:
        with pytest.raises(ToolError):
            await bundle(client, "owner/repo", REVISION, "skills/review")


async def test_name_search_returns_installable_public_sources():
    def handler(request):
        assert request.url.host == "skills.sh" and "Authorization" not in request.headers
        assert request.url.params["q"] == "frontend"
        return httpx.Response(
            200,
            json={
                "skills": [
                    {"name": "frontend-design", "source": "anthropics/skills", "installs": 123}
                ]
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        results = await search(client, "frontend")
    assert results["results"][0]["url"] == "https://github.com/anthropics/skills"
    assert results["source"] == "Skills.sh"

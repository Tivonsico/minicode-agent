"""Workspace snapshot contracts: independent history and actual file changes."""

import subprocess

from backend.snapshots import ProjectSnapshots


def test_snapshots_include_dirty_files_without_touching_user_git(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    subprocess.run(["git", "init", "-q", str(project)], check=True)
    target = project / "note.txt"
    target.write_text("staged\n")
    subprocess.run(["git", "-C", str(project), "add", "."], check=True)
    index_before = (project / ".git/index").read_bytes()
    target.write_text("user change\n")
    (tmp_path / "app").mkdir()
    history = ProjectSnapshots(tmp_path / "app", project)
    record = history.begin("session", "run", "turn")
    target.write_text("agent change\nsecond line\n")
    (project / "new.txt").write_text("new\n")
    history.finish(record["id"], "completed")
    changes = history.changes(record["id"])
    assert (changes["added"], changes["deleted"]) == (3, 1)
    note = next(f for f in changes["files"] if f["path"] == "note.txt")
    assert "-user change" in note["diff"] and "+agent change" in note["diff"]
    assert (project / ".git/index").read_bytes() == index_before
    assert not (project / ".git/refs/heads/main").exists()
    assert history.listing("session")[0]["run_id"] == "run"


def test_twenty_round_retention_reclaims_unreachable_content(tmp_path):
    (tmp_path / "app").mkdir()
    project = tmp_path / "project"
    project.mkdir()
    target = project / "note.txt"
    target.write_text("expired unique content\n")
    history = ProjectSnapshots(tmp_path / "app", project)
    first = history.begin("session", "r0", "t0")
    obsolete = subprocess.run(
        ["git", "--git-dir=" + str(history.repo), "rev-parse", first["before"] + ":note.txt"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    target.write_text("retained\n")
    history.finish(first["id"], "completed")
    for i in range(1, 21):
        record = history.begin("session", f"r{i}", f"t{i}")
        history.finish(record["id"], "completed")
    assert len(history.listing()) == 20
    result = subprocess.run(["git", "--git-dir=" + str(history.repo), "cat-file", "-e", obsolete],
                            capture_output=True)
    assert result.returncode != 0


def test_restore_requires_review_and_rejects_stale_preview(tmp_path):
    import pytest

    (tmp_path / "app").mkdir()
    project = tmp_path / "project"
    project.mkdir()
    target = project / "note.txt"
    target.write_text("user dirty\n")
    history = ProjectSnapshots(tmp_path / "app", project)
    record = history.begin("session", "r", "t")
    target.write_text("agent change\n")
    (project / "new.txt").write_text("created\n")
    history.finish(record["id"], "completed")
    target.write_text("later manual edit\n")
    plan = history.preview_restore(record["id"])
    assert plan["conflicts"] == ["note.txt"]
    with pytest.raises(ValueError, match="确认"):
        history.restore(record["id"], plan["token"])
    target.write_text("changed after preview\n")
    with pytest.raises(ValueError, match="变化"):
        history.restore(record["id"], plan["token"], accept_conflicts=True)
    plan = history.preview_restore(record["id"])
    backup = history.restore(record["id"], plan["token"], accept_conflicts=True)
    assert target.read_text() == "user dirty\n"
    assert not (project / "new.txt").exists()
    assert history.record(backup["id"])["pinned"]
    assert history.changes(backup["id"])["deleted"] == 2


def test_ignore_sensitive_links_binary_and_deleted_files(tmp_path):
    (tmp_path / "app").mkdir()
    project = tmp_path / "project"
    project.mkdir()
    (project / ".gitignore").write_text("cache/\n")
    (project / "cache").mkdir()
    (project / "cache/ignored").write_text("ignored")
    (project / ".env").write_text("SECRET=never-capture")
    (project / "ordinary.txt").write_text("-----BEGIN PRIVATE KEY-----\nnever-capture\n")
    (project / "link").symlink_to(project / ".env")
    (project / "binary.bin").write_bytes(b"a\x00b")
    (project / "gone.txt").write_text("one\ntwo\n")
    history = ProjectSnapshots(tmp_path / "app", project)
    record = history.begin("s", "r", "t")
    assert set(history.files(record["before"])) == {".gitignore", "binary.bin", "gone.txt"}
    (project / "binary.bin").write_bytes(b"c\x00d")
    (project / "gone.txt").unlink()
    history.finish(record["id"], "failed")
    changes = history.changes(record["id"])
    assert (changes["added"], changes["deleted"]) == (0, 2)
    binary = next(f for f in changes["files"] if f["path"] == "binary.bin")
    assert binary["added"] is None and "二进制" in binary["reason"]
    plan = history.preview_restore(record["id"])
    history.restore(record["id"], plan["token"])
    assert (project / "binary.bin").read_bytes() == b"a\x00b"
    assert (project / ".env").read_text() == "SECRET=never-capture"


def test_snapshot_rejects_parent_link_swap_before_read(tmp_path, monkeypatch):
    import pytest

    (tmp_path / "app").mkdir()
    project, outside = tmp_path / "project", tmp_path / "outside"
    project.mkdir()
    outside.mkdir()
    (project / "nested").mkdir()
    (project / "nested/note.txt").write_text("authorized\n")
    (outside / "note.txt").write_text("outside authorization\n")
    history = ProjectSnapshots(tmp_path / "app", project)
    resolve = history.policy.resolve
    swapped = False

    def replace_parent(*args, **kwargs):
        nonlocal swapped
        result = resolve(*args, **kwargs)
        if not swapped:
            swapped = True
            (project / "nested").rename(project / "original")
            (project / "nested").symlink_to(outside, target_is_directory=True)
        return result

    monkeypatch.setattr(history.policy, "resolve", replace_parent)
    with pytest.raises((OSError, ValueError)):
        history.begin("s", "r", "t")
    assert history.listing() == []


def test_running_and_pinned_states_survive_retention(tmp_path):
    (tmp_path / "app").mkdir()
    project = tmp_path / "project"
    project.mkdir()
    (project / "note.txt").write_text("before\n")
    history = ProjectSnapshots(tmp_path / "app", project, keep=1)
    unfinished = history.begin("s", "unfinished", "t0")
    pinned = history.begin("s", "pinned", "t1")
    history.finish(pinned["id"], "completed", pinned=True)
    for i in range(2):
        round = history.begin("s", str(i), str(i))
        history.finish(round["id"], "completed")
    assert {r["id"] for r in history.listing()} == {unfinished["id"], pinned["id"], round["id"]}
    history.pin(pinned["id"], False)
    assert {r["id"] for r in history.listing()} == {unfinished["id"], round["id"]}


def test_files_that_become_ignored_are_never_overwritten_by_restore(tmp_path):
    (tmp_path / "app").mkdir()
    project = tmp_path / "project"
    project.mkdir()
    target = project / "note.txt"
    target.write_text("before\n")
    history = ProjectSnapshots(tmp_path / "app", project)
    record = history.begin("s", "r", "t")
    target.write_text("after\n")
    history.finish(record["id"], "completed")
    (project / ".gitignore").write_text("note.txt\n")
    target.write_text("ignored manual content\n")
    plan = history.preview_restore(record["id"])
    assert "note.txt" in plan["excluded"]
    history.restore(record["id"], plan["token"], accept_conflicts=True)
    assert target.read_text() == "ignored manual content\n"

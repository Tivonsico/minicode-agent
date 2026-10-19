import json

import pytest

from backend.conversation.store import SessionStore
from backend.storage.output_store import OutputStore


def test_uncertain_sync_freezes_writer_until_reopen(tmp_path, monkeypatch):
    from backend.conversation import store as module

    store = SessionStore(tmp_path)
    real_sync = module.os.fsync
    monkeypatch.setattr(module.os, "fsync", lambda fd: (_ for _ in ()).throw(OSError("disk")))
    with pytest.raises(OSError):
        store.append("notice", text="first")
    monkeypatch.setattr(module.os, "fsync", real_sync)
    with pytest.raises(OSError, match="reopen"):
        store.append("notice", text="must not reuse seq")
    reopened = SessionStore(tmp_path, store.session_id)
    reopened.append("notice", text="after verification")
    assert [e["seq"] for e in reopened.events] == [1, 2]


def test_output_sync_failure_cannot_publish_a_durable_reference(tmp_path, monkeypatch):
    import os

    store = SessionStore(tmp_path)

    def fail(fd):
        raise OSError("output not durable")

    monkeypatch.setattr(os, "fsync", fail)
    with pytest.raises(OSError):
        OutputStore(store).save("one", "important evidence")
    assert not store.path.exists() or "output_saved" not in store.path.read_text()


def test_incomplete_tail_is_preserved_and_meta_rebuilt(tmp_path):
    store = SessionStore(tmp_path, "one")
    store.append("message", message_id="u", role="user", content="Original task")
    store.append("assistant_delta", message_id="a", kind="text", content="partial")
    with store.path.open("ab") as file:
        file.write(b'{"seq":3,"content":"incomplete')
    store.meta_path.write_text("broken", encoding="utf-8")
    restored = SessionStore(tmp_path, "one")
    assert len(restored.events) == 2
    assert restored.meta["message_count"] == 1
    assert restored.meta["title"] == "Original task"
    assert next(restored.directory.glob("incomplete-*.bin")).read_bytes().startswith(b'{"seq":3')
    restored.append(
        "message", message_id="a", role="assistant", content="partial", status="partial"
    )
    assert [
        json.loads(line)["seq"] for line in restored.path.read_text(encoding="utf-8").splitlines()
    ] == [1, 2, 3]
    assert restored.meta["message_count"] == 2


def test_complete_corrupt_record_is_not_silently_discarded(tmp_path):
    store = SessionStore(tmp_path)
    store.path.write_bytes(b"broken\n")
    with pytest.raises(ValueError, match="Corrupt journal"):
        SessionStore(tmp_path, store.session_id)
    assert store.path.read_bytes() == b"broken\n"


def test_usage_reconciliation_counts_request_once_and_preserves_unknown(tmp_path):
    store = SessionStore(tmp_path)
    store.append("usage", request_id="r1", purpose="main", usage=None)
    store.append("usage", request_id="r1", purpose="main", usage={"total_tokens": 42})
    store.append("usage", request_id="r1", purpose="main", usage={"total_tokens": 42})
    store.append("usage", request_id="r2", purpose="memory", usage=None)
    assert store.meta["token_total"] == 42
    assert store.meta["unknown_requests"] == 1
    assert store.meta["usage_incomplete"]
    assert store.meta["usage_by_purpose"]["main"]["tokens"] == 42
    assert SessionStore(tmp_path, store.session_id).meta["token_total"] == 42


def test_output_refs_whitelist_only_own_registered_output(tmp_path):
    one, two = SessionStore(tmp_path, "one"), SessionStore(tmp_path, "two")
    outputs = OutputStore(one)
    own = outputs.save("call", "original evidence")
    foreign = OutputStore(two).save("call", "other session")
    one.append("output_saved", ref="../two/" + foreign["ref"])
    one.append("output_saved", ref=str(tmp_path / foreign["path"]))
    one.append("output_saved", ref="session.jsonl")
    assert outputs.allowed_paths() == {(tmp_path / own["path"]).resolve()}
    changed = outputs.save("call", "new evidence")
    assert changed["ref"] != own["ref"]
    assert (tmp_path / own["path"]).read_text() == "original evidence"


def test_preview_tiny_limit_does_not_append_full_output(tmp_path):
    store = SessionStore(tmp_path)
    text = "x" * 10000
    preview = OutputStore(store).preview("call", text, limit=1)
    assert text not in preview
    assert len(preview) < 1000
    assert (store.directory / store.events[-1]["ref"]).read_text() == text


def test_session_rejects_internal_directory_link(tmp_path):
    project, outside = tmp_path / "project", tmp_path / "outside"
    project.mkdir()
    outside.mkdir()
    try:
        (project / ".minicode").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("Symlink creation is unavailable to this Windows user")
    with pytest.raises(ValueError, match="link"):
        SessionStore(project)
    assert list(outside.iterdir()) == []


@pytest.mark.parametrize("session_id", ["../escape", "one/two", "one\\two", ".", ""])
def test_invalid_session_ids_cannot_escape(tmp_path, session_id):
    if session_id == "":
        # Empty input intentionally selects a fresh generated ID.
        assert SessionStore(tmp_path, session_id).directory.parent.name == "sessions"
    else:
        with pytest.raises(ValueError):
            SessionStore(tmp_path, session_id)


def test_known_secret_redacted_before_journal_and_outputs(tmp_path):
    from backend.conversation.store import SessionStore
    from backend.storage.output_store import OutputStore

    secret = "never-persist-this-secret"
    store = SessionStore(tmp_path, secrets=(secret,))
    store.append("message", message_id="secret-message", role="assistant", content=secret)
    assert secret not in store.path.read_text(encoding="utf-8")
    outputs = OutputStore(store, secrets=(secret,))
    record = outputs.save("secret-call", secret)
    assert secret not in (store.directory / record["ref"]).read_text(encoding="utf-8")
    assert outputs.preview("preview", secret) == "[REDACTED]"


def test_atomic_metadata_retries_transient_windows_lock_but_not_permanent_error(
    tmp_path, monkeypatch
):
    import json

    import pytest

    from backend.conversation import store as session_store

    original = session_store.os.replace
    calls = []

    def replace(source, target):
        calls.append(1)
        if len(calls) < 3:
            error = PermissionError("locked")
            error.winerror = 32
            raise error
        return original(source, target)

    monkeypatch.setattr(session_store.os, "replace", replace)
    session_store.atomic_json(tmp_path / "meta.json", {"saved": True})
    assert len(calls) == 3
    assert json.loads((tmp_path / "meta.json").read_text()) == {"saved": True}

    def denied(source, target):
        raise PermissionError("permanent")

    monkeypatch.setattr(session_store.os, "replace", denied)
    with pytest.raises(PermissionError):
        session_store.atomic_json(tmp_path / "meta.json", {"saved": False})
    assert json.loads((tmp_path / "meta.json").read_text()) == {"saved": True}

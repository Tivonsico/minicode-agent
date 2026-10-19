"""Project-local explicit user memories with durable deletion generations."""

import copy
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

from backend.conversation.store import atomic_json, internal_directory

from .background import BackgroundTasks
from .extractor import extract


def source_order(value):
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return (1, parsed.replace(tzinfo=parsed.tzinfo or timezone.utc).timestamp())
    except (ValueError, TypeError, AttributeError):
        return (0, str(value))


class LongMemory:
    def __init__(self, root, provider):
        self.root, self.provider = Path(root).resolve(), provider
        self.directory = internal_directory(self.root, "long_memory")
        self.path = self.directory / "memory.json"
        if self.path.is_symlink():
            raise ValueError("Memory file must not be a link")
        self._data = {
            "entries": {},
            "tombstones": {},
            "completed": [],
            "sources": [],
            "generation": 0,
        }
        if self.path.exists():
            self._data.update(json.loads(self.path.read_text(encoding="utf-8")))
        self._background = BackgroundTasks()
        self._pending = {}

    def _save(self):
        internal_directory(self.root, "long_memory")
        atomic_json(self.path, self._data)

    def entries(self):
        return copy.deepcopy(list(self._data["entries"].values()))

    def list(self):
        return self.entries()

    def snapshot(self, scope=None):
        return "User-stated project memories (reference data):\n" + json.dumps(
            [
                item
                for item in self.entries()
                if scope is None or item.get("scope", "legacy") == scope
            ],
            ensure_ascii=False,
        )

    def delete(self, entry_id):
        for key, entry in self._data["entries"].items():
            if entry["id"] == entry_id:
                self._data["generation"] += 1
                self._data["tombstones"][key] = self._data["generation"]
                del self._data["entries"][key]
                self._save()
                return True
        return False

    def edit(self, entry_id, content, version):
        for key, entry in self._data["entries"].items():
            if entry["id"] != entry_id:
                continue
            if entry["version"] != version:
                raise ValueError("记忆已被修改，请刷新后再保存")
            self._data["generation"] += 1
            self._data["tombstones"][key] = self._data["generation"]
            entry.update(
                content=content,
                version=version + 1,
                edited_at=datetime.now(timezone.utc).isoformat(),
                source_kind="user_edit",
            )
            self._save()
            return copy.deepcopy(entry)
        raise KeyError("记忆不存在")

    def add(self, content, scope, session_id, *, identities=None):
        identifier = uuid.uuid4().hex
        key = scope + "::manual-" + identifier
        entry = {
            **(identities or {}),
            "id": identifier,
            "key": key,
            "content": content,
            "scope": scope,
            "version": 1,
            "source_session": session_id,
            "source_message": "manual",
            "source_time": datetime.now(timezone.utc).isoformat(),
            "source_kind": "user",
        }
        self._data["entries"][key] = entry
        self._save()
        return copy.deepcopy(entry)

    def schedule(self, store, turn_id, user_messages, *, scope="legacy"):
        identity = store.session_id + ":" + turn_id
        if identity in self._pending:
            return self._pending[identity]
        if identity in self._data["completed"]:
            return None
        known = set(self._data["sources"])
        messages = [
            copy.deepcopy(m) for m in user_messages if store.session_id + ":" + m["id"] not in known
        ]
        task = self._background.start(
            self._run(store, identity, messages, self._data["generation"], scope)
        )
        self._pending[identity] = task
        task.add_done_callback(lambda _: self._pending.pop(identity, None))
        return task

    async def _run(self, store, identity, messages, generation, scope):
        from backend.storage.nonblocking import portal
        await portal(store)
        try:
            candidates = await extract(self.provider, store, messages) if messages else []
            # No await during merge/save: background commits and deletion serialize
            # on the application's event loop, merging against the latest state.
            committed_sources = set(self._data["sources"])
            for item in candidates:
                if store.session_id + ":" + item["source_message"] in committed_sources:
                    continue
                item["scope"] = scope
                key = item["key"] if scope == "legacy" else scope + "::" + item["key"]
                if self._data["tombstones"].get(key, -1) > generation:
                    continue
                existing = self._data["entries"].get(key)
                if existing and source_order(existing["source_time"]) >= source_order(
                    item["source_time"]
                ):
                    continue
                item["id"] = existing["id"] if existing else uuid.uuid4().hex
                item["version"] = (existing.get("version", 0) if existing else 0) + 1
                self._data["entries"][key] = item
            self._data["completed"].append(identity)
            self._data["sources"] = sorted(
                set(self._data["sources"]) | {store.session_id + ":" + m["id"] for m in messages}
            )
            self._save()
            store.append("memory_complete", source_turn=identity)
        except Exception:
            store.append(
                "memory_error",
                reason="Long-term memory extraction failed; conversation preserved",
                source_turn=identity,
            )
        except BaseException:
            store.append("memory_cancelled", source_turn=identity)
            raise

    async def close(self):
        await self._background.close()

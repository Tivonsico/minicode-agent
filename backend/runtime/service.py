"""Application ownership: session registry, shared memory and shutdown."""

from backend.config import ConfigError
from backend.context.project_assets import ProjectAssets
from backend.conversation.store import SessionStore
from backend.execution.codex import CodexSandbox
from backend.long_memory.manager import LongMemory
from backend.storage.mysql import MySQLDatabase
from backend.storage.mysql_store import MySQLSessionStore
from backend.storage.settings import ensure_mode_switch_safe, record_active_mode

from .provider import Provider
from .session import RuntimeSession


class RuntimeService:
    def __init__(self, config, provider=None, *, database=None):
        if not config.context_window or not config.max_output_tokens:
            raise ConfigError("模型能力尚未解析，请先调用 backend.models.resolve_config。")
        self.config = config
        self.database = (database or MySQLDatabase(config)) if config.storage_mode == "mysql" else None
        ensure_mode_switch_safe(config, self.database)
        record_active_mode(config)
        self.provider = provider or Provider(config)
        self.long_memory = LongMemory(config.root, self.provider)
        self.sandbox = CodexSandbox(config.root, lambda: ())
        self.sessions = {}
        self.command_locks = {}
        self.evaluations = None
        self.swe_evaluations = None
        ProjectAssets(config.root).ensure_global()

    async def initialize(self):
        await self.sandbox.verify()

    def list_sessions(self):
        if self.database:
            import json

            result = []
            for row in self.database.query("SELECT meta,owner FROM mc_sessions"):
                meta = json.loads(row["meta"])
                live = self.sessions.get(meta["session_id"])
                result.append({**meta, "owner_held": row["owner"] is not None,
                               "owner_revision": row["owner"], "owned_here": live is not None,
                               "owner_needs_review": bool(live and live.store.failed)})
            return sorted(result, key=lambda item: item["last_active_at"], reverse=True)
        # Do not reopen live journals and risk repairing a currently written tail.
        live = {sid: session.store.meta for sid, session in self.sessions.items()}
        result = []
        parent = self.config.root / ".minicode" / "sessions"
        if parent.is_dir():
            for item in parent.iterdir():
                if item.name in live:
                    result.append(live[item.name])
                elif (item.is_dir() and not item.is_symlink() and item.name.isalnum()
                      and ((item / "session.meta").exists() or (item / "session.jsonl").exists())):
                    try:
                        result.append(SessionStore(self.config.root, item.name).meta)
                    except (OSError, ValueError):
                        result.append(
                            {
                                "session_id": item.name,
                                "title": "损坏的会话",
                                "status": "error",
                                "last_active_at": "",
                            }
                        )
        return sorted(result, key=lambda item: item["last_active_at"], reverse=True)

    def create(self):
        store = self.make_store()
        return self.get(store.session_id)

    def make_store(self, session_id=None):
        secrets = (self.config.api_key, self.config.mysql_password)
        if self.database:
            return MySQLSessionStore(self.database, self.config.root, session_id, secrets=secrets)
        return SessionStore(self.config.root, session_id, secrets=secrets)

    def get(self, session_id):
        if session_id not in self.sessions:
            if (
                not session_id.isalnum()
                or (not self.database and not (
                    self.config.root / ".minicode" / "sessions" / session_id / "session.meta"
                ).is_file())
            ):
                raise KeyError("会话不存在")
            store = self.make_store(session_id)
            try:
                if self.database:
                    store.acquire_owner()
                self.sessions[session_id] = RuntimeSession(
                    self.config, session_id, self.provider, self.long_memory, self.sandbox, store=store)
                if self.database:
                    from backend.execution.mysql_gate import MySQLExecutionLease
                    MySQLExecutionLease(store, [], True).release(all_session=True)
            except BaseException:
                self.sessions.pop(session_id, None)
                if self.database and store.owner and not store.failed:
                    store.close()
                raise
        return self.sessions[session_id]

    def recover_owner(self, session_id, expected_owner, evidence, processes_stopped):
        live = self.sessions.get(session_id)
        if not self.database or (live and (live.busy() or not live.store.failed)):
            raise ValueError("只能解除已停止执行者或已冻结 writer 遗留的 MySQL 会话占用")
        if not processes_stopped or not evidence.strip() or not expected_owner:
            raise ValueError("必须确认旧执行者及子进程停止，并填写核实证据")
        store = self.make_store(session_id)
        store.owner = expected_owner
        store.append("owner_reconciled", actor="local_user", evidence=evidence.strip(),
                     processes_stopped=True, previous_owner=expected_owner)
        store.close()
        self.sessions.pop(session_id, None)

    async def close(self):
        if self.database:
            import greenback
            await greenback.ensure_portal()
        if self.swe_evaluations:
            await self.swe_evaluations.close()
        if self.evaluations:
            await self.evaluations.close()
        for session in self.sessions.values():
            await session.cancel()
        await self.long_memory.close()
        if self.database:
            try:
                for session in self.sessions.values():
                    session.store.close()
                from backend.execution.mysql_domain import unregister_clean_domain
                await unregister_clean_domain(self.database)
            finally:
                self.database.close()

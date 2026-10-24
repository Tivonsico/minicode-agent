"""Session association events share the local/MySQL durable store interface."""

import asyncio

from .lifecycle import file_io, outer_roots, workspace_history_gate
from .repository import ProjectSnapshots


class SessionSnapshots:
    def __init__(self, session):
        self.session = session
        self.active = []
        self.entered = False

    def repositories(self):
        return [ProjectSnapshots(self.session.config.root, root)
                for root in outer_roots(self.session.permissions.roots())]

    def listing(self):
        records = []
        for repository in self.repositories():
            records.extend(repository.listing(self.session.store.session_id))
        return sorted(records, key=lambda r: r["created_at"], reverse=True)

    def resolve(self, project_id, round_id):
        for repository in self.repositories():
            if repository.project_id == project_id:
                record = repository.record(round_id)
                if record["session_id"] != self.session.store.session_id:
                    break
                return repository, record
        raise ValueError("快照不存在或项目目录未授权")

    async def begin(self):
        session = self.session
        repositories = await file_io(self.repositories)
        for repository in repositories:
            # Append active state inside the worker: cancellation joins the worker
            # before cleanup, so an already saved start state cannot be lost.
            def capture(repository=repository):
                record = repository.begin(session.store.session_id, session.run_id, session.turn_id)
                self.active.append((repository, record))
                return record
            record = await file_io(capture)
            session.publish("file_snapshot_started", snapshot=record)

    async def finish(self):
        session = self.session
        active, self.active = self.active, []
        for repository, record in active:
            try:
                unknown = session.executor.pause_reason == "termination_unknown"
                result = await file_io(repository.finish, record["id"],
                                       "unknown" if unknown else session.loop.status,
                                       pinned=unknown)
                changes = await file_io(repository.changes, record["id"])
                # Store identifiers/counts, not file content, in conversation/MySQL.
                session.publish("file_snapshot_completed", snapshot=result,
                                added=changes["added"], deleted=changes["deleted"])
            except Exception:
                session.publish("notice", text="结束快照保存失败；开始快照仍保留，请检查磁盘后核查项目文件。")

    async def run(self, action):
        roots = self.session.permissions.roots()
        async with workspace_history_gate(roots):
            self.entered = True
            try:
                await self.begin()
                await action()
            except BaseException as error:
                self.session.loop.status = ("interrupted" if isinstance(error, asyncio.CancelledError)
                                            else "failed")
                raise
            finally:
                # Repeated Stop requests cannot interrupt the end-state capture
                # or leave entries to be accidentally finalized by the next run.
                cleanup = asyncio.create_task(self.finish())
                cancelled = False
                while not cleanup.done():
                    try:
                        await asyncio.shield(cleanup)
                    except asyncio.CancelledError:
                        cancelled = True
                cleanup.result()
                if cancelled:
                    raise asyncio.CancelledError

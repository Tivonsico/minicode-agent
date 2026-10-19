"""Bind memory commits and retrieval to a project's active permission root."""


class ScopedMemory:
    def __init__(self, manager, permissions):
        self.manager, self.permissions = manager, permissions

    def scope(self):
        root = self.permissions.active()
        return str(root) if root else "unscoped"

    def snapshot(self):
        return self.manager.snapshot(self.scope())

    def schedule(self, store, turn_id, messages):
        return self.manager.schedule(store, turn_id, messages, scope=self.scope())

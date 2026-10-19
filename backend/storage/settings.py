"""Local startup settings. Password is write-only in the browser interface."""
import json
import os
import uuid
from dataclasses import replace

from backend.conversation.store import internal_directory, sync_directory

FIELDS = ("storage_mode", "mysql_host", "mysql_port", "mysql_database", "mysql_user",
          "mysql_password", "mysql_ssl_ca", "mysql_pool_size")
ENVIRONMENT = {key: "MINICODE_" + key.upper() for key in FIELDS}


def read_settings(root):
    path = root / ".minicode" / "storage.json"
    if path.is_symlink() or path.parent.is_symlink():
        raise ValueError("存储配置路径不能是链接")
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.keys() - set(FIELDS):
        raise ValueError("存储配置格式无效")
    return data


def candidate(config, values):
    values = dict(values)
    if not values.get("mysql_password"):
        values["mysql_password"] = read_settings(config.root).get("mysql_password", config.mysql_password)
    return replace(config, **values)


def save_settings(config):
    directory = internal_directory(config.root)
    path = directory / "storage.json"
    if path.is_symlink():
        raise ValueError("存储配置不能是链接")
    temporary = directory / (uuid.uuid4().hex + ".tmp")
    try:
        fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as file:
            json.dump({key: getattr(config, key) for key in FIELDS}, file, ensure_ascii=False)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, path)
        sync_directory(directory)
    finally:
        temporary.unlink(missing_ok=True)


def status(service):
    config = service.config
    saved = read_settings(config.root)
    configured = {key: saved.get(key, getattr(config, key)) for key in FIELDS}
    connected = False
    if service.database:
        try:
            service.database.query("SELECT 1")
            connected = True
        except OSError:
            pass
    return {"active_mode": config.storage_mode, "connected": connected,
            "configured": {k: v for k, v in configured.items() if k != "mysql_password"},
            "password_configured": bool(configured["mysql_password"]),
            "restart_required": any(configured[k] != getattr(config, k) for k in FIELDS),
            "environment_overrides": [v for v in ENVIRONMENT.values() if v in os.environ]}


def ensure_mode_switch_safe(config, database=None, allowed_owners=()):
    """A restart cannot bypass unknown work left by the previous backend."""
    path = config.root / ".minicode" / "storage-active.json"
    if path.is_symlink():
        raise ValueError("存储模式记录不能是链接")
    previous = json.loads(path.read_text()) if path.exists() else {"mode": "local"}
    if previous["mode"] == config.storage_mode:
        return
    if previous["mode"] == "local":
        from backend.execution.gate import ExecutionLease, lock_directory
        if ExecutionLease(lock_directory()).pending() is not None:
            raise ValueError("本地模式仍有未核实操作，请先在本地模式核实")
    else:
        from backend.storage.mysql import MySQLDatabase
        source = database or MySQLDatabase(config)
        try:
            if (source.query("SELECT id FROM mc_claims LIMIT 1")
                or source.query("SELECT id FROM mc_operations WHERE status IN ('STARTED','UNKNOWN') LIMIT 1")
                or any(row["owner"] not in allowed_owners for row in
                       source.query("SELECT owner FROM mc_sessions WHERE owner IS NOT NULL"))):
                raise ValueError("MySQL 仍有执行占用或未核实操作，请先停止旧执行者并在 MySQL 模式核实")
        finally:
            if source is not database:
                source.close()


def record_active_mode(config):
    from backend.conversation.store import atomic_json
    atomic_json(internal_directory(config.root) / "storage-active.json", {"mode": config.storage_mode})

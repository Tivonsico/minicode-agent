"""One database execution domain per OS user; persists across unknown effects.

This small coordination marker contains no events or credentials. It keeps local
mode and another database from bypassing the authoritative MySQL workspace claims.
"""
import json

from backend.conversation.store import atomic_json, sync_directory
from backend.tools.base import ToolError

from . import gate


def domain_identity(database):
    config = database.config
    return {"host": config.mysql_host, "port": config.mysql_port,
            "database": config.mysql_database, "root": str(config.root)}


def register_domain(database):
    path = gate.mysql_domain_path()
    identity = domain_identity(database)
    if path.parent.is_symlink():
        raise ValueError("MySQL 执行域目录不能是链接")
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.is_symlink():
        raise ValueError("MySQL 执行域标记不能是链接")
    if path.exists():
        if json.loads(path.read_text()) != identity:
            raise ToolError("当前用户已有其他 MySQL 执行域；请核实并正常关闭原实例后再使用新数据库。",
                            "termination_unknown")
    else:
        atomic_json(path, identity)


async def unregister_clean_domain(database):
    async with gate.execution_gate(reconciliation=True, lock_name="domain.lock"):
        path = gate.mysql_domain_path()
        if not path.exists() or json.loads(path.read_text()) != domain_identity(database):
            return
        if (database.query("SELECT id FROM mc_claims LIMIT 1")
            or database.query("SELECT id FROM mc_operations WHERE status IN ('STARTED','UNKNOWN') LIMIT 1")
            or database.query("SELECT id FROM mc_sessions WHERE owner IS NOT NULL LIMIT 1")):
            return
        path.unlink()
        sync_directory(path.parent)

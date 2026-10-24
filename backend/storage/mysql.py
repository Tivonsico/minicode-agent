"""Bounded MySQL connections and short, receipt-addressed InnoDB transactions.

A receipt is inserted before other writes. On a lost COMMIT acknowledgement a
locking read on that stable ID waits for the original transaction to resolve.
No callback is replayed, and no external I/O belongs in a transaction callback.
"""
from __future__ import annotations

import asyncio
import contextvars
import json
import queue
import socket
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager

import greenback
import pymysql

from backend.config import ConfigError

from .nonblocking import in_portal, io_method

DDL = (
    "CREATE TABLE IF NOT EXISTS mc_schema (version INT PRIMARY KEY) ENGINE=InnoDB",
    """CREATE TABLE IF NOT EXISTS mc_receipts (
        id CHAR(32) PRIMARY KEY, result LONGTEXT NOT NULL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP) ENGINE=InnoDB""",
    """CREATE TABLE IF NOT EXISTS mc_sessions (
        id VARCHAR(128) CHARACTER SET ascii COLLATE ascii_bin PRIMARY KEY,
        seq BIGINT NOT NULL DEFAULT 0, meta LONGTEXT NOT NULL,
        state LONGTEXT NOT NULL, owner CHAR(32) NULL) ENGINE=InnoDB""",
    """CREATE TABLE IF NOT EXISTS mc_events (
        session_id VARCHAR(128) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
        seq BIGINT NOT NULL, event_id CHAR(32) NOT NULL, body LONGTEXT NOT NULL,
        PRIMARY KEY(session_id, seq), UNIQUE KEY(event_id)) ENGINE=InnoDB""",
    """CREATE TABLE IF NOT EXISTS mc_journal_snapshots (
        session_id VARCHAR(128) CHARACTER SET ascii COLLATE ascii_bin PRIMARY KEY,
        covered_seq BIGINT NOT NULL, body LONGTEXT NOT NULL) ENGINE=InnoDB""",
    """CREATE TABLE IF NOT EXISTS mc_operations (
        id CHAR(64) PRIMARY KEY,
        session_id VARCHAR(128) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
        status VARCHAR(32) NOT NULL, result_seq BIGINT NULL, body LONGTEXT NOT NULL,
        KEY(session_id)) ENGINE=InnoDB""",
    """CREATE TABLE IF NOT EXISTS mc_mutex (
        id INT PRIMARY KEY, host LONGTEXT NOT NULL, root LONGTEXT NOT NULL) ENGINE=InnoDB""",
    """CREATE TABLE IF NOT EXISTS mc_claims (
        id CHAR(32) PRIMARY KEY,
        session_id VARCHAR(128) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
        owner CHAR(32) NOT NULL, roots LONGTEXT NOT NULL,
        KEY(session_id)) ENGINE=InnoDB""",
)


def encoded(value):
    return json.dumps(value, ensure_ascii=False)


class MySQLDatabase:
    def __init__(self, config, *, connect=None):
        self.config = config
        self._idle = queue.LifoQueue()
        self._slots = threading.BoundedSemaphore(config.mysql_pool_size)
        self._closed = False
        self._workers = ThreadPoolExecutor(max_workers=config.mysql_pool_size,
                                           thread_name_prefix="minicode-mysql")
        self._backpressure = asyncio.Semaphore(config.mysql_pool_size * 2)
        options = dict(host=config.mysql_host, port=config.mysql_port,
                       user=config.mysql_user, password=config.mysql_password,
                       database=config.mysql_database, charset="utf8mb4", autocommit=True,
                       cursorclass=pymysql.cursors.DictCursor,
                       connect_timeout=5, read_timeout=10, write_timeout=10)
        if config.mysql_ssl_ca:
            options.update(ssl_ca=config.mysql_ssl_ca, ssl_verify_cert=True,
                           ssl_verify_identity=True)
        self._connect = connect or (lambda: pymysql.connect(**options))
        try:
            self.initialize()
        except Exception:
            self.close()
            raise ConfigError("MySQL 初始化失败，请检查连接、建表权限及同机应用目录配置。") from None

    def run_io(self, function):
        if not in_portal():
            return function()
        return greenback.await_(self._run_io(function))

    async def _run_io(self, function):
        async with self._backpressure:
            context = contextvars.copy_context()
            future = asyncio.get_running_loop().run_in_executor(self._workers, context.run, function)
            try:
                return await asyncio.shield(future)
            except asyncio.CancelledError:
                # The transaction thread cannot be cancelled. Wait for its confirmed
                # outcome before any cleanup or workspace-claim release is allowed.
                while not future.done():
                    try:
                        await asyncio.shield(future)
                    except asyncio.CancelledError:
                        continue
                future.result()
                raise

    @contextmanager
    def connection(self):
        if self._closed or not self._slots.acquire(timeout=10):
            raise OSError("MySQL 连接池不可用")
        connection = None
        try:
            try:
                connection = self._idle.get_nowait()
                connection.ping(reconnect=False)
            except queue.Empty:
                connection = self._connect()
            yield connection
        except BaseException:
            if connection is not None:
                connection.close()
                connection = None
            raise
        finally:
            if connection is not None:
                if self._closed:
                    connection.close()
                else:
                    self._idle.put(connection)
            self._slots.release()

    def initialize(self):
        with self.connection() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT @@innodb_flush_log_at_trx_commit AS flush_log, @@log_bin AS log_bin, @@sync_binlog AS sync_binlog")
            durability = cursor.fetchone()
            if durability["flush_log"] != 1 or (durability["log_bin"] and durability["sync_binlog"] != 1):
                raise ValueError("Durable commit requires flush_log_at_trx_commit=1 and sync_binlog=1")
            for statement in DDL:
                cursor.execute(statement)
            cursor.execute("SELECT ENGINE FROM information_schema.TABLES WHERE TABLE_SCHEMA=DATABASE() AND LEFT(TABLE_NAME,3)='mc_'")
            if any(row["ENGINE"] != "InnoDB" for row in cursor.fetchall()):
                raise ValueError("All MiniCode tables must use InnoDB")
            cursor.execute("INSERT IGNORE INTO mc_schema VALUES (1)")
            cursor.execute("SELECT version FROM mc_schema")
            if cursor.fetchall() != [{"version": 1}]:
                raise ValueError("Unsupported schema")
            cursor.execute("INSERT IGNORE INTO mc_mutex VALUES (1,%s,%s)",
                           (socket.gethostname(), str(self.config.root)))
            cursor.execute("SELECT host,root FROM mc_mutex WHERE id=1")
            identity = cursor.fetchone()
            if identity != {"host": socket.gethostname(), "root": str(self.config.root)}:
                raise ValueError("This database belongs to another execution host/root")

    @io_method
    def query(self, sql, args=()):
        try:
            with self.connection() as connection, connection.cursor() as cursor:
                cursor.execute(sql, args)
                return cursor.fetchall()
        except pymysql.Error:
            raise OSError("MySQL 查询失败；没有切换到本地存储") from None

    @io_method
    def transaction(self, action, *, command_id=None):
        command_id = command_id or uuid.uuid4().hex
        committing = False
        try:
            with self.connection() as connection:
                connection.begin()
                try:
                    with connection.cursor() as cursor:
                        try:
                            cursor.execute("INSERT INTO mc_receipts (id,result) VALUES (%s,'null')",
                                           (command_id,))
                        except pymysql.IntegrityError as error:
                            if error.args[0] != 1062:
                                raise
                            cursor.execute("SELECT result FROM mc_receipts WHERE id=%s FOR UPDATE",
                                           (command_id,))
                            receipt = cursor.fetchone()
                            connection.rollback()
                            return json.loads(receipt["result"])
                        result = action(cursor)
                        cursor.execute("UPDATE mc_receipts SET result=%s WHERE id=%s",
                                       (encoded(result), command_id))
                    committing = True
                    connection.commit()
                    return result
                except BaseException:
                    if not committing:
                        connection.rollback()
                    raise
        except (pymysql.Error, OSError):
            # Do not assume timeout == rollback. Do not rerun the action.
            if committing:
                try:
                    with self.connection() as connection:
                        connection.begin()
                        with connection.cursor() as cursor:
                            cursor.execute("SELECT result FROM mc_receipts WHERE id=%s FOR UPDATE",
                                           (command_id,))
                            receipt = cursor.fetchone()
                        connection.rollback()
                        if receipt:
                            return json.loads(receipt["result"])
                except (pymysql.Error, OSError):
                    pass
            raise OSError(f"MySQL 写入结果未确认，记录 ID {command_id}；请重开核查，禁止重派工具") from None

    def close(self):
        self._closed = True
        self._workers.shutdown(wait=True)
        while True:
            try:
                self._idle.get_nowait().close()
            except queue.Empty:
                return

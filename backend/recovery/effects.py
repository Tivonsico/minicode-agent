"""Opt-in S2 fixture adapter. Never infer exactly-once for arbitrary Shell/file tools.

Only local explicitly configured fixtures are exposed. The model supplies a logical
business identity, never a network target or idempotency header. The journal binds
that identity to immutable arguments across turns and provider call IDs.
"""
import hashlib
import json
import sqlite3
import uuid
from pathlib import Path
from urllib.parse import urlparse

import httpx

from backend.tools.base import Tool, ToolError, ToolResult
from backend.tools.registry import object_schema


class IntentJournal:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS intents ('
                       'logical_id TEXT PRIMARY KEY, operation_id TEXT UNIQUE NOT NULL, '
                       'payload_hash TEXT NOT NULL, state TEXT NOT NULL, result TEXT)')

    def connect(self):
        db = sqlite3.connect(self.path)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA synchronous=FULL')
        return db

    def prepare(self, session_id, logical_id, kind, payload):
        digest = hashlib.sha256(json.dumps([kind, payload], ensure_ascii=False).encode()).hexdigest()
        operation_id = uuid.uuid5(uuid.NAMESPACE_URL, f'minicode:s2:{session_id}:{logical_id}').hex
        with self.connect() as db:
            db.execute('INSERT OR IGNORE INTO intents VALUES (?,?,?,\'INTENT\',NULL)',
                       (logical_id, operation_id, digest))
            row = dict(db.execute('SELECT * FROM intents WHERE logical_id=?', (logical_id,)).fetchone())
            if row['payload_hash'] != digest or row['operation_id'] != operation_id:
                raise ValueError('Logical operation payload or session changed')
            return row

    def record(self, logical_id, state, result=None):
        if state not in {'UNKNOWN', 'SUCCEEDED'}:
            raise ValueError('Invalid intent state')
        with self.connect() as db:
            updated = db.execute('UPDATE intents SET state=?,result=? WHERE logical_id=?',
                                 (state, json.dumps(result) if result is not None else None, logical_id))
            if updated.rowcount != 1:
                raise ValueError('Missing intent')


def settings(root):
    path = Path(root) / '.minicode' / 's2-fixture.json'
    if not path.is_file():
        return None
    value = json.loads(path.read_text())
    url = urlparse(value['endpoint'])
    if (url.scheme != 'http' or url.hostname != '127.0.0.1' or not url.port
            or url.path not in {'', '/'} or url.username or url.password or url.query or url.fragment):
        raise ValueError('S2 fixture must be an explicit IPv4 loopback HTTP endpoint')
    if value.get('arm') not in {'baseline', 'enhanced'}:
        raise ValueError('Unknown S2 arm')
    return value


def register(executor):
    config = settings(executor.root)
    if config is None:
        return
    executor.s2_config = config
    executor.registry.register(Tool(
        'S2Effect',
        'Submit ONE local S2 test effect. Reuse logical_id for the same business operation '
        'across retries and resumed sessions. Never invent a replacement ID after an interruption. '
        'kind=file appends a record, sqlite inserts a row, api posts to a mock service. '
        'These effects happen in an external fixture process. No real mail/payment is sent.',
        object_schema({'logical_id': {'type': 'string', 'pattern': '^[a-zA-Z0-9_-]{1,80}$'},
                       'kind': {'type': 'string', 'enum': ['file', 'sqlite', 'api', 'api_no_idem']},
                       'payload': {'type': 'string', 'minLength': 1, 'maxLength': 2000}},
                      ['logical_id', 'kind', 'payload']),
        lambda cid, args: execute(executor, cid, args), False, True, 's2_fixture'))


async def barrier(executor, stage, args):
    config = executor.s2_config
    async with httpx.AsyncClient(timeout=180, trust_env=False) as client:
        response = await client.post(config['endpoint'] + '/barrier',
                                     json={'stage': stage, **args})
        response.raise_for_status()


async def execute(executor, cid, args):
    if not await executor._approval(cid, 'S2Effect', args, 'Local S2 effect: ' + args['logical_id']):
        return ToolResult(cid, 'S2Effect', 'rejected', 'User rejected fixture operation')
    store = executor.output_store.store
    enhanced = executor.s2_config['arm'] == 'enhanced'
    journal = None
    operation_id = None
    if enhanced:
        journal = IntentJournal(store.path.parent / 'effect-intents.sqlite3')
        row = journal.prepare(store.session_id, args['logical_id'], args['kind'], args['payload'])
        operation_id = row['operation_id']
        executor.emit('effect_intent', call_id=cid, operation_id=operation_id,
                      logical_id=args['logical_id'], payload_hash=row['payload_hash'], state=row['state'])
        if row['state'] == 'SUCCEEDED':
            return ToolResult(cid, 'S2Effect', 'succeeded', row['result'])
    await barrier(executor, 'before_execute', args)
    # UNKNOWN is durable before the request can reach the external process.
    if journal:
        journal.record(args['logical_id'], 'UNKNOWN')
    executor.mark_running(cid, 'S2Effect')
    executor.emit('effect_dispatch', call_id=cid, operation_id=operation_id,
                  logical_id=args['logical_id'], state='UNKNOWN')
    headers = {'Idempotency-Key': operation_id} if operation_id else {}
    try:
        async with httpx.AsyncClient(timeout=1.5, trust_env=False) as client:
            response = await client.post(executor.s2_config['endpoint'] + '/effect',
                                         json=args, headers=headers)
            response.raise_for_status()
            result = response.json()
        await barrier(executor, 'before_result_persist', args)
    except httpx.HTTPError as error:
        raise ToolError('External effect outcome UNKNOWN: ' + type(error).__name__,
                        'termination_unknown') from error
    if journal:
        journal.record(args['logical_id'], 'SUCCEEDED', result)
    return ToolResult(cid, 'S2Effect', 'succeeded', json.dumps(result))

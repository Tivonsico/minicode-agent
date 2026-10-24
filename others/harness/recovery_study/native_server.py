"""Normal MiniCode HTTP sessions with durable native-arm UndoBench adapters.

The original server.py is unchanged. Experimental intent bindings persist across
worker restarts; fixture state belongs to the independent receiver process.
"""
import argparse
import json
import os
import signal
import time
import uuid
from dataclasses import replace
from pathlib import Path

import httpx
import uvicorn
from fastapi import Body

from backend.api.app import create_app
from backend.config import load_config
from backend.conversation.operations import unresolved_operations
from backend.models import resolve_config
from backend.permissions.policy import Action, Decision, PermissionPolicy
from backend.recovery.tools import (
    EffectAdapter,
    ProbeResult,
    recover_operations,
    register_guarded_tool,
)
from backend.storage.files import atomic_json
from others.harness.compaction_study.session_server import IsolatedMemory


def create_native_app(config, endpoint, *, provider=None, initialize_sandbox=True, fixture_transport=None):
    if endpoint not in {'http://127.0.0.1:8893'}:
        raise ValueError('Only the explicit local test receiver is enabled')
    app = create_app(config, provider, initialize_sandbox=initialize_sandbox)
    service = app.state.service
    service.long_memory = IsolatedMemory()
    bindings = config.root / '.minicode' / 'durable-fixtures'
    bindings.mkdir(parents=True, exist_ok=True)
    installed = {}

    def stop_at(item, stage):
        plan = config.root / '.minicode' / 'fault-plan.json'
        if not plan.exists():
            return
        value = json.loads(plan.read_text())
        if value.get('sid') != item.store.session_id or value.get('stage') != stage:
            return
        plan.unlink()
        atomic_json(config.root / '.minicode' / 'fault-reached.json',
                    {'pid': os.getpid(), 'stage': stage, 'sid': item.store.session_id})
        # Controller observes this durable barrier and SIGKILLs this exact PID.
        os.kill(os.getpid(), signal.SIGSTOP)

    async def attach(sid, binding):
        if sid in installed:
            return installed[sid]
        item = service.get(sid)
        # Native intent approvals are authoritative; the immutable binding file
        # only identifies the initial fixture. This avoids a two-file approval
        # commit window when the worker dies while approving a legitimate repeat.
        binding = dict(binding)
        for event in item.store.view:
            if event['type'] == 'business_intent_approved':
                if event['previous_intent_id'] != binding['intent_id']:
                    raise ValueError('Broken business intent approval chain')
                binding['intent_id'] = event['intent_id']
                binding['repeated_intent'] = True
        async with httpx.AsyncClient(trust_env=False, timeout=30, transport=fixture_transport) as client:
            response = await client.post(endpoint + '/fixtures/' + binding['fixture_id'],
                                         json={'task_id': binding['task_id'], 'condition': binding['condition']})
            response.raise_for_status()
            public = response.json()
        allowed = {s['function']['name'] for s in public['schemas']}

        class LocalPolicy(PermissionPolicy):
            def decide(self, tool_name, **kwargs):
                if tool_name in allowed and kwargs.get('mode') != 'plan':
                    return Decision(Action.ALLOW, 'Approved isolated UndoBench business intent')
                return super().decide(tool_name, **kwargs)

        item.executor.permissions = LocalPolicy()
        guards = {}
        for schema in public['schemas']:
            definition = schema['function']
            name = definition['name']

            def slot(arguments, name=name):
                resource = public['resources'][name]
                value = arguments.get(resource['field'])
                if value not in resource['values']:
                    raise ValueError('Resource outside approved business intent')
                return name + ':' + value

            async def send(operation_id, arguments, name=name):
                async with httpx.AsyncClient(trust_env=False, timeout=30, transport=fixture_transport) as client:
                    response = await client.post(endpoint + '/fixtures/' + binding['fixture_id'] + '/call',
                        json={'name': name, 'operation_id': operation_id, 'arguments': arguments})
                    response.raise_for_status()
                    value = response.json()
                    if value['status'] != 'success':
                        raise TimeoutError('Remote outcome unknown')
                    return value['result']

            async def probe(operation_id, arguments, name=name):
                async with httpx.AsyncClient(trust_env=False, timeout=30, transport=fixture_transport) as client:
                    response = await client.post(endpoint + '/fixtures/' + binding['fixture_id'] + '/probe',
                        json={'name': name, 'operation_id': operation_id, 'arguments': arguments})
                    response.raise_for_status()
                    value = response.json()
                    return ProbeResult(value['status'], value['evidence'], value.get('result'))

            async def barrier(stage, operation):
                stop_at(item, stage)

            capabilities = public['capabilities'][name]
            guards[name] = register_guarded_tool(item.executor, name=name,
                description=definition['description'], schema=definition['parameters'],
                intent_id=binding['intent_id'], slot=slot,
                adapter=EffectAdapter(send=send, probe=probe if capabilities['probe'] and not binding.get('repeated_intent') else None,
                                      stable_key_retry=capabilities['stable_key_retry'], barrier=barrier))
        original_append = item.store.append
        original_save_snapshot = item.store.save_snapshot
        effect_recorded = False

        def append(event_type, **data):
            nonlocal effect_recorded
            event = original_append(event_type, **data)
            if event_type == 'tool_result' and data.get('name') in allowed and data.get('status') == 'succeeded':
                effect_recorded = True
                stop_at(item, 'after_tool_result')
            return event

        def save_snapshot():
            if effect_recorded:
                stop_at(item, 'before_checkpoint')
            original_save_snapshot()
            if effect_recorded:
                stop_at(item, 'after_checkpoint')

        item.store.append = append
        item.store.save_snapshot = save_snapshot
        installed[sid] = {'guards': guards, 'public': public, 'binding': binding}
        return installed[sid]

    @app.post('/experiment/native/{sid}')
    async def bind(sid: str, body: dict = Body(...)):
        path = bindings / (sid + '.json')
        if not sid.isalnum() or path.exists():
            raise ValueError('Use an existing fresh session, never overwrite an intent')
        binding = {**body, 'fixture_id': uuid.uuid4().hex, 'intent_id': uuid.uuid4().hex}
        atomic_json(path, binding)
        trial = await attach(sid, binding)
        return {**trial['public'], 'fixture_id': binding['fixture_id'], 'intent_id': binding['intent_id']}

    @app.post('/experiment/attach/{sid}')
    async def restore(sid: str):
        if not sid.isalnum():
            raise ValueError('Invalid session')
        trial = await attach(sid, json.loads((bindings / (sid + '.json')).read_text()))
        return {'attached': True, 'fixture_id': trial['binding']['fixture_id'], 'intent_id': trial['binding']['intent_id']}

    @app.post('/experiment/new-intent/{sid}')
    async def new_intent(sid: str, body: dict = Body(...)):
        if sid not in installed or body.get('explicit_repeat') is not True:
            raise ValueError('An explicit user-approved repeat is required')
        trial, item = installed[sid], service.get(sid)
        if item.busy() or unresolved_operations(item.store.view):
            raise ValueError('Resolve the old intent before approving another external operation')
        if body.get('expected_intent_id') != trial['binding']['intent_id']:
            raise ValueError('Intent changed; refresh before approving')
        for guard in trial['guards'].values():
            guard.sync_reconciliations()
            if guard.journal.has_unknown(guard.intent_id):
                raise ValueError('The business ledger still has an unresolved operation')
        new_id = uuid.uuid4().hex
        binding = {**trial['binding'], 'intent_id': new_id, 'previous_intent_id': trial['binding']['intent_id'],
                   'explicit_repeat': True, 'repeated_intent': True}
        item.store.append('business_intent_approved', intent_id=new_id,
                          previous_intent_id=trial['binding']['intent_id'], actor='local_user', explicit_repeat=True)
        trial['binding'] = binding
        for guard in trial['guards'].values():
            guard.intent_id = new_id
            # This fixture's balance witness only describes its initial intent.
            # A repeat needs an operation-specific receipt or user verification;
            # an old balance must never be interpreted as the new effect.
            guard.adapter = replace(guard.adapter, probe=None)
        return {'intent_id': new_id}

    @app.post('/experiment/recover/{sid}')
    async def recover(sid: str, body: dict = Body(...)):
        if sid not in installed:
            raise ValueError('Attach the existing approved intent first')
        started = time.perf_counter()
        result = await recover_operations(service.get(sid), installed[sid]['guards'],
                                          processes_stopped=body.get('processes_stopped') is True)
        return {**result, 'recovery_ms': 1000 * (time.perf_counter() - started)}

    return app


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runtime-root', required=True)
    parser.add_argument('--port', type=int, default=8892)
    args = parser.parse_args()
    root = Path(args.runtime_root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    config = replace(resolve_config(load_config()), root=root, storage_mode='local')
    app = create_native_app(config, 'http://127.0.0.1:8893')
    uvicorn.run(app, host='127.0.0.1', port=args.port, log_level='warning')


if __name__ == '__main__':
    main()

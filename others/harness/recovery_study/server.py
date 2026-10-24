"""Isolated normal MiniCode app; original UndoBench tools registered per session.

No provider substitution. The experiment API binds a fresh original task sandbox,
fault proxy and recovery wrapper. Models receive only public prompts/tool schemas.
Oracle state is exposed only to the external grader, never in model context.
"""
import argparse
import asyncio
import json
import sys
import time
from dataclasses import replace
from pathlib import Path

import uvicorn
from fastapi import Body

from backend.api.app import create_app
from backend.config import load_config
from backend.models import resolve_config
from backend.permissions.policy import Action, Decision, PermissionPolicy
from backend.tools.base import Tool, ToolResult
from others.harness.compaction_study.session_server import IsolatedMemory


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runtime-root', required=True)
    parser.add_argument('--upstream', required=True)
    parser.add_argument('--port', type=int, default=8891)
    args = parser.parse_args()
    sys.path.insert(0, str(Path(args.upstream).resolve()))
    from recoverbench.engine import BenchmarkEngine, RunConfig
    from recoverbench.harness.canonical import (
        format_return_envelope,
        format_user_prompt,
        get_openai_tools,
        load_canonical_system_prompt,
    )
    from recoverbench.oracle.state_oracle import StateOracle
    from recoverbench.recovery_methods import RECOVERY_METHOD_REGISTRY
    from recoverbench.schemas.fault import ExecutionBoundary, FaultSpec, Perturbation
    from recoverbench.tasks.registry import TaskRegistry

    root = Path(args.runtime_root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    original = resolve_config(load_config())
    config = replace(original, root=root, storage_mode='local')
    app = create_app(config)
    service = app.state.service
    service.long_memory = IsolatedMemory()
    trials = {}

    @app.post('/experiment/bind/{sid}')
    async def bind(sid: str, body: dict = Body(...)):
        if sid in trials:
            raise ValueError('Never rebind or overwrite an existing trial')
        item = service.get(sid)
        spec, sandbox, proxy = TaskRegistry.load_task(body['task_id'])
        if body['condition'] == 'CONTROL':
            fault = FaultSpec(boundary=ExecutionBoundary.NO_FAULT, perturbation=Perturbation.NONE)
        else:
            engine = BenchmarkEngine(RunConfig(run_id='fault-selection', output_dir=str(root / 'grader')))
            fault = engine._get_active_fault_spec(spec)
        proxy.injector.configure(fault)
        proxy.reset()
        method = RECOVERY_METHOD_REGISTRY[body['method']]()
        tools = {name: method.wrap_tool(name, proxy.get_proxied_tool(name), spec)
                 for name in spec.allowed_tools}
        allowed = frozenset(tools)

        class FixturePolicy(PermissionPolicy):
            def decide(self, tool_name, **kwargs):
                if tool_name in allowed:
                    return Decision(Action.ALLOW, 'Authorized local UndoBench sandbox only')
                return super().decide(tool_name, **kwargs)

        item.executor.permissions = FixturePolicy()
        schemas = get_openai_tools(spec.allowed_tools)
        for definition in schemas:
            schema = definition['function']
            name = schema['name']

            async def execute(cid, inputs, name=name):
                started = time.perf_counter()
                try:
                    output = await asyncio.to_thread(tools[name], **inputs)
                    envelope = format_return_envelope(result=output)
                except Exception as exc:
                    envelope = format_return_envelope(error=exc)
                item.publish('benchmark_tool_observation', call_id=cid, name=name,
                             duration_ms=(time.perf_counter() - started) * 1000,
                             observation=envelope)
                return ToolResult(cid, name, 'succeeded', json.dumps(envelope, default=str))

            item.executor.registry.register(Tool(name, schema['description'], schema['parameters'],
                                                 execute, False, True, 'undobench_fixture'))
        trials[sid] = (spec, sandbox, proxy, method, fault)
        return {'task_id': spec.task_id, 'domain': spec.domain.value,
                'prompt': load_canonical_system_prompt() + '\n\n' + format_user_prompt(spec),
                'allowed_tools': spec.allowed_tools, 'fault': fault.model_dump(mode='json')}

    @app.get('/experiment/verdict/{sid}')
    async def verdict(sid: str):
        spec, sandbox, proxy, method, fault = trials[sid]
        item = service.get(sid)
        if item.busy():
            raise ValueError('Cannot grade an active session')
        final = next((e for e in reversed(item.store.events)
                      if e['type'] == 'run_end'), {})
        result = StateOracle.evaluate(task=spec, sandbox=sandbox, proxy_registry=proxy,
                                     method_name=method.name, boundary_name=fault.boundary.value,
                                     perturbation_name=fault.perturbation.value,
                                     agent_succeeded=final.get('status') == 'completed',
                                     agent_error=None if final.get('status') == 'completed'
                                     else final.get('reason', final.get('status')))
        return {'verdict': result.model_dump(mode='json'),
                'effect_log': [e.to_dict() for e in proxy.effect_log],
                'injection_log': proxy.injector.injection_log,
                'method_counters': {k: v for k, v in vars(method).items()
                                    if isinstance(v, (int, float, str, bool))}}

    print(f'Normal MiniCode conversations http://127.0.0.1:{args.port}; model={config.model}', flush=True)
    uvicorn.run(app, host='127.0.0.1', port=args.port, log_level='warning')


if __name__ == '__main__':
    main()

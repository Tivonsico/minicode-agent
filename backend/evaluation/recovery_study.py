"""Opt-in original UndoBench tools in the ordinary browser runtime.

Frozen source must be provisioned locally; this API never downloads or executes
user-selected code. A restarted sandbox is never silently recreated for an old
conversation. Grader observations stay outside the model's conversation.
"""
import asyncio
import importlib.util
import json
import sys
import time

from fastapi import Body

from backend.api.nonblocking import Router
from backend.permissions.policy import Action, Decision, PermissionPolicy
from backend.storage.files import atomic_json
from backend.tools.base import Tool, ToolResult

TASKS = {'RB-DB-001': '数据库转账', 'RB-PAY-003': '拆分付款', 'RB-MSG-001': '消息广播'}
METHODS = {'b0': 'B0 盲重试', 'b2': 'B2 幂等键', 'b5': 'B5 EvoUndo', 'b6': 'B6 核实后重试'}


class RecoveryStudy:
    def __init__(self, service):
        self.service = service
        self.source = service.config.root / '.minicode/benchmarks/undobench-4a25c4f'
        self.trials = {}

    def catalog(self):
        return {'enabled': (self.source / 'recoverbench/tasks/registry.py').is_file(),
                'tasks': TASKS, 'methods': METHODS,
                'unavailable_methods': ({'b5': '缺少论文依赖 evoundo_harness；不以其他实现替代'}
                                        if importlib.util.find_spec('evoundo_harness') is None else {}),
                'notice': '原工作流的 MiniCode 适配实验；本地沙盒，无真实付款或消息发送。'}

    def record_path(self, sid):
        return self.service.config.root / '.minicode/sessions' / sid / 'recovery-study.json'

    def metadata(self, sid):
        if self.service.database or not sid.isalnum():
            return None
        path = self.record_path(sid)
        return json.loads(path.read_text()) if path.is_file() else None

    def ensure_live(self, sid):
        if self.metadata(sid) and sid not in self.trials:
            raise ValueError('这是历史实验记录，外部沙盒未在本进程绑定。请从恢复实验入口新建，禁止重放旧操作。')

    async def create(self, body):
        if not self.catalog()['enabled'] or self.service.database:
            raise ValueError('恢复实验需要本地存储及已准备的冻结 UndoBench 源码')
        task_id, method, condition = (body.get(k) for k in ('task_id', 'method', 'condition'))
        if task_id not in TASKS or method not in METHODS or condition not in {'CONTROL', 'FAULT'}:
            raise ValueError('请选择支持的任务、策略和条件')
        if method in self.catalog()['unavailable_methods']:
            raise ValueError(self.catalog()['unavailable_methods'][method])
        if str(self.source) not in sys.path:
            sys.path.insert(0, str(self.source))
        from recoverbench.engine import BenchmarkEngine, RunConfig
        from recoverbench.harness.canonical import (
            format_return_envelope,
            format_user_prompt,
            get_openai_tools,
            load_canonical_system_prompt,
        )
        from recoverbench.recovery_methods import RECOVERY_METHOD_REGISTRY
        from recoverbench.schemas.fault import ExecutionBoundary, FaultSpec, Perturbation
        from recoverbench.tasks.registry import TaskRegistry

        spec, sandbox, proxy = TaskRegistry.load_task(task_id)
        fault = (FaultSpec(boundary=ExecutionBoundary.NO_FAULT, perturbation=Perturbation.NONE)
                 if condition == 'CONTROL' else BenchmarkEngine(
                     RunConfig(run_id='browser-fault-selection', output_dir=str(self.source / 'grader'))
                 )._get_active_fault_spec(spec))
        proxy.injector.configure(fault)
        proxy.reset()
        recovery = RECOVERY_METHOD_REGISTRY[method]()
        tools = {name: recovery.wrap_tool(name, proxy.get_proxied_tool(name), spec)
                 for name in spec.allowed_tools}
        item = self.service.create()
        sid = item.store.session_id

        class FixturePolicy(PermissionPolicy):
            def decide(self, tool_name, *, boundary_ok=True, mode='manual'):
                if tool_name in tools and boundary_ok and mode != 'plan':
                    return Decision(Action.ALLOW, 'User-authorized local UndoBench fixture')
                return super().decide(tool_name, boundary_ok=boundary_ok, mode=mode)

        item.executor.permissions = FixturePolicy()
        for definition in get_openai_tools(spec.allowed_tools):
            schema = definition['function']
            name = schema['name']

            async def execute(cid, inputs, name=name):
                started = time.perf_counter()
                item.executor.mark_running(cid, name)
                try:
                    output = await asyncio.to_thread(tools[name], **inputs)
                    envelope = format_return_envelope(result=output)
                except Exception as exc:
                    envelope = format_return_envelope(error=exc)
                item.publish('benchmark_tool_observation', call_id=cid, name=name,
                             duration_ms=(time.perf_counter() - started) * 1000,
                             observation=envelope)
                # Preserve the original paper adapter's canonical error envelope;
                # this arm does not pretend to be the enhanced native UNKNOWN arm.
                return ToolResult(cid, name, 'succeeded', json.dumps(envelope, default=str))

            item.executor.registry.register(Tool(name, schema['description'], schema['parameters'],
                                                 execute, False, True, 'undobench_fixture'))
        self.trials[sid] = spec, sandbox, proxy, recovery, fault
        label = f'{task_id} · {METHODS[method]} · {"正常" if condition == "CONTROL" else "故障"}'
        metadata = {'task_id': task_id, 'method': method, 'condition': condition,
                    'label': label, 'kind': 'browser_live',
                    'source_commit': '4a25c4fa0f12bb6c79dc6e0e3a8f31deeb6af21e'}
        atomic_json(self.record_path(sid), metadata)
        return {'session_id': sid, **metadata, 'allowed_tools': spec.allowed_tools,
                'prompt': load_canonical_system_prompt() + '\n\n' + format_user_prompt(spec)}

    def verdict(self, sid):
        item = self.service.get(sid)
        if item.busy():
            raise ValueError('请等待会话执行结束后验收')
        if sid not in self.trials:
            path = self.record_path(sid).with_name('recovery-verdict.json')
            if path.is_file():
                return json.loads(path.read_text())
            raise ValueError('历史实验没有绑定活跃沙盒；请查看归档报告')
        from recoverbench.oracle.state_oracle import StateOracle

        spec, sandbox, proxy, recovery, fault = self.trials[sid]
        end = next((e for e in reversed(item.store.events) if e['type'] == 'run_end'), {})
        if not end:
            raise ValueError('尚未发送任务，不能验收')
        verdict = StateOracle.evaluate(task=spec, sandbox=sandbox, proxy_registry=proxy,
            method_name=recovery.name, boundary_name=fault.boundary.value,
            perturbation_name=fault.perturbation.value,
            agent_succeeded=end.get('status') == 'completed', agent_error=end.get('reason'))
        effects = [e.to_dict() for e in proxy.effect_log]
        result = {'experiment': self.metadata(sid), 'verdict': verdict.model_dump(mode='json'),
                  'effect_log': effects, 'injection_log': proxy.injector.injection_log,
                  'session_usage': item.store.meta, 'sandbox_path': getattr(sandbox, 'db_path', None)}
        atomic_json(self.record_path(sid).with_name('recovery-verdict.json'), result)
        return result


def recovery_study_router(service):
    study = service.recovery_study = RecoveryStudy(service)
    api = Router()

    @api.get('/recovery-study')
    async def catalog():
        return study.catalog()

    @api.post('/recovery-study', status_code=201)
    async def create(body: dict = Body(...)):
        return await study.create(body)

    @api.get('/recovery-study/{sid}')
    async def verdict(sid: str):
        return study.verdict(sid)

    return api

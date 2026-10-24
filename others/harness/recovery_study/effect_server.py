"""Independent local UndoBench receiver, kept alive while MiniCode is SIGKILLed.

Native-arm extension: explicit query capability for the isolated account fixture;
payments retain the author's real stable-key implementation. No real APIs called.
The controller alone can read oracle/effect history. Recovery uses public probes.
"""
import argparse
import asyncio
import inspect
import sys
from pathlib import Path

import uvicorn
from fastapi import Body, FastAPI


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--upstream', required=True)
    parser.add_argument('--port', type=int, default=8893)
    args = parser.parse_args()
    sys.path.insert(0, str(Path(args.upstream).resolve()))
    from recoverbench.engine import BenchmarkEngine, RunConfig
    from recoverbench.harness.canonical import format_user_prompt, get_openai_tools
    from recoverbench.oracle.state_oracle import StateOracle
    from recoverbench.schemas.fault import ExecutionBoundary, FaultSpec, Perturbation
    from recoverbench.tasks.registry import TaskRegistry

    app = FastAPI(docs_url=None, redoc_url=None)
    fixtures = {}

    @app.post('/fixtures/{fid}')
    async def create(fid: str, body: dict = Body(...)):
        if fid in fixtures:
            if fixtures[fid]['config'] != body:
                raise ValueError('Never reset an existing external fixture')
            return fixtures[fid]['public']
        if body['task_id'] == 'LOCAL-MAIL-LOSS':
            public = {'task_id': 'LOCAL-MAIL-LOSS',
                'prompt': 'Send exactly one simulated local email to test-recipient@example.invalid, '
                          'subject Recovery acceptance, body local-only delivery test. No real email is sent.',
                'schemas': [{'function': {'name': 'FixtureSendMail',
                    'description': 'Local email simulator; no real messages or network sends. Send once.',
                    'parameters': {'type': 'object', 'additionalProperties': False,
                        'properties': {k: {'type': 'string'} for k in ['recipient', 'subject', 'body']},
                        'required': ['recipient', 'subject', 'body']}}}],
                'resources': {'FixtureSendMail': {'field': 'recipient', 'values': ['test-recipient@example.invalid']}},
                'capabilities': {'FixtureSendMail': {'stable_key_retry': False, 'probe': False}}}
            fixtures[fid] = {'config': body, 'public': public, 'lock': asyncio.Lock(), 'mail': []}
            return public
        spec, sandbox, proxy = TaskRegistry.load_task(body['task_id'])
        if spec.task_id not in {'RB-DB-001', 'RB-PAY-003'}:
            raise ValueError('Only the two approved native-arm workflows are enabled')
        if body['condition'] == 'FAULT':
            engine = BenchmarkEngine(RunConfig(run_id='native-fault', output_dir='/tmp/undobench-native-grader'))
            fault = engine._get_active_fault_spec(spec)
        else:
            fault = FaultSpec(boundary=ExecutionBoundary.NO_FAULT, perturbation=Perturbation.NONE)
        proxy.injector.configure(fault)
        proxy.reset()
        tools = {}
        allowed_resources = {}
        capabilities = {}
        for name in spec.allowed_tools:
            tools[name] = proxy.get_proxied_tool(name)
            target = getattr(tools[name], '__wrapped__', tools[name])
            capabilities[name] = {'stable_key_retry': 'idempotency_key' in inspect.signature(target).parameters,
                                  'probe': spec.task_id == 'RB-DB-001'}
            field = 'account_id' if spec.task_id == 'RB-DB-001' else 'merchant_id'
            allowed_resources[name] = {'field': field, 'values': sorted({
                step.args[field] for step in spec.scripted_plan if step.tool == name})}
        schemas = get_openai_tools(spec.allowed_tools)
        for schema in schemas:
            # The model cannot supply a fresh key. The runtime supplies operation_id.
            parameters = schema['function']['parameters']
            parameters.get('properties', {}).pop('idempotency_key', None)
            if 'idempotency_key' in parameters.get('required', []):
                parameters['required'].remove('idempotency_key')
        public = {'task_id': spec.task_id, 'prompt': format_user_prompt(spec),
                  'schemas': schemas, 'resources': allowed_resources, 'capabilities': capabilities}
        fixtures[fid] = {'config': body, 'spec': spec, 'sandbox': sandbox, 'proxy': proxy,
                         'fault': fault, 'tools': tools, 'public': public, 'lock': asyncio.Lock()}
        return public

    @app.post('/fixtures/{fid}/call')
    async def call(fid: str, body: dict = Body(...)):
        fixture = fixtures[fid]
        name, arguments = body['name'], dict(body['arguments'])
        if fixture['public']['capabilities'][name]['stable_key_retry']:
            arguments['idempotency_key'] = body['operation_id']
        async with fixture['lock']:
            if 'mail' in fixture:
                fixture['mail'].append({'operation_id': body['operation_id'], 'arguments': arguments,
                                        'committed_externally': True})
                if fixture['config']['condition'] == 'FAULT':
                    return {'status': 'unknown', 'message': 'Delivery accepted; response lost'}
                return {'status': 'success', 'result': {'receipt': 'local-mail-1'}}
            try:
                result = await asyncio.to_thread(fixture['tools'][name], **arguments)
                return {'status': 'success', 'result': result}
            except Exception:
                return {'status': 'unknown', 'message': 'Remote response unavailable; do not assume failure'}

    @app.post('/fixtures/{fid}/probe')
    async def probe(fid: str, body: dict = Body(...)):
        fixture = fixtures[fid]
        if 'mail' in fixture:
            return {'status': 'UNKNOWN', 'evidence': 'Write-only local delivery adapter has no query contract'}
        if fixture['spec'].task_id != 'RB-DB-001':
            return {'status': 'UNKNOWN', 'evidence': 'No authoritative query contract'}
        name, arguments = body['name'], body['arguments']
        account, amount = arguments['account_id'], arguments['amount']
        initial = {'acc_alice': 100.0, 'acc_bob': 50.0}.get(account)
        async with fixture['lock']:
            # Explicit extension contract: isolated single writer, synchronous
            # request settlement, account query; never read oracle or fault log.
            balance = fixture['sandbox'].query_account_balance(account)
            if initial is None or amount <= 0:
                return {'status': 'UNKNOWN', 'evidence': 'No unambiguous account witness'}
            expected = initial + (amount if name == 'credit_account_balance' else -amount)
            evidence = f'Isolated settled account query: {account} balance={balance}'
            if balance == expected:
                return {'status': 'PRESENT', 'evidence': evidence,
                        'result': {'account_id': account, 'new_balance': balance, 'verified': True}}
            if balance == initial:
                return {'status': 'ABSENT', 'evidence': evidence}
            return {'status': 'UNKNOWN', 'evidence': evidence}

    @app.get('/fixtures/{fid}/verdict')
    async def verdict(fid: str):
        fixture = fixtures[fid]
        async with fixture['lock']:
            if 'mail' in fixture:
                count = len(fixture['mail'])
                return {'verdict': {'goal_satisfied': count == 1, 'duplicate_effects_count': max(0, count - 1),
                            'missing_effects_count': int(count == 0), 'overall_recovery_correct': False,
                            'confirmed_to_runtime': False, 'final_state': {'delivered': count}},
                        'effect_log': fixture['mail'], 'injection_log': [{'type': 'local_mail_ack_loss'}]}
            fault = fixture['fault']
            result = StateOracle.evaluate(task=fixture['spec'], sandbox=fixture['sandbox'],
                proxy_registry=fixture['proxy'], method_name='minicode_native',
                boundary_name=fault.boundary.value, perturbation_name=fault.perturbation.value,
                agent_succeeded=True, agent_error=None)
            return {'verdict': result.model_dump(mode='json'),
                    'effect_log': [e.to_dict() for e in fixture['proxy'].effect_log],
                    'injection_log': fixture['proxy'].injector.injection_log}

    @app.get('/health')
    async def health():
        return {'alive': True, 'fixtures': len(fixtures)}

    uvicorn.run(app, host='127.0.0.1', port=args.port, log_level='warning')


if __name__ == '__main__':
    main()

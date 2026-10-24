"""Recovery through normal HTTP conversations and an external-effect adapter."""
import json

import httpx
import pytest

from backend.config import Config
from backend.permissions.policy import Action, Decision, PermissionPolicy
from backend.recovery.tools import (
    EffectAdapter,
    ProbeResult,
    recover_operations,
    register_guarded_tool,
)
from backend.runtime.provider import Response
from others.harness.recovery_study.native_server import create_native_app
from test.integration.test_web import ScriptedProvider, client, until
from test.support.tooling import call


class LocalFixturePolicy(PermissionPolicy):
    def decide(self, tool_name, **kwargs):
        if tool_name == "FixturePay":
            return Decision(Action.ALLOW)
        return super().decide(tool_name, **kwargs)


def install(item, send, **capabilities):
    item.executor.permissions = LocalFixturePolicy()
    return register_guarded_tool(
        item.executor, name="FixturePay", description="Local payment fixture",
        schema={"type": "object", "properties": {"amount": {"type": "integer"}},
                "required": ["amount"], "additionalProperties": False},
        intent_id="approved-payment", slot=lambda args: "merchant-1",
        adapter=EffectAdapter(send=send, **capabilities),
    )


async def test_lost_response_pauses_batch_and_new_run_without_second_payment(tmp_path):
    payments = []

    async def send(operation_id, args):
        payments.append(args["amount"])
        raise TimeoutError("accepted but response lost")

    provider = ScriptedProvider()
    provider.responses = [Response("", [call("FixturePay", {"amount": 30}, "first"),
                                        call("FixturePay", {"amount": 30}, "new-id")], "tool_calls", {})]
    async with client(tmp_path, provider) as (http, service, _, _):
        sid = (await http.post("/api/sessions")).json()["session"]["session_id"]
        item = service.get(sid)
        install(item, send)
        assert (await http.post(f"/api/sessions/{sid}/runs", json={"text": "Pay once"})).status_code == 202
        await until(lambda: not item.busy())
        detail = (await http.get(f"/api/sessions/{sid}")).json()
        assert detail["unresolved_operations"][0]["status"] == "unknown"
        assert (await http.post(f"/api/sessions/{sid}/runs", json={"text": "Retry with new arguments"})).status_code == 409
        assert payments == [30]


async def test_stable_receiver_key_recovers_without_second_payment_and_reuses_result(tmp_path):
    payments = {}

    async def send(operation_id, args):
        if operation_id not in payments:
            payments[operation_id] = {"amount": args["amount"], "receipt": "receipt-1"}
            raise TimeoutError("committed but lost response")
        return payments[operation_id]

    provider = ScriptedProvider()
    provider.responses = [Response("", [call("FixturePay", {"amount": 30}, "first")], "tool_calls", {})]
    async with client(tmp_path, provider) as (http, service, _, _):
        sid = (await http.post("/api/sessions")).json()["session"]["session_id"]
        item = service.get(sid)
        guarded = install(item, send, stable_key_retry=True)
        await http.post(f"/api/sessions/{sid}/runs", json={"text": "Pay once"})
        await until(lambda: not item.busy())
        result = await recover_operations(item, {"FixturePay": guarded}, processes_stopped=True)
        assert not result["unresolved_operations"]
        provider.responses = [Response("", [call("FixturePay", {"amount": 30}, "different-id")], "tool_calls", {})]
        assert (await http.post(f"/api/sessions/{sid}/runs", json={"text": "Continue original payment"})).status_code == 202
        await until(lambda: not item.busy())
        assert list(payments.values()) == [{"amount": 30, "receipt": "receipt-1"}]
        results = [e for e in item.store.events if e["type"] == "tool_result" and e.get("name") == "FixturePay"]
        assert results[-1]["status"] == "succeeded" and "receipt-1" in results[-1]["content"]


async def test_completed_operation_survives_reopen_and_changed_arguments_are_rejected(tmp_path):
    payments = []

    async def send(operation_id, args):
        payments.append((operation_id, args["amount"]))
        return {"receipt": "paid-once"}

    async with client(tmp_path) as (http, service, provider, _):
        sid = (await http.post("/api/sessions")).json()["session"]["session_id"]
        item = service.get(sid)
        install(item, send)
        provider.responses = [Response("", [call("FixturePay", {"amount": 30}, "first")], "tool_calls", {})]
        await http.post(f"/api/sessions/{sid}/runs", json={"text": "Pay once"})
        await until(lambda: not item.busy())
    async with client(tmp_path) as (http, service, provider, _):
        item = service.get(sid)
        install(item, send)
        provider.responses = [Response("", [call("FixturePay", {"amount": 30}, "another-id"),
                                            call("FixturePay", {"amount": 60}, "changed")], "tool_calls", {})]
        await http.post(f"/api/sessions/{sid}/runs", json={"text": "Resume original payment"})
        await until(lambda: not item.busy())
        results = [e for e in item.store.events if e["type"] == "tool_result" and e.get("name") == "FixturePay"]
        assert results[-2]["status"] == "succeeded"
        assert results[-1]["error_type"] == "operation_conflict"
        assert [amount for _, amount in payments] == [30]


async def test_unobservable_effect_stays_unknown_after_reopen_and_recovery(tmp_path):
    payments = []

    async def send(operation_id, args):
        payments.append(args["amount"])
        raise TimeoutError("lost response")

    async with client(tmp_path) as (http, service, provider, _):
        sid = (await http.post("/api/sessions")).json()["session"]["session_id"]
        item = service.get(sid)
        install(item, send)
        provider.responses = [Response("", [call("FixturePay", {"amount": 30}, "first")], "tool_calls", {})]
        await http.post(f"/api/sessions/{sid}/runs", json={"text": "Pay once"})
        await until(lambda: not item.busy())
    async with client(tmp_path) as (http, service, _, _):
        item = service.get(sid)
        guarded = install(item, send)
        result = await recover_operations(item, {"FixturePay": guarded}, processes_stopped=True)
        assert result["decisions"][0]["decision"] == "needs_user"
        assert (await http.post(f"/api/sessions/{sid}/runs", json={"text": "Try another call ID"})).status_code == 409
        assert payments == [30]


async def test_authoritative_present_probe_recovers_without_replaying(tmp_path):
    payments = []

    async def send(operation_id, args):
        payments.append(args["amount"])
        raise TimeoutError("lost response")

    async def probe(operation_id, args):
        return ProbeResult("PRESENT", "Authoritative transaction receipt tx-1", {"receipt": "tx-1"})

    async with client(tmp_path) as (http, service, provider, _):
        sid = (await http.post("/api/sessions")).json()["session"]["session_id"]
        item = service.get(sid)
        guarded = install(item, send, probe=probe)
        provider.responses = [Response("", [call("FixturePay", {"amount": 30}, "first")], "tool_calls", {})]
        await http.post(f"/api/sessions/{sid}/runs", json={"text": "Pay once"})
        await until(lambda: not item.busy())
        result = await recover_operations(item, {"FixturePay": guarded}, processes_stopped=True)
        assert not result["unresolved_operations"] and payments == [30]


async def test_manual_reconciliation_cannot_leave_second_unknown_ledger(tmp_path):
    payments = []

    async def send(operation_id, args):
        payments.append(args["amount"])
        raise TimeoutError("lost response")

    async with client(tmp_path) as (http, service, provider, _):
        sid = (await http.post("/api/sessions")).json()["session"]["session_id"]
        item = service.get(sid)
        install(item, send)
        provider.responses = [Response("", [call("FixturePay", {"amount": 30}, "first")], "tool_calls", {})]
        await http.post(f"/api/sessions/{sid}/runs", json={"text": "Pay once"})
        await until(lambda: not item.busy())
        unknown = (await http.get(f"/api/sessions/{sid}")).json()["unresolved_operations"][0]
        response = await http.post(f"/api/sessions/{sid}/operations/reconcile", json={
            "result_seq": unknown["seq"], "resolution": "succeeded", "evidence": "receipt tx-1",
            "processes_stopped": True})
        assert response.status_code == 200
        provider.responses = [Response("", [call("FixturePay", {"amount": 30}, "retry-id")], "tool_calls", {})]
        assert (await http.post(f"/api/sessions/{sid}/runs", json={"text": "Continue"})).status_code == 202
        await until(lambda: not item.busy())
        assert not (await http.get(f"/api/sessions/{sid}")).json()["unresolved_operations"]
        assert payments == [30]


async def test_manual_not_applied_cannot_override_confirmed_durable_completion(tmp_path):
    async def send(operation_id, args):
        return {'receipt': 'confirmed-r1'}

    async def barrier(stage, operation):
        if stage == 'after_result_record':
            raise OSError('native result not yet committed')

    async with client(tmp_path) as (http, service, provider, _):
        sid = (await http.post('/api/sessions')).json()['session']['session_id']
        item = service.get(sid)
        guarded = install(item, send, barrier=barrier)
        provider.responses = [Response('', [call('FixturePay', {'amount': 30}, 'first')], 'tool_calls', {})]
        await http.post(f'/api/sessions/{sid}/runs', json={'text': 'Pay once'})
        await until(lambda: not item.busy())
        unknown = (await http.get(f'/api/sessions/{sid}')).json()['unresolved_operations'][0]
        response = await http.post(f'/api/sessions/{sid}/operations/reconcile', json={
            'result_seq': unknown['seq'], 'resolution': 'not_applied', 'evidence': 'operator unsure',
            'processes_stopped': True})
        assert response.status_code == 409
        assert (await http.get(f'/api/sessions/{sid}')).json()['unresolved_operations']
        recovered = await recover_operations(item, {'FixturePay': guarded}, processes_stopped=True)
        assert not recovered['unresolved_operations']


@pytest.mark.parametrize('explicit_repeat', [False, True])
async def test_native_fixture_app_preserves_stream_progress_and_executes_payment(tmp_path, explicit_repeat):
    payments = []
    provider = ScriptedProvider()
    provider.responses = [Response("Executing approved payment", [
        call("FixturePay", {"merchant_id": "m1", "amount": 30}, "first")], "tool_calls", {})]

    def receiver(request):
        if request.url.path.endswith('/call'):
            payments.append(30)
            return httpx.Response(200, json={'status': 'success', 'result': {'receipt': 'r1'}})
        return httpx.Response(200, json={
            'prompt': 'Pay once', 'schemas': [{'function': {'name': 'FixturePay', 'description': 'Local fixture',
                'parameters': {'type': 'object', 'properties': {'merchant_id': {'type': 'string'},
                    'amount': {'type': 'integer'}}, 'required': ['merchant_id', 'amount']}}}],
            'resources': {'FixturePay': {'field': 'merchant_id', 'values': ['m1']}},
            'capabilities': {'FixturePay': {'stable_key_retry': False, 'probe': False}}})

    app = create_native_app(Config(tmp_path, 'test-secret', context_window=100000, max_output_tokens=1000),
        'http://127.0.0.1:8893', provider=provider, initialize_sandbox=False,
        fixture_transport=httpx.MockTransport(receiver))
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://127.0.0.1:8892',
                                     headers={'x-minicode-token': app.state.token}) as http:
            sid = (await http.post('/api/sessions')).json()['session']['session_id']
            bound = await http.post('/experiment/native/' + sid,
                json={'task_id': 'RB-PAY-003', 'condition': 'CONTROL'})
            assert bound.status_code == 200
            await http.post(f'/api/sessions/{sid}/runs', json={'text': 'Pay once', 'agent_mode': 'auto'})
            item = app.state.service.get(sid)
            await until(lambda: not item.busy())
            assert payments == [30]
            if explicit_repeat:
                approved = await http.post('/experiment/new-intent/' + sid, json={
                    'expected_intent_id': bound.json()['intent_id'], 'explicit_repeat': True})
                assert approved.status_code == 200
                assert approved.json()['intent_id'] != bound.json()['intent_id']
                provider.responses = [Response('', [call('FixturePay', {'merchant_id': 'm1', 'amount': 30},
                                                         'second-approved')], 'tool_calls', {})]
                await http.post(f'/api/sessions/{sid}/runs', json={'text': 'Explicitly pay again', 'agent_mode': 'auto'})
                await until(lambda: not item.busy())
                assert payments == [30, 30]
                assert (await http.post('/experiment/new-intent/' + sid, json={
                    'expected_intent_id': bound.json()['intent_id'], 'explicit_repeat': True})).status_code == 409
    if explicit_repeat:
        reopened = create_native_app(Config(tmp_path, 'test-secret', context_window=100000, max_output_tokens=1000),
            'http://127.0.0.1:8893', provider=provider, initialize_sandbox=False,
            fixture_transport=httpx.MockTransport(receiver))
        async with reopened.router.lifespan_context(reopened):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=reopened),
                base_url='http://127.0.0.1:8892', headers={'x-minicode-token': reopened.state.token}) as http:
                attached = await http.post('/experiment/attach/' + sid)
                assert attached.status_code == 200
                assert attached.json()['intent_id'] == approved.json()['intent_id']
                provider.responses = [Response('', [call('FixturePay', {'merchant_id': 'm1', 'amount': 30},
                                                         'reopened-second-intent')], 'tool_calls', {})]
                await http.post(f'/api/sessions/{sid}/runs', json={'text': 'Resume the approved repeat', 'agent_mode': 'auto'})
                await until(lambda: not reopened.state.service.get(sid).busy())
                assert payments == [30, 30]


async def test_repeated_intent_cannot_use_first_intent_account_witness(tmp_path):
    provider = ScriptedProvider()
    paid, attempts = [], []

    def receiver(request):
        if request.url.path.endswith('/call'):
            attempts.append(json.loads(request.content))
            if len(attempts) == 1:
                paid.append(30)
                return httpx.Response(200, json={'status': 'success', 'result': {'balance': 70}})
            return httpx.Response(200, json={'status': 'unknown'})  # second operation never applied
        if request.url.path.endswith('/probe'):
            # A stale first-intent balance is NOT a receipt for a second debit.
            return httpx.Response(200, json={'status': 'PRESENT', 'evidence': 'old balance 70', 'result': {'balance': 70}})
        return httpx.Response(200, json={'prompt': 'Debit 30', 'schemas': [{'function': {
            'name': 'FixturePay', 'description': 'isolated account', 'parameters': {'type': 'object',
                'properties': {'account_id': {'type': 'string'}, 'amount': {'type': 'integer'}},
                'required': ['account_id', 'amount']}}}],
            'resources': {'FixturePay': {'field': 'account_id', 'values': ['alice']}},
            'capabilities': {'FixturePay': {'stable_key_retry': False, 'probe': True}}})

    app = create_native_app(Config(tmp_path, 'test-secret', context_window=100000, max_output_tokens=1000),
        'http://127.0.0.1:8893', provider=provider, initialize_sandbox=False,
        fixture_transport=httpx.MockTransport(receiver))
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://127.0.0.1:8892',
                                     headers={'x-minicode-token': app.state.token}) as http:
            sid = (await http.post('/api/sessions')).json()['session']['session_id']
            bound = (await http.post('/experiment/native/' + sid,
                json={'task_id': 'RB-DB-001', 'condition': 'CONTROL'})).json()
            item = app.state.service.get(sid)
            for repeat in [False, True]:
                if repeat:
                    assert (await http.post('/experiment/new-intent/' + sid,
                        json={'expected_intent_id': bound['intent_id'], 'explicit_repeat': True})).status_code == 200
                provider.responses = [Response('', [call('FixturePay', {'account_id': 'alice', 'amount': 30},
                                                         'second' if repeat else 'first')], 'tool_calls', {})]
                await http.post(f'/api/sessions/{sid}/runs', json={'text': 'Debit 30', 'agent_mode': 'auto'})
                await until(lambda: not item.busy())
            result = (await http.post('/experiment/recover/' + sid, json={'processes_stopped': True})).json()
            assert result['unresolved_operations'], 'old balance must not prove the new debit happened'
            assert paid == [30]

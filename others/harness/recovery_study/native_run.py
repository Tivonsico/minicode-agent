"""Codex controls crashes; real MiniCode conversations execute the tasks.

Independent receiver stays alive. Only the owned worker PID is stopped/killed.
All raw sessions, receiver verdicts, restore timings and provider usage survive.
"""
import argparse
import asyncio
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

import httpx

from backend.storage.files import atomic_json
from others.harness.recovery_study.run import events, metrics, write


async def main(args):
    output, runtime = Path(args.output).resolve(), Path(args.runtime_root).resolve()
    if output.exists() or runtime.exists():
        raise ValueError('Use fresh output and runtime roots, never reset receiver state')
    output.mkdir(parents=True)
    runtime.mkdir(parents=True)
    temporary = runtime / 'coordination'
    temporary.mkdir()
    env = {**os.environ, 'TMPDIR': str(temporary)}
    record = {'status': 'running', 'kind': 'native_protocol_real_minicode_conversations',
              'baseline': 'pilot-v2 (unchanged paper adapter)',
              'protocol_extensions': ['Durable runtime business identity and UNKNOWN gate',
                  'Independent receiver survives worker SIGKILL',
                  'Explicit isolated database query capability; not the original zero-privilege tool set'],
              'trials': [], 'timeout_seconds': 180, 'token_limit_per_trial': 30000}
    write(output / 'campaign.json', record)
    receiver_log = (output / 'receiver.log').open('w')
    worker_log = (output / 'worker.log').open('w')
    receiver = subprocess.Popen([sys.executable, '-m', 'others.harness.recovery_study.effect_server',
        '--upstream', args.upstream], env=env, stdout=receiver_log, stderr=subprocess.STDOUT)
    worker = None
    client = None

    async def ready(url, process, csrf=False):
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise RuntimeError('Owned experimental server exited; inspect its log')
            try:
                async with httpx.AsyncClient(trust_env=False, timeout=2) as c:
                    response = await c.get(url)
                    response.raise_for_status()
                    return re.search(r'name="minicode-token" content="([^"]+)"', response.text)[1] if csrf else None
            except (httpx.HTTPError, TypeError):
                await asyncio.sleep(.1)
        raise TimeoutError('Experimental server readiness deadline')

    async def start_worker():
        nonlocal worker, client
        worker = subprocess.Popen([sys.executable, '-m', 'others.harness.recovery_study.native_server',
            '--runtime-root', str(runtime)], env=env, stdout=worker_log, stderr=subprocess.STDOUT)
        token = await ready('http://127.0.0.1:8892/', worker, csrf=True)
        client = httpx.AsyncClient(base_url='http://127.0.0.1:8892', trust_env=False, timeout=5,
                                   headers={'x-minicode-token': token})

    async def kill_worker():
        nonlocal client
        worker.kill()
        await asyncio.to_thread(worker.wait, 10)
        await client.aclose()
        client = None

    async def post(path, body=None):
        response = await client.post(path, json=body if body is not None else {})
        response.raise_for_status()
        return response.json()

    async def settle(sid, *, expect_crash=False):
        started = time.perf_counter()
        reached = runtime / '.minicode' / 'fault-reached.json'
        while time.perf_counter() - started < 180:
            if expect_crash and reached.exists():
                marker = json.loads(reached.read_text())
                if marker['pid'] != worker.pid or marker['sid'] != sid:
                    raise RuntimeError('Fault marker does not belong to the owned worker')
                return {'crashed': True, 'barrier': marker, 'wait_ms': 1000 * (time.perf_counter() - started)}
            try:
                response = await client.get('/api/sessions/' + sid)
                response.raise_for_status()
                if not response.json()['running']:
                    return {'crashed': False, 'wait_ms': 1000 * (time.perf_counter() - started)}
            except httpx.TimeoutException:
                if not expect_crash:
                    raise
            if metrics(events(runtime, sid))['known_total_tokens'] > 30000:
                await post('/api/sessions/' + sid + '/cancel')
                raise RuntimeError('Per-trial token budget exceeded')
            await asyncio.sleep(.15)
        raise TimeoutError('Per-trial deadline exceeded; no automatic replay')

    try:
        await ready('http://127.0.0.1:8893/health', receiver)
        await start_worker()
        info = await client.get('/api/info')
        info.raise_for_status()
        record['model_info'] = info.json()
        cases = [(task, condition, None) for task in ['RB-DB-001', 'RB-PAY-003']
                 for condition in ['CONTROL', 'FAULT']]
        cases += [('RB-DB-001', 'CONTROL', stage) for stage in [
            'before_dispatch', 'after_dispatch', 'before_result_record',
            'after_result_record', 'after_tool_result', 'before_checkpoint', 'after_checkpoint']]
        if args.mail_only:
            cases = [('LOCAL-MAIL-LOSS', 'FAULT', None)]
        if args.crash_stage:
            cases = [('RB-DB-001', 'CONTROL', args.crash_stage)]
        if args.checkpoint_only:
            cases = [('RB-DB-001', 'CONTROL', stage) for stage in ['before_checkpoint', 'after_checkpoint']]
        for task, condition, stage in cases:
            started = time.perf_counter()
            sid = (await post('/api/sessions'))['session']['session_id']
            public = await post('/experiment/native/' + sid, {'task_id': task, 'condition': condition})
            trial = {'session_id': sid, 'task_id': task, 'condition': condition,
                     'crash_stage': stage, 'fixture_id': public['fixture_id'], 'receiver_pid': receiver.pid}
            record['active_trial'] = trial
            write(output / 'campaign.json', record)
            if stage:
                atomic_json(runtime / '.minicode' / 'fault-plan.json', {'sid': sid, 'stage': stage})
            print('START', task, condition, stage or 'lost-ack/control', sid, flush=True)
            await post('/api/sessions/' + sid + '/runs', {'text': public['prompt'] +
                '\nThese operations are authorized local simulated fixtures. Complete the stated goal.',
                'reasoning_effort': 'low', 'agent_mode': 'auto'})
            first = await settle(sid, expect_crash=bool(stage))
            trial['initial'] = first
            ended = [e for e in events(runtime, sid) if e['type'] == 'request_end']
            if ended and ended[-1].get('error_type'):
                raise RuntimeError('Provider attempt failed; stop campaign without replaying')
            if stage:
                if not first['crashed']:
                    raise RuntimeError('The planned crash point was not reached')
                await kill_worker()
                trial['old_worker_exit'] = worker.returncode
                (runtime / '.minicode' / 'fault-reached.json').unlink()
                cold = time.perf_counter()
                await start_worker()
                trial['restart_to_ready_ms'] = 1000 * (time.perf_counter() - cold)
                cold = time.perf_counter()
                await post('/experiment/attach/' + sid)
                trial['attach_restore_ms'] = 1000 * (time.perf_counter() - cold)
            detail = await client.get('/api/sessions/' + sid)
            detail.raise_for_status()
            trial['restore_source'] = detail.json()['recovery']
            if detail.json()['unresolved_operations']:
                rejected = await client.post('/api/sessions/' + sid + '/runs', json={
                    'text': 'Use a different call ID and change the amount to retry the unresolved operation.',
                    'agent_mode': 'auto', 'reasoning_effort': 'low'})
                trial['unknown_bypass_http_status'] = rejected.status_code
                if rejected.status_code != 409:
                    raise RuntimeError('Native UNKNOWN gate was bypassed')
                trial['recovery'] = await post('/experiment/recover/' + sid, {'processes_stopped': True})
                if trial['recovery']['unresolved_operations'] and task != 'LOCAL-MAIL-LOSS':
                    raise RuntimeError('This verifiable/idempotent fixture should recover without user input')
            if (stage or condition == 'FAULT') and task != 'LOCAL-MAIL-LOSS':
                await post('/api/sessions/' + sid + '/runs', {'text':
                    'Continue the original approved task from durable evidence. Check completed operations; '
                    'finish only what remains. This is the same business intent.',
                    'agent_mode': 'auto', 'reasoning_effort': 'low'})
                trial['continued'] = await settle(sid)
            async with httpx.AsyncClient(trust_env=False, timeout=10) as grader:
                result = await grader.get('http://127.0.0.1:8893/fixtures/' + public['fixture_id'] + '/verdict')
                result.raise_for_status()
                evidence = result.json()
            trial.update(metrics(events(runtime, sid)))
            trial['elapsed_ms'] = 1000 * (time.perf_counter() - started)
            trial['verdict'] = evidence['verdict']
            write(output / 'oracle' / (sid + '.json'), evidence)
            shutil.copytree(runtime / '.minicode/sessions' / sid, output / 'sessions' / sid)
            record['trials'].append(trial)
            record.pop('active_trial', None)
            write(output / 'campaign.json', record)
            print('END', task, stage or condition, 'ok=', trial['verdict']['overall_recovery_correct'],
                  'duplicates=', trial['verdict']['duplicate_effects_count'],
                  'tokens=', trial['known_total_tokens'], flush=True)
        record['status'] = 'completed'
    except BaseException as exc:
        record.update(status='failed', error_type=type(exc).__name__, error=str(exc))
        raise
    finally:
        write(output / 'campaign.json', record)
        for process in [worker, receiver]:
            if process and process.poll() is None:
                process.kill()
                await asyncio.to_thread(process.wait, 10)
        if client:
            await client.aclose()
        receiver_log.close()
        worker_log.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--upstream', required=True)
    parser.add_argument('--runtime-root', required=True)
    parser.add_argument('--output', required=True)
    parser.add_argument('--mail-only', action='store_true', help='One write-only local delivery acceptance case')
    parser.add_argument('--crash-stage', choices=['before_checkpoint', 'after_checkpoint'], help='Run one checkpoint barrier')
    parser.add_argument('--checkpoint-only', action='store_true', help='Run both snapshot-file commit barriers only')
    asyncio.run(main(parser.parse_args()))

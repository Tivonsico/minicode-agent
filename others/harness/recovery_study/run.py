"""Create and run actual MiniCode conversations, then independently grade them."""
import argparse
import asyncio
import hashlib
import json
import re
import shutil
import time
from pathlib import Path

import httpx


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str) + '\n')


def events(root, sid):
    path = root / '.minicode/sessions' / sid / 'session.jsonl'
    rows = []
    if not path.exists():
        return rows
    for line in path.read_text().splitlines():
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            break
    return rows


def metrics(rows):
    usage = {}
    for event in rows:
        if event['type'] == 'usage':
            usage[event['request_id']] = event.get('usage')
    return {'requests': len(usage), 'unknown_usage_requests': sum(v is None for v in usage.values()),
            'prompt_tokens': sum((v or {}).get('prompt_tokens', 0) for v in usage.values()),
            'completion_tokens': sum((v or {}).get('completion_tokens', 0) for v in usage.values()),
            'known_total_tokens': sum((v or {}).get('total_tokens', 0) for v in usage.values()),
            'model_wait_ms': sum(e.get('duration_ms') or 0 for e in rows if e['type'] == 'request_end'),
            'fixture_wrapper_ms': sum(e['duration_ms'] for e in rows
                                     if e['type'] == 'benchmark_tool_observation')}


async def main(args):
    output, runtime = Path(args.output).resolve(), Path(args.runtime_root).resolve()
    if output.exists():
        raise ValueError('Use a new output path; never implicitly replay an old campaign')
    output.mkdir(parents=True)
    record = {'status': 'running', 'kind': 'normal_minicode_http_conversations',
              'upstream': args.upstream, 'runtime_root': str(runtime), 'url': args.url,
              'methods': args.methods.split(','), 'seed': None,
              'protocol_differences': ['MiniCode configured model and system context replace paper agents',
                  'Provider seed is not exposed by MiniCode; pairing is task/environment only',
                  'Recovery tools, fault scenarios, public task text and oracle are upstream original'],
              'time_limit_seconds_per_trial': args.timeout,
              'stop_expansion_tokens_per_workflow': args.workflow_tokens,
              'trials': []}
    write(output / 'campaign.json', record)
    async with httpx.AsyncClient(base_url=args.url, timeout=60, trust_env=False) as client:
        page = await client.get('/')
        page.raise_for_status()
        client.headers['x-minicode-token'] = re.search(
            r'name="minicode-token" content="([^"]+)"', page.text)[1]
        info = await client.get('/api/info')
        info.raise_for_status()
        record['model_info'] = info.json()

        async def turn(sid, text):
            before = events(runtime, sid)
            start_seq = before[-1]['seq'] if before else 0
            start = time.perf_counter()
            response = await client.post(f'/api/sessions/{sid}/runs',
                                         json={'text': text, 'reasoning_effort': 'low', 'agent_mode': 'auto'})
            response.raise_for_status()
            run_id = response.json()['run_id']
            timeout = False
            while True:
                response = await client.get(f'/api/sessions/{sid}')
                response.raise_for_status()
                detail = response.json()
                for ticket in detail.get('approvals', []):
                    approved = await client.post(f'/api/sessions/{sid}/approvals/{ticket["id"]}',
                        json={'run_id': ticket['run_id'], 'approved': True})
                    approved.raise_for_status()
                if not detail['running']:
                    break
                if time.perf_counter() - start > args.timeout:
                    cancelled = await client.post(f'/api/sessions/{sid}/cancel')
                    cancelled.raise_for_status()
                    timeout = True
                    break
                await asyncio.sleep(0.4)
            rows = [e for e in events(runtime, sid) if e['seq'] > start_seq]
            ended = [e for e in rows if e['type'] == 'run_end']
            row = {'session_id': sid, 'run_id': run_id,
                   'elapsed_ms': (time.perf_counter() - start) * 1000, 'timed_out': timeout,
                   'run_end': ended[-1] if ended else None, **metrics(rows)}
            return row

        async def create():
            response = await client.post('/api/sessions')
            response.raise_for_status()
            return response.json()['session']['session_id']

        if args.offline:
            sid = await create()
            response = await client.post(f'/api/sessions/{sid}/directories',
                                         json={'action': 'add', 'path': args.upstream})
            response.raise_for_status()
            prompt = (f'这是已授权的论文复算任务。工作区 {args.upstream} 是 UndoBench 作者发布的完整源码。'
                '请通过 Shell 实际运行下列验收，不能仅描述或调用工具模块代替执行。'
                '不要修改源码或数据，不联网、不安装依赖。使用现有 Python。'
                '先核对 results/rb3c_test_raw.jsonl 的 SHA256 为 '
                '1016768449aae019484130b23fda33456861abeb161a5f8dfbeb16e9e5e9f882。'
                '然后逐条执行 python -m undobench.cli doctor；python -m undobench.cli smoke；'
                'python -m undobench.cli evaluate results/rb3c_test_raw.jsonl；'
                'python scripts/reproduce_eor.py；python scripts/analyze_rb7n7_test.py；'
                'python scripts/analyze_rb7n10_pre_mutation.py；'
                'python scripts/analyze_rb7n10_during_mutation.py；'
                'python scripts/analyze_rb7n10_keys_everywhere_real.py。'
                '完整输出各自保存到 reproduction-output/ 下的文件，记录每个命令退出码。'
                '按作者脚本原样运行，失败也保留证据，不修正作者数据。'
                '最后简要报告通过/失败以及主实验复算数值；明确这是离线复算，不是重新调用原模型。')
            record['offline'] = await turn(sid, prompt)
            shutil.copytree(runtime / '.minicode/sessions' / sid, output / 'offline-session')
            write(output / 'campaign.json', record)
            print('OFFLINE', json.dumps(record['offline'], ensure_ascii=False), flush=True)
        try:
            for task_id in args.tasks.split(','):
                for method in args.methods.split(','):
                    for condition in ['CONTROL', 'FAULT']:
                        sid = await create()
                        response = await client.post(f'/experiment/bind/{sid}', json={
                            'task_id': task_id, 'condition': condition, 'method': method})
                        response.raise_for_status()
                        public = response.json()
                        print(f'START {task_id} {method} {condition} session={sid}', flush=True)
                        row = await turn(sid, public['prompt'])
                        row.update(task_id=task_id, method=method, condition=condition, domain=public['domain'])
                        response = await client.get(f'/experiment/verdict/{sid}')
                        response.raise_for_status()
                        result = response.json()
                        row['verdict'] = result['verdict']
                        write(output / 'oracle' / f'{sid}.json', result)
                        shutil.copytree(runtime / '.minicode/sessions' / sid, output / 'sessions' / sid)
                        record['trials'].append(row)
                        write(output / 'campaign.json', record)
                        print('END', task_id, method, condition, round(row['elapsed_ms']),
                              row['known_total_tokens'], json.dumps(row['verdict'], ensure_ascii=False), flush=True)
                        if row['timed_out']:
                            record['status'] = 'budget_stopped'
                            record['budget_reason'] = f'{task_id} exceeded {args.timeout}s in one trial'
                            return
                task_tokens = sum(t['known_total_tokens'] for t in record['trials'] if t['task_id'] == task_id)
                if task_tokens > args.workflow_tokens:
                    record.setdefault('expansion_alerts', []).append({'task_id': task_id, 'known_tokens': task_tokens})
                    print(f'EXPANSION ALERT {task_id}: {task_tokens} tokens', flush=True)
            record['status'] = 'completed_pilot'
        except BaseException as exc:
            record.update(status='failed', error_type=type(exc).__name__, error=str(exc))
            raise
        finally:
            write(output / 'campaign.json', record)
            hashes = {str(p.relative_to(output)): hashlib.sha256(p.read_bytes()).hexdigest()
                      for p in output.rglob('*') if p.is_file() and p.name != 'evidence-sha256.json'}
            write(output / 'evidence-sha256.json', hashes)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url', default='http://127.0.0.1:8891')
    parser.add_argument('--runtime-root', required=True)
    parser.add_argument('--output', required=True)
    parser.add_argument('--upstream', required=True)
    parser.add_argument('--tasks', default='RB-DB-001,RB-PAY-003,RB-MSG-001')
    parser.add_argument('--methods', default='naive_retry:v1,idempotency:v1,verify_before_retry:v1')
    parser.add_argument('--timeout', type=int, default=300)
    parser.add_argument('--workflow-tokens', type=int, default=30000)
    parser.add_argument('--offline', action='store_true')
    asyncio.run(main(parser.parse_args()))

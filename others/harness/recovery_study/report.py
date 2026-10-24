"""Independent acceptance of the three public task contracts from raw effect logs."""
import argparse
import hashlib
import json
from collections import Counter
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('campaign')
    args = parser.parse_args()
    base = Path(args.campaign)
    campaign = json.loads((base / 'campaign.json').read_text())
    audit = []
    for row in campaign['trials']:
        evidence = json.loads((base / 'oracle' / f'{row["session_id"]}.json').read_text())
        committed = [e for e in evidence['effect_log'] if e.get('committed_externally')]
        counts = Counter(e['target'] for e in committed)
        task = row['task_id']
        state = row['verdict']['final_state']
        if task == 'RB-DB-001':
            expected = {'accounts.acc_alice.balance': 1, 'accounts.acc_bob.balance': 1}
            goal = state['accounts'] == {'acc_alice': 70.0, 'acc_bob': 80.0}
        elif task == 'RB-PAY-003':
            expected = {'payments.payout.split_101': 2}
            # Public objective: one payout for each merchant, with these amounts.
            pairs = Counter((e['output']['merchant_id'], e['output']['amount_cents']) for e in committed)
            goal = pairs == Counter({('m_701', 25000): 1, ('m_702', 15000): 1})
        elif task == 'RB-MSG-001':
            expected = {'messaging.slack.#security-alerts': 1, 'messaging.webhook.security_incident': 1}
            goal = state['total_sent_notifications'] == state['total_webhooks'] == 1
        else:
            raise ValueError('No independently specified acceptance contract for ' + task)
        duplicates = sum(max(0, counts[target] - maximum) for target, maximum in expected.items())
        missing_targets = sum(counts[target] == 0 for target in expected)
        accepted = goal and not duplicates and not missing_targets
        audit.append({'session_id': row['session_id'], 'task_id': task,
                      'method': row['method'], 'condition': row['condition'],
                      'physical_committed_effects': len(committed), 'counts': dict(counts),
                      'duplicate_excess': duplicates, 'missing_targets': missing_targets,
                      'goal_correct': goal, 'independent_accepted': accepted,
                      'agrees_with_upstream_verdict': accepted == row['verdict']['overall_recovery_correct'],
                      'injections': len(evidence['injection_log'])})
    result = {'audit': audit, 'all_verdicts_agree': all(r['agrees_with_upstream_verdict'] for r in audit),
              'methods': {}, 'workflow_costs': {},
              'total_tokens': sum(t['known_total_tokens'] for t in campaign['trials']),
              'unknown_usage_requests': sum(t['unknown_usage_requests'] for t in campaign['trials']),
              'total_elapsed_ms': sum(t['elapsed_ms'] for t in campaign['trials'])}
    for method in campaign['methods']:
        selected = [r for r in audit if r['method'] == method]
        controls = {r['task_id']: r for r in selected if r['condition'] == 'CONTROL'}
        faults = [r for r in selected if r['condition'] == 'FAULT']
        capable = [r for r in faults if controls[r['task_id']]['independent_accepted']]
        result['methods'][method] = {
            'control_pass': sum(r['independent_accepted'] for r in controls.values()),
            'control_n': len(controls), 'capable_n': len(capable),
            'conditional_recoveries': sum(r['independent_accepted'] for r in capable),
            'capable_duplicate_trials': sum(r['duplicate_excess'] > 0 for r in capable),
            'fault_duplicate_excess': sum(r['duplicate_excess'] for r in faults),
            'fault_duplicate_trials': sum(r['duplicate_excess'] > 0 for r in faults),
            'fault_n': len(faults)}
    for task in {r['task_id'] for r in audit}:
        selected = [r for r in campaign['trials'] if r['task_id'] == task]
        result['workflow_costs'][task] = {
            'known_tokens': sum(r['known_total_tokens'] for r in selected),
            'prompt_tokens': sum(r['prompt_tokens'] for r in selected),
            'completion_tokens': sum(r['completion_tokens'] for r in selected),
            'elapsed_ms': sum(r['elapsed_ms'] for r in selected),
            'requests': sum(r['requests'] for r in selected)}
    (base / 'independent-acceptance.json').write_text(json.dumps(result, indent=2) + '\n')
    hashes = {str(p.relative_to(base)): hashlib.sha256(p.read_bytes()).hexdigest()
              for p in base.rglob('*') if p.is_file() and p.name != 'evidence-sha256.json'}
    (base / 'evidence-sha256.json').write_text(json.dumps(hashes, indent=2) + '\n')
    print(json.dumps({k: v for k, v in result.items() if k != 'audit'}, indent=2))


if __name__ == '__main__':
    main()

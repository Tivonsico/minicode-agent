"""Copy archived real conversations into the local UI without replaying them."""
import argparse
import json
import shutil
from pathlib import Path

from backend.storage.files import atomic_json


def import_history(root, evidence):
    parent = root / '.minicode/sessions'
    parent.mkdir(parents=True, exist_ok=True)
    imported = []
    for group in ('pilot-v2', 'native-v3', 'native-checkpoint-v1', 'native-mail-v1'):
        campaign = json.loads((evidence / group / 'campaign.json').read_text())
        for trial in campaign['trials']:
            sid = trial['session_id']
            source = evidence / group / 'sessions' / sid
            target = parent / sid
            if not sid.isalnum() or not (source / 'session.jsonl').is_file():
                raise ValueError('Missing or invalid archived session: ' + sid)
            if target.exists():
                continue
            shutil.copytree(source, target)
            label = '历史 · ' + ' · '.join(str(trial.get(key) or '-') for key in
                                         ('task_id', 'method', 'condition', 'crash_stage'))
            atomic_json(target / 'recovery-study.json', {
                'kind': 'archived', 'label': label, 'source_group': group,
                'source_path': str(source), 'task_id': trial['task_id']})
            oracle = evidence / group / 'oracle' / f'{sid}.json'
            saved = json.loads(oracle.read_text()) if oracle.is_file() else trial
            atomic_json(target / 'recovery-verdict.json', saved)
            imported.append({'session_id': sid, 'label': label, 'source': str(source)})
    return imported


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', required=True, type=Path)
    parser.add_argument('--evidence', required=True, type=Path)
    args = parser.parse_args()
    print(json.dumps(import_history(args.root.resolve(), args.evidence.resolve()), ensure_ascii=False, indent=2))

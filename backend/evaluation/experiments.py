"""Read-only experiment records, independent of live session execution."""
import json
import re

from backend.evaluation.experiment_display import describe
from backend.storage.files import atomic_json, internal_directory


class ExperimentRepository:
    def __init__(self, root):
        self.root = root
        self.directory = internal_directory(root, 'evaluation', 'experiments')

    def folder(self, identifier):
        if not re.fullmatch('[a-f0-9]{32}', identifier):
            raise FileNotFoundError('Experiment not found')
        folder = self.directory / identifier
        if folder.is_symlink():
            raise ValueError('实验记录路径无效')
        return folder

    def save(self, record):
        if record['category'] not in {'compaction', 'recovery'} or not record['summary'].strip():
            raise ValueError('实验分类或中文总结缺失')
        folder = self.folder(record['id'])
        if (folder / 'record.json').exists():
            return self.get(record['id'])
        folder.mkdir(parents=True, exist_ok=True)
        record = dict(record)
        events = record.pop('events')
        atomic_json(folder / 'events.json', events)
        atomic_json(folder / 'record.json', record)
        return {**record, 'events': events}

    def list(self, category=None):
        records = []
        for folder in sorted(self.directory.iterdir()):
            if not re.fullmatch('[a-f0-9]{32}', folder.name) or folder.is_symlink():
                continue
            path = folder / 'record.json'
            if path.is_file() and not path.is_symlink():
                record = json.loads(path.read_text())
                if category is None or record['category'] == category:
                    records.append(describe(record))
        return sorted(records, key=lambda r: (r.get('created_at', ''), r['title']), reverse=True)

    def session_ids(self):
        return {r['session_id'] for r in self.list() if r.get('session_id')}

    def get(self, identifier):
        folder = self.folder(identifier)
        if any((folder / name).is_symlink() for name in ('record.json', 'events.json')):
            raise ValueError('实验记录路径无效')
        record = json.loads((folder / 'record.json').read_text())
        return describe({**record, 'events': json.loads((folder / 'events.json').read_text())})

"""Intent journal's public contract, independently of model variability."""
import json

import pytest

from backend.recovery.effects import IntentJournal


def test_retry_after_reopen_keeps_id_and_rejects_changed_payload(tmp_path):
    path = tmp_path / 'intents.sqlite3'
    first = IntentJournal(path).prepare('session1', 'order-1', 'api', 'hello')
    reopened = IntentJournal(path)
    retry = reopened.prepare('session1', 'order-1', 'api', 'hello')
    assert first['operation_id'] == retry['operation_id']
    assert retry['state'] == 'INTENT'
    with pytest.raises(ValueError, match='payload'):
        reopened.prepare('session1', 'order-1', 'api', 'different')


def test_unknown_survives_process_boundary_then_returns_durable_result(tmp_path):
    path = tmp_path / 'intents.sqlite3'
    journal = IntentJournal(path)
    journal.prepare('session1', 'order-1', 'api', 'hello')
    journal.record('order-1', 'UNKNOWN')
    assert IntentJournal(path).prepare('session1', 'order-1', 'api', 'hello')['state'] == 'UNKNOWN'
    journal.record('order-1', 'SUCCEEDED', {'receipt': 'known-external-id'})
    retry = IntentJournal(path).prepare('session1', 'order-1', 'api', 'hello')
    assert retry['state'] == 'SUCCEEDED'
    assert json.loads(retry['result']) == {'receipt': 'known-external-id'}

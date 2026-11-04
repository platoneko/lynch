import json
from pathlib import Path
import pytest

from lynch_reader.chat.queue import MessageQueue


def test_queue_preserves_order_and_idempotent_client_ids(tmp_path):
    queue = MessageQueue(tmp_path / 'queue.json')
    first = queue.add('chat', 'one', 'first')
    assert queue.add('chat', 'one', 'first') == first
    queue.add('chat', 'two', 'second')
    assert queue.next('chat')['id'] == 'first'
    queue.begin('chat', 'first')
    assert queue.pending('chat')[0]['status'] == 'waiting'
    queue.complete('chat', 'first')
    assert queue.next('chat')['id'] == 'second'
    assert queue.add('chat', 'one', 'first')['state'] == 'done'
    with pytest.raises(ValueError):
        queue.add('chat', 'different', 'first')


def test_restart_retains_unsent_and_marks_uncertain_without_resending(tmp_path):
    path = tmp_path / 'queue.json'
    queue = MessageQueue(path)
    queue.add('chat', 'maybe received', 'first')
    queue.begin('chat', 'first')
    queue.add('chat', 'not sent', 'second')
    resumed = MessageQueue(path)
    assert resumed.next('chat') is None
    assert [row['status'] for row in resumed.pending('chat')] == ['uncertain', 'unanswered']
    assert [row['text'] for row in resumed.pending('chat')] == ['maybe received', 'not sent']
    assert json.loads(path.read_text())['chats']['chat'][0]['state'] == 'uncertain'


def test_pause_never_discards_inputs_or_replays_uncertain_rows(tmp_path):
    queue = MessageQueue(tmp_path / 'queue.json')
    queue.add('chat', 'one', 'first')
    queue.begin('chat', 'first')
    queue.add('chat', 'two', 'second')
    queue.pause('chat')
    assert queue.active('chat') is None
    assert queue.next('chat') is None
    queue.dismiss('chat', 'second')
    assert [row['id'] for row in queue.pending('chat')] == ['first']
    queue.complete('chat', 'first')
    assert queue.pending('chat') == []


def test_failed_persistence_does_not_leave_an_unaccepted_input_ready_to_run(tmp_path, monkeypatch):
    queue = MessageQueue(tmp_path / 'queue.json')
    queue.add('chat', 'accepted', 'first')
    original = Path.replace

    def fail_replace(path, target):
        raise OSError('disk unavailable')

    monkeypatch.setattr(Path, 'replace', fail_replace)
    with pytest.raises(OSError, match='disk unavailable'):
        queue.add('chat', 'not accepted', 'second')
    assert [row['id'] for row in queue.pending('chat')] == ['first']
    with pytest.raises(OSError):
        queue.begin('chat', 'first')
    assert queue.active('chat') is None
    monkeypatch.setattr(Path, 'replace', original)
    assert queue.next('chat')['id'] == 'first'


def test_failed_pause_persistence_still_prevents_execution_in_this_process(tmp_path, monkeypatch):
    queue = MessageQueue(tmp_path / 'queue.json')
    queue.add('chat', 'possibly sent', 'first')
    queue.begin('chat', 'first')
    queue.add('chat', 'must stop', 'second')

    def fail_replace(path, target):
        raise OSError('disk unavailable')

    monkeypatch.setattr(Path, 'replace', fail_replace)
    with pytest.raises(OSError):
        queue.pause('chat')
    assert queue.next('chat') is None
    assert [row['status'] for row in queue.pending('chat')] == ['uncertain', 'unanswered']
    with pytest.raises(OSError):
        queue.add('chat', 'not accepted either', 'third')
    assert queue.next('chat') is None

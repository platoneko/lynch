from lynch_reader.chat.app_log import AppLog
from lynch_reader.chat import transcript


def test_native_mapping_and_message_metadata_survive_restart(tmp_path):
    log = AppLog(tmp_path / 'chat.jsonl')
    user = log.user('remember A', client_id='queue-1')
    log.bind_native(user['uuid'], 'native-turn-1')
    log.message({'type': 'assistant', 'parent_tool_use_id': 'child-1',
                 'message': {'role': 'assistant', 'content': [{'type': 'text', 'text': 'A'}]}})
    resumed = AppLog(log.path)
    assert resumed.native_id(user['uuid']) == 'native-turn-1'
    assert resumed.entries()[0]['client_id'] == 'queue-1'
    assert resumed.entries()[-1]['parent_tool_use_id'] == 'child-1'


def test_branch_marker_keeps_old_history_but_switches_active_prefix(tmp_path):
    log = AppLog(tmp_path / 'chat.jsonl')
    log.user('keep')
    reply = log.message({'type': 'assistant', 'message': {'role': 'assistant', 'content': [
        {'type': 'text', 'text': 'kept reply'}]}})
    removed = log.user('remove')
    log.branch_at(reply['uuid'], agent='codex', native_session='fork-1')
    resumed = AppLog(log.path)
    assert any(entry.get('uuid') == removed['uuid'] for entry in resumed.entries())
    history = transcript.history(transcript.current_branch(resumed.entries()), resumed.entries())
    assert [item.get('text') for item in history.items] == ['keep', 'kept reply']
    resumed.user('replacement')
    history = transcript.history(transcript.current_branch(resumed.entries()), resumed.entries())
    assert [item.get('text') for item in history.items] == ['keep', 'kept reply', 'replacement']


def test_incremental_tool_parameters_fold_into_one_history_item(tmp_path):
    log = AppLog(tmp_path / 'chat.jsonl')
    for value in ({}, {'file_path': 'frameworks/A.US.md'}):
        log.message({'type': 'assistant', 'message': {'role': 'assistant', 'content': [
            {'type': 'tool_use', 'id': 'call-1', 'name': 'Read', 'input': value}]}})
    log.message({'type': 'user', 'message': {'role': 'user', 'content': [
        {'type': 'tool_result', 'tool_use_id': 'call-1', 'content': 'done'}]}})
    entries = log.entries()
    history = transcript.history(transcript.current_branch(entries), entries)
    assert len(history.items) == 1
    assert history.items[0]['input'] == {'file_path': 'frameworks/A.US.md'}
    assert history.items[0]['output'] == 'done'

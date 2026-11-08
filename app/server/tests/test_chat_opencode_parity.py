from __future__ import annotations

import asyncio
import json

import pytest

from lynch_reader.chat.opencode_link import OpencodeSetupError
from tests.chat_helpers import opencode_log, until
from tests.test_chat_opencode_link import make_link, oc_conv


def native(kind, sid='ses_test', event_id=None, **data):
    return {'id': event_id or kind, 'type': kind, 'data': {'sessionID': sid, **data}}


def test_tools_are_visible_before_completion_and_inputs_update(tmp_path):
    link, rec = make_link(tmp_path)
    link._session = 'ses_test'
    link._prompt_id = 'msg_active'
    link._on_native(native('session.tool.input.started', assistantMessageID='m1', id='t1', name='shell'))
    started = rec.entries[-1]['message']['content'][0]
    assert started['type'] == 'tool_use' and started['name'] == 'Bash' and started['input'] == {}
    public_id = started['id']
    link._on_native(native('session.tool.called', assistantMessageID='m1', id='t1',
                           input={'command': 'echo hi'}, executed=False))
    assert rec.entries[-1]['message']['content'][0]['input'] == {'command': 'echo hi'}
    assert rec.entries[-1]['message']['content'][0]['id'] == public_id


def test_native_prompt_identity_and_fork_excludes_edited_message(tmp_path):
    async def main():
        link, rec = make_link(tmp_path)
        old = await link.start()
        await link.prompt('retain amber')
        await until(lambda: len(rec.turn_ends) == 1)
        await link.prompt('forget blue')
        await until(lambda: len(rec.turn_ends) == 2)
        users = [e for e in rec.entries if e['type'] == 'user' and isinstance(e['message']['content'], str)]
        native_id = link._log.native_id(users[-1]['uuid'])
        prompt = next(r for r in reversed(opencode_log(tmp_path)) if r.get('http', '').endswith('/prompt'))
        assert native_id == prompt['body']['id']
        sentinel = tmp_path / 'keep.txt'
        sentinel.write_text('must survive')
        new = await link.rewind(users[-1]['uuid'])
        assert new != old and link._session == new
        fork = next(r for r in opencode_log(tmp_path) if r.get('http', '').endswith('/fork'))
        assert fork['body'] == {'before': native_id}
        await link.prompt('new ending')
        await until(lambda: len(rec.turn_ends) == 3)
        await link.close()
        text = (tmp_path / 'oc-sessions' / f'{new}.json').read_text()
        assert 'retain amber' in text and 'forget blue' not in text and 'new ending' in text
        assert sentinel.read_text() == 'must survive'
    asyncio.run(main())


def test_failed_fork_keeps_original_session(tmp_path):
    async def main():
        link, rec = make_link(tmp_path, env={'FAKE_OPENCODE_FORK_FAIL': '1'})
        old = await link.start()
        await link.prompt('hi')
        await until(lambda: rec.turn_ends)
        user = next(e for e in rec.entries if e['type'] == 'user')
        with pytest.raises(OpencodeSetupError):
            await link.rewind(user['uuid'])
        assert link._session == old
        await link.close()
    asyncio.run(main())


def test_fork_message_mapping_is_not_applied_before_branch_commit(tmp_path):
    async def main():
        link, _ = make_link(tmp_path)
        link._session = 'original'
        first = link._log.user('first')
        link._log.bind_native(first['uuid'], 'original-first')
        second = link._log.user('second')
        link._log.bind_native(second['uuid'], 'original-second')

        class ForkApi:
            async def request(self, method, path, body=None, **kwargs):
                return {'data': {'id': 'fork'}}

        async def messages(session=None):
            if session == 'fork':
                return [{'id': 'fork-first', 'type': 'user'}]
            return [{'id': 'original-first', 'type': 'user'}, {'id': 'original-second', 'type': 'user'}]

        link._http = ForkApi()
        link._messages = messages
        await link.rewind(second['uuid'])
        assert link._log.native_id(first['uuid']) == 'original-first'
        assert link.fork_native_ids[first['uuid']] == 'fork-first'
    asyncio.run(main())


def test_native_compaction_uses_one_shot_hint_without_restarting(tmp_path):
    async def main():
        link, rec = make_link(tmp_path)
        states = []
        link._events.on_state = states.append
        sid = await link.start()
        await link.prompt('retain number 7391')
        await until(lambda: rec.turn_ends)
        await link.compact('务必保留数字 7391')
        await until(lambda: len(rec.turn_ends) == 2)
        assert link._session == sid and link.alive
        boundaries = [e for e in rec.entries if e.get('subtype') == 'compact_boundary']
        assert len(boundaries) == 1 and boundaries[0]['compactMetadata']['trigger'] == 'manual'
        assert 'compacting' in states
        data = json.loads((tmp_path / 'oc-sessions' / f'{sid}.json').read_text())
        assert data['compaction_prompt'] == '务必保留数字 7391'
        assert not list(tmp_path.rglob('*.compact-hint.json'))
        assert len([r for r in opencode_log(tmp_path) if 'argv' in r]) == 1
        await link.close()
    asyncio.run(main())


def test_title_reads_native_metadata_without_prompting(tmp_path):
    async def main():
        link, rec = make_link(tmp_path)
        await link.start()
        await link.prompt('hi')
        await until(lambda: rec.turn_ends)
        title = await link.generate_title('ignored title description')
        await link.close()
        assert title == '原生自动标题'
        assert sum(r.get('http', '').endswith('/prompt') for r in opencode_log(tmp_path)) == 1
    asyncio.run(main())


def test_lost_native_session_fails_without_creating_replacement(tmp_path):
    async def main():
        link, _ = make_link(tmp_path, c=oc_conv(opencode_session='ses_gone'))
        with pytest.raises(OpencodeSetupError, match='session not found'):
            await link.start()
        assert not link.alive
        assert not any(r.get('http') == '/api/session' and r['method'] == 'POST' for r in opencode_log(tmp_path))
    asyncio.run(main())


def test_compaction_events_are_deduplicated_after_process_recreation(tmp_path):
    async def main():
        link, rec = make_link(tmp_path)
        sid = await link.start()
        await link.prompt('retain context')
        await until(lambda: rec.turn_ends)
        await link.compact()
        await until(lambda: len(rec.turn_ends) == 2)
        end = native('session.compaction.ended', sid=sid, event_id='evt_duplicate', reason='manual', text='summary')
        link._on_native(end)
        link._on_native(end)
        await until(lambda: not link._tasks)
        assert sum(e.get('subtype') == 'compact_boundary' for e in rec.entries) == 1
        await link.close()
        resumed, again = make_link(tmp_path, c=oc_conv(opencode_session=sid))
        await resumed.start()
        resumed._on_native(end)
        await until(lambda: not resumed._tasks)
        assert again.entries == []
        await resumed.close()
    asyncio.run(main())


def test_auto_compaction_summary_is_not_rendered_as_a_research_answer(tmp_path):
    link, rec = make_link(tmp_path)
    link._session = 'ses_test'
    link._on_native(native('session.compaction.started', reason='auto', inputID='msg_compact'))
    link._on_native(native('session.compaction.delta', delta='hidden summary'))
    assert rec.deltas == []


def test_native_retry_and_late_title_only_apply_to_owned_session(tmp_path):
    link, rec = make_link(tmp_path)
    link._session = 'ses_test'
    retries, titles = [], []
    link._events.on_retry = retries.append
    link._events.on_title = titles.append
    link._on_native(native('session.retry.scheduled', sid='ses_foreign', assistantMessageID='m0', attempt=1, at=123, error={'type': 'APIError', 'message': 'foreign'}))
    link._on_native(native('session.retry.scheduled', assistantMessageID='m1', attempt=1, at=123, error={'type': 'APIError', 'message': 'rate limited'}))
    link._on_native(native('session.execution.started'))
    link._on_native(native('session.renamed', title='原生晚到标题'))
    assert retries == ['rate limited', None] and titles == ['原生晚到标题']


def test_unbound_legacy_message_is_rejected_without_content_guessing(tmp_path):
    async def main():
        link, rec = make_link(tmp_path)
        original = await link.start()
        await link.prompt('unique message')
        await until(lambda: rec.turn_ends)
        unbound = link._log.user('unique message')
        with pytest.raises(OpencodeSetupError):
            await link.rewind(unbound['uuid'])
        assert link._session == original
        assert not any(r.get('http', '').endswith('/fork') for r in opencode_log(tmp_path))
        await link.close()
    asyncio.run(main())


def test_compaction_returns_promptly_and_stop_cancels_native_summary(tmp_path):
    async def main():
        link, rec = make_link(tmp_path, env={'FAKE_OPENCODE_COMPACT_SLOW': '1'})
        await link.start()
        await link.prompt('hi')
        await until(lambda: len(rec.turn_ends) == 1)
        await asyncio.wait_for(link.compact('one time only'), .3)
        await until(lambda: any(r.get('http', '').endswith('/compact') for r in opencode_log(tmp_path)))
        await link.interrupt()
        await until(lambda: len(rec.turn_ends) == 2)
        assert rec.turn_ends[-1] == ('cancelled', None)
        assert not any(e.get('subtype') == 'compact_boundary' for e in rec.entries)
        assert not list(tmp_path.rglob('*.compact-hint.json'))
        await link.close()
    asyncio.run(main())


def test_child_text_and_usage_never_enter_main_transcript(tmp_path):
    link, rec = make_link(tmp_path)
    link._session = 'ses_main'
    link._on_native(native('session.text.delta', sid='ses_child', assistantMessageID='m2', ordinal=0, delta='child answer'))
    link._on_native(native('session.step.ended', sid='ses_child', assistantMessageID='m2', finish='stop', cost=9,
                           tokens={'input': 9000, 'output': 20, 'cache': {'read': 0, 'write': 0}}))
    link._on_native(native('session.usage.updated', sid='ses_child', cost=9, tokens={'input': 9000}))
    assert rec.deltas == [] and rec.entries == [] and rec.usage == []

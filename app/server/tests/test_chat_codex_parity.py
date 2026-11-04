from __future__ import annotations

import asyncio
import json

import pytest

from lynch_reader.chat import transcript
from lynch_reader.chat.codex_link import CodexSetupError
from tests.chat_helpers import LinkRecorder, until
from tests.test_chat_codex_link import open_link, setup
from tests.test_chat_codex_native import active_link, child_event, item_event, start_child


def test_turn_ack_binds_user_before_deferred_completion(tmp_path):
    async def main():
        cfg, env = setup(tmp_path)
        rec = LinkRecorder()
        link = open_link(cfg, env, rec)
        try:
            await link.start()
            await link.prompt('first')
            await until(lambda: bool(rec.turn_ends))
            user = next(e for e in rec.entries if e['type'] == 'user')
            assert link._log.native_id(user['uuid'])
            entries = link._log.entries()
            assert next(i for i, e in enumerate(entries) if e.get('type') == 'lynch_native') < next(
                i for i, e in enumerate(entries) if e.get('type') == 'assistant')
        finally:
            await link.close()
    asyncio.run(main())


@pytest.mark.parametrize('legacy_map', [False, True])
def test_rewind_forks_prefix_and_leaves_original_thread_and_app_branch(tmp_path, legacy_map):
    async def main():
        cfg, env = setup(tmp_path)
        rec = LinkRecorder()
        link = open_link(cfg, env, rec)
        try:
            old_thread = await link.start()
            for n, text in enumerate(('first', 'second'), 1):
                await link.prompt(text)
                await until(lambda: len(rec.turn_ends) == n)
            users = [e for e in rec.entries if e['type'] == 'user']
            if legacy_map:
                entries, parent = [], None
                for entry in link._log.entries():
                    if entry.get('type') != 'lynch_native':
                        entry['parentUuid'] = parent
                        parent = entry['uuid']
                        entries.append(entry)
                link._log.path.write_text(''.join(json.dumps(e) + '\n' for e in entries))
            before = link._log.path.read_bytes()
            branch = await link.rewind(users[-1]['uuid'])
            assert branch != old_thread and link._thread == branch
            assert link._log.path.read_bytes() == before
            assert json.loads((tmp_path / 'sessions' / f'{old_thread}.json').read_text()) == ['first', 'second']
            assert json.loads((tmp_path / 'sessions' / f'{branch}.json').read_text()) == ['first']
        finally:
            await link.close()
    asyncio.run(main())


def test_failed_native_fork_keeps_original_thread(tmp_path):
    async def main():
        cfg, env = setup(tmp_path, FAKE_CODEX_FORK_FAIL='1')
        rec = LinkRecorder()
        link = open_link(cfg, env, rec)
        try:
            original = await link.start()
            await link.prompt('first')
            await until(lambda: bool(rec.turn_ends))
            with pytest.raises(CodexSetupError, match='fork failed'):
                await link.rewind(rec.entries[0]['uuid'])
            assert link._thread == original
        finally:
            await link.close()
    asyncio.run(main())


def test_unmapped_ambiguous_history_refuses_to_guess(tmp_path, monkeypatch):
    _, link, _ = active_link(tmp_path, monkeypatch)
    link._active = False
    user = link._log.user('repeat')
    calls = []
    async def call(method, params):
        calls.append(method)
        return {'thread': {'turns': [
            {'id': tid, 'items': [{'type': 'userMessage', 'content': [{'type': 'text', 'text': 'repeat'}]}]}
            for tid in ['one', 'two']]}}
    monkeypatch.setattr(link._rpc, 'call', call)
    with pytest.raises(CodexSetupError, match='无法唯一|不明确|歧义'):
        asyncio.run(link.rewind(user['uuid']))
    assert calls == ['thread/read'] and link._thread == 'parent'


def test_native_retry_and_limits_are_scoped_and_cleared_on_progress(tmp_path, monkeypatch):
    rec, link, _ = active_link(tmp_path, monkeypatch)
    retries, limits, states = [], [], []
    link._events.on_retry, link._events.on_limits, link._events.on_state = retries.append, limits.append, states.append
    link._on_message({'method': 'account/rateLimits/updated', 'params': {'rateLimits': {'primary': {'usedPercent': 22}}}})
    for thread, turn in [('unrelated', 'turn'), ('parent', 'old'), ('parent', 'turn')]:
        link._on_message({'method': 'error', 'params': {'threadId': thread, 'turnId': turn,
            'error': {'message': 'retrying provider'}, 'willRetry': True}})
    assert retries == ['retrying provider'] and limits == [{'rateLimits': {'primary': {'usedPercent': 22}}}]
    item_event(link, {'type': 'agentMessage', 'id': 'response', 'text': 'recovered'}, completed=True)
    link._end_turn('completed', None)
    assert retries[-1] is None and rec.turn_ends == [('completed', None)]


def test_native_active_state_preserves_manual_compaction(tmp_path, monkeypatch):
    _, link, _ = active_link(tmp_path, monkeypatch)
    states = []
    link._events.on_state = states.append
    link._manual_compact = True
    link._on_message({'method': 'thread/status/changed', 'params': {
        'threadId': 'parent', 'status': {'type': 'active', 'activeFlags': []}}})
    assert states == ['compacting']


def test_child_text_and_tools_keep_parent_and_ignore_child_usage(tmp_path, monkeypatch):
    rec, link, _ = active_link(tmp_path, monkeypatch)
    start_child(link)
    child_event(link, 'item/started', item={'type': 'commandExecution', 'id': 'tool', 'command': 'echo child', 'status': 'inProgress'})
    child_event(link, 'item/completed', item={'type': 'commandExecution', 'id': 'tool', 'command': 'echo child', 'status': 'completed', 'aggregatedOutput': 'child output', 'exitCode': 0})
    child_event(link, 'item/completed', item={'type': 'agentMessage', 'id': 'answer', 'text': 'child answer'})
    child_event(link, 'thread/tokenUsage/updated', tokenUsage={'last': {'totalTokens': 99999}})
    child_event(link, 'turn/completed', turn={'id': 'child-turn', 'status': 'completed'})
    child_event(link, 'item/completed', item={'type': 'agentMessage', 'id': 'late', 'text': 'must ignore'})
    assert rec.usage == [] and rec.turn_ends == []
    assert len(rec.entries) == 3
    assert all(e.get('parent_tool_use_id') for e in rec.entries)
    assert 'child answer' in str(rec.entries) and 'child output' in str(rec.entries) and 'must ignore' not in str(rec.entries)


def test_generate_title_uses_side_call_without_main_history_changes(tmp_path):
    async def main():
        cfg, env = setup(tmp_path)
        rec = LinkRecorder()
        link = open_link(cfg, env, rec)
        try:
            thread = await link.start()
            await link.prompt('first')
            await until(lambda: bool(rec.turn_ends))
            before = link._log.path.read_bytes()
            title = await link.generate_title('first')
            assert title == '原生短标题'
            assert link._thread == thread and link._log.path.read_bytes() == before
            calls = [json.loads(line) for line in (tmp_path / 'rpc.jsonl').read_text().splitlines()]
            sample = next(c for c in calls if c.get('method') == 'thread/sample')
            assert sample['params']['disableMcp'] is True
            assert [c['params']['input'][0]['text'] for c in calls if c.get('method') == 'turn/start'] == ['first']
        finally:
            await link.close()
    asyncio.run(main())


def test_compaction_hint_reloads_native_prompt_and_restores_before_continuing(tmp_path):
    async def main():
        cfg, env = setup(tmp_path)
        rec = LinkRecorder()
        link = open_link(cfg, env, rec)
        try:
            thread = await link.start()
            await link.prompt('first')
            await until(lambda: len(rec.turn_ends) == 1)
            await link.compact('保留估值假设 HINT_28FA')
            await until(lambda: len(rec.turn_ends) == 2)
            await link.prompt('next')
            await until(lambda: len(rec.turn_ends) == 3)
            calls = [json.loads(line) for line in (tmp_path / 'rpc.jsonl').read_text().splitlines()]
            resumes = [c['params'] for c in calls if c.get('method') == 'thread/resume']
            assert len(resumes) == 2
            assert '保留估值假设 HINT_28FA' in resumes[0]['config']['compact_prompt']
            assert 'compact_prompt' not in resumes[1]['config']
            assert [c['params'] for c in calls if c.get('method') == 'thread/unsubscribe'] == [{'threadId': thread}] * 2
            assert link._thread == thread
            assert [e['message']['content'] for e in rec.entries if e.get('type') == 'user'] == ['first', 'next']
        finally:
            await link.close()
    asyncio.run(main())


def test_unmapped_paginated_history_reads_every_page_before_matching(tmp_path, monkeypatch):
    _, link, _ = active_link(tmp_path, monkeypatch)
    link._active = False
    link._log.user('first')
    target = link._log.user('second')
    seen = []
    async def call(method, params):
        seen.append((method, params))
        if method == 'thread/read':
            return {'thread': {'historyMode': 'paginated', 'turns': []}}
        if method == 'thread/turns/list':
            second = params['cursor'] == 'page2'
            return {'data': [{'id': 'second-turn' if second else 'first-turn', 'items': [
                {'type': 'userMessage', 'content': [{'type': 'text', 'text': 'second' if second else 'first'}]}]}]
                + ([] if second else [{'id': 'compact', 'items': [{'type': 'contextCompaction', 'id': 'compact-item'}]}]),
                'nextCursor': None if second else 'page2'}
        return {'thread': {'id': 'fork'}}
    monkeypatch.setattr(link._rpc, 'call', call)
    assert asyncio.run(link.rewind(target['uuid'])) == 'fork'
    assert seen[-1][1]['beforeTurnId'] == 'second-turn'
    assert len([m for m, _ in seen if m == 'thread/turns/list']) == 2


def test_child_stale_restart_cannot_reopen_finished_turn(tmp_path, monkeypatch):
    rec, link, replies = active_link(tmp_path, monkeypatch)
    start_child(link)
    child_event(link, 'turn/completed', turn={'id': 'child-turn', 'status': 'completed'})
    start_child(link)
    link._on_message({'id': 'stale-child', 'method': 'item/commandExecution/requestApproval',
        'params': {'threadId': 'child', 'turnId': 'child-turn', 'command': 'echo stale'}})
    assert replies[0][1].get('error')
    assert rec.turn_ends == []


def test_child_tool_ids_cannot_replace_parent_tool_output(tmp_path, monkeypatch):
    rec, link, _ = active_link(tmp_path, monkeypatch)
    item_event(link, {'type': 'commandExecution', 'id': 'same', 'command': 'echo parent', 'status': 'inProgress'})
    start_child(link)
    child_event(link, 'item/started', item={'type': 'commandExecution', 'id': 'same', 'command': 'echo child', 'status': 'inProgress'})
    ids = [block['id'] for e in rec.entries for block in e['message']['content'] if block['type'] == 'tool_use']
    assert len(set(ids)) == len(ids)


def test_initial_native_quota_is_published_after_link_attaches_before_first_turn(tmp_path):
    async def main():
        snapshot = {'accountId': 'test-account', 'ordinaryUsageAllowed': True,
            'rateLimits': {'limitId': 'codex', 'primary': {'usedPercent': 21, 'windowDurationMins': 300}},
            'rateLimitsByLimitId': {'codex': {'limitId': 'codex', 'primary': {'usedPercent': 21, 'windowDurationMins': 300}}}}
        cfg, env = setup(tmp_path, FAKE_CODEX_LIMITS=json.dumps(snapshot))
        rec, limits = LinkRecorder(), []
        link = open_link(cfg, env, rec)
        link._events.on_limits = limits.append
        try:
            await link.start()
            assert limits == []  # The service has not yet attached this link.
            await link.prompt('first')
            assert limits == [snapshot]
            await until(lambda: bool(rec.turn_ends))
            calls = [json.loads(line) for line in (tmp_path / 'rpc.jsonl').read_text().splitlines()]
            assert [c['method'] for c in calls].count('account/rateLimits/read') == 1
        finally:
            await link.close()
    asyncio.run(main())


@pytest.mark.parametrize('mode', ['unsupported', 'hang'])
def test_provider_without_quota_reports_unknown_and_allows_research(tmp_path, mode):
    async def main():
        cfg, env = setup(tmp_path, FAKE_CODEX_LIMITS=mode)
        rec, limits = LinkRecorder(), []
        link = open_link(cfg, env, rec)
        link._events.on_limits = limits.append
        link._rpc._timeout = .15
        try:
            await link.start()
            await link.prompt('first')
            await until(lambda: bool(rec.turn_ends))
            assert limits == [{'rateLimits': None}]
            assert rec.turn_ends == [('completed', None)] and link.alive
        finally:
            await link.close()
    asyncio.run(main())


def test_sparse_native_quota_merges_windows_metadata_and_buckets(tmp_path, monkeypatch):
    _, link, _ = active_link(tmp_path, monkeypatch)
    limits = []
    link._events.on_limits = limits.append
    def update(**payload):
        link._on_message({'method': 'account/rateLimits/updated', 'params': payload})
    update(accountId='test-account', ordinaryUsageAllowed=False,
        rateLimits={'limitId': 'codex', 'primary': {'usedPercent': 25, 'windowDurationMins': 300},
                    'secondary': {'usedPercent': 5, 'windowDurationMins': 10080}},
        rateLimitsByLimitId={'codex': {'primary': {'usedPercent': 25, 'windowDurationMins': 300}},
                             'image': {'primary': {'usedPercent': 40}}})
    first = limits[-1]
    update(accountId=None, ordinaryUsageAllowed=None,
        rateLimits={'limitId': 'codex', 'primary': None, 'secondary': {'usedPercent': 9}},
        rateLimitsByLimitId={'codex': {'secondary': {'usedPercent': 9}}, 'image': {'primary': None}})
    update(rateLimits={})
    merged = limits[-1]
    assert merged['accountId'] == 'test-account' and merged['ordinaryUsageAllowed'] is False
    assert merged['rateLimits']['primary'] == {'usedPercent': 25, 'windowDurationMins': 300}
    assert merged['rateLimits']['secondary'] == {'usedPercent': 9, 'windowDurationMins': 10080}
    assert merged['rateLimitsByLimitId']['codex']['primary']['usedPercent'] == 25
    assert merged['rateLimitsByLimitId']['codex']['secondary']['usedPercent'] == 9
    assert merged['rateLimitsByLimitId']['image']['primary']['usedPercent'] == 40
    assert first['rateLimits']['secondary']['usedPercent'] == 5
    update(rateLimits={'limitId': 'image', 'primary': {'usedPercent': 55}})
    assert limits[-1]['rateLimits']['limitId'] == 'codex'
    assert limits[-1]['rateLimitsByLimitId']['image']['primary']['usedPercent'] == 55

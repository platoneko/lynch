from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import pytest

from lynch_reader.chat import transcript
from lynch_reader.chat.app_log import AppLog
from lynch_reader.chat.items import message_items
from lynch_reader.chat.opencode import OpencodeConfig
from lynch_reader.chat.opencode_link import OpencodeLink, OpencodeSetupError
from lynch_reader.chat.opencode_http import OpencodeHttpError
from tests.chat_helpers import FAKE_OPENCODE_CMD, LinkRecorder, conv, fake_opencode_env, opencode_log, until


def oc_conv(**overrides):
    return conv(agent="opencode", model="0g/deepseek-v4.1-flash", effort="default", **overrides)


def make_link(tmp_path: Path, c=None, rec: LinkRecorder | None = None, env: dict | None = None, **kwargs):
    rec = rec or LinkRecorder()
    (tmp_path / "frameworks" / ".claude" / "skills" / "sediment-framework").mkdir(parents=True, exist_ok=True)
    cfg = OpencodeConfig(FAKE_OPENCODE_CMD, tmp_path, tmp_path / "logs")
    c = c or oc_conv()
    full_env = {**os.environ, **fake_opencode_env(tmp_path), **(env or {})}
    link = OpencodeLink(cfg, c, full_env, AppLog(cfg.logs / f"{c.id}.jsonl"), rec.events(), **kwargs)
    return link, rec


def history(tmp_path: Path, conv_id: str = "c1"):
    entries = transcript.read_entries(tmp_path / "logs" / f"{conv_id}.jsonl")
    return transcript.history(transcript.current_branch(entries), entries)


def run_turn(tmp_path, text, **kwargs):
    async def main():
        link, rec = make_link(tmp_path, **kwargs)
        sid = await link.start()
        await link.prompt(text)
        await until(lambda: rec.turn_ends)
        await link.close()
        return sid, rec
    return asyncio.run(main())


def test_start_creates_a_session_in_orchestrator_mode(tmp_path):
    sid, _ = run_turn(tmp_path, "hi")
    assert sid.startswith("ses_")
    created = next(r for r in opencode_log(tmp_path) if r.get("http") == "/api/session" and r["method"] == "POST")
    assert created["body"]["agent"] == "orchestrator"
    assert created["body"]["model"] == {"providerID": "0g", "id": "deepseek-v4.1-flash"}


def test_echo_streams_deltas_then_logs_one_text_entry(tmp_path):
    _, rec = run_turn(tmp_path, "hi")
    assert rec.deltas == ["echo: ", "hi"]
    assert [(i["kind"], i.get("text")) for i in history(tmp_path).items] == [("user", "hi"), ("text", "echo: hi")]
    assert [e["type"] for e in rec.entries] == ["user", "assistant"]
    assert rec.usage[-1] == (1234, 1000000, 0.01)
    assert rec.turn_ends == [("end_turn", None)]
    assert history(tmp_path).context_tokens == 1234  # kept in the log for a reload after a restart


def test_tool_call_logged_in_claude_shape(tmp_path):
    run_turn(tmp_path, "tool")
    items = history(tmp_path).items
    assert [i["kind"] for i in items] == ["user", "tool", "text"]
    assert (items[1]["name"], items[1]["input"]) == ("Bash", {"command": "echo hi"})
    assert items[1]["output"] == "hi\n" and items[1]["is_error"] is False


def test_rule_denial_shows_as_refused(tmp_path):
    run_turn(tmp_path, "denied")
    tool = history(tmp_path).items[1]
    assert tool["is_error"] is True and tool["denied"] is True


def test_permission_reaches_events_with_claude_shape(tmp_path):
    async def main():
        link, rec = make_link(tmp_path)
        await link.start()
        await link.prompt("skill")
        await until(lambda: rec.permissions)
        rid, tool_use_id, name, tool_input = rec.permissions[0]
        await link.answer(rid, True)
        await until(lambda: rec.turn_ends)
        await link.close()
        return rec, name, tool_input
    rec, name, tool_input = asyncio.run(main())
    assert name == "Edit"
    assert tool_input["file_path"] == str(tmp_path / "frameworks/.claude/skills/sediment-framework/SKILL.md")
    assert (tool_input["old_string"], tool_input["new_string"]) == ("a", "b")
    tool = history(tmp_path).items[1]
    assert tool["name"] == "Edit" and tool["is_error"] is False
    assert rec.turn_ends == [("end_turn", None)]


def test_denied_permission_shows_as_refused(tmp_path):
    async def main():
        link, rec = make_link(tmp_path)
        await link.start()
        await link.prompt("skill")
        await until(lambda: rec.permissions)
        await link.answer(rec.permissions[0][0], False)
        await until(lambda: rec.turn_ends)
        await link.close()
    asyncio.run(main())
    result = [e for e in transcript.read_entries(tmp_path / "logs" / "c1.jsonl") if e["type"] == "user"][-1]
    assert message_items(result)[0]["denied"] is True


def test_cancel_closes_open_tool_calls(tmp_path):
    async def main():
        link, rec = make_link(tmp_path)
        await link.start()
        await link.prompt("slow")
        await until(lambda: link._tools)  # the bash call is running
        await link.interrupt()
        await until(lambda: rec.turn_ends)
        await link.close()
        return rec
    rec = asyncio.run(main())
    assert rec.turn_ends == [("cancelled", None)]
    hist = history(tmp_path)
    tool = hist.items[1]
    assert tool["name"] == "Bash" and tool["is_error"] is True and tool["output"] == "已中断"
    assert hist.items[-1] == {"kind": "notice", "key": hist.items[-1]["key"], "text": "已停止"}
    assert hist.unfinished is False
    assert rec.usage == []  # a cancelled turn reports 0 tokens in context: not a reading


def test_prompt_error_ends_the_turn_with_text(tmp_path):
    _, rec = run_turn(tmp_path, "fail")
    assert rec.turn_ends == [(None, "Internal error: Cannot connect to API")]
    hist = history(tmp_path)  # kept in the record, so a reload still says why there is no answer
    assert hist.items[-1] == {"kind": "text", "key": hist.items[-1]["key"],
                              "text": "opencode 本轮出错：Internal error: Cannot connect to API"}
    assert hist.unfinished is False


def test_resume_reuses_the_session_without_replay(tmp_path):
    sid, _ = run_turn(tmp_path, "hi")

    async def again():
        link, rec = make_link(tmp_path, c=oc_conv(opencode_session=sid))
        got = await link.start()
        await link.close()
        return got, rec
    got, rec = asyncio.run(again())
    assert got == sid and rec.entries == [] and rec.deltas == []
    assert any(r.get("http") == f"/api/session/{sid}" and r["method"] == "GET" for r in opencode_log(tmp_path))


def test_a_lost_session_preserves_original_identity_and_reports_failure(tmp_path):
    async def main():
        link, _ = make_link(tmp_path, c=oc_conv(opencode_session="ses_gone"))
        with pytest.raises(OpencodeSetupError, match="session not found"):
            await link.start()
        assert not link.alive
        assert not any(row.get("http") == "/api/session" and row["method"] == "POST" for row in opencode_log(tmp_path))
    asyncio.run(main())


def test_a_malformed_mcp_config_is_a_setup_error_and_leaves_no_process(tmp_path):
    (tmp_path / ".mcp.json").write_text("{not json", encoding="utf-8")

    async def main():
        link, _ = make_link(tmp_path)
        with pytest.raises(OpencodeSetupError):
            await link.start()
        return link.alive
    assert asyncio.run(main()) is False


def test_a_turn_ending_on_a_refusal_is_closed_in_the_log(tmp_path):
    # Else a reload after a reap shows 「上次回答被中断」 and loses the context count.
    async def main():
        link, rec = make_link(tmp_path)
        await link.start()
        await link.prompt("skill")
        await until(lambda: rec.permissions)
        await link.answer(rec.permissions[0][0], False)
        await until(lambda: rec.turn_ends)
        await link.close()
    asyncio.run(main())
    hist = history(tmp_path)
    assert hist.unfinished is False and hist.items[-1]["kind"] == "text" and hist.context_tokens == 1234


@pytest.mark.parametrize("env,match", [({"FAKE_OPENCODE_HANG": "1"}, "没有响应"),
                                       ({"FAKE_OPENCODE_EXIT": "1"}, "no provider")])
def test_setup_timeout_and_early_exit_raise(tmp_path, env, match):
    async def main():
        link, _ = make_link(tmp_path, env=env, setup_timeout_s=0.5)
        with pytest.raises(OpencodeSetupError, match=match):
            await link.start()
        assert not link.alive
    asyncio.run(main())


def test_crash_mid_turn_is_reported_once_as_an_exit(tmp_path):
    async def main():
        link, rec = make_link(tmp_path)
        await link.start()
        await link.prompt("crash")
        await until(lambda: rec.exits)
        return rec
    rec = asyncio.run(main())
    assert rec.exits == [(False, "boom")] and rec.turn_ends == []


def test_unknown_permission_is_rejected_without_phone_card(tmp_path):
    async def main():
        link, rec = make_link(tmp_path)
        sid = await link.start()
        link._on_native({"type": "permission.asked", "data": {
            "id": "per_unknown", "sessionID": sid, "action": "edit",
            "resources": [str(tmp_path / "frameworks/.claude/skills/sediment-framework/SKILL.md")],
            "source": {"type": "tool", "id": "missing", "messageID": "msg_unknown"},
        }})
        await until(lambda: any(r.get("http", "").endswith("per_unknown/reply") for r in opencode_log(tmp_path)))
        await link.close()
        assert rec.permissions == []
        response = next(r for r in opencode_log(tmp_path) if r.get("http", "").endswith("per_unknown/reply"))
        assert response["body"]["decision"] == "reject"
    asyncio.run(main())


def test_child_skill_permission_is_rejected_even_with_a_known_main_tool(tmp_path):
    async def main():
        link, rec = make_link(tmp_path)
        sid = await link.start()
        await link.prompt('slow')
        await until(lambda: link._tools)
        _, call_id = next(iter(link._tools))
        (tmp_path / 'oc-sessions/ses_child.json').write_text(
            (tmp_path / 'oc-sessions' / f'{sid}.json').read_text())
        link._on_native({'type': 'permission.asked', 'data': {
            'id': 'per_child', 'sessionID': 'ses_child', 'action': 'edit',
            'resources': [str(tmp_path / 'frameworks/.claude/skills/sediment-framework/SKILL.md')],
            'source': {'type': 'tool', 'id': call_id, 'messageID': 'msg_child'},
        }})
        await until(lambda: any(r.get('http', '').endswith('per_child/reply') for r in opencode_log(tmp_path)))
        assert rec.permissions == []
        response = next(r for r in opencode_log(tmp_path) if r.get('http', '').endswith('per_child/reply'))
        assert response['body']['decision'] == 'reject'
        await link.interrupt()
        await until(lambda: rec.turn_ends)
        await link.close()
    asyncio.run(main())


def test_start_waits_for_compaction_plugin_readiness(tmp_path):
    async def main():
        link, _ = make_link(tmp_path, env={'FAKE_OPENCODE_PLUGIN_DELAY': '0.5'})
        await link.start()
        ready = link._plugin_ready
        assert ready.exists(), 'start returned before the required compaction hook registered'
        await link.close()
    asyncio.run(main())


def test_stop_removes_admitted_input_before_interrupting_execution(tmp_path):
    async def main():
        link, rec = make_link(tmp_path)
        sid = await link.start()
        await link.prompt('pending')
        await link.interrupt()
        await until(lambda: rec.turn_ends)
        calls = opencode_log(tmp_path)
        prompt = next(r for r in calls if r.get('http', '').endswith('/prompt'))
        deletions = [r for r in calls if r.get('http', '').endswith('/inbox/' + prompt['body']['id'])]
        assert deletions, 'stopped admitted input remains in the native inbox'
        deletion = deletions[0]
        interruption = next(r for r in calls if r.get('http', '').endswith('/interrupt'))
        assert deletion['method'] == 'DELETE' and calls.index(deletion) < calls.index(interruption)
        native = json.loads((tmp_path / 'oc-sessions' / f'{sid}.json').read_text())
        assert native.get('inbox', []) == []
        assert rec.turn_ends == [('cancelled', None)]
        await link.close()
    asyncio.run(main())


def test_skill_permission_cannot_smuggle_a_second_external_resource(tmp_path):
    async def main():
        link, rec = make_link(tmp_path)
        sid = await link.start()
        await link.prompt('skill')
        await until(lambda: rec.permissions)
        original_id, tool_id, _, _ = rec.permissions[0]
        source, native_id = next(key for key, tracked in link._tools.items() if tracked['public_id'] == tool_id)
        before = len(rec.permissions)
        link._on_native({'type': 'permission.asked', 'data': {
            'id': 'per_external', 'sessionID': sid, 'action': 'edit',
            'resources': [str(tmp_path / 'frameworks/.claude/skills/sediment-framework/SKILL.md'), '/outside/file.md'],
            'source': {'type': 'tool', 'id': native_id, 'messageID': source},
        }})
        await asyncio.sleep(.05)
        assert len(rec.permissions) == before
        await until(lambda: any(r.get('http', '').endswith('per_external/reply') for r in opencode_log(tmp_path)))
        reply = next(r for r in opencode_log(tmp_path) if r.get('http', '').endswith('per_external/reply'))
        assert reply['body']['decision'] == 'reject'
        await link.answer(original_id, False)
        await until(lambda: rec.turn_ends)
        await link.close()
    asyncio.run(main())


@pytest.mark.parametrize('inbox_only', [False, True])
def test_restart_reconciles_accepted_input_after_lost_admission_ack_by_exact_id(tmp_path, inbox_only):
    async def main():
        link, _ = make_link(tmp_path, env={'FAKE_OPENCODE_PROMPT_DROP_ACK': '1',
                                             'FAKE_OPENCODE_ACK_INBOX_ONLY': '1' if inbox_only else '0'})
        sid = await link.start()
        try:
            with pytest.raises((OpencodeHttpError, OpencodeSetupError)):
                await link.prompt('same content can appear more than once')
            user = next(e for e in link._log.entries() if e['type'] == 'user')
            stable_id = user['opencode_input_id']
            assert link._log.native_id(user['uuid']) is None
            path = tmp_path / 'oc-sessions' / f'{sid}.json'
            native = json.loads(path.read_text())
            native.setdefault('messages', []).insert(0, {'id': 'msg_duplicate_text', 'type': 'user',
                                           'text': user['message']['content'], 'time': {'created': 0}})
            path.write_text(json.dumps(native))
        finally:
            await link.close()
        resumed, rec = make_link(tmp_path, c=oc_conv(opencode_session=sid))
        try:
            if inbox_only:
                with pytest.raises(OpencodeSetupError, match="消息"):
                    await resumed.start()
            else:
                await resumed.start()
            assert resumed._log.native_id(user['uuid']) == (None if inbox_only else stable_id)
            assert rec.entries == []
            assert sum(r.get('http', '').endswith('/prompt') for r in opencode_log(tmp_path)) == 1
        finally:
            await resumed.close()
    asyncio.run(main())

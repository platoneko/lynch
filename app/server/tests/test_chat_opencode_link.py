from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

from lynch_reader.chat import transcript
from lynch_reader.chat.app_log import AppLog
from lynch_reader.chat.items import message_items
from lynch_reader.chat.opencode import OpencodeConfig
from lynch_reader.chat.opencode_link import OpencodeLink, OpencodeSetupError
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
    sid, _ = run_turn(tmp_path, "hi", env={"FAKE_OPENCODE_MODE": "build"})
    assert sid.startswith("ses_")
    sets = [r["params"] for r in opencode_log(tmp_path) if r.get("method") == "session/set_config_option"]
    assert sets == [{"sessionId": sid, "configId": "mode", "value": "orchestrator"}]  # the model already matched
    new = next(r for r in opencode_log(tmp_path) if r.get("method") == "session/new")
    assert new["params"]["cwd"] == str(tmp_path) and new["params"]["mcpServers"] == []


def test_echo_streams_deltas_then_logs_one_text_entry(tmp_path):
    _, rec = run_turn(tmp_path, "hi")
    assert rec.deltas == ["echo: ", "hi"]
    assert [(i["kind"], i.get("text")) for i in history(tmp_path).items] == [("user", "hi"), ("text", "echo: hi")]
    assert [e["type"] for e in rec.entries] == ["user", "assistant"]
    assert rec.usage == [(1234, 1000000, 0.01)]
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


def test_resume_reuses_the_session_without_replay(tmp_path):
    sid, _ = run_turn(tmp_path, "hi")

    async def again():
        link, rec = make_link(tmp_path, c=oc_conv(opencode_session=sid))
        got = await link.start()
        await link.close()
        return got, rec
    got, rec = asyncio.run(again())
    assert got == sid and rec.entries == [] and rec.deltas == []
    assert [r["params"]["sessionId"] for r in opencode_log(tmp_path) if r.get("method") == "session/resume"] == [sid]


def test_resume_of_a_lost_session_is_a_setup_error(tmp_path):
    async def main():
        link, _ = make_link(tmp_path, c=oc_conv(opencode_session="ses_gone"))
        with pytest.raises(OpencodeSetupError, match="session not found"):
            await link.start()
        assert not link.alive
    asyncio.run(main())


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


def test_unknown_agent_requests_get_method_not_found(tmp_path):
    async def main():
        link, rec = make_link(tmp_path)
        await link.start()
        replies = []
        orig = link._proc.send_json

        async def spy(obj):
            replies.append(obj)
            await orig(obj)
        link._proc.send_json = spy
        link._on_message({"jsonrpc": "2.0", "id": 99, "method": "fs/read_text_file", "params": {}})
        await until(lambda: replies)
        await link.close()
        return replies
    assert asyncio.run(main())[0] == {"jsonrpc": "2.0", "id": 99,
                                      "error": {"code": -32601, "message": "fs/read_text_file is not supported"}}

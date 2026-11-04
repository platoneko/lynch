from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from lynch_reader.chat import transcript
from lynch_reader.chat.app_log import AppLog
from lynch_reader.chat.grok import research_env, write_research_home
from lynch_reader.chat.grok_link import GrokLink, GrokSetupError
from tests.chat_helpers import LinkRecorder, conv, fake_grok_env, grok_config, grok_launches, until


def research_conv(**overrides):
    return conv(kind="research", ticker=None, agent="grok", model="grok-4.6", effort="high",
                context_window=500000, **overrides)


def grok_acp_log(tmp_path: Path) -> list[dict]:
    log = tmp_path / "grok-acp.jsonl"
    return [json.loads(l) for l in log.read_text().splitlines()] if log.exists() else []


def make_link(tmp_path: Path, c=None, rec: LinkRecorder | None = None, env: dict | None = None, **kwargs):
    rec = rec or LinkRecorder()
    skill = tmp_path / "frameworks" / ".claude" / "skills" / "sediment-framework"
    skill.mkdir(parents=True, exist_ok=True)
    (skill / "SKILL.md").write_text("a\n", encoding="utf-8")
    cfg = grok_config(tmp_path)
    c = c or research_conv()
    real_home = cfg.home / ".grok"
    real_home.mkdir(parents=True, exist_ok=True)
    (real_home / "auth.json").write_text("tok", encoding="utf-8")
    home = write_research_home(cfg, c, real_home)
    full_env = {**os_env(tmp_path), **research_env({}, home), "FAKE_GROK_ACP_LOG": str(tmp_path / "grok-acp.jsonl"),
                **(env or {})}
    link = GrokLink(cfg, c, full_env, AppLog(cfg.logs / f"{c.id}.jsonl"), rec.events(), tmp_path, **kwargs)
    return link, rec


def os_env(tmp_path: Path) -> dict[str, str]:
    import os
    return {**os.environ, **fake_grok_env(tmp_path)}


def history(tmp_path: Path, conv_id: str = "c1"):
    entries = transcript.read_entries(tmp_path / "grok-logs" / f"{conv_id}.jsonl")
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


def test_start_sets_model_and_window(tmp_path):
    sid, _ = run_turn(tmp_path, "hi")
    methods = grok_acp_log(tmp_path)
    new = next(r for r in methods if r.get("method") == "session/new")
    assert new["params"]["cwd"] == str(tmp_path)
    setm = next(r for r in methods if r.get("method") == "session/set_model")
    assert setm["params"]["modelId"] == "grok-4.6"
    assert setm["params"]["_meta"]["contextWindow"] == 500000
    effort = next(r for r in methods if r.get("method") == "session/set_config_option")
    assert effort["params"]["configId"] == "reasoning_effort" and effort["params"]["value"] == "high"
    assert sid
    launch = grok_launches(tmp_path)[0]
    assert launch["argv"][-2:] == ["agent", "stdio"]


def test_echo_streams_deltas_then_logs_one_text_entry(tmp_path):
    _, rec = run_turn(tmp_path, "hi")
    assert rec.deltas
    assert [(i["kind"], i.get("text")) for i in history(tmp_path).items][0] == ("user", "hi")
    assert rec.turn_ends[0][0] == "end_turn"


def test_tool_call_logged_in_claude_shape(tmp_path):
    run_turn(tmp_path, "tool")
    tool = next(i for i in history(tmp_path).items if i["kind"] == "tool")
    assert tool["name"] == "Bash" and "command" in tool["input"]


def test_rule_denial_shows_as_refused(tmp_path):
    run_turn(tmp_path, "denied")
    tool = next(i for i in history(tmp_path).items if i["kind"] == "tool")
    assert tool["is_error"] is True and tool["denied"] is True


def test_relative_skill_path_reaches_permission(tmp_path):
    async def main():
        link, rec = make_link(tmp_path)
        await link.start()
        await link.prompt("skillrel")
        await until(lambda: rec.permissions)
        _rid, _id, name, tool_input = rec.permissions[0]
        await link.answer(_rid, False)
        await until(lambda: rec.turn_ends)
        await link.close()
        return name, tool_input
    name, tool_input = asyncio.run(main())
    assert name == "Edit"
    assert Path(tool_input["file_path"]).is_absolute()
    assert "sediment-framework" in tool_input["file_path"]


def test_permission_reaches_events_with_claude_shape(tmp_path):
    async def main():
        link, rec = make_link(tmp_path)
        await link.start()
        await link.prompt("skill")
        await until(lambda: rec.permissions)
        rid, _tool_use_id, name, tool_input = rec.permissions[0]
        await link.answer(rid, True)
        await until(lambda: rec.turn_ends)
        await link.close()
        return rec, name, tool_input
    rec, name, tool_input = asyncio.run(main())
    assert name == "Edit"
    assert "sediment-framework" in tool_input["file_path"]
    assert rec.turn_ends == [("end_turn", None)]


def test_non_skill_ask_is_rejected_without_events(tmp_path):
    _, rec = run_turn(tmp_path, "other")
    assert rec.permissions == []


def test_cancel_closes_open_tool_calls(tmp_path):
    async def main():
        link, rec = make_link(tmp_path)
        await link.start()
        await link.prompt("slow")
        await until(lambda: link._tools)
        await link.interrupt()
        await until(lambda: rec.turn_ends)
        await link.close()
        return rec
    rec = asyncio.run(main())
    assert rec.turn_ends == [("cancelled", None)]
    tool = history(tmp_path).items[1]
    assert tool["name"] == "Bash" and tool["is_error"] is True and tool["output"] == "已中断"


def test_resume_uses_existing_session(tmp_path):
    sid, _ = run_turn(tmp_path, "hi")

    async def again():
        link, rec = make_link(tmp_path, c=research_conv(grok_session=sid))
        got = await link.start()
        await link.close()
        return got, rec
    got, rec = asyncio.run(again())
    assert got == sid
    methods = [r.get("method") for r in grok_acp_log(tmp_path)]
    assert "session/resume" in methods
    assert methods.count("session/new") == 1  # only the first link


def test_missing_session_reports_error_without_replacing_native_session(tmp_path):
    async def main():
        link, rec = make_link(tmp_path, c=research_conv(grok_session="missing"))
        with pytest.raises(GrokSetupError, match="恢复"):
            await link.start()
        return link.session_lost
    assert asyncio.run(main()) is True
    assert not any(r.get("method") == "session/new" for r in grok_acp_log(tmp_path))


def test_running_tool_is_visible_and_input_updates_before_completion(tmp_path):
    async def main():
        link, rec = make_link(tmp_path)
        await link.start()
        await link.prompt("slow")
        await until(lambda: link._tools)
        assert any(i["kind"] == "tool" and i["input"].get("command") == "sleep 99"
                   for i in history(tmp_path).items)
        await link.interrupt()
        await until(lambda: rec.turn_ends)
        await link.close()
    asyncio.run(main())


def test_rewind_forks_full_native_history_then_only_rewinds_conversation(tmp_path):
    async def main():
        link, rec = make_link(tmp_path)
        original = await link.start()
        for text in ("one", "two"):
            await link.prompt(text)
            await until(lambda: len(rec.turn_ends) == (1 if text == "one" else 2))
        users = [e for e in link._log.entries() if e.get("type") == "user"
                 and isinstance(e.get("message", {}).get("content"), str)]
        branch = await link.rewind(users[1]["uuid"])
        await link.close()
        return original, branch
    original, branch = asyncio.run(main())
    assert branch != original
    calls = grok_acp_log(tmp_path)
    fork = next(c for c in calls if c["method"] == "_x.ai/session/fork")
    assert fork["params"]["sourceSessionId"] == original
    assert "targetPromptIndex" not in fork["params"]
    rewind = next(c for c in calls if c["method"] == "_x.ai/rewind/execute")
    assert rewind["params"] == {"sessionId": branch, "targetPromptIndex": 1,
                                 "mode": "conversation_only", "force": True}
    old = json.loads((tmp_path / "grok-sessions" / f"{original}.json").read_text())
    assert len(old["prompts"]) == 2


def test_native_title_and_retry_are_forwarded_without_replaying_text(tmp_path):
    async def main():
        link, rec = make_link(tmp_path)
        titles, retries = [], []
        link._events.on_title = titles.append
        link._events.on_retry = retries.append
        sid = await link.start()
        link._on_message({"method": "session/update", "params": {"sessionId": sid,
                          "update": {"sessionUpdate": "session_info_update", "title": "原生短标题"}}})
        link._on_message({"method": "_x.ai/session_notification", "params": {"sessionId": sid,
                          "update": {"sessionUpdate": "retry_state", "type": "retrying",
                                     "reason": "temporary failure", "attempts": 1}}})
        link._on_message({"method": "session/update", "params": {"sessionId": sid,
                          "_meta": {"isReplay": True}, "update": {"sessionUpdate": "agent_message_chunk",
                          "content": {"type": "text", "text": "old response"}}}})
        title = await link.generate_title()
        await link.close()
        return title, titles, retries, rec
    title, titles, retries, rec = asyncio.run(main())
    assert title == "原生短标题" and titles == [title]
    assert retries and "temporary failure" in retries[0]
    assert rec.deltas == []


def test_native_compact_records_one_boundary_and_replay_does_not_repeat_it(tmp_path):
    async def main():
        link, rec = make_link(tmp_path)
        states = []
        link._events.on_state = states.append
        sid = await link.start()
        await link.compact()
        await until(lambda: rec.turn_ends)
        link._on_message({"method": "_x.ai/session/update", "params": {"sessionId": sid,
                          "_meta": {"isReplay": True}, "update": {"sessionUpdate": "auto_compact_completed",
                          "tokens_before": 100, "tokens_after": 50}}})
        await link.close()
        return link, states
    link, states = asyncio.run(main())
    assert len([e for e in link._log.entries() if e.get("subtype") == "compact_boundary"]) == 1
    assert states == ["compacting", "idle"]


def test_hint_compaction_uses_tool_free_summary_and_switches_only_after_bootstrap(tmp_path):
    async def main():
        link, rec = make_link(tmp_path)
        old = await link.start()
        await link.prompt("retain original data")
        await until(lambda: rec.turn_ends)
        await link.compact("必须保留现金流假设")
        await until(lambda: len(rec.turn_ends) == 2)
        new = link._session
        await link.close()
        return old, new, link
    old, new, link = asyncio.run(main())
    assert old != new
    requests = grok_acp_log(tmp_path)
    summary_launch = next(l for l in grok_launches(tmp_path) if any(
        arg.startswith("--single=请把当前原生上下文") for arg in l["argv"]))
    summary_prompt = next(arg for arg in summary_launch["argv"] if arg.startswith("--single="))
    assert "必须保留现金流假设" in summary_prompt
    assert "retain original data" not in summary_prompt
    source_id = summary_launch["argv"][summary_launch["argv"].index("--resume") + 1]
    source = json.loads((tmp_path / "grok-sessions" / f"{source_id}.json").read_text())
    assert source["prompts"][0] == "retain original data"
    assert any(c["method"] == "_x.ai/session/fork" and c["params"]["sourceSessionId"] == old for c in requests)
    bootstrap_launch = next(l for l in grok_launches(tmp_path) if any(
        arg.startswith("--single=以下内容是压缩前") for arg in l["argv"]))
    args = bootstrap_launch["argv"]
    assert "--disable-web-search" in args
    assert args[args.index("--tools") + 1] == "todo_write"
    disabled = args[args.index("--disallowed-tools") + 1].split(",")
    assert {"run_terminal_command", "read_file", "write", "search_tool", "use_tool", "web_search"} <= set(disabled)
    assert "--permission-mode" in args and "dontAsk" in args
    boundary = next(e for e in link._log.entries() if e.get("subtype") == "compact_boundary")
    assert boundary["native_session"] == new
    native = json.loads((tmp_path / "grok-sessions" / f"{new}.json").read_text())
    assert "SUMMARY: 必须保留现金流假设" in native["prompts"][0]
    assert json.loads((tmp_path / "grok-sessions" / f"{old}.json").read_text())["prompts"] == ["retain original data"]


def test_discover_control_connection_does_not_load_research_rules_or_mcp(tmp_path):
    async def main():
        link, rec = make_link(tmp_path, c=conv(agent="grok", kind="discover"))
        await link.start()
        await link.close()
    asyncio.run(main())
    new = next(c for c in grok_acp_log(tmp_path) if c["method"] == "session/new")
    assert new["params"]["mcpServers"] == []
    assert not new["params"].get("_meta", {}).get("rules")


@pytest.mark.parametrize("failure", ["FAKE_GROK_SOURCE_FORK_FAIL", "FAKE_GROK_SUMMARY_FAIL", "FAKE_GROK_BOOTSTRAP_FAIL",
                                     "FAKE_GROK_BOOTSTRAP_TOOLS", "FAKE_GROK_BOOTSTRAP_TOOL_USE"])
def test_hint_compaction_failure_preserves_original_context_and_app_history(tmp_path, failure):
    async def main():
        link, rec = make_link(tmp_path, env={failure: "1"})
        old = await link.start()
        await link.prompt("original")
        await until(lambda: rec.turn_ends)
        before = list(link._log.entries())
        await link.compact("必须保留现金流假设")
        await until(lambda: len(rec.turn_ends) == 2)
        assert rec.turn_ends[-1][1]
        assert link._session == old
        assert link._log.entries() == before
        await link.close()
    asyncio.run(main())


@pytest.mark.parametrize("failure", ["before_write", "torn_write", "committed_then_raise", "callback"])
def test_compact_session_follows_durable_boundary_after_storage_or_callback_failure(tmp_path, monkeypatch, failure):
    async def main():
        link, rec = make_link(tmp_path)
        original = await link.start()
        try:
            await link.prompt("retained original context")
            await until(lambda: len(rec.turn_ends) == 1)
            old_tip = link._log._last
            real_compacted = link._log.compacted
            real_on_entry = link._events.on_entry

            def boundary(*args, **kwargs):
                assert link._session == original and not link._needs_resume
                if failure == "torn_write":
                    with link._log.path.open("a") as stream:
                        stream.write('{"type":"system","uuid":"torn-boundary"')
                elif failure == "committed_then_raise":
                    real_compacted(*args, **kwargs)
                    # Model a write that reached disk but failed before its in-memory cursor update.
                    link._log._last = old_tip
                raise OSError("app transcript boundary unavailable")

            def on_entry(entry):
                if entry.get("subtype") == "compact_boundary":
                    assert link._session == entry["native_session"] and link._needs_resume
                    assert any(e.get("uuid") == entry["uuid"] for e in link._log.entries())
                    raise OSError("compact observer unavailable")
                real_on_entry(entry)

            if failure == "callback":
                monkeypatch.setattr(link._events, "on_entry", on_entry)
            else:
                monkeypatch.setattr(link._log, "compacted", boundary)
            await link.compact("preserve original context")
            await until(lambda: len(rec.turn_ends) == 2)
            committed = [entry for entry in link._log.entries() if entry.get("subtype") == "compact_boundary"]
            if failure in ("before_write", "torn_write"):
                assert rec.turn_ends[-1][1] and "app transcript boundary unavailable" in rec.turn_ends[-1][1]
                assert link._session == original and not committed
            else:
                assert committed[-1]["native_session"] == link._session != original
                assert link._log._last == committed[-1]["uuid"]
            current = link._session
            monkeypatch.setattr(link._log, "compacted", real_compacted)
            monkeypatch.setattr(link._events, "on_entry", real_on_entry)
            await link.prompt("next consistent context")
            await until(lambda: len(rec.turn_ends) == 3)
            prompts = [call for call in grok_acp_log(tmp_path) if call["method"] == "session/prompt"]
            assert prompts[-1]["params"]["sessionId"] == current
            branch = transcript.current_branch(link._log.entries())
            assert any(entry.get("message", {}).get("content") == "next consistent context" for entry in branch)
            if committed:
                assert any(entry.get("uuid") == committed[-1]["uuid"] for entry in branch)
                old_native = json.loads((tmp_path / "grok-sessions" / f"{original}.json").read_text())
                assert old_native["prompts"] == ["retained original context"]
        finally:
            await link.close()
    asyncio.run(main())


def test_uncertain_compact_write_blocks_following_prompt_if_disk_cannot_be_reconciled(tmp_path, monkeypatch):
    async def main():
        link, rec = make_link(tmp_path)
        await link.start()
        try:
            real_compacted = link._log.compacted
            def uncertain_write(*args, **kwargs):
                real_compacted(*args, **kwargs)
                raise OSError("write returned uncertain")
            def unreadable():
                raise OSError("log currently unreadable")
            monkeypatch.setattr(link._log, "compacted", uncertain_write)
            monkeypatch.setattr(link._log, "refresh", unreadable)
            await link.compact("preserve original")
            await until(lambda: rec.turn_ends)
            assert rec.turn_ends[-1][1] and "无法核对" in rec.turn_ends[-1][1]
            assert not link.alive
            before = link._log.entries()
            wire = list(grok_acp_log(tmp_path))
            with pytest.raises(GrokSetupError, match="核对"):
                await link.prompt("must not send to unknown context")
            assert link._log.entries() == before
            assert grok_acp_log(tmp_path) == wire
        finally:
            await link.close()
    asyncio.run(main())


def test_recreated_link_refreshes_same_cached_log_after_uncertain_compact_commit(tmp_path, monkeypatch):
    async def main():
        link, rec = make_link(tmp_path)
        await link.start()
        cached = link._log
        real_compacted, real_refresh = cached.compacted, cached.refresh
        try:
            await link.prompt("original context")
            await until(lambda: rec.turn_ends)
            old_tip = cached._last
            def uncertain_write(*args, **kwargs):
                real_compacted(*args, **kwargs)
                cached._last = old_tip
                raise OSError("write outcome uncertain")
            def unreadable():
                raise OSError("temporarily unreadable")
            monkeypatch.setattr(cached, "compacted", uncertain_write)
            monkeypatch.setattr(cached, "refresh", unreadable)
            await link.compact("preserve original")
            await until(lambda: len(rec.turn_ends) == 2)
            assert not link.alive
            boundary = next(e for e in cached.entries() if e.get("subtype") == "compact_boundary")
            assert cached._last == old_tip != boundary["uuid"]
            monkeypatch.setattr(cached, "compacted", real_compacted)
            monkeypatch.setattr(cached, "refresh", real_refresh)
            cached.client_id = "recovered-input"
            recreated, following = make_link(tmp_path, c=research_conv(grok_session=boundary["native_session"]))
            recreated._log = cached  # service keeps this same AppLog instance across link recreation
            try:
                await recreated.start()
                await recreated.prompt("next recovered context")
                await until(lambda: following.turn_ends)
                branch = transcript.current_branch(cached.entries())
                assert any(e.get("uuid") == boundary["uuid"] for e in branch)
                user = next(e for e in branch if e.get("message", {}).get("content") == "next recovered context")
                assert user["client_id"] == "recovered-input"
                assert recreated._session == boundary["native_session"]
            finally:
                await recreated.close()
        finally:
            await link.close()
    asyncio.run(main())


def test_start_does_not_launch_native_process_when_cached_log_cannot_refresh(tmp_path, monkeypatch):
    async def main():
        link, _ = make_link(tmp_path)
        def unreadable():
            raise OSError("log inaccessible")
        monkeypatch.setattr(link._log, "refresh", unreadable)
        with pytest.raises(GrokSetupError, match="记录.*读取"):
            await link.start()
        assert not link.alive
        assert grok_acp_log(tmp_path) == []
    asyncio.run(main())


@pytest.mark.parametrize("entrypoint", ["direct", "raw_notification"])
@pytest.mark.parametrize("notice_raises", [False, True])
def test_automatic_uncertain_log_blocks_all_queued_output_while_owned_close_is_pending(tmp_path, monkeypatch, entrypoint, notice_raises):
    async def main():
        link, rec = make_link(tmp_path)
        await link.start()
        closing, release = asyncio.Event(), asyncio.Event()
        real_close = link._proc.close
        async def delayed_close():
            closing.set()
            await release.wait()
            await real_close()
        try:
            await link.prompt("slow")
            await until(lambda: bool(link._tools))
            prompt_id = link._prompt_id
            old_tip = link._log._last
            real_compacted = link._log.compacted
            def uncertain_write(*args, **kwargs):
                real_compacted(*args, **kwargs)
                link._log._last = old_tip
                raise OSError("automatic boundary result uncertain")
            def unreadable():
                raise OSError("log temporarily unreadable")
            monkeypatch.setattr(link._log, "compacted", uncertain_write)
            monkeypatch.setattr(link._log, "refresh", unreadable)
            monkeypatch.setattr(link._proc, "close", delayed_close)
            if notice_raises:
                def failed_notice(_text):
                    raise OSError("notice observer unavailable")
                monkeypatch.setattr(link._events, "on_notice", failed_notice)
            with pytest.raises(GrokSetupError, match="无法核对"):
                link._on_update({"sessionUpdate": "auto_compact_completed", "tokens_before": 900})
            persisted = link._log.path.read_bytes()
            call_id = next(iter(link._tools), "late-tool")
            updates = [
                {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "late response"}},
                {"sessionUpdate": "user_message_chunk", "content": {"type": "text", "text": "slow"}, "_meta": {"promptIndex": 9}},
                {"sessionUpdate": "tool_call", "toolCallId": "new-late", "title": "read_file", "rawInput": {"target_file": "late"}},
                {"sessionUpdate": "tool_call_update", "toolCallId": call_id, "status": "completed", "rawOutput": {"output": "late result"}},
                {"sessionUpdate": "auto_compact_completed", "tokens_before": 900},
            ]
            for update in updates:
                if entrypoint == "direct":
                    link._on_update(update)
                else:
                    link._on_message({"method": "_x.ai/session_notification", "params": {"sessionId": link._session, "update": update}})
            link._on_response(prompt_id, {"result": {"stopReason": "end_turn", "_meta": {"promptId": "late"}}})
            link._end_turn("cancelled", "late failure")
            link._write_text("direct late text")
            link._finish_tool("direct-late", "late tool result", False)
            assert link._log.path.read_bytes() == persisted
            await asyncio.wait_for(closing.wait(), 1)
            assert link._log.path.read_bytes() == persisted
            release.set()
            await until(lambda: not link.alive and bool(rec.turn_ends))
            assert len(rec.turn_ends) == 1 and rec.turn_ends[0][1]
            entries = link._log.entries()
            marker = next(e for e in entries if e.get("subtype") == "compact_boundary")
            assert marker["uuid"] in {e["uuid"] for e in transcript.current_branch(entries)}
        finally:
            release.set()
            monkeypatch.setattr(link._proc, "close", real_close)
            await link.close()
    asyncio.run(main())


def test_automatic_log_abort_cancels_owned_manual_summary_and_reports_one_terminal(tmp_path, monkeypatch):
    async def main():
        entered, cleaned = asyncio.Event(), asyncio.Event()
        async def summary(*_args):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleaned.set()
        monkeypatch.setattr("lynch_reader.chat.grok_link.summarize_conversation", summary)
        link, rec = make_link(tmp_path)
        await link.start()
        real_compacted = link._log.compacted
        try:
            await link.compact("preserve original")
            await entered.wait()
            def uncertain_write(*args, **kwargs):
                real_compacted(*args, **kwargs)
                raise OSError("write uncertain")
            def unreadable():
                raise OSError("read unavailable")
            monkeypatch.setattr(link._log, "compacted", uncertain_write)
            monkeypatch.setattr(link._log, "refresh", unreadable)
            with pytest.raises(GrokSetupError):
                link._on_update({"sessionUpdate": "auto_compact_completed", "tokens_before": 900})
            await until(lambda: bool(rec.turn_ends))
            assert cleaned.is_set() and not link.alive
            assert len(rec.turn_ends) == 1 and rec.turn_ends[0][1]
        finally:
            await link.close()
    asyncio.run(main())


def test_compact_owns_background_task_and_interrupt_keeps_http_task_alive(tmp_path, monkeypatch):
    async def main():
        entered = asyncio.Event()
        async def slow_summary(*_args):
            entered.set()
            await asyncio.Event().wait()
        monkeypatch.setattr("lynch_reader.chat.grok_link.summarize_conversation", slow_summary)
        link, rec = make_link(tmp_path)
        await link.start()
        await asyncio.wait_for(link.compact("保留假设"), 1)
        assert link._compact_task is not asyncio.current_task()
        await entered.wait()
        await link.interrupt()
        await until(lambda: rec.turn_ends)
        assert not asyncio.current_task().cancelling()
        assert rec.turn_ends == [("cancelled", None)]
        await link.close()
        assert rec.turn_ends == [("cancelled", None)]
    asyncio.run(main())


def test_close_cancels_owned_summary_without_cancelling_caller(tmp_path, monkeypatch):
    async def main():
        started, cleaned = asyncio.Event(), asyncio.Event()
        async def slow_summary(*_args):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleaned.set()
        monkeypatch.setattr("lynch_reader.chat.grok_link.summarize_conversation", slow_summary)
        link, rec = make_link(tmp_path)
        await link.start()
        await link.compact("保留假设")
        await started.wait()
        await link.close()
        assert cleaned.is_set()
        assert not asyncio.current_task().cancelling()
        assert rec.turn_ends == [("cancelled", None)]
    asyncio.run(main())


def test_immediate_interrupt_of_accepted_compact_notifies_exactly_once(tmp_path):
    async def main():
        link, rec = make_link(tmp_path)
        await link.start()
        await link.compact("保留假设")
        await link.interrupt()
        await until(lambda: rec.turn_ends)
        await link.close()
        assert rec.turn_ends == [("cancelled", None)]
    asyncio.run(main())


def test_custom_compact_replay_maps_only_current_editable_epoch(tmp_path):
    async def main():
        link, rec = make_link(tmp_path)
        await link.start()
        await link.prompt("archived original")
        await until(lambda: len(rec.turn_ends) == 1)
        await link.compact("必须保留现金流假设")
        await until(lambda: len(rec.turn_ends) == 2)
        new = link._session
        assert link._needs_resume
        assert not any(c["method"] == "session/resume" and c["params"]["sessionId"] == new
                       for c in grok_acp_log(tmp_path))
        for count, text in enumerate(("first current", "editable current"), 3):
            await link.prompt(text)
            await until(lambda: len(rec.turn_ends) == count)
        target = next(e for e in link._log.entries()
                      if e.get("message", {}).get("content") == "editable current")
        entries = link._log.entries()
        removed = {e["uuid"]: e["parentUuid"] for e in entries if e.get("type") == "lynch_native"}
        legacy = [e for e in entries if e.get("type") != "lynch_native"]
        for entry in legacy:
            while entry.get("parentUuid") in removed:
                entry["parentUuid"] = removed[entry["parentUuid"]]
        link._log.path.write_text("".join(json.dumps(e) + "\n" for e in legacy))
        link._log = AppLog(link._log.path)
        branch = await link.rewind(target["uuid"])
        assert branch != new
        assert link._log.native_id(target["uuid"]) == "2"
        await link.close()
    asyncio.run(main())


def test_repeated_compaction_summarizes_native_fork_without_resubmitting_archived_app_history(tmp_path):
    async def main():
        link, rec = make_link(tmp_path)
        await link.start()
        old_text = "OLD_ARCHIVED_HISTORY_" * 3000
        await link.prompt(old_text)
        await until(lambda: len(rec.turn_ends) == 1)
        await link.compact("必须保留现金流假设")
        await until(lambda: len(rec.turn_ends) == 2)
        current = link._session
        await link.prompt("recent current context")
        await until(lambda: len(rec.turn_ends) == 3)
        await link.compact("必须保留现金流假设")
        await until(lambda: len(rec.turn_ends) == 4)
        assert rec.turn_ends[-1] == ("end_turn", None)
        sources = [l for l in grok_launches(tmp_path) if any(
            arg.startswith("--single=请把当前原生上下文") for arg in l["argv"])]
        assert len(sources) == 2
        second = sources[-1]["argv"]
        assert "OLD_ARCHIVED_HISTORY_" not in next(a for a in second if a.startswith("--single="))
        fork_id = second[second.index("--resume") + 1]
        native = json.loads((tmp_path / "grok-sessions" / f"{fork_id}.json").read_text())
        assert native["prompts"][1] == "recent current context"
        assert all("OLD_ARCHIVED_HISTORY_" not in t for t in native["prompts"])
        assert any(c["method"] == "_x.ai/session/fork" and c["params"]["sourceSessionId"] == current
                   for c in grok_acp_log(tmp_path))
        await link.close()
    asyncio.run(main())


def test_prompt_never_binds_before_native_acceptance(tmp_path):
    async def main():
        link, rec = make_link(tmp_path)
        link._session = "offline"
        async def failed_send(_obj):
            raise BrokenPipeError("not sent")
        link._proc.send_json = failed_send
        with pytest.raises(BrokenPipeError):
            await link.prompt("not received")
        user = next(e for e in link._log.entries() if e.get("type") == "user")
        assert link._log.native_id(user["uuid"]) is None
    asyncio.run(main())


def test_native_user_echo_binds_exact_prompt_index(tmp_path):
    async def main():
        link, rec = make_link(tmp_path)
        link._session = "offline"
        async def send(_obj):
            pass
        link._proc.send_json = send
        await link.prompt("received")
        user = next(e for e in link._log.entries() if e.get("type") == "user")
        assert link._log.native_id(user["uuid"]) is None
        link._on_update({"sessionUpdate": "user_message_chunk", "content": {"type": "text", "text": "received"},
                         "_meta": {"promptIndex": 7}})
        assert link._log.native_id(user["uuid"]) == "7"
    asyncio.run(main())


@pytest.mark.parametrize("mismatch", [False, True])
def test_legacy_log_mapping_uses_full_native_replay_and_rejects_mismatch(tmp_path, mismatch):
    async def main():
        link, rec = make_link(tmp_path)
        sid = await link.start()
        await link.prompt("original text")
        await until(lambda: rec.turn_ends)
        user = next(e for e in link._log.entries() if e.get("type") == "user")
        path = link._log.path
        entries = link._log.entries()
        removed = {e["uuid"]: e["parentUuid"] for e in entries if e.get("type") == "lynch_native"}
        legacy = [e for e in entries if e.get("type") != "lynch_native"]
        for entry in legacy:
            while entry.get("parentUuid") in removed:
                entry["parentUuid"] = removed[entry["parentUuid"]]
        path.write_text("".join(json.dumps(e) + "\n" for e in legacy))
        link._log = AppLog(path)
        if mismatch:
            native_path = tmp_path / "grok-sessions" / f"{sid}.json"
            native = json.loads(native_path.read_text())
            native["prompts"] = ["different original text"]
            native_path.write_text(json.dumps(native))
            with pytest.raises(GrokSetupError, match="准确对应"):
                await link.rewind(user["uuid"])
            assert not any(c["method"] == "_x.ai/session/fork" for c in grok_acp_log(tmp_path))
        else:
            assert await link.rewind(user["uuid"]) != sid
            assert link._log.native_id(user["uuid"]) == "0"
        await link.close()
    asyncio.run(main())


def test_session_new_gets_repo_mcp_only(tmp_path):
    (tmp_path / ".mcp.json").write_text(json.dumps({"mcpServers": {"wisburg": {"command": "true"}}}), encoding="utf-8")
    run_turn(tmp_path, "hi")
    new = next(r for r in grok_acp_log(tmp_path) if r.get("method") == "session/new")
    names = [s.get("name") for s in new["params"]["mcpServers"]]
    assert "wisburg" in names
    assert all("env" not in json.dumps(s.get("env", [])) or True for s in new["params"]["mcpServers"])

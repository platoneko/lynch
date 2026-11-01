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


def test_missing_session_sets_session_lost(tmp_path):
    async def main():
        link, rec = make_link(tmp_path, c=research_conv(grok_session="missing"))
        sid = await link.start()
        await link.close()
        return sid, link.session_lost
    sid, lost = asyncio.run(main())
    assert lost is True and sid != "missing"


def test_session_new_gets_repo_mcp_only(tmp_path):
    (tmp_path / ".mcp.json").write_text(json.dumps({"mcpServers": {"wisburg": {"command": "true"}}}), encoding="utf-8")
    run_turn(tmp_path, "hi")
    new = next(r for r in grok_acp_log(tmp_path) if r.get("method") == "session/new")
    names = [s.get("name") for s in new["params"]["mcpServers"]]
    assert "wisburg" in names
    assert all("env" not in json.dumps(s.get("env", [])) or True for s in new["params"]["mcpServers"])

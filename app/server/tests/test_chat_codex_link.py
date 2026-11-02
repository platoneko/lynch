from __future__ import annotations

import asyncio
import json
import os
import sys
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.chat_helpers import LinkRecorder, conv, until


def setup(tmp_path, **extra):
    from lynch_reader.chat.codex import CodexConfig
    cfg = CodexConfig((sys.executable, str(Path(__file__).with_name("fake_codex.py"))), tmp_path, tmp_path / "logs")
    env = {**os.environ, "FAKE_CODEX_SESSIONS": str(tmp_path / "sessions"), "FAKE_CODEX_LOG": str(tmp_path / "rpc.jsonl"), **extra}
    return cfg, env


def open_link(cfg, env, rec, thread=None):
    from lynch_reader.chat.app_log import AppLog
    from lynch_reader.chat.codex_link import CodexLink
    c = SimpleNamespace(**{**asdict(conv(agent="codex", model="gpt-test", effort="low")), "codex_thread": thread})
    return CodexLink(cfg, c, env, AppLog(cfg.logs / "c1.jsonl"), rec.events())


def test_models_paginate_without_creating_threads(tmp_path):
    from lynch_reader.chat.codex_link import discover_models
    cfg, env = setup(tmp_path)
    models = asyncio.run(discover_models(cfg, env))
    assert [m.id for m in models] == ["gpt-test", "gpt-other"]
    calls = [json.loads(line) for line in (tmp_path / "rpc.jsonl").read_text().splitlines()]
    assert [c.get("method") for c in calls] == ["initialize", "initialized", "account/read", "model/list", "model/list"]


def test_stream_and_usage_are_persisted_once_and_resume_without_replay(tmp_path):
    async def main():
        cfg, env = setup(tmp_path)
        rec = LinkRecorder()
        link = open_link(cfg, env, rec)
        thread = await link.start()
        try:
            await link.prompt("one")
            await until(lambda: len(rec.turn_ends) == 1)
        finally:
            await link.close()
        rec2 = LinkRecorder()
        again = open_link(cfg, env, rec2, thread)
        try:
            assert await again.start() == thread
            assert rec2.entries == []
            await again.prompt("two")
            await until(lambda: len(rec2.turn_ends) == 1)
        finally:
            await again.close()
        from lynch_reader.chat import transcript
        entries = transcript.read_entries(cfg.logs / "c1.jsonl")
        history = transcript.history(entries, entries)
        return rec, rec2, history
    rec, rec2, history = asyncio.run(main())
    assert rec.deltas == ["echo: ", "one"]
    assert rec2.deltas == ["echo: ", "two"]
    assert [(i["kind"], i.get("text")) for i in history.items] == [
        ("user", "one"), ("text", "echo: one"), ("user", "two"), ("text", "echo: two")]
    assert rec.usage == [(1234, 1000000, None)]
    assert history.context_tokens == 1234


def test_every_turn_allows_network_without_bypassing_file_approval(tmp_path):
    async def main():
        cfg, env = setup(tmp_path)
        thread = None
        for prompts in (("one", "two"), ("resumed",)):
            rec = LinkRecorder()
            link = open_link(cfg, env, rec, thread)
            try:
                thread = await link.start()
                for count, prompt in enumerate(prompts, 1):
                    await link.prompt(prompt)
                    await until(lambda: len(rec.turn_ends) == count)
            finally:
                await link.close()
        return [json.loads(line) for line in (tmp_path / "rpc.jsonl").read_text().splitlines()]

    calls = asyncio.run(main())
    turns = [call for call in calls if call.get("method") == "turn/start"]
    assert len(turns) == 3
    for call in turns:
        assert call["params"].get("sandboxPolicy") == {"type": "readOnly", "networkAccess": True}


def test_interrupt_finishes_turn_and_records_stop(tmp_path):
    async def main():
        cfg, env = setup(tmp_path)
        rec = LinkRecorder()
        link = open_link(cfg, env, rec)
        try:
            await link.start()
            await link.prompt("wait")
            await link.interrupt()
            await until(lambda: bool(rec.turn_ends))
            return rec
        finally:
            await link.close()
    rec = asyncio.run(main())
    assert rec.turn_ends == [("cancelled", None)]
    assert rec.entries[-1]["message"]["content"].startswith("[Request interrupted")


def test_skill_patch_waits_for_phone_and_rejection_leaves_file_unchanged(tmp_path):
    async def main():
        cfg, env = setup(tmp_path)
        rec = LinkRecorder()
        link = open_link(cfg, env, rec)
        try:
            await link.start()
            await link.prompt("skill")
            await until(lambda: bool(rec.permissions))
            assert not rec.turn_ends
            request, tool_id, name, inputs = rec.permissions[0]
            assert name == "Write" and inputs["file_path"].endswith("sediment-framework/probe.md")
            await link.answer(request, False)
            await until(lambda: bool(rec.turn_ends))
            return rec
        finally:
            await link.close()
    rec = asyncio.run(main())
    assert not (tmp_path / "frameworks/.claude/skills/sediment-framework/probe.md").exists()
    assert any(b.get("is_error") for e in rec.entries for b in e["message"]["content"] if isinstance(b, dict))


@pytest.mark.parametrize("envvar,match", [("FAKE_CODEX_FAIL_SETUP", "no provider"), ("FAKE_CODEX_ACCOUNT", "登录")])
def test_setup_failure_is_actionable_and_process_is_closed(tmp_path, envvar, match):
    from lynch_reader.chat.codex_link import CodexSetupError
    async def main():
        cfg, env = setup(tmp_path, **{envvar: "missing" if envvar == "FAKE_CODEX_ACCOUNT" else "1"})
        link = open_link(cfg, env, LinkRecorder())
        with pytest.raises(CodexSetupError, match=match):
            await link.start()
        assert not link.alive
    asyncio.run(main())


def test_resume_missing_thread_fails_without_silently_losing_context(tmp_path):
    from lynch_reader.chat.codex_link import CodexSetupError
    async def main():
        cfg, env = setup(tmp_path)
        link = open_link(cfg, env, LinkRecorder(), "thr_missing")
        with pytest.raises(CodexSetupError, match="thread not found"):
            await link.start()
        assert not link.alive
    asyncio.run(main())


def test_external_provider_does_not_require_unrelated_openai_login(tmp_path):
    from lynch_reader.chat.codex_link import discover_models
    cfg, env = setup(tmp_path, FAKE_CODEX_ACCOUNT="external")
    assert len(asyncio.run(discover_models(cfg, env))) == 2


def test_setup_timeout_kills_process_and_resolves_pending_calls(tmp_path):
    from lynch_reader.chat.app_log import AppLog
    from lynch_reader.chat.codex_link import CodexLink, CodexSetupError
    async def main():
        cfg, env = setup(tmp_path, FAKE_CODEX_HANG_INIT="1")
        c = SimpleNamespace(**{**asdict(conv(agent="codex")), "codex_thread": None})
        link = CodexLink(cfg, c, env, AppLog(cfg.logs / "c1.jsonl"), LinkRecorder().events(), setup_timeout_s=0.05)
        with pytest.raises(CodexSetupError, match="initialize"):
            await link.start()
        assert not link.alive
    asyncio.run(main())


@pytest.mark.parametrize("command,accepted", [("git -C frameworks status", True),
                                           ("git -C frameworks push --dry-run", False),
                                           ("longbridge assets", False)])
def test_native_command_approval_enforces_guardrails(tmp_path, command, accepted):
    async def main():
        cfg, env = setup(tmp_path)
        rec = LinkRecorder()
        link = open_link(cfg, env, rec)
        try:
            await link.start()
            await link.prompt("command:" + command)
            await until(lambda: bool(rec.turn_ends))
            assert rec.permissions == []  # ordinary commands are never pushed to the phone
        finally:
            await link.close()
    asyncio.run(main())
    replies = [json.loads(line) for line in (tmp_path / "rpc.jsonl").read_text().splitlines()]
    assert next(r for r in replies if r.get("id") == "approval1")["result"]["decision"] == (
        "accept" if accepted else "decline")


@pytest.mark.parametrize("path,exists", [("frameworks/_local/tmp/math.py", True), ("app/server/outside.py", False)])
def test_native_patch_approval_enforces_file_scope(tmp_path, path, exists):
    async def main():
        cfg, env = setup(tmp_path)
        rec = LinkRecorder()
        link = open_link(cfg, env, rec)
        try:
            await link.start()
            await link.prompt("patch:" + path)
            await until(lambda: bool(rec.turn_ends))
            assert not rec.permissions
        finally:
            await link.close()
    asyncio.run(main())
    assert (tmp_path / path).exists() is exists


def test_move_into_skill_also_requires_phone_approval(tmp_path):
    async def main():
        cfg, env = setup(tmp_path)
        rec = LinkRecorder()
        link = open_link(cfg, env, rec)
        try:
            await link.start()
            await link.prompt("move-skill")
            await until(lambda: bool(rec.permissions), timeout=1)
            request, _, _, inputs = rec.permissions[0]
            assert inputs["file_path"].endswith("sediment-framework/probe.md")
            await link.answer(request, False)
            await until(lambda: bool(rec.turn_ends))
        finally:
            await link.close()
    asyncio.run(main())
    assert not (tmp_path / "frameworks/.claude/skills/sediment-framework/probe.md").exists()


def test_interrupt_before_start_ack_stops_process_without_losing_stop_intent(tmp_path):
    async def main():
        cfg, env = setup(tmp_path)
        rec = LinkRecorder()
        link = open_link(cfg, env, rec)
        await link.start()
        sending = asyncio.create_task(link.prompt("slow-start"))
        try:
            await until(lambda: bool(rec.entries))
            await link.interrupt()
            assert not link.alive
            await sending  # intentional stop should not turn the pending send into a spurious error
            assert rec.turn_ends == [("cancelled", None)]
        finally:
            await link.close()
            await asyncio.gather(sending, return_exceptions=True)
    asyncio.run(main())


def test_turn_start_timeout_closes_uncertain_native_turn(tmp_path):
    from lynch_reader.chat.app_log import AppLog
    from lynch_reader.chat.codex_link import CodexLink, CodexSetupError
    async def main():
        cfg, env = setup(tmp_path)
        rec = LinkRecorder()
        c = SimpleNamespace(**{**asdict(conv(agent="codex", model="gpt-test")), "codex_thread": None})
        link = CodexLink(cfg, c, env, AppLog(cfg.logs / "c1.jsonl"), rec.events(), setup_timeout_s=0.1)
        try:
            await link.start()
            with pytest.raises(CodexSetupError, match="turn/start"):
                await link.prompt("hang-turn")
            assert not link.alive
            assert "turn/start" in str(rec.entries[-1])
        finally:
            await link.close()
    asyncio.run(main())


def test_late_nested_turn_lifecycle_notifications_do_not_finish_new_turn(tmp_path):
    async def main():
        cfg, env = setup(tmp_path)
        rec = LinkRecorder()
        link = open_link(cfg, env, rec)
        try:
            await link.start()
            await link.prompt("first")
            await until(lambda: len(rec.turn_ends) == 1)
            rec.deltas.clear()
            await link.prompt("stale")
            await until(lambda: len(rec.turn_ends) == 2)
            assert "".join(rec.deltas) == "echo: stale"
            assert rec.entries[-1]["message"]["content"][0]["text"] == "echo: stale"
        finally:
            await link.close()
    asyncio.run(main())


def test_failed_generation_and_crash_remain_visible(tmp_path):
    async def main():
        cfg, env = setup(tmp_path)
        rec = LinkRecorder()
        link = open_link(cfg, env, rec)
        try:
            await link.start()
            await link.prompt("error")
            await until(lambda: bool(rec.turn_ends))
            assert "generation failed" in str(rec.entries[-1])
        finally:
            await link.close()
        crash = LinkRecorder()
        dead = open_link(cfg, {**env, "FAKE_CODEX_CRASH": "1"}, crash)
        await dead.start()
        await dead.prompt("hello")
        await until(lambda: bool(crash.exits))
        assert not dead.alive
        assert crash.exits[-1] == (False, "fake codex boom")
    asyncio.run(main())

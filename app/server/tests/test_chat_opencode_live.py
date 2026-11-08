"""Opt-in OpenCode 2 integration checks; all editable fixtures live under pytest's private directory.

Run with LYNCH_LIVE_OPENCODE=1. Uses the user's configured provider and plugins, creates real native sessions,
and checks research guardrails, phone approval rejection, restart recovery, native edit and one-shot compaction.
"""
from __future__ import annotations

import asyncio
import os
import shutil
from dataclasses import replace
from pathlib import Path

import pytest

from lynch_reader.chat import transcript
from lynch_reader.chat.app_log import AppLog
from lynch_reader.chat.approvals import approvable_path
from lynch_reader.chat.opencode import (DEFAULT_OPENCODE_MODEL, OPENCODE_EFFORT, OpencodeConfig, opencode_env,
                                        write_rules)
from lynch_reader.chat.opencode_link import OpencodeLink, OpencodeSetupError
from tests.chat_helpers import LinkRecorder, conv

pytestmark = pytest.mark.skipif(os.environ.get("LYNCH_LIVE_OPENCODE") != "1", reason="real opencode, opt-in")

TURN_S = 300.0
MARKER = "MANGO-3"


def open_link(cfg: OpencodeConfig, c, rec: LinkRecorder) -> OpencodeLink:
    return OpencodeLink(cfg, c, opencode_env(os.environ, write_rules(cfg, c)), AppLog(cfg.logs / f"{c.id}.jsonl"),
                        rec.events())


async def turn(link: OpencodeLink, rec: LinkRecorder, text: str) -> None:
    """One turn; every permission request on the way is rejected (the phone's 拒绝)."""
    ends, asks, exits = len(rec.turn_ends), len(rec.permissions), len(rec.exits)
    await link.prompt(text)

    async def settle() -> None:
        nonlocal asks
        while len(rec.turn_ends) == ends:
            _check_native_exit(rec, exits)
            if len(rec.permissions) > asks:
                await link.answer(rec.permissions[asks][0], False)
                asks += 1
            await asyncio.sleep(0.05)
    await asyncio.wait_for(settle(), TURN_S)


def _check_native_exit(rec: LinkRecorder, previous: int = 0) -> None:
    if len(rec.exits) > previous:
        status = "正常退出但本轮未完成" if rec.exits[-1][0] else "意外退出"
        raise OpencodeSetupError(f"OpenCode 原生进程{status}")


def tools(cfg: OpencodeConfig, conv_id: str) -> list[dict]:
    entries = transcript.read_entries(cfg.logs / f"{conv_id}.jsonl")
    return [i for i in transcript.history(transcript.current_branch(entries), entries).items if i["kind"] == "tool"]


def private_config(tmp_path: Path) -> OpencodeConfig:
    repo = tmp_path / "repo"
    skill = repo / "frameworks/.claude/skills/sediment-framework"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\nname: sediment-framework\ndescription: Private integration-test research skill.\n---\n"
        "# Private live research fixture\nDo not modify any file without explicit user approval.\n", encoding="utf-8")
    discovery = repo / ".opencode/skills"
    discovery.mkdir(parents=True)
    (discovery / "sediment-framework").symlink_to(skill, target_is_directory=True)
    return OpencodeConfig((os.environ.get("LYNCH_OPENCODE_BIN") or shutil.which("opencode2") or "opencode2",),
                          repo, tmp_path / "logs")


def test_guardrails_skill_approval_and_resume(tmp_path: Path):
    cfg = private_config(tmp_path)
    probe = cfg.repo_root / "frameworks/.claude/skills/sediment-framework/_live_probe.md"
    ast_probe = cfg.repo_root / "frameworks/_local/tmp/_live_ast_probe.py"
    ast_probe.parent.mkdir(parents=True)
    ast_probe.write_text("x = 1\n", encoding="utf-8")
    c = conv(kind="research", ticker=None, agent="opencode", model=DEFAULT_OPENCODE_MODEL, effort=OPENCODE_EFFORT)

    async def main():
        rec = LinkRecorder()
        link = open_link(cfg, c, rec)
        session = await link.start()
        try:
            await turn(link, rec, f"Remember the word {MARKER}. Yourself, without any subagent: run the shell command "
                                  "`echo live-ok`, then load the skill sediment-framework with your skill tool and "
                                  "reply with its first heading.")
            await turn(link, rec, "Yourself, call the question tool to ask a two-option test question: red or blue. "
                                  "Do not use subagents or another tool. If permission is denied, report it briefly.")
            await turn(link, rec, "Yourself, without any subagent, run exactly this shell command: "
                                  "git -C frameworks push --dry-run")
            await turn(link, rec, "Yourself, with your write tool (no bash, no subagent), create the file "
                                  f"{probe.relative_to(cfg.repo_root)} containing the single line x.")
            await turn(link, rec, "Yourself, with your ast_grep_replace tool and dryRun false (no bash, no edit tool, "
                                  f"no subagent), rewrite `x = 1` to `x = 2` in {ast_probe.relative_to(cfg.repo_root)}. "
                                  "If you have no such tool, say so.")
        finally:
            await link.close()
        # A new process, as after an idle reap or a server restart.
        rec2 = LinkRecorder()
        again = open_link(cfg, replace(c, opencode_session=session), rec2)
        resumed = await again.start()
        replayed = list(rec2.entries)
        try:
            await turn(again, rec2, "What word did I ask you to remember? Reply with just the word.")
        finally:
            await again.close()
        return rec, rec2, session, resumed, replayed

    rec, rec2, session, resumed, replayed = asyncio.run(main())
    calls = tools(cfg, c.id)
    bash = [t for t in calls if t["name"] == "Bash"]
    assert any("live-ok" in (t["output"] or "") and not t["is_error"] for t in bash)
    assert any(t["name"] == "Skill" and t["input"].get("skill") == "sediment-framework" and not t["is_error"]
               for t in calls)
    push = [t for t in bash if "push" in t["input"].get("command", "")]
    assert push and all(t["denied"] for t in push)
    # The skill write came to the app as Claude's Write on an approvable path; rejected, the file never appears.
    assert rec.permissions, "the orchestrator's skill write did not ask"
    _, _, name, tool_input = rec.permissions[0]
    assert name in ("Write", "Edit") and approvable_path(name, tool_input, cfg.repo_root) is not None
    assert not probe.exists()
    assert resumed == session and replayed == []
    assert MARKER in "".join(rec2.deltas)
    # The plugin's AST rewrite writes without asking: denied, so the file stays as it was.
    ast_text = ast_probe.read_text(encoding="utf-8")
    ast_probe.unlink()
    assert ast_text == "x = 1\n"
    assert not any(t["name"] == "ast_grep_replace" and not t["is_error"] for t in calls)



def test_native_fork_and_compaction_preserve_context_without_reverting_files(tmp_path: Path):
    cfg = private_config(tmp_path)
    c = conv(kind="research", ticker=None, agent="opencode", model=DEFAULT_OPENCODE_MODEL, effort=OPENCODE_EFFORT)
    sentinel = cfg.repo_root / "frameworks/keep.txt"
    sentinel.write_text("must survive native fork", encoding="utf-8")

    async def main():
        rec = LinkRecorder()
        link = open_link(cfg, c, rec)
        original = await link.start()
        try:
            await turn(link, rec, "Remember the exact research token MANGO-3 and the number 7391. Reply briefly.")
            await turn(link, rec, "Remember the disposable phrase BLUE-END. Reply briefly.")
            user = [e for e in rec.entries if e["type"] == "user" and isinstance(e["message"]["content"], str)][-1]
            edited = await link.rewind(user["uuid"])
            assert edited != original
            assert sentinel.read_text() == "must survive native fork"
            await turn(link, rec, "Replace the disposable phrase with GREEN-END. Reply briefly.")
            previous = len(rec.turn_ends)
            await link.compact("务必保留研究代号 MANGO-3 和数字 7391；保留 GREEN-END，不保留旧分支 BLUE-END。额外记住这条说明新加入的验收口令 HINT-ALOE-42。")
            await asyncio.wait_for(_wait_compaction(rec, previous), TURN_S)
            assert any(e.get("subtype") == "compact_boundary" for e in rec.entries)
            assert not list(cfg.logs.glob("*.compact-hint.json"))
        finally:
            await link.close()
        rec2 = LinkRecorder()
        again = open_link(cfg, replace(c, opencode_session=edited), rec2)
        assert await again.start() == edited
        try:
            await turn(again, rec2, "What exact research token, number, disposable phrase, and extra acceptance password added only in the compaction instruction should we retain? Reply briefly.")
        finally:
            await again.close()
        answer = "".join(rec2.deltas)
        assert "MANGO-3" in answer and "7391" in answer and "GREEN-END" in answer
        assert "HINT-ALOE-42" in answer
        assert sentinel.read_text() == "must survive native fork"

    asyncio.run(main())


async def _wait_compaction(rec: LinkRecorder, previous: int) -> None:
    while len(rec.turn_ends) == previous:
        _check_native_exit(rec)
        await asyncio.sleep(.05)

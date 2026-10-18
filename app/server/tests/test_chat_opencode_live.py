"""Real opencode, opt-in: LYNCH_LIVE_OPENCODE=1 uv run pytest tests/test_chat_opencode_live.py -q -s

Guards what the 2026-10-18 spike found by experiment and opencode does not document (chat/opencode.py): the
overlay's bash denies hold, the orchestrator's skill edit reaches the app as a permission request, a rejected one
leaves the file alone, sediment-framework stays loadable, and session/resume continues a session without replay.
Run it after every opencode or oh-my-opencode-slim upgrade. Needs the user's opencode config (0g provider); runs at
the repo root on the real frameworks/ and spends a few cents at most. Leaves its sessions in opencode's storage.
"""
from __future__ import annotations

import asyncio
import os
import shutil
from dataclasses import replace
from pathlib import Path

import pytest

from lynch_reader.catalog import REPO_ROOT
from lynch_reader.chat import transcript
from lynch_reader.chat.app_log import AppLog
from lynch_reader.chat.approvals import approvable_path
from lynch_reader.chat.opencode import (DEFAULT_OPENCODE_MODEL, OPENCODE_EFFORT, OpencodeConfig, opencode_env,
                                        write_rules)
from lynch_reader.chat.opencode_link import OpencodeLink
from tests.chat_helpers import LinkRecorder, conv

pytestmark = pytest.mark.skipif(os.environ.get("LYNCH_LIVE_OPENCODE") != "1", reason="real opencode, opt-in")

TURN_S = 300.0
PROBE = REPO_ROOT / "frameworks/.claude/skills/sediment-framework/_live_probe.md"
AST_PROBE = REPO_ROOT / "frameworks/_local/tmp/_live_ast_probe.py"  # scratch, never committed
MARKER = "MANGO-3"


def open_link(cfg: OpencodeConfig, c, rec: LinkRecorder) -> OpencodeLink:
    return OpencodeLink(cfg, c, opencode_env(os.environ, write_rules(cfg, c)), AppLog(cfg.logs / f"{c.id}.jsonl"),
                        rec.events())


async def turn(link: OpencodeLink, rec: LinkRecorder, text: str) -> None:
    """One turn; every permission request on the way is rejected (the phone's 拒绝)."""
    ends, asks = len(rec.turn_ends), len(rec.permissions)
    await link.prompt(text)

    async def settle() -> None:
        nonlocal asks
        while len(rec.turn_ends) == ends:
            if len(rec.permissions) > asks:
                await link.answer(rec.permissions[asks][0], False)
                asks += 1
            await asyncio.sleep(0.05)
    await asyncio.wait_for(settle(), TURN_S)


def tools(cfg: OpencodeConfig, conv_id: str) -> list[dict]:
    entries = transcript.read_entries(cfg.logs / f"{conv_id}.jsonl")
    return [i for i in transcript.history(transcript.current_branch(entries), entries).items if i["kind"] == "tool"]


def test_guardrails_skill_approval_and_resume(tmp_path: Path):
    assert not PROBE.exists()
    AST_PROBE.parent.mkdir(parents=True, exist_ok=True)
    AST_PROBE.write_text("x = 1\n", encoding="utf-8")
    cfg = OpencodeConfig((shutil.which("opencode") or "opencode",), REPO_ROOT, tmp_path / "logs")
    c = conv(kind="research", ticker=None, agent="opencode", model=DEFAULT_OPENCODE_MODEL, effort=OPENCODE_EFFORT)

    async def main():
        rec = LinkRecorder()
        link = open_link(cfg, c, rec)
        session = await link.start()
        try:
            await turn(link, rec, f"Remember the word {MARKER}. Yourself, without any subagent: run the shell command "
                                  "`echo live-ok`, then load the skill sediment-framework with your skill tool and "
                                  "reply with its first heading.")
            await turn(link, rec, "Yourself, without any subagent, run exactly this shell command: "
                                  "git -C frameworks push --dry-run")
            await turn(link, rec, "Yourself, with your write tool (no bash, no subagent), create the file "
                                  f"{PROBE.relative_to(REPO_ROOT)} containing the single line x.")
            await turn(link, rec, "Yourself, with your ast_grep_replace tool and dryRun false (no bash, no edit tool, "
                                  f"no subagent), rewrite `x = 1` to `x = 2` in {AST_PROBE.relative_to(REPO_ROOT)}. "
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
    assert name in ("Write", "Edit") and approvable_path(name, tool_input, REPO_ROOT) is not None
    assert not PROBE.exists()
    assert resumed == session and replayed == []
    assert MARKER in "".join(rec2.deltas)
    # The plugin's AST rewrite writes without asking: denied, so the file stays as it was.
    ast_text = AST_PROBE.read_text(encoding="utf-8")
    AST_PROBE.unlink()
    assert ast_text == "x = 1\n"
    assert not any(t["name"] == "ast_grep_replace" and not t["is_error"] for t in calls)

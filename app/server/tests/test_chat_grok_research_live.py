"""Real grok research ACP, opt-in: LYNCH_LIVE_GROK=1 uv run pytest tests/test_chat_grok_research_live.py -q -s

Guards the 2026-10-31 spike (chat/grok.py, chat/grok_link.py): GROK_HOME dontAsk/allow/ask/deny, skill edits
reach session/request_permission, git push is denied, session/resume keeps a nonce, and Cursor/user MCP besides
repo wisburg is not connected. Run it after every grok upgrade. Needs a logged-in `grok` (grok.com). Spends a
few cents; does not write frameworks/ (skill edits are rejected).
"""
from __future__ import annotations

import asyncio
import os
import shutil
from pathlib import Path

import pytest

from lynch_reader.catalog import REPO_ROOT
from lynch_reader.chat.app_log import AppLog
from lynch_reader.chat.grok import (DEFAULT_RESEARCH_EFFORT, DEFAULT_RESEARCH_MODEL, GrokConfig, grok_models,
                                    research_env, write_research_home, write_research_rules)
from lynch_reader.chat.grok_link import GrokLink
from tests.chat_helpers import LinkRecorder, conv

pytestmark = pytest.mark.skipif(os.environ.get("LYNCH_LIVE_GROK") != "1", reason="real grok, opt-in")

TURN_S = 300.0
NONCE = "LYNCH-GROK-LIVE-9173"
SKILL = "frameworks/.claude/skills/sediment-framework/SKILL.md"


def config(tmp_path: Path) -> GrokConfig:
    return GrokConfig((shutil.which("grok") or "grok",), tmp_path / "wd", Path.home(), tmp_path / "logs",
                      tmp_path / "invest", tmp_path / "search.md")


async def turn(link: GrokLink, rec: LinkRecorder, text: str, *, allow: bool = False) -> None:
    ends, asks = len(rec.turn_ends), len(rec.permissions)
    await link.prompt(text)

    async def settle() -> None:
        nonlocal asks
        while len(rec.turn_ends) == ends:
            if len(rec.permissions) > asks:
                await link.answer(rec.permissions[asks][0], allow)
                asks += 1
            await asyncio.sleep(0.05)
    await asyncio.wait_for(settle(), TURN_S)


def test_research_guardrails_skill_ask_resume_and_mcp(tmp_path: Path):
    models = grok_models(Path.home(), search_only=False)
    if DEFAULT_RESEARCH_MODEL not in {m.id for m in models}:
        pytest.skip(f"{DEFAULT_RESEARCH_MODEL} not in grok's model cache")
    cfg = config(tmp_path)
    c = conv(kind="research", ticker=None, agent="grok", model=DEFAULT_RESEARCH_MODEL,
             effort=DEFAULT_RESEARCH_EFFORT, context_window=256000)
    home = write_research_home(cfg, c, Path.home() / ".grok")
    write_research_rules(cfg, c)
    env = research_env(os.environ, home)
    rec = LinkRecorder()
    before = Path(REPO_ROOT / SKILL).read_text(encoding="utf-8")

    async def main():
        link = GrokLink(cfg, c, env, AppLog(cfg.logs / f"{c.id}.jsonl"), rec.events(), REPO_ROOT)
        session = await link.start()
        try:
            await turn(link, rec, f"Remember the nonce {NONCE}. Run the shell command `longbridge version` "
                                  "(a query, not an order) and then reply with the word ok1.")
            await turn(link, rec, f"Using the edit or write tool (not bash), append a line LIVE_SKILL to {SKILL}.")
            await turn(link, rec, "Run exactly this shell command: git push --dry-run")
            await turn(link, rec, "Do not call any MCP tool. Reply with the MCP server names you believe are "
                                  "connected, comma-separated, or NONE.")
        finally:
            await link.close()
        rec2 = LinkRecorder()
        c2 = conv(id=c.id, kind="research", ticker=None, agent="grok", model=DEFAULT_RESEARCH_MODEL,
                  effort=DEFAULT_RESEARCH_EFFORT, context_window=256000, grok_session=session)
        env2 = research_env(os.environ, home)
        link2 = GrokLink(cfg, c2, env2, AppLog(cfg.logs / f"{c.id}.jsonl"), rec2.events(), REPO_ROOT)
        sid2 = await link2.start()
        lost = link2.session_lost
        try:
            await turn(link2, rec2, "What nonce did I ask you to remember? Reply with the nonce only.")
        finally:
            await link2.close()
        return session, sid2, lost, rec, rec2

    session, sid2, lost, rec, rec2 = asyncio.run(main())
    assert sid2 == session and lost is False
    assert rec.turn_ends and rec2.turn_ends
    assert any("sediment-framework" in (p[3].get("file_path") or "") for p in rec.permissions)
    assert Path(REPO_ROOT / SKILL).read_text(encoding="utf-8") == before
    assert NONCE in "".join(rec2.deltas)
    assert "github-nekomoto" not in "".join(rec.deltas).lower()

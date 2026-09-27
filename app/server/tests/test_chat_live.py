"""Real Claude Code, opt-in: LYNCH_LIVE_CLAUDE=1 uv run pytest tests/test_chat_live.py -q

Guards the undocumented CLI flags and the launch config in lynch_reader/chat/config.py. Uses Haiku at low
effort to spend little of the subscription. Run it after every Claude Code upgrade.

Needs: a logged-in `claude` (subscription), `.mcp.json` at the repo root, a logged-in `longbridge` CLI, `just`,
and `frameworks/` as a real directory under the repo. In a git worktree where `frameworks` is a symlink, claude refuses `cd frameworks`,
so the interrupt test cannot pass there.
"""
from __future__ import annotations

import asyncio
import contextlib
import os
import shutil
import uuid
from pathlib import Path

import pytest

from lynch_reader.catalog import REPO_ROOT
from lynch_reader.chat.config import LaunchConfig, launch_argv, launch_env
from lynch_reader.chat.index import ChatIndex
from lynch_reader.chat.process import ChatProcess
from lynch_reader.chat.service import ChatService, NotFound
from lynch_reader.chat.transcript import projects_dir_for
from tests.chat_helpers import conv, until

pytestmark = pytest.mark.skipif(os.environ.get("LYNCH_LIVE_CLAUDE") != "1", reason="real claude, opt-in")

MODEL, EFFORT = "claude-haiku-4-5", "low"
CFG = LaunchConfig((shutil.which("claude") or "claude",), REPO_ROOT)


def service(tmp_path: Path) -> ChatService:
    return ChatService(ChatIndex(tmp_path / "index.json"), CFG, projects_dir_for(REPO_ROOT, Path.home()),
                       dict(os.environ))


async def cleanup(svc: ChatService, conv_id: str | None) -> None:
    """Always runs, so a timed-out wait never leaves a `claude -p` process group spending the subscription."""
    if conv_id is not None:
        with contextlib.suppress(NotFound):
            await svc.archive(conv_id)
    await svc.shutdown()


def last_text(svc, conv_id):
    return next((i["text"] for i in reversed(svc.snapshot(conv_id)["items"]) if i["kind"] == "text"), "")


def tool_pending(svc, conv_id):
    return any(i["kind"] == "tool" and i["output"] is None for i in svc.snapshot(conv_id)["items"])


def one_turn(prompt: str) -> list[dict]:
    """Every stream message of a fresh process's first turn."""
    async def main():
        got = []
        c = conv(id=str(uuid.uuid4()), model=MODEL, effort=EFFORT)
        proc = ChatProcess(launch_argv(CFG, c, resume=False), launch_env(os.environ), REPO_ROOT,
                           got.append, lambda *a: None)
        try:
            await proc.start()
            await proc.send_user(prompt)
            await until(lambda: any(m.get("type") == "result" for m in got), timeout=120)
        finally:
            await proc.close()
        return got
    return asyncio.run(main())


def test_launch_config_loads_wisburg_and_no_claudeai_connectors():
    init = next(m for m in one_turn("reply exactly: ok") if m.get("subtype") == "init")
    names = {s["name"] for s in init["mcp_servers"]}
    assert "wisburg" in names
    assert not [n for n in names if n.startswith("claude.ai")]
    assert "sediment-framework" in init["skills"]



def test_longbridge_cli_queries_run_and_orders_are_denied():
    got = one_turn("Run these two Bash commands one at a time, then reply exactly: done\n"
                   "1. `longbridge quote AAPL.US --format json`\n2. `longbridge order --help`")
    denied = [d["tool_input"].get("command", "") for m in got if m.get("type") == "result"
              for d in m.get("permission_denials", [])]
    results = [b for m in got if m.get("type") == "user" for b in m["message"]["content"]
               if isinstance(b, dict) and b.get("type") == "tool_result"]
    assert [c for c in denied if "longbridge order" in c]
    assert not [c for c in denied if "longbridge quote" in c]
    assert [r for r in results if not r.get("is_error") and "AAPL" in str(r.get("content"))]

def test_interjection_and_interrupt(tmp_path):
    async def main():
        svc = service(tmp_path)
        conv_id = None
        try:
            c = await svc.create("research", None,
                                 "Run `cd frameworks && just check` with Bash, then reply exactly: one", MODEL, EFFORT)
            conv_id = c.id
            await until(lambda: tool_pending(svc, c.id), timeout=60)  # interject while the tool runs
            await svc.send(c.id, "Also reply exactly: two")
            await until(lambda: svc.snapshot(c.id)["conv"]["state"] == "idle", timeout=240)
            both = last_text(svc, c.id)
            await svc.send(c.id, "Run `cd frameworks && just check` with Bash, then reply exactly: three")
            await until(lambda: tool_pending(svc, c.id), timeout=60)
            await svc.interrupt(c.id)
            await until(lambda: svc.snapshot(c.id)["conv"]["state"] == "idle", timeout=60)
            return both, svc.snapshot(c.id)["items"]
        finally:
            await cleanup(svc, conv_id)
    both, items = asyncio.run(main())
    assert "two" in both
    assert items[-1] == {"kind": "notice", "key": items[-1]["key"], "text": "已停止"}


def test_edit_truncates_and_continues_on_the_new_branch(tmp_path):
    async def main():
        svc = service(tmp_path)
        conv_id = None
        try:
            c = await svc.create("research", None, "Remember the word ALPHA. Reply exactly: ok1", MODEL, EFFORT)
            conv_id = c.id
            await until(lambda: svc.snapshot(c.id)["conv"]["state"] == "idle", timeout=120)
            await svc.send(c.id, "Remember the word BRAVO. Reply exactly: ok2")
            await until(lambda: "ok2" in last_text(svc, c.id), timeout=120)
            bravo = [i for i in svc.snapshot(c.id)["items"] if i["kind"] == "user"][-1]["uuid"]
            await svc.edit(c.id, bravo, "Remember the word CHARLIE. Reply exactly: ok3")
            await until(lambda: "ok3" in last_text(svc, c.id), timeout=120)
            await svc.send(c.id, "List every word I asked you to remember, comma separated, nothing else.")
            await until(lambda: svc.snapshot(c.id)["conv"]["state"] == "idle"
                        and "," in last_text(svc, c.id), timeout=120)
            return last_text(svc, c.id)
        finally:
            await cleanup(svc, conv_id)
    words = asyncio.run(main())
    assert "ALPHA" in words and "CHARLIE" in words and "BRAVO" not in words


def test_a_new_conversation_gets_claudes_own_title(tmp_path):
    async def main():
        svc = service(tmp_path)
        conv_id = None
        try:
            c = await svc.create("research", None, "Reply exactly: ok. (Topic: AEHR wafer-level burn-in valuation)",
                                 MODEL, EFFORT)
            conv_id = c.id
            await until(lambda: svc._index.get(c.id).title != c.title, timeout=120)
            return c.title, svc._index.get(c.id).title
        finally:
            await cleanup(svc, conv_id)
    first, titled = asyncio.run(main())
    assert titled and titled != first

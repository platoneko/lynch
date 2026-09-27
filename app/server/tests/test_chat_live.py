"""Real Claude Code, opt-in: LYNCH_LIVE_CLAUDE=1 uv run pytest tests/test_chat_live.py -q

Guards the undocumented flags and the launch config (spec §9). Uses Haiku at low effort to spend
little of the subscription. Run it after every Claude Code upgrade.
"""
from __future__ import annotations

import asyncio
import os
import shutil
import uuid
from pathlib import Path

import pytest

from lynch_reader.catalog import REPO_ROOT
from lynch_reader.chat.config import LaunchConfig, launch_argv, launch_env
from lynch_reader.chat.index import ChatIndex
from lynch_reader.chat.process import ChatProcess
from lynch_reader.chat.service import ChatService
from lynch_reader.chat.transcript import projects_dir_for
from tests.chat_helpers import conv, until

pytestmark = pytest.mark.skipif(os.environ.get("LYNCH_LIVE_CLAUDE") != "1", reason="real claude, opt-in")

MODEL, EFFORT = "claude-haiku-4-5", "low"
CFG = LaunchConfig((shutil.which("claude") or "claude",), REPO_ROOT)


def service(tmp_path: Path) -> ChatService:
    return ChatService(ChatIndex(tmp_path / "index.json"), CFG, projects_dir_for(REPO_ROOT, Path.home()),
                       dict(os.environ))


def last_text(svc, conv_id):
    return next((i["text"] for i in reversed(svc.snapshot(conv_id)["items"]) if i["kind"] == "text"), "")


def test_launch_config_loads_both_mcp_servers_and_no_claudeai_connectors():
    async def main():
        got = []
        c = conv(id=str(uuid.uuid4()), model=MODEL, effort=EFFORT)
        proc = ChatProcess(launch_argv(CFG, c, resume=False), launch_env(os.environ), REPO_ROOT,
                           got.append, lambda *a: None)
        await proc.start()
        await proc.send_user("reply exactly: ok")
        await until(lambda: any(m.get("type") == "result" for m in got), timeout=120)
        await proc.close()
        return next(m for m in got if m.get("subtype") == "init")
    init = asyncio.run(main())
    names = {s["name"] for s in init["mcp_servers"]}
    assert {"wisburg", "longbridge"} <= names
    assert not [n for n in names if n.startswith("claude.ai")]
    assert "sediment-framework" in init["skills"]


def test_interjection_and_interrupt(tmp_path):
    async def main():
        svc = service(tmp_path)
        c = await svc.create("research", None,
                             "Run `cd frameworks && just check` with Bash, then reply exactly: one", MODEL, EFFORT)
        await until(lambda: svc.snapshot(c.id)["conv"]["state"] == "running", timeout=60)
        await svc.send(c.id, "Also reply exactly: two")
        await until(lambda: svc.snapshot(c.id)["conv"]["state"] == "idle", timeout=240)
        both = last_text(svc, c.id)
        await svc.send(c.id, "Run `cd frameworks && just check` with Bash, then reply exactly: three")
        await until(lambda: any(i["kind"] == "tool" and i["output"] is None
                                for i in svc.snapshot(c.id)["items"]), timeout=60)
        await svc.interrupt(c.id)
        await until(lambda: svc.snapshot(c.id)["conv"]["state"] == "idle", timeout=60)
        items = svc.snapshot(c.id)["items"]
        await svc.archive(c.id)
        await svc.shutdown()
        return both, items
    both, items = asyncio.run(main())
    assert "two" in both
    assert items[-1] == {"kind": "notice", "key": items[-1]["key"], "text": "已停止"}


def test_edit_truncates_and_continues_on_the_new_branch(tmp_path):
    async def main():
        svc = service(tmp_path)
        c = await svc.create("research", None, "Remember the word ALPHA. Reply exactly: ok1", MODEL, EFFORT)
        await until(lambda: svc.snapshot(c.id)["conv"]["state"] == "idle", timeout=120)
        await svc.send(c.id, "Remember the word BRAVO. Reply exactly: ok2")
        await until(lambda: last_text(svc, c.id).strip() == "ok2", timeout=120)
        bravo = [i for i in svc.snapshot(c.id)["items"] if i["kind"] == "user"][-1]["uuid"]
        await svc.edit(c.id, bravo, "Remember the word CHARLIE. Reply exactly: ok3")
        await until(lambda: last_text(svc, c.id).strip() == "ok3", timeout=120)
        await svc.send(c.id, "List every word I asked you to remember, comma separated, nothing else.")
        await until(lambda: svc.snapshot(c.id)["conv"]["state"] == "idle"
                    and "," in last_text(svc, c.id), timeout=120)
        words = last_text(svc, c.id)
        await svc.archive(c.id)
        await svc.shutdown()
        return words
    words = asyncio.run(main())
    assert "ALPHA" in words and "CHARLIE" in words and "BRAVO" not in words

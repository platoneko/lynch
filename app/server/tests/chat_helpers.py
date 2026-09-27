"""Shared helpers for the chat tests."""
from __future__ import annotations

import asyncio
import json
import os
import sys
from collections.abc import Callable
from pathlib import Path

from lynch_reader.chat.config import LaunchConfig
from lynch_reader.chat.index import ChatIndex, Conversation

FAKE = Path(__file__).with_name("fake_claude.py")
FAKE_CMD = (sys.executable, str(FAKE))


def conv(**overrides) -> Conversation:
    base = dict(id="c1", kind="ticker", ticker="AAA.US", title="t", model="claude-opus-5-5",
                effort="medium", created_at=1.0, updated_at=1.0)
    return Conversation(**{**base, **overrides})


async def until(pred: Callable[[], bool], timeout: float = 5.0) -> None:
    async def poll():
        while not pred():
            await asyncio.sleep(0.02)
    await asyncio.wait_for(poll(), timeout)


def make_service(tmp_path: Path, **kwargs):
    """A ChatService on the fake claude; returns it and the launch log path."""
    from lynch_reader.chat.service import ChatService
    projects = tmp_path / "projects"
    projects.mkdir(exist_ok=True)
    (tmp_path / "frameworks").mkdir(exist_ok=True)
    log = tmp_path / "launches.jsonl"
    env = {**os.environ, "FAKE_CLAUDE_PROJECTS": str(projects), "FAKE_CLAUDE_LOG": str(log),
           "ANTHROPIC_API_KEY": "must-not-leak"}
    svc = ChatService(ChatIndex(tmp_path / "index.json"), LaunchConfig(FAKE_CMD, tmp_path), projects, env, **kwargs)
    return svc, log


def launches(log: Path) -> list[dict]:
    return [json.loads(l) for l in log.read_text().splitlines()] if log.exists() else []


def state_of(svc, conv_id: str) -> str:
    return svc.snapshot(conv_id)["conv"]["state"]


async def next_event(q: asyncio.Queue, name: str, pred: Callable[[dict], bool] = lambda d: True,
                     timeout: float = 5.0) -> dict:
    async def scan():
        while True:
            event, data = await q.get()
            if event == name and pred(data):
                return data
    return await asyncio.wait_for(scan(), timeout)

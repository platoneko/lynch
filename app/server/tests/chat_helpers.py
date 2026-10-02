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


def write_invest(root: Path, list_id: str | None = "2051493926612484289", handles: tuple[str, ...] = ("alice", "bob"),
                 hours: int | None = 36, broken: bool = False) -> Path:
    """A stand-in for ../invest with the two loaders lynch calls (scripts/load_kol_handles.py, load_policies.py)."""
    scripts = root / "scripts"
    scripts.mkdir(parents=True, exist_ok=True)
    (scripts / "load_kol_handles.py").write_text(
        f"def load_list_id():\n    return {list_id!r}\n\ndef load_handles():\n    return {list(handles)!r}\n",
        encoding="utf-8")
    policies = {"fetch": {"tweets": {"lookback_hours": hours}}} if hours is not None else {}
    body = "raise RuntimeError('bad policies.toml')\n" if broken else f"POLICIES = {policies!r}\n"
    (scripts / "load_policies.py").write_text(body, encoding="utf-8")
    return root


def write_models_cache(home: Path) -> None:
    """A ~/.grok/models_cache.json shaped like grok 1.0.46's: one usable model, one hidden, one without X search."""
    def model(mid: str, **info) -> dict:
        return {"info": {"id": mid, "name": mid.replace("grok-", "Grok "), "hidden": False,
                         "supports_backend_search": True, "context_window": 256000,
                         "context_windows": [256000, 500000], "reasoning_effort": "high",
                         "reasoning_efforts": [{"id": e} for e in ("high", "medium", "low")], **info}}
    models = {"grok-4.6": model("grok-4.6"), "grok-secret": model("grok-secret", hidden=True),
              "grok-plain": model("grok-plain", supports_backend_search=False)}
    (home / ".grok").mkdir(parents=True, exist_ok=True)
    (home / ".grok" / "models_cache.json").write_text(json.dumps({"models": models}), encoding="utf-8")

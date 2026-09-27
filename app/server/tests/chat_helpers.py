"""Shared helpers for the chat tests."""
from __future__ import annotations

import asyncio
import sys
from collections.abc import Callable
from pathlib import Path

from lynch_reader.chat.index import Conversation

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

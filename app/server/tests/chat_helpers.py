"""Shared helpers for the chat tests."""
from __future__ import annotations

from lynch_reader.chat.index import Conversation


def conv(**overrides) -> Conversation:
    base = dict(id="c1", kind="ticker", ticker="AAA.US", title="t", model="claude-opus-5-5",
                effort="medium", created_at=1.0, updated_at=1.0)
    return Conversation(**{**base, **overrides})

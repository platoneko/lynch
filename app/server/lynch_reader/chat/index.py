"""The app's list of conversations (output/chat/index.json).

Conversation content lives in Claude Code's own transcripts; this only records what the app
needs to list, label and relaunch a conversation.
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass, fields, replace
from pathlib import Path

log = logging.getLogger(__name__)

TITLE_MAX = 40


@dataclass(frozen=True)
class Conversation:
    id: str  # also the Claude Code session id (Claude kinds)
    kind: str  # "research" (new ticker, no framework yet) | "ticker" | "discover" (Grok on X)
    ticker: str | None  # research: None until the agent writes frameworks/<TICKER>.md
    title: str
    model: str
    effort: str
    created_at: float
    updated_at: float
    context_window: int | None = None  # from the latest `result` event
    archived: bool = False
    grok_session: str | None = None  # discover: grok's own session id (made over ACP, chat/grok.py)
    cost_usd: float | None = None  # discover: what its turns have cost, from each `result`
    lookback_days: int | None = None  # discover: how many days back the KOL search looks, picked at creation


def make_title(text: str) -> str:
    line = " ".join(text.split())
    return line if len(line) <= TITLE_MAX else line[: TITLE_MAX - 1] + "…"


def _parse_row(row: object) -> Conversation | None:
    """A Conversation from one stored row, or None when a field is missing or of the wrong type."""
    if not isinstance(row, dict):
        return None
    known = {f.name for f in fields(Conversation)}
    try:
        c = Conversation(**{k: v for k, v in row.items() if k in known})
    except TypeError:  # a required field is missing
        return None

    def number(v: object) -> bool:
        return isinstance(v, (int, float)) and not isinstance(v, bool)
    well_formed = (all(isinstance(v, str) for v in (c.id, c.kind, c.title, c.model, c.effort))
                   and (c.ticker is None or isinstance(c.ticker, str))
                   and number(c.created_at) and number(c.updated_at)
                   and (c.context_window is None or number(c.context_window))
                   and isinstance(c.archived, bool)
                   and (c.grok_session is None or isinstance(c.grok_session, str))
                   and (c.cost_usd is None or number(c.cost_usd))
                   and (c.lookback_days is None or type(c.lookback_days) is int))
    return c if well_formed else None


class ChatIndex:
    """Single writer: only the server's event loop touches it, so no lock."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._rows: dict[str, Conversation] = {}
        if path.exists():
            for row in json.loads(path.read_text(encoding="utf-8")):
                conv = _parse_row(row)
                if conv is None:  # one bad row must not take the whole reader down at startup
                    log.warning("skipping malformed chat index row: %r", row)
                    continue
                self._rows[conv.id] = conv

    def get(self, conv_id: str) -> Conversation | None:
        return self._rows.get(conv_id)

    def all(self) -> list[Conversation]:
        return list(self._rows.values())

    def put(self, conv: Conversation) -> None:
        self._rows[conv.id] = conv
        self._save()

    def update(self, conv_id: str, **changes) -> Conversation:
        conv = replace(self._rows[conv_id], **changes)
        self.put(conv)
        return conv

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".tmp")
        rows = [asdict(c) for c in self._rows.values()]
        tmp.write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, self._path)  # a crash mid-write must not leave a torn index

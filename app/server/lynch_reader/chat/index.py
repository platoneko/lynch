"""The app's list of conversations (output/chat/index.json).

Conversation content lives in Claude Code's own transcripts; this only records what the app
needs to list, label and relaunch a conversation.
"""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, fields, replace
from pathlib import Path

TITLE_MAX = 40


@dataclass(frozen=True)
class Conversation:
    id: str  # also the Claude Code session id
    kind: str  # "research" (new ticker, no framework yet) | "ticker"
    ticker: str | None  # research: None until the agent writes frameworks/<TICKER>.md
    title: str
    model: str
    effort: str
    created_at: float
    updated_at: float
    context_window: int | None = None  # from the latest `result` event
    archived: bool = False


def make_title(text: str) -> str:
    line = " ".join(text.split())
    return line if len(line) <= TITLE_MAX else line[: TITLE_MAX - 1] + "…"


class ChatIndex:
    """Single writer: only the server's event loop touches it, so no lock."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._rows: dict[str, Conversation] = {}
        if path.exists():
            known = {f.name for f in fields(Conversation)}
            for row in json.loads(path.read_text(encoding="utf-8")):
                conv = Conversation(**{k: v for k, v in row.items() if k in known})
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

"""The app's own record of a discover chat (output/chat/grok/<id>.jsonl, spec §3h).

grok prints neither the user's message nor parent links, and its session files use an internal format. So the
app writes each turn here in Claude Code's transcript shape, each line chained to the one before, and
chat/transcript.py reads it back unchanged.
"""
from __future__ import annotations

import json
import uuid as uuidlib
from pathlib import Path

from lynch_reader.chat.items import INTERRUPT_PREFIX
from lynch_reader.chat.transcript import read_entries


class GrokLog:
    def __init__(self, path: Path) -> None:
        self._path = path
        entries = read_entries(path) if path.exists() else []
        self._last: str | None = next((e["uuid"] for e in reversed(entries) if "uuid" in e), None)

    def user(self, text: str) -> dict:
        return self._append({"type": "user", "message": {"role": "user", "content": text}})

    def interrupted(self) -> dict:
        """The marker Claude Code writes on a stop; the reader shows it as 已停止."""
        return self.user(f"{INTERRUPT_PREFIX}]")

    def message(self, msg: dict) -> dict:
        """One assistant line or tool-result user line of grok's stream; its own uuid is kept."""
        return self._append({"type": msg["type"], "uuid": msg.get("uuid"), "message": msg["message"]})

    def _append(self, entry: dict) -> dict:
        entry = {**entry, "uuid": entry.get("uuid") or str(uuidlib.uuid4()), "parentUuid": self._last}
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        self._last = entry["uuid"]
        return entry

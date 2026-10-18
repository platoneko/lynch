"""The app's own record of a chat whose agent keeps no transcript the app can read: Grok
(output/chat/grok/<id>.jsonl, spec §3h) and opencode (output/chat/opencode/<id>.jsonl).

Neither prints the user's message nor parent links, and their session files use internal formats. So the app
writes each turn here in Claude Code's transcript shape, each line chained to the one before, and
chat/transcript.py reads it back unchanged.
"""
from __future__ import annotations

import json
import uuid as uuidlib
from pathlib import Path

from lynch_reader.chat.items import INTERRUPT_PREFIX
from lynch_reader.chat.transcript import read_entries


class AppLog:
    def __init__(self, path: Path) -> None:
        self._path = path
        entries = read_entries(path) if path.exists() else []
        self._last: str | None = next((e["uuid"] for e in reversed(entries) if "uuid" in e), None)
        # A server that died mid-write leaves a torn last line; the next entry must not be glued onto it.
        self._torn = path.exists() and path.stat().st_size > 0 and _last_byte(path) != b"\n"

    def user(self, text: str) -> dict:
        return self._append({"type": "user", "message": {"role": "user", "content": text}})

    def interrupted(self) -> dict:
        """The marker Claude Code writes on a stop; the reader shows it as 已停止."""
        return self.user(f"{INTERRUPT_PREFIX}]")

    def message(self, msg: dict) -> dict:
        """One assistant line or tool-result user line in Claude's shape; its own uuid is kept."""
        return self._append({"type": msg["type"], "uuid": msg.get("uuid"), "message": msg["message"]})

    def _append(self, entry: dict) -> dict:
        entry = {**entry, "uuid": entry.get("uuid") or str(uuidlib.uuid4()), "parentUuid": self._last}
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._path.open("a", encoding="utf-8") as f:
            f.write(("\n" if self._torn else "") + json.dumps(entry, ensure_ascii=False) + "\n")
        self._torn = False
        self._last = entry["uuid"]
        return entry


def _last_byte(path: Path) -> bytes:
    with path.open("rb") as f:
        f.seek(-1, 2)
        return f.read(1)

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
        self.client_id: str | None = None
        self.refresh()

    def refresh(self) -> list[dict]:
        """Reconcile the append cursor with disk after an uncertain or partial write."""
        entries = self.entries()
        self._last: str | None = next((e["uuid"] for e in reversed(entries) if "uuid" in e), None)
        # A server that died mid-write leaves a torn last line; the next entry must not be glued onto it.
        self._torn = self._path.exists() and self._path.stat().st_size > 0 and _last_byte(self._path) != b"\n"
        return entries

    @property
    def path(self) -> Path:
        return self._path

    def entries(self) -> list[dict]:
        return read_entries(self._path) if self._path.exists() else []

    def user(self, text: str, **metadata) -> dict:
        if self.client_id is not None:
            metadata.setdefault('client_id', self.client_id)
        return self._append({**metadata, "type": "user", "message": {"role": "user", "content": text}})

    def bind_native(self, user_uuid: str, native_id: str) -> None:
        self._append({"type": "lynch_native", "user_uuid": user_uuid, "native_id": native_id})

    def native_id(self, user_uuid: str) -> str | None:
        return next((entry["native_id"] for entry in reversed(self.entries())
                     if entry.get("type") == "lynch_native" and entry.get("user_uuid") == user_uuid), None)

    def branch_at(self, parent_uuid: str, *, agent: str, native_session: str) -> dict:
        if not any(entry.get("uuid") == parent_uuid for entry in self.entries()):
            raise ValueError("branch parent is not in the transcript")
        self._last = parent_uuid
        return self._append({"type": "system", "subtype": "lynch_branch", "agent": agent,
                             "native_session": native_session})

    def interrupted(self) -> dict:
        """The marker Claude Code writes on a stop; the reader shows it as 已停止."""
        return self.user(f"{INTERRUPT_PREFIX}]")

    def compacted(self, trigger: str, pre_tokens: int | None, **metadata) -> dict:
        """Keep a display boundary while the native agent owns the actual summary."""
        return self._append({**metadata, "type": "system", "subtype": "compact_boundary",
                             "compactMetadata": {"trigger": trigger, "preTokens": pre_tokens}})

    def message(self, msg: dict) -> dict:
        """One assistant line or tool-result user line in Claude's shape; its own uuid is kept."""
        return self._append({**msg, "type": msg["type"], "uuid": msg.get("uuid"), "message": msg["message"]})

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

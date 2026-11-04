"""Durable, single-writer chat inputs; uncertain sends are never automatically replayed."""
from __future__ import annotations

import json
import os
import uuid
from pathlib import Path


class MessageQueue:
    def __init__(self, path: Path) -> None:
        self._path = path
        data = json.loads(path.read_text()) if path.exists() else {"chats": {}}
        self._chats: dict[str, list[dict]] = data["chats"]
        self._stable = json.dumps(data, ensure_ascii=False)
        changed = False
        for rows in self._chats.values():
            for row in rows:
                if (not all(isinstance(row.get(key), str) for key in ("id", "text"))
                        or row.get("state") not in ("queued", "sending", "done", "unanswered", "uncertain")):
                    raise ValueError("invalid persisted chat queue")
                if row["state"] in ("queued", "sending"):
                    row["state"] = "uncertain" if row["state"] == "sending" else "unanswered"
                    changed = True
        if changed:
            self._save()

    def add(self, chat: str, text: str, client_id: str | None = None) -> dict:
        rows = self._chats.setdefault(chat, [])
        key = client_id or str(uuid.uuid4())
        old = next((row for row in rows if row["id"] == key), None)
        if old is not None:
            if old["text"] != text:
                raise ValueError("message id already belongs to another input")
            return dict(old)
        row = {"id": key, "text": text, "state": "queued"}
        rows.append(row)
        self._save()
        return dict(row)

    def pending(self, chat: str) -> list[dict]:
        labels = {"queued": "behind", "sending": "waiting", "unanswered": "unanswered", "uncertain": "uncertain"}
        return [{"id": row["id"], "text": row["text"], "status": labels[row["state"]]}
                for row in self._chats.get(chat, []) if row["state"] != "done"]

    def next(self, chat: str) -> dict | None:
        return next((dict(row) for row in self._chats.get(chat, []) if row["state"] == "queued"), None)

    def active(self, chat: str) -> dict | None:
        return next((dict(row) for row in self._chats.get(chat, []) if row["state"] == "sending"), None)

    def begin(self, chat: str, key: str) -> None:
        self._set(chat, key, "sending")

    def complete(self, chat: str, key: str) -> None:
        self._set(chat, key, "done")

    def dismiss(self, chat: str, key: str) -> None:
        row = next((row for row in self._chats.get(chat, []) if row["id"] == key), None)
        if row is not None and row["state"] in ("unanswered", "uncertain"):
            self._set(chat, key, "done")

    def pause(self, chat: str) -> None:
        paused = {}
        for row in self._chats.get(chat, []):
            if row["state"] in ("queued", "sending"):
                row["state"] = "uncertain" if row["state"] == "sending" else "unanswered"
                paused[row["id"]] = row["state"]
        if paused:
            try:
                self._save()
            except OSError:
                # Stopping remains effective in this process even when storage is unavailable.
                # A restart also recovers interrupted rows as paused, never as runnable work.
                for row in self._chats.get(chat, []):
                    if row["id"] in paused:
                        row["state"] = paused[row["id"]]
                self._stable = json.dumps({"chats": self._chats}, ensure_ascii=False)
                raise

    def _set(self, chat: str, key: str, state: str) -> None:
        row = next(row for row in self._chats.get(chat, []) if row["id"] == key)
        row["state"] = state
        self._save()

    def _save(self) -> None:
        content = json.dumps({"chats": self._chats}, ensure_ascii=False)
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self._path.with_name(self._path.name + ".tmp")
            with temporary.open("w", encoding="utf-8") as stream:
                stream.write(content + "\n")
                stream.flush()
                os.fsync(stream.fileno())
            temporary.replace(self._path)
        except OSError:
            # A failed REST request must not leave an unsaved input eligible for a later drain.
            self._chats = json.loads(self._stable)["chats"]
            raise
        self._stable = content

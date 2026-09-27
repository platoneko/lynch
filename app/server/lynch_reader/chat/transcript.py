"""Read a conversation back from Claude Code's transcript (~/.claude/projects/<slug>/<id>.jsonl).

The file is append-only: an edit leaves the old branch in place, and a compaction starts a new
chain whose boundary points back via `logicalParentUuid` (spec §9 第 3 条). The current branch is
the parent chain from the newest message entry.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from lynch_reader.chat.items import compact_item, context_tokens, message_items

_MESSAGE_TYPES = ("user", "assistant", "system")


class EditRejected(ValueError):
    pass


@dataclass(frozen=True)
class History:
    items: list[dict]
    context_tokens: int | None  # None right after a compaction, until the next reply
    unfinished: bool  # the branch ends on a user message or a tool call with no result


def projects_dir_for(repo_root: Path, home: Path) -> Path:
    return home / ".claude" / "projects" / re.sub(r"[^A-Za-z0-9]", "-", str(repo_root))


def read_entries(path: Path) -> list[dict]:
    entries = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            continue  # Claude Code may be mid-write on the last line
    return entries


def current_branch(entries: list[dict]) -> list[dict]:
    by_uuid = {e["uuid"]: e for e in entries if "uuid" in e}
    tips = [e for e in entries if e.get("type") in _MESSAGE_TYPES and "uuid" in e and not e.get("isSidechain")]
    if not tips:
        return []
    chain: list[dict] = []
    seen: set[str] = set()
    cur: dict | None = tips[-1]
    while cur is not None and cur["uuid"] not in seen:
        seen.add(cur["uuid"])
        chain.append(cur)
        parent = cur.get("parentUuid") or cur.get("logicalParentUuid")
        cur = by_uuid.get(parent) if parent else None
    chain.reverse()
    return chain


def history(branch: list[dict]) -> History:
    items: list[dict] = []
    tools: dict[str, dict] = {}
    tokens: int | None = None
    for entry in branch:
        if _is_compaction(entry):
            items.append(compact_item(entry["uuid"], entry.get("compactMetadata") or {}))
            tokens = None
            continue
        if entry.get("type") not in ("user", "assistant"):
            continue
        if entry["type"] == "assistant":
            tokens = context_tokens(entry["message"]) or tokens
        for item in message_items(entry):
            if item["kind"] == "tool_result":
                if item["id"] in tools:
                    tools[item["id"]].update(output=item["output"], is_error=item["is_error"], denied=item["denied"])
                continue
            items.append(item)
            if item["kind"] == "tool":
                tools[item["id"]] = item
    last = next((i for i in reversed(items) if i["kind"] != "notice"), None)
    unfinished = last is not None and (last["kind"] == "user" or (last["kind"] == "tool" and last["output"] is None))
    if items and items[-1]["kind"] == "notice":
        unfinished = False  # an interrupt marker closes the turn
    return History(items, tokens, unfinished)


def edit_cut(branch: list[dict], message_uuid: str) -> str:
    """The uuid to resume at so that `message_uuid` and everything after it are dropped."""
    index = next((i for i, e in enumerate(branch) if e.get("uuid") == message_uuid), None)
    if index is None:
        raise EditRejected("message is not on the current branch")
    if not any(item["kind"] == "user" for item in _items_of(branch[index])):
        raise EditRejected("not a user message")
    if any(_is_compaction(e) for e in branch[index + 1:]):
        raise EditRejected("message is before the latest compaction")
    for entry in reversed(branch[:index]):
        if _is_compaction(entry):
            break
        if entry.get("type") == "assistant":
            return entry["uuid"]
    raise EditRejected("no earlier reply to cut at")


def _items_of(entry: dict) -> list[dict]:
    return message_items(entry) if entry.get("type") in ("user", "assistant") else []


def _is_compaction(entry: dict) -> bool:
    return entry.get("type") == "system" and entry.get("subtype") == "compact_boundary"

"""Claude Code messages → the app's chat items.

The live stream and the transcript carry user/assistant messages in the same shape
(`{"type", "uuid", "message": {"content": ...}}`), so one mapping serves both.
"""
from __future__ import annotations

import uuid as uuidlib
from pathlib import Path

from lynch_reader.chat.candidates import parse_candidates
from lynch_reader.chat.watch import parse_watch

OUTPUT_MAX = 4000
# Tool results that mean "the whitelist refused this call" (verified texts, spec §9 第 2 条).
DENIAL_MARKERS = (
    "requires approval",
    "haven't granted it yet",
    "changes directory before running a version-control command",
    # opencode's (chat/opencode_link.py): a rule's deny, and a refused ask
    "a rule which prevents you from using this specific tool call",
    "rejected permission to use this specific tool call",
)
INTERRUPT_PREFIX = "[Request interrupted by user"
# User-role entries Claude Code writes for itself: local-command echoes and the post-compaction summary.
_INTERNAL_PREFIXES = (
    "<local-command-caveat>", "<local-command-stdout>", "<command-name>", "<command-message>",
    "This session is being continued from a previous conversation",
)
_NOT_FRAMEWORKS = {"AGENTS", "README"}


def message_items(entry: dict) -> list[dict]:
    """Items for one user/assistant message; tool results come back as `tool_result` patches."""
    if any(entry.get(flag) for flag in ("isSidechain", "isMeta", "isSynthetic", "isCompactSummary")):
        return []
    # A message claude injects itself (a background task's completion) names where it came from;
    # the app's own messages carry no origin, a terminal session's typed ones say "human".
    origin = entry.get("origin")
    if origin and origin.get("kind") != "human":
        return []
    content = entry["message"]["content"]
    if entry["type"] == "assistant":
        return [item for i, block in enumerate(content) for item in _assistant_block(entry["uuid"], i, block)]
    if isinstance(content, str):
        return _user_text(entry["uuid"], content)
    patches = [_tool_result(b) for b in content if b.get("type") == "tool_result"]
    texts = [b["text"] for b in content if b.get("type") == "text"]
    return patches + (_user_text(entry["uuid"], "\n".join(texts)) if texts else [])


def compact_item(uuid: str, meta: dict) -> dict:
    return {
        "kind": "compact", "key": uuid, "trigger": meta.get("trigger"),
        "pre_tokens": meta.get("preTokens", meta.get("pre_tokens")),
        "post_tokens": meta.get("postTokens", meta.get("post_tokens")),
    }


def notice_item(text: str) -> dict:
    return {"kind": "notice", "key": str(uuidlib.uuid4()), "text": text}


def retry_text(msg: dict) -> str:
    """An `api_retry` message for the reader: why the request failed, and how long the retry may take."""
    attempt = f"第 {msg.get('attempt')}/{msg.get('max_retries')} 次重试"
    no_response = msg.get("no_response")
    if no_response:  # no first byte within claude's window; the retry waits much longer
        return (f"接口 {no_response['waited_ms'] // 1000} 秒没有响应，{attempt}，"
                f"最多再等 {round(no_response['retry_wait_ms'] / 60000)} 分钟")
    cause = msg.get("error_status") or msg.get("error") or "连接失败"
    return f"接口出错（{cause}），{round(msg.get('retry_delay_ms', 0) / 1000)} 秒后{attempt}"


def context_tokens(message: dict) -> int | None:
    usage = message.get("usage")
    if not usage:
        return None
    return (usage.get("input_tokens", 0) + usage.get("cache_read_input_tokens", 0)
            + usage.get("cache_creation_input_tokens", 0))


def framework_ticker(file_path: str, repo_root: Path) -> str | None:
    """The ticker whose framework `file_path` is (frameworks/<TICKER>.md), else None."""
    path = Path(file_path)
    if not path.is_absolute():
        path = repo_root / path
    try:
        rel = path.resolve().relative_to((repo_root / "frameworks").resolve())
    except ValueError:
        return None
    if len(rel.parts) != 1 or rel.suffix != ".md" or rel.stem.startswith("_") or rel.stem in _NOT_FRAMEWORKS:
        return None
    return rel.stem


def _assistant_block(uuid: str, index: int, block: dict) -> list[dict]:
    if block.get("type") == "text" and block["text"].strip():
        item = {"kind": "text", "key": f"{uuid}#{index}", "text": block["text"]}
        parsed = parse_candidates(block["text"])
        if parsed is not None:  # a discover reply's candidate list (chat/candidates.py)
            item.update(text=parsed.text, candidates=parsed.candidates, candidates_dropped=parsed.dropped)
        watch = parse_watch(item["text"])
        if watch is not None:  # a 讨论 reply's watch card (chat/watch.py)
            item.update(text=watch.text, watch=watch.card)
        return [item]
    if block.get("type") == "tool_use":
        tool_input = block.get("input") or {}
        if not isinstance(tool_input, dict):  # grok prints a server-side tool's input as a Python repr string
            tool_input = {"value": str(tool_input)}
        return [{"kind": "tool", "key": block["id"], "id": block["id"], "name": block["name"],
                 "input": tool_input, "output": None, "is_error": False, "denied": False}]
    return []  # thinking and anything newer


def _user_text(uuid: str, text: str) -> list[dict]:
    if text.startswith(INTERRUPT_PREFIX):
        return [{"kind": "notice", "key": uuid, "text": "已停止"}]
    if text.lstrip().startswith(_INTERNAL_PREFIXES):
        return []
    return [{"kind": "user", "key": uuid, "uuid": uuid, "text": text}]


def _tool_result(block: dict) -> dict:
    raw = block.get("content")
    text = raw if isinstance(raw, str) else "\n".join(
        part.get("text", "") for part in raw or [] if part.get("type") == "text")
    is_error = block.get("is_error") in (True, "True")  # grok prints the flag as a string
    return {
        "kind": "tool_result", "id": block["tool_use_id"],
        "output": text if len(text) <= OUTPUT_MAX else text[: OUTPUT_MAX - 1] + "…",
        "is_error": is_error,
        "denied": is_error and any(m in text for m in DENIAL_MARKERS),
    }

from __future__ import annotations

from pathlib import Path

from lynch_reader.chat.items import compact_item, context_tokens, framework_ticker, message_items, notice_item


def user(uid, content, **kw):
    return {"type": "user", "uuid": uid, "message": {"role": "user", "content": content}, **kw}


def assistant(uid, *blocks):
    return {"type": "assistant", "uuid": uid, "message": {"role": "assistant", "content": list(blocks)}}


def test_user_string_and_text_blocks():
    assert message_items(user("u1", "hi")) == [{"kind": "user", "key": "u1", "uuid": "u1", "text": "hi"}]
    assert message_items(user("u2", [{"type": "text", "text": "a"}, {"type": "text", "text": "b"}]))[0]["text"] == "a\nb"


def test_assistant_text_and_tool_skip_thinking():
    items = message_items(assistant("a1", {"type": "thinking", "thinking": "hm"}, {"type": "text", "text": "ok"},
                                    {"type": "tool_use", "id": "t1", "name": "Read", "input": {"file_path": "x"}}))
    assert items == [
        {"kind": "text", "key": "a1#1", "text": "ok"},
        {"kind": "tool", "key": "t1", "id": "t1", "name": "Read", "input": {"file_path": "x"},
         "output": None, "is_error": False, "denied": False},
    ]


def test_blank_text_is_dropped():
    assert message_items(assistant("a1", {"type": "text", "text": "  \n"})) == []


def test_tool_result_patch_and_denials():
    ok = message_items(user("u", [{"type": "tool_result", "tool_use_id": "t1", "content": "fine"}]))
    assert ok == [{"kind": "tool_result", "id": "t1", "output": "fine", "is_error": False, "denied": False}]
    for text in ("This command requires approval",
                 "Claude requested permissions to write to /x, but you haven't granted it yet.",
                 "This command changes directory before running a version-control command, which can pick up"):
        (patch,) = message_items(user("u", [{"type": "tool_result", "tool_use_id": "t", "is_error": True,
                                            "content": [{"type": "text", "text": text}]}]))
        assert patch["denied"] and patch["is_error"]
    (failed,) = message_items(user("u", [{"type": "tool_result", "tool_use_id": "t", "is_error": True,
                                         "content": "exit 1"}]))
    assert failed["is_error"] and not failed["denied"]


def test_long_tool_output_is_truncated():
    (patch,) = message_items(user("u", [{"type": "tool_result", "tool_use_id": "t", "content": "x" * 10_000}]))
    assert len(patch["output"]) < 10_000 and patch["output"].endswith("…")


def test_interrupt_marker_becomes_a_notice():
    assert message_items(user("u9", "[Request interrupted by user for tool use]")) == [
        {"kind": "notice", "key": "u9", "text": "已停止"}]


def test_internal_entries_are_hidden():
    assert message_items(user("u", "<local-command-stdout>Compacted </local-command-stdout>")) == []
    assert message_items(user("u", "<command-name>/compact</command-name>")) == []
    assert message_items(user("u", "This session is being continued from a previous conversation that ran out")) == []
    assert message_items(user("u", "summary", isCompactSummary=True)) == []
    assert message_items(user("u", "meta", isMeta=True)) == []
    # The live stream marks a skill's injected body isSynthetic; the transcript marks the same entry isMeta.
    assert message_items(user("u", [{"type": "text", "text": "Base directory for this skill: /x"}],
                              isSynthetic=True)) == []
    assert message_items(user("u", "side", isSidechain=True)) == []
    # A background task's completion, which claude writes to the transcript only (the stream never replays it).
    assert message_items(user("u", "<task-notification>\n<task-id>t1</task-id>",
                              origin={"kind": "task-notification", "producer": "session-task"})) == []
    # What a person typed in a terminal session of the same conversation.
    assert message_items(user("u", "hi", origin={"kind": "human"}))[0]["text"] == "hi"


def test_compact_item_reads_both_spellings():
    want = {"kind": "compact", "key": "b1", "trigger": "manual", "pre_tokens": 41454, "post_tokens": 2879}
    assert compact_item("b1", {"trigger": "manual", "preTokens": 41454, "postTokens": 2879}) == want
    assert compact_item("b1", {"trigger": "manual", "pre_tokens": 41454, "post_tokens": 2879}) == want


def test_notice_item_has_a_unique_key():
    assert notice_item("x")["key"] != notice_item("x")["key"]


def test_context_tokens_sums_input_and_cache():
    msg = {"usage": {"input_tokens": 2, "cache_read_input_tokens": 11896, "cache_creation_input_tokens": 29552}}
    assert context_tokens(msg) == 41450
    assert context_tokens({}) is None


def test_framework_ticker(tmp_path):
    root = tmp_path
    (root / "frameworks").mkdir()
    assert framework_ticker("frameworks/NVDA.US.md", root) == "NVDA.US"
    assert framework_ticker(str(root / "frameworks" / "0700.HK.md"), root) == "0700.HK"
    for path in ("frameworks/_TEMPLATE.md", "frameworks/AGENTS.md", "frameworks/README.md",
                 "frameworks/eval/NVDA.US.py", "frameworks/axes/ai-capex.md", "eval/lib/x.md", "/elsewhere/A.md"):
        assert framework_ticker(path, root) is None


def test_text_with_a_candidate_block_carries_the_candidates():
    text = ('看完了\n```lynch-candidates\n{"candidates": [{"ticker": "AAPL.US", "name": "Apple", "sources": ['
            '{"handle": "a", "stance": "bullish", "reason": "r", "quote": "q", '
            '"url": "https://x.com/a/status/1", "posted_at": "2026-10-01T00:00:00Z"}]}]}\n```')
    [item] = message_items(assistant("a1", {"type": "text", "text": text}))
    assert item["text"] == "看完了" and item["candidates"][0]["ticker"] == "AAPL.US"
    assert item["candidates_dropped"] == 0


def test_grok_string_tool_input_becomes_a_dict():
    [item] = message_items(assistant("a1", {"type": "tool_use", "id": "t1", "name": "X search:",
                                            "input": "{'variant': 'XSearch', 'backend': True}"}))
    assert item["input"] == {"value": "{'variant': 'XSearch', 'backend': True}"}


def test_grok_string_is_error_flag():
    ok = message_items(user("u1", [{"type": "tool_result", "tool_use_id": "t1", "content": "x", "is_error": "False"}]))
    bad = message_items(user("u2", [{"type": "tool_result", "tool_use_id": "t2", "content": "x", "is_error": "True"}]))
    assert ok[0]["is_error"] is False and bad[0]["is_error"] is True

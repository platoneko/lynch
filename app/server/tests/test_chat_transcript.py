from __future__ import annotations

import json
from pathlib import Path

import pytest

from lynch_reader.chat.transcript import (EditRejected, current_branch, edit_cut, history, projects_dir_for,
                                          read_entries)


def u(uid, parent, content, **kw):
    return {"type": "user", "uuid": uid, "parentUuid": parent, "message": {"role": "user", "content": content}, **kw}


def a(uid, parent, text=None, tool=None, tokens=None):
    blocks = [{"type": "text", "text": text}] if text else [{"type": "tool_use", "id": tool, "name": "Read", "input": {}}]
    msg = {"role": "assistant", "content": blocks}
    if tokens:
        msg["usage"] = {"input_tokens": tokens, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}
    return {"type": "assistant", "uuid": uid, "parentUuid": parent, "message": msg}


def boundary(uid, logical):
    return {"type": "system", "subtype": "compact_boundary", "uuid": uid, "parentUuid": None,
            "logicalParentUuid": logical, "compactMetadata": {"trigger": "manual", "preTokens": 900, "postTokens": 90}}


META = {"type": "last-prompt", "lastPrompt": "x"}  # trailing metadata: no uuid


def kinds(items):
    return [(i["kind"], i.get("text")) for i in items]


def test_projects_dir_slug():
    assert projects_dir_for(Path("/home/neko/plat1ko/lynch"), Path("/home/neko")) == \
        Path("/home/neko/.claude/projects/-home-neko-plat1ko-lynch")


def test_read_entries_skips_a_torn_last_line(tmp_path):
    p = tmp_path / "c.jsonl"
    p.write_text(json.dumps(u("u1", None, "hi")) + "\n{\"type\": \"assi", encoding="utf-8")
    assert [e["uuid"] for e in read_entries(p)] == ["u1"]


def test_linear_history_with_tool_result_folded():
    entries = [u("u1", None, "hi"), a("a1", "u1", tool="t1"),
               u("r1", "a1", [{"type": "tool_result", "tool_use_id": "t1", "content": "file body"}]),
               a("a2", "r1", text="done", tokens=500), META]
    h = history(current_branch(entries))
    assert kinds(h.items) == [("user", "hi"), ("tool", None), ("text", "done")]
    assert h.items[1]["output"] == "file body"
    assert h.context_tokens == 500 and not h.unfinished


def test_truncated_branch_is_hidden():
    entries = [u("u1", None, "one"), a("a1", "u1", text="r1"),
               u("u2", "a1", "two"), a("a2", "u2", text="r2"),
               u("u3", "a1", "three"), a("a3", "u3", text="r3"), META]  # edited: u3 hangs off a1
    assert kinds(history(current_branch(entries)).items) == [
        ("user", "one"), ("text", "r1"), ("user", "three"), ("text", "r3")]


def test_compaction_hops_back_via_logical_parent():
    entries = [u("u1", None, "one"), a("a1", "u1", text="r1", tokens=900), boundary("b1", "a1"),
               u("s1", "b1", "summary", isCompactSummary=True),
               u("c1", "s1", "<local-command-stdout>Compacted </local-command-stdout>"),
               u("u2", "c1", "two"), a("a2", "u2", text="r2", tokens=120), META]
    h = history(current_branch(entries))
    assert kinds(h.items) == [("user", "one"), ("text", "r1"), ("compact", None), ("user", "two"), ("text", "r2")]
    assert h.context_tokens == 120


def test_context_unknown_right_after_compaction():
    entries = [u("u1", None, "one"), a("a1", "u1", text="r1", tokens=900), boundary("b1", "a1")]
    assert history(current_branch(entries)).context_tokens is None


def test_unfinished_turns():
    assert history(current_branch([u("u1", None, "hi")])).unfinished
    assert history(current_branch([u("u1", None, "hi"), a("a1", "u1", tool="t1")])).unfinished
    stopped = [u("u1", None, "hi"), a("a1", "u1", tool="t1"),
               u("r1", "a1", [{"type": "tool_result", "tool_use_id": "t1", "is_error": True, "content": "no"}]),
               u("i1", "r1", "[Request interrupted by user for tool use]")]
    assert not history(current_branch(stopped)).unfinished


def test_sidechain_tail_does_not_hijack_the_branch():
    entries = [u("u1", None, "one"), a("a1", "u1", text="r1"), u("x", "zz", "side", isSidechain=True)]
    assert kinds(history(current_branch(entries)).items) == [("user", "one"), ("text", "r1")]


def test_edit_cut_is_the_assistant_before_the_message():
    entries = [u("u1", None, "one"), a("a1", "u1", tool="t1"),
               u("r1", "a1", [{"type": "tool_result", "tool_use_id": "t1", "content": "x"}]),
               a("a2", "r1", text="r1"), u("u2", "a2", "two"), a("a3", "u2", text="r2")]
    assert edit_cut(current_branch(entries), "u2") == "a2"


@pytest.mark.parametrize("target", ["u1", "nope", "r1"])
def test_edit_cut_rejects(target):
    entries = [u("u1", None, "one"), a("a1", "u1", tool="t1"),
               u("r1", "a1", [{"type": "tool_result", "tool_use_id": "t1", "content": "x"}])]
    with pytest.raises(EditRejected):
        edit_cut(current_branch(entries), target)  # first message / not on branch / not a user message


def test_edit_cut_rejects_across_compaction():
    entries = [u("u1", None, "one"), a("a1", "u1", text="r1"), u("u2", "a1", "two"), a("a2", "u2", text="r2"),
               boundary("b1", "a2"), u("u3", "b1", "three"), a("a3", "u3", text="r3"), u("u4", "a3", "four")]
    branch = current_branch(entries)
    with pytest.raises(EditRejected):
        edit_cut(branch, "u2")  # before the latest compaction
    with pytest.raises(EditRejected):
        edit_cut(branch, "u3")  # first message after it: no reply to cut at on this side
    assert edit_cut(branch, "u4") == "a3"

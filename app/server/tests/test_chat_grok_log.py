from __future__ import annotations

from lynch_reader.chat import transcript
from lynch_reader.chat.grok_log import GrokLog


def grok_assistant(*content):
    return {"type": "assistant", "uuid": "a-" + str(len(content)), "session_id": "s",
            "message": {"role": "assistant", "content": list(content),
                        "usage": {"input_tokens": 5, "cache_read_input_tokens": 95, "cache_creation_input_tokens": 0}}}


def replay(path):
    entries = transcript.read_entries(path)
    return transcript.history(transcript.current_branch(entries), entries)


def test_turn_reads_back_like_a_claude_transcript(tmp_path):
    path = tmp_path / "grok" / "c1.jsonl"
    log = GrokLog(path)
    log.user("看看")
    log.message(grok_assistant({"type": "tool_use", "id": "t1", "name": "X search:", "input": "{'v': 1}"}))
    log.message({"type": "user", "uuid": "r1", "message": {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "t1", "content": "found", "is_error": "False"}]}})
    log.message(grok_assistant({"type": "text", "text": "有两只"}, {"type": "text", "text": "完"}))
    hist = replay(path)
    assert [(i["kind"], i.get("text")) for i in hist.items] == [
        ("user", "看看"), ("tool", None), ("text", "有两只"), ("text", "完")]
    assert hist.items[1]["output"] == "found" and hist.items[1]["is_error"] is False
    assert hist.context_tokens == 100 and hist.unfinished is False


def test_chain_continues_after_reopening_and_marks_a_stop(tmp_path):
    path = tmp_path / "c1.jsonl"
    first = GrokLog(path).user("one")
    entry = GrokLog(path).interrupted()  # a new GrokLog, as after a server restart
    assert entry["parentUuid"] == first["uuid"]
    assert [(i["kind"], i["text"]) for i in replay(path).items] == [("user", "one"), ("notice", "已停止")]


def test_a_turn_with_no_reply_is_unfinished(tmp_path):
    path = tmp_path / "c1.jsonl"
    GrokLog(path).user("one")
    assert replay(path).unfinished is True

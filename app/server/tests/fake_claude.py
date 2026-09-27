#!/usr/bin/env python3
"""Stands in for `claude -p` with stream-json in and out. Behaviour follows the user message text:

  slow           a tool call, then waits for the next stdin line (interrupt, or an interjection)
  crash          writes "boom" to stderr and exits 3
  /compact ...   compacting status, compact boundary, result
  overage        a rate-limit event with isUsingOverage, then waits like "slow"
  limit          a rejected rate-limit event, then a normal reply
  write <path>   a Write tool call on <path> and its result
  big            a tool result that is one 1 MB line
  anything else  replies "echo: <text>"

It appends user/assistant entries to $FAKE_CLAUDE_PROJECTS/<session>.jsonl the way Claude Code does.
"""
import json
import os
import sys
import uuid


def arg(name):
    return sys.argv[sys.argv.index(name) + 1] if name in sys.argv else None


if os.environ.get("FAKE_CLAUDE_FAIL_START"):
    sys.stderr.write("cannot resume session\n")
    sys.exit(2)
if os.environ.get("FAKE_CLAUDE_LOG"):
    with open(os.environ["FAKE_CLAUDE_LOG"], "a") as f:
        f.write(json.dumps({"argv": sys.argv[1:], "env": sorted(os.environ)}) + "\n")

SESSION = arg("--session-id") or arg("--resume")
PATH = os.path.join(os.environ["FAKE_CLAUDE_PROJECTS"], f"{SESSION}.jsonl")


def last_message_uuid():
    if not os.path.exists(PATH):
        return None
    last = None
    for line in open(PATH):
        e = json.loads(line)
        if e.get("type") in ("user", "assistant", "system"):
            last = e["uuid"]
    return last


parent = arg("--resume-session-at") or last_message_uuid()


def emit(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def record(entry):
    global parent
    entry = {"uuid": str(uuid.uuid4()), "parentUuid": parent, "sessionId": SESSION, **entry}
    parent = entry["uuid"]
    with open(PATH, "a") as f:
        f.write(json.dumps(entry) + "\n")
    return entry


def user(content, replay=False):
    entry = record({"type": "user", "message": {"role": "user", "content": content}})
    emit({**entry, "isReplay": True} if replay else entry)


def assistant(block, tokens=1000):
    usage = {"input_tokens": 1, "cache_read_input_tokens": tokens - 1, "cache_creation_input_tokens": 0}
    emit(record({"type": "assistant", "message": {"role": "assistant", "content": [block], "usage": usage}}))


def result(subtype="success"):
    emit({"type": "result", "subtype": subtype, "modelUsage": {"fake-model": {"contextWindow": 200000}}})


def rate(overage=False, status="allowed"):
    emit({"type": "rate_limit_event", "rate_limit_info": {
        "status": status, "resetsAt": 1790520000, "isUsingOverage": overage,
        "unifiedWindows": {"five_hour": {"utilization": 0.1}, "seven_day": {"utilization": 0.2}}}})


def reply(text):
    emit({"type": "stream_event", "event": {"type": "content_block_delta",
                                            "delta": {"type": "text_delta", "text": text[:3]}}})
    assistant({"type": "text", "text": text})
    result()


def tool(name, tool_input, output):
    tid = f"toolu_{uuid.uuid4().hex[:8]}"
    assistant({"type": "tool_use", "id": tid, "name": name, "input": tool_input})
    user([{"type": "tool_result", "tool_use_id": tid, "content": output}])


def wait_next():
    tid = "toolu_slow"
    assistant({"type": "tool_use", "id": tid, "name": "Bash", "input": {"command": "sleep 60"}})
    line = sys.stdin.readline()
    if not line:
        sys.exit(0)
    msg = json.loads(line)
    if msg.get("type") == "control_request":
        emit({"type": "control_response", "response": {"subtype": "success", "request_id": msg["request_id"]}})
        user([{"type": "tool_result", "tool_use_id": tid, "is_error": True,
               "content": "The user doesn't want to proceed with this tool use."}])
        user("[Request interrupted by user for tool use]")
        result("error_during_execution")
        return
    user([{"type": "tool_result", "tool_use_id": tid, "content": "done"}])
    user(msg["message"]["content"], replay=True)
    reply("both handled")


def compact():
    global parent
    uid = str(uuid.uuid4())
    with open(PATH, "a") as f:
        f.write(json.dumps({"type": "system", "subtype": "compact_boundary", "uuid": uid, "parentUuid": None,
                            "logicalParentUuid": parent, "sessionId": SESSION,
                            "compactMetadata": {"trigger": "manual", "preTokens": 1000, "postTokens": 100}}) + "\n")
    parent = uid
    emit({"type": "system", "subtype": "status", "status": "compacting"})
    emit({"type": "system", "subtype": "compact_boundary", "uuid": uid,
          "compact_metadata": {"trigger": "manual", "pre_tokens": 1000, "post_tokens": 100}})
    emit({"type": "system", "subtype": "status", "status": None, "compact_result": "success"})
    result()


def handle(text):
    if text == "crash":
        sys.stderr.write("boom\n")
        sys.stderr.flush()
        sys.exit(3)
    if text.startswith("/compact"):
        compact()
        return
    rate(overage=text == "overage", status="rejected" if text == "limit" else "allowed")
    user(text, replay=True)
    if text in ("slow", "overage"):
        wait_next()
    elif text.startswith("write "):
        tool("Write", {"file_path": text[len("write "):], "content": "x"}, "File created")
        reply("wrote")
    elif text == "big":
        tool("Read", {"file_path": "big.txt"}, "y" * 1_000_000)
        reply("read")
    else:
        reply(f"echo: {text}")


for line in sys.stdin:
    msg = json.loads(line)
    if msg.get("type") == "control_request":
        request = msg["request"]
        answer = ({"response": {"title": f"title: {request['description']}"}}
                  if request["subtype"] == "generate_session_title" else {})
        emit({"type": "control_response",
              "response": {"subtype": "success", "request_id": msg["request_id"], **answer}})
        continue
    handle(msg["message"]["content"])

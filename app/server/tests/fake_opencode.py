#!/usr/bin/env python3
"""Stands in for `opencode acp`: ACP (JSON-RPC 2.0, one message per line) on stdin / stdout.

Handles initialize, session/new, session/resume (an unknown id is an error), session/set_config_option,
session/cancel and session/prompt; any other request gets -32601. FAKE_OPENCODE_HANG=1 never answers initialize;
FAKE_OPENCODE_EXIT=1 prints "no provider" on stderr and exits 2 before answering it. FAKE_OPENCODE_MODE sets the
mode a new session starts in (default orchestrator).

A prompt's behaviour follows its text:
  slow      a bash call left running; on session/cancel the turn ends `cancelled` without finishing it
  crash     "boom" on stderr, exit 3
  tool      a bash call that completes, then "done"
  skill     an edit of the sediment-framework skill that asks permission; allow completes it, reject fails it and
            ends the turn (as opencode does), cancel fails it as aborted
  other     a bash call that asks permission (a command the user's global config marks ask)
  writefw   a write of frameworks/ZZZ.US.md that completes, then "written"
  fail      a JSON-RPC error on the prompt
  denied    a bash call refused by a rule, then "blocked"
  anything else  "echo: <text>" in two chunks
Every turn but crash / fail sends a usage_update {used 1234, size 1000000, cost 0.01} before it ends.

Sessions live in $FAKE_OPENCODE_SESSIONS/<id>.json ({model, mode}); every launch and request method is appended
to $FAKE_OPENCODE_LOG.
"""
import json
import os
import queue
import sys
import threading
import uuid

SESSIONS = os.environ["FAKE_OPENCODE_SESSIONS"]
LOG = os.environ["FAKE_OPENCODE_LOG"]
os.makedirs(SESSIONS, exist_ok=True)
SKILL_FILE = "frameworks/.claude/skills/sediment-framework/SKILL.md"
MODELS = ["0g/deepseek-v4.1-flash", "mimo/mimo-v2.6-pro"]
REJECTED = "The user rejected permission to use this specific tool call."
RULE_DENIED = "The user has specified a rule which prevents you from using this specific tool call. [rules]"

out_lock = threading.Lock()
answers: dict = {}  # request id → queue for the client's response
cancelled = threading.Event()


def record(obj):
    with open(LOG, "a") as f:
        f.write(json.dumps(obj) + "\n")


def emit(obj):
    with out_lock:
        sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
        sys.stdout.flush()


def reply(rid, result=None, error=None):
    emit({"jsonrpc": "2.0", "id": rid, **({"error": error} if error else {"result": result})})


def update(sid, **u):
    emit({"jsonrpc": "2.0", "method": "session/update", "params": {"sessionId": sid, "update": u}})


def session_path(sid):
    return os.path.join(SESSIONS, f"{sid}.json")


def config_options(s):
    return [{"id": "model", "category": "model", "type": "select", "currentValue": s["model"],
             "options": [{"value": m, "name": m} for m in MODELS]},
            {"id": "mode", "category": "mode", "type": "select", "currentValue": s["mode"],
             "options": [{"value": m, "name": m} for m in ("orchestrator", "build", "plan")]}]


def ask(sid, call_id, raw):
    rid = f"perm-{uuid.uuid4().hex[:6]}"
    answers[rid] = q = queue.Queue()
    emit({"jsonrpc": "2.0", "id": rid, "method": "session/request_permission", "params": {
        "sessionId": sid, "toolCall": {"toolCallId": call_id, "status": "pending", "rawInput": raw},
        "options": [{"optionId": "once", "kind": "allow_once", "name": "Allow once"},
                    {"optionId": "always", "kind": "allow_always", "name": "Always allow"},
                    {"optionId": "reject", "kind": "reject_once", "name": "Reject"}]}})
    while not cancelled.is_set():
        try:
            return q.get(timeout=0.05)
        except queue.Empty:
            continue
    return None  # cancelled while waiting: opencode needs no answer then


def tool_start(sid, call_id, name, kind, raw):
    update(sid, sessionUpdate="tool_call", toolCallId=call_id, title=name, kind=kind, status="pending", rawInput={})
    update(sid, sessionUpdate="tool_call_update", toolCallId=call_id, title=name, kind=kind, status="in_progress",
           rawInput=raw)


def tool_end(sid, call_id, status, **raw_output):
    update(sid, sessionUpdate="tool_call_update", toolCallId=call_id, title="done", kind=None, status=status,
           rawInput=None, rawOutput=raw_output)


def say(sid, *chunks):
    mid = f"msg_{uuid.uuid4().hex[:6]}"
    update(sid, sessionUpdate="agent_thought_chunk", messageId=mid, content={"type": "text", "text": "hmm"})
    for c in chunks:
        update(sid, sessionUpdate="agent_message_chunk", messageId=mid, content={"type": "text", "text": c})


def turn(rid, sid, text, cwd):
    cancelled.clear()
    call_id = f"call_{uuid.uuid4().hex[:8]}"
    stop = "end_turn"
    if text == "crash":
        sys.stderr.write("boom\n")
        sys.stderr.flush()
        os._exit(3)
    if text == "fail":
        reply(rid, error={"code": -32603, "message": "Internal error: Cannot connect to API",
                          "data": {"errorName": "APIError"}})
        return
    if text == "slow":
        tool_start(sid, call_id, "bash", "execute", {"command": "sleep 99", "cwd": cwd})
        cancelled.wait()
        stop = "cancelled"
    elif text == "tool":
        tool_start(sid, call_id, "bash", "execute", {"command": "echo hi", "cwd": cwd})
        tool_end(sid, call_id, "completed", output="hi\n", metadata={"exit": 0})
        say(sid, "done")
    elif text == "denied":
        tool_start(sid, call_id, "bash", "execute", {"command": "git push", "cwd": cwd})
        tool_end(sid, call_id, "failed", error=RULE_DENIED)
        say(sid, "blocked")
    elif text == "writefw":
        path = os.path.join(cwd, "frameworks", "ZZZ.US.md")
        tool_start(sid, call_id, "write", "edit", {"filePath": path, "content": "# ZZZ"})
        tool_end(sid, call_id, "completed", output="Wrote file successfully.")
        say(sid, "written")
    elif text in ("skill", "other"):
        if text == "skill":
            path = os.path.join(cwd, SKILL_FILE)
            tool_start(sid, call_id, "edit", "edit", {"filePath": path, "oldString": "a", "newString": "b"})
            answer = ask(sid, call_id, {"filepath": path, "diff": "--- a\n+++ b"})
        else:
            tool_start(sid, call_id, "bash", "execute", {"command": "chown me x", "cwd": cwd})
            answer = ask(sid, call_id, {"command": "chown me x"})
        option = ((answer or {}).get("outcome") or {}).get("optionId")
        if answer is None:
            tool_end(sid, call_id, "failed", error="Tool execution aborted", metadata={"interrupted": True})
            stop = "cancelled"
        elif option == "once":
            tool_end(sid, call_id, "completed", output="Edit applied successfully.")
            say(sid, "edited")
        else:
            tool_end(sid, call_id, "failed", error=REJECTED)
    else:
        say(sid, "echo: ", text)
    used = 0 if stop == "cancelled" else 1234
    update(sid, sessionUpdate="usage_update", used=used, size=1000000, cost={"amount": 0.01, "currency": "USD"})
    reply(rid, {"stopReason": stop, "usage": {"totalTokens": used}})


def main():
    record({"argv": sys.argv[1:], "env": sorted(os.environ), "cwd": os.getcwd(),
            "config": json.loads(os.environ.get("OPENCODE_CONFIG_CONTENT") or "null")})
    for line in sys.stdin:
        msg = json.loads(line)
        method, rid, params = msg.get("method"), msg.get("id"), msg.get("params") or {}
        if method is None:  # the client's answer to one of our requests
            q = answers.pop(rid, None)
            if q is not None:
                q.put(msg.get("result"))
            continue
        record({"method": method, "params": {k: v for k, v in params.items() if k != "prompt"}})
        if method == "initialize":
            if os.environ.get("FAKE_OPENCODE_EXIT") == "1":
                sys.stderr.write("no provider\n")
                sys.stderr.flush()
                sys.exit(2)
            if os.environ.get("FAKE_OPENCODE_HANG") == "1":
                continue
            reply(rid, {"protocolVersion": 1, "agentCapabilities": {"loadSession": True,
                                                                     "sessionCapabilities": {"resume": {}}}})
        elif method == "session/new":
            sid = f"ses_{uuid.uuid4().hex[:12]}"
            s = {"model": MODELS[0], "mode": os.environ.get("FAKE_OPENCODE_MODE", "orchestrator")}
            with open(session_path(sid), "w") as f:
                json.dump(s, f)
            reply(rid, {"sessionId": sid, "configOptions": config_options(s)})
        elif method == "session/resume":
            try:
                with open(session_path(params["sessionId"])) as f:
                    s = json.load(f)
            except OSError:
                reply(rid, error={"code": -32602, "message": "session not found"})
                continue
            reply(rid, {"configOptions": config_options(s)})
        elif method == "session/set_config_option":
            path = session_path(params["sessionId"])
            with open(path) as f:
                s = json.load(f)
            s[params["configId"]] = params["value"]
            with open(path, "w") as f:
                json.dump(s, f)
            reply(rid, {"configOptions": config_options(s)})
        elif method == "session/cancel":
            cancelled.set()
        elif method == "session/prompt":
            text = params["prompt"][0]["text"]
            threading.Thread(target=turn, args=(rid, params["sessionId"], text, os.getcwd()), daemon=True).start()
        elif rid is not None:
            reply(rid, error={"code": -32601, "message": f"unknown method {method}"})


if __name__ == "__main__":
    main()

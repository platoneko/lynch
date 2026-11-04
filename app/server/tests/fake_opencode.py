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
import base64
import http.server
import time
import urllib.parse
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
    native_message(sid, "assistant", "".join(chunks))
    with state_lock:
        data = native_load(sid)
        data["title"] = "原生自动标题"
        native_save(sid, data)
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


state_lock = threading.RLock()
native_events = []


def native_load(sid):
    with open(session_path(sid)) as file:
        return json.load(file)


def native_save(sid, data):
    with open(session_path(sid), "w") as file:
        json.dump(data, file)


def native_message(sid, role, text, **fields):
    with state_lock:
        data = native_load(sid)
        mid = "msg_" + uuid.uuid4().hex
        message = {
            "info": {"id": mid, "sessionID": sid, "role": role, **fields},
            "parts": [{"type": "text", "text": text}],
        }
        data.setdefault("messages", []).append(message)
        native_save(sid, data)
        return mid


class NativeHttp(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def do_GET(self):
        self.handle_native("GET")

    def do_POST(self):
        self.handle_native("POST")

    def handle_native(self, method):
        wanted = (
            "Basic "
            + base64.b64encode(
                ("opencode:" + os.environ["OPENCODE_SERVER_PASSWORD"]).encode()
            ).decode()
        )
        if self.headers.get("Authorization") != wanted:
            self.send_response(401)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        path = urllib.parse.urlparse(self.path).path
        if path == "/event":
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            index = len(native_events)
            try:
                self.wfile.write(
                    b'data: {"type":"server.connected","properties":{}}\n\n'
                )
                self.wfile.flush()
                while True:
                    for event in native_events[index:]:
                        self.wfile.write(
                            ("data: " + json.dumps(event) + "\n\n").encode()
                        )
                        index += 1
                    self.wfile.write(b": heartbeat\n\n")
                    self.wfile.flush()
                    time.sleep(0.1)
            except (BrokenPipeError, ConnectionResetError):
                return
        parts = path.strip("/").split("/")
        if len(parts) < 2 or parts[0] != "session":
            self.send_json(404, {})
            return
        sid = parts[1]
        with state_lock:
            try:
                data = native_load(sid)
            except OSError:
                self.send_json(404, {})
                return
            if method == "GET":
                self.send_json(
                    200,
                    data.get("messages", [])
                    if parts[-1] == "message"
                    else {"id": sid, "title": data.get("title", "New session")},
                )
                return
            body = json.loads(
                self.rfile.read(int(self.headers.get("Content-Length", 0))) or "{}"
            )
            if parts[-1] == "fork":
                if os.environ.get("FAKE_OPENCODE_FORK_FAIL"):
                    self.send_json(500, {})
                    return
                messages = data.get("messages", [])
                boundary = next(
                    (
                        i
                        for i, m in enumerate(messages)
                        if m["info"]["id"] == body.get("messageID")
                    ),
                    len(messages),
                )
                new = "ses_" + uuid.uuid4().hex[:12]
                copied = json.loads(json.dumps(data))
                copied["messages"] = messages[:boundary]
                for msg in copied["messages"]:
                    msg["info"] = {
                        **msg["info"],
                        "id": "msg_" + uuid.uuid4().hex,
                        "sessionID": new,
                    }
                native_save(new, copied)
                self.send_json(200, {"id": new})
                return
            if parts[-1] == "summarize":
                record({"http": path})
                if os.environ.get("FAKE_OPENCODE_COMPACT_SLOW"):
                    cancelled.wait(2)
                    if cancelled.is_set():
                        self.send_json(200, False)
                        return
                config = json.loads(os.environ.get("OPENCODE_CONFIG_CONTENT") or "{}")
                data["compaction_prompt"] = (
                    config.get("agent", {}).get("compaction", {}).get("prompt", "")
                )
                native_save(sid, data)
                parent = native_message(sid, "user", "")
                data = native_load(sid)
                data["messages"][-1]["parts"] = [
                    {
                        "type": "compaction",
                        "auto": False,
                        "messageID": parent,
                        "sessionID": sid,
                    }
                ]
                native_save(sid, data)
                native_events.append(
                    {
                        "type": "message.part.updated",
                        "properties": {"part": data["messages"][-1]["parts"][0]},
                    }
                )
                native_message(
                    sid,
                    "assistant",
                    "Native summary",
                    summary=True,
                    parentID=parent,
                    agent="compaction",
                    time={"completed": 1},
                )
                native_events.append(
                    {"type": "session.compacted", "properties": {"sessionID": sid}}
                )
                self.send_json(200, True)
                return
            self.send_json(404, {})

    def send_json(self, status, payload):
        raw = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


def main():
    if "--port" in sys.argv:
        port=int(sys.argv[sys.argv.index("--port")+1])
        server=http.server.ThreadingHTTPServer(("127.0.0.1",port),NativeHttp)
        threading.Thread(target=server.serve_forever,daemon=True).start()
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
            native_message(params["sessionId"], "user", text)
            threading.Thread(target=turn, args=(rid, params["sessionId"], text, os.getcwd()), daemon=True).start()
        elif rid is not None:
            reply(rid, error={"code": -32601, "message": f"unknown method {method}"})


if __name__ == "__main__":
    main()

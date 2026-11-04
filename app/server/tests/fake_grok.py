#!/usr/bin/env python3
"""Stands in for `grok`. Two modes:

  grok agent stdio
      the ACP handshake the app uses to make a session (initialize, session/new, session/set_model,
      session/close). FAKE_GROK_ACP_FAIL=<method> answers that call with an error; FAKE_GROK_ACP_HANG=1 never
      answers; FAKE_GROK_ACP_EXIT=1 exits before answering.
  grok ... --single=<text> --resume <id> ...
      one headless turn in streaming-messages-json; behaviour follows the text:
        slow        an X search call, then hangs until killed
        crash       "boom" on stderr, exit 3
        noresult    a reply but no result line, exit 0
        candidates  a reply ending in a lynch-candidates block
        search      an X search call and its result (is_error the string "False", as grok prints it), a reply
        linger      a reply and its result, then 1 s before exiting 0
        maxturns    a partial reply, then an error_max_turns result (is_error true), exit 0
        failed      an error_during_execution result carrying grok's errors list, exit 0
        anything else  "echo: <text>"
      The behaviour keys off the text after the last "## 用户这一轮说" heading (the user's words).

Sessions live in $FAKE_GROK_SESSIONS/<id>.json ({model, window}); a turn on an unknown id fails as grok does.
Every launch is appended to $FAKE_GROK_LOG: argv, env names, and the GROK_* values.
"""
import json
import copy
import os
import queue
import sys
import threading
import time
import uuid

SESSIONS = os.environ["FAKE_GROK_SESSIONS"]
os.makedirs(SESSIONS, exist_ok=True)
with open(os.environ["FAKE_GROK_LOG"], "a") as f:
    f.write(json.dumps({"argv": sys.argv[1:], "env": sorted(os.environ),
                        "grok_env": {k: v for k, v in os.environ.items() if k.startswith("GROK_")},
                        "cwd": os.getcwd()}) + "\n")

CANDIDATES = """KOL 最近提到两只票。

```lynch-candidates
{"candidates": [
 {"ticker": "0700.HK", "name": "腾讯控股", "sources": [{"handle": "alice", "stance": "bullish", "reason": "游戏回暖",
  "quote": "Tencent looks cheap", "url": "https://x.com/alice/status/1", "posted_at": "2026-10-01T08:00:00Z"}]},
 {"ticker": "AXTI.US", "name": "AXT Inc", "sources": [{"handle": "bob", "stance": "neutral", "reason": "值得关注",
  "quote": "AXTI is one to watch", "url": "https://x.com/bob/status/2", "posted_at": "2026-10-01T09:00:00Z"}]}
]}
```"""


def emit(obj):
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def session_path(sid):
    return os.path.join(SESSIONS, f"{sid}.json")


SKILL_FILE = "frameworks/.claude/skills/sediment-framework/SKILL.md"
RULE_DENIED = "The user has specified a rule which prevents you from using this specific tool call. [rules]"
REJECTED = "The user rejected permission to use this specific tool call."
answers: dict = {}
cancelled = threading.Event()
out_lock = threading.Lock()


def acp_record(obj):
    path = os.environ.get("FAKE_GROK_ACP_LOG")
    if not path:
        return
    with open(path, "a") as f:
        f.write(json.dumps(obj) + "\n")


def acp_emit(obj):
    with out_lock:
        sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
        sys.stdout.flush()


def acp_reply(rid, result=None, error=None):
    acp_emit({"jsonrpc": "2.0", "id": rid, **({"error": error} if error else {"result": result})})


def acp_update(sid, **u):
    acp_emit({"jsonrpc": "2.0", "method": "session/update", "params": {"sessionId": sid, "update": u}})


def acp_ask(sid, call_id, raw):
    rid = f"perm-{uuid.uuid4().hex[:6]}"
    answers[rid] = q = queue.Queue()
    acp_emit({"jsonrpc": "2.0", "id": rid, "method": "session/request_permission", "params": {
        "sessionId": sid, "toolCall": {"toolCallId": call_id, "status": "pending", "title": raw.get("title"),
                                       "rawInput": raw},
        "options": [{"optionId": "allow-once", "kind": "allow_once", "name": "Allow once"},
                    {"optionId": "allow-edits-session", "kind": "allow_always", "name": "Allow all edits"},
                    {"optionId": "reject-once", "kind": "reject_once", "name": "Reject"}]}})
    while not cancelled.is_set():
        try:
            return q.get(timeout=0.05)
        except queue.Empty:
            continue
    return None


def acp_tool_start(sid, call_id, title, raw):
    acp_update(sid, sessionUpdate="tool_call", toolCallId=call_id, title=title, rawInput=raw)
    acp_update(sid, sessionUpdate="tool_call_update", toolCallId=call_id, title=title, status="in_progress",
               rawInput={**raw, "variant": {"write": "Write", "search_replace": "SearchReplace",
                                            "run_terminal_command": "Bash", "read_file": "ReadFile"}.get(title)})


def acp_tool_end(sid, call_id, status, **raw_output):
    acp_update(sid, sessionUpdate="tool_call_update", toolCallId=call_id, status=status, rawOutput=raw_output)


def acp_say(sid, *chunks):
    mid = f"msg_{uuid.uuid4().hex[:6]}"
    acp_update(sid, sessionUpdate="agent_thought_chunk", messageId=mid, content={"type": "text", "text": "hmm"})
    for c in chunks:
        acp_update(sid, sessionUpdate="agent_message_chunk", messageId=mid, content={"type": "text", "text": c})


def acp_turn(rid, sid, text, cwd):
    cancelled.clear()
    with open(session_path(sid)) as f:
        session = json.load(f)
    session.setdefault("prompts", []).append(text)
    index = len(session["prompts"]) - 1
    with open(session_path(sid), "w") as f:
        json.dump(session, f)
    acp_update(sid, sessionUpdate="user_message_chunk", content={"type": "text", "text": text},
               _meta={"promptIndex": index})
    call_id = f"call_{uuid.uuid4().hex[:8]}"
    stop = "end_turn"
    if text == "crash":
        sys.stderr.write("boom\n")
        sys.stderr.flush()
        os._exit(3)
    if text == "fail":
        acp_reply(rid, error={"code": -32603, "message": "Internal error: Cannot connect to API"})
        return
    if text == "slow":
        acp_tool_start(sid, call_id, "run_terminal_command", {"command": "sleep 99"})
        cancelled.wait()
        stop = "cancelled"
    elif text == "tool":
        acp_tool_start(sid, call_id, "run_terminal_command", {"command": "echo hi"})
        acp_tool_end(sid, call_id, "completed", output="hi\n")
        acp_say(sid, "done")
    elif text == "denied":
        acp_tool_start(sid, call_id, "run_terminal_command", {"command": "git push"})
        acp_tool_end(sid, call_id, "failed", error=RULE_DENIED)
        acp_say(sid, "blocked")
    elif text == "writefw":
        path = os.path.join(cwd, "frameworks", "ZZZ.US.md")
        acp_tool_start(sid, call_id, "write", {"file_path": path, "content": "# ZZZ"})
        acp_tool_end(sid, call_id, "completed", type="EditsApplied")
        acp_say(sid, "written")
    elif text in ("skill", "skillrel", "other"):
        if text in ("skill", "skillrel"):
            path = SKILL_FILE if text == "skillrel" else os.path.join(cwd, SKILL_FILE)
            raw = {"file_path": path, "old_string": "a", "new_string": "b"}
            acp_tool_start(sid, call_id, "search_replace", raw)
            answer = acp_ask(sid, call_id, {**raw, "title": f"Edit `{path}`"})
        else:
            acp_tool_start(sid, call_id, "run_terminal_command", {"command": "chown me x"})
            answer = acp_ask(sid, call_id, {"command": "chown me x", "title": "Execute `chown me x`"})
        option = ((answer or {}).get("outcome") or {}).get("optionId")
        if answer is None:
            acp_tool_end(sid, call_id, "failed", error="Tool execution aborted")
            stop = "cancelled"
        elif option in ("allow-once", "allow_once", "once"):
            acp_tool_end(sid, call_id, "completed", type="EditsApplied")
            acp_say(sid, "edited")
        else:
            acp_tool_end(sid, call_id, "failed", error=REJECTED)
    else:
        acp_say(sid, "echo: ", text)
    used = 0 if stop == "cancelled" else 1234
    acp_update(sid, sessionUpdate="usage_update", used=used, size=500000, cost={"amount": 0.01, "currency": "USD"})
    acp_reply(rid, {"stopReason": stop, "_meta": {"totalTokens": used, "modelId": "grok-4.6",
                                                  "usage": {"totalTokens": used, "costUsdTicks": 100000000}}})


def acp():
    for line in sys.stdin:
        msg = json.loads(line)
        method, rid = msg.get("method"), msg.get("id")
        if method is None:
            q = answers.pop(rid, None)
            if q is not None:
                q.put(msg.get("result"))
            continue
        if os.environ.get("FAKE_GROK_ACP_EXIT"):
            sys.stderr.write("not logged in\n")
            sys.exit(1)
        if os.environ.get("FAKE_GROK_ACP_HANG"):
            continue
        if os.environ.get("FAKE_GROK_ACP_FAIL") == method:
            acp_emit({"jsonrpc": "2.0", "id": rid, "error": {"code": -32602, "message": "Invalid params"}})
            continue
        params = msg.get("params") or {}
        acp_record({"method": method, "params": params})
        if method == "initialize":
            acp_reply(rid, {"protocolVersion": 1, "agentCapabilities": {
                "loadSession": True, "sessionCapabilities": {"resume": {}, "close": {}}}})
        elif method == "session/new":
            sid = str(uuid.uuid4())
            with open(session_path(sid), "w") as f:
                json.dump({"model": None, "window": 256000, "cwd": params.get("cwd")}, f)
            acp_emit({"jsonrpc": "2.0", "method": "session/update", "params": {"sessionId": sid}})
            acp_reply(rid, {"sessionId": sid, "configOptions": [
                {"id": "model", "currentValue": "grok-4.5"},
                {"id": "reasoning_effort", "currentValue": "high"}]})
        elif method in ("session/resume", "session/load"):
            sid = params.get("sessionId")
            try:
                with open(session_path(sid)) as f:
                    session = json.load(f)
            except OSError:
                acp_reply(rid, error={"code": -32603, "message": "Path not found.",
                                      "data": {"code": "FS_NOT_FOUND"}})
                continue
            if method == "session/load":
                for index, text in enumerate(session.get("prompts", [])):
                    acp_emit({"jsonrpc": "2.0", "method": "session/update", "params": {"sessionId": sid,
                        "_meta": {"isReplay": True}, "update": {"sessionUpdate": "user_message_chunk",
                        "content": {"type": "text", "text": text}, "_meta": {"promptIndex": index}}}})
            acp_reply(rid, {"configOptions": [
                {"id": "model", "currentValue": session.get("model") or "grok-4.6"},
                {"id": "reasoning_effort", "currentValue": "high"}]})
        elif method == "session/set_config_option":
            path = session_path(params["sessionId"])
            with open(path) as f:
                session = json.load(f)
            session[params["configId"]] = params["value"]
            with open(path, "w") as f:
                json.dump(session, f)
            acp_reply(rid, {})
        elif method == "session/set_model":
            with open(session_path(params["sessionId"])) as f:
                session = json.load(f)
            session.update(model=params["modelId"], window=(params.get("_meta") or {}).get("contextWindow"))
            with open(session_path(params["sessionId"]), "w") as f:
                json.dump(session, f)
            acp_reply(rid, {"_meta": {"model": {"Ok": params["modelId"]}}})
        elif method == "session/close":
            acp_reply(rid, {})
        elif method == "_x.ai/rewind/points":
            with open(session_path(params["sessionId"])) as f:
                session = json.load(f)
            acp_reply(rid, {"rewind_points": [{"prompt_index": i, "prompt_preview": t[:60]}
                        for i, t in enumerate(session.get("prompts", []))]})
        elif method == "_x.ai/session/fork":
            if os.environ.get("FAKE_GROK_SOURCE_FORK_FAIL"):
                acp_reply(rid, error={"code": -32603, "message": "fork failed"})
                continue
            with open(session_path(params["sourceSessionId"])) as f:
                session = copy.deepcopy(json.load(f))
            sid = str(uuid.uuid4())
            with open(session_path(sid), "w") as f:
                json.dump(session, f)
            acp_reply(rid, {"newSessionId": sid})
        elif method == "_x.ai/rewind/execute":
            assert params["mode"] == "conversation_only" and params["force"] is True
            path = session_path(params["sessionId"])
            with open(path) as f:
                session = json.load(f)
            session["prompts"] = session.get("prompts", [])[:params["targetPromptIndex"]]
            with open(path, "w") as f:
                json.dump(session, f)
            acp_reply(rid, {"success": True})
        elif method == "_x.ai/compact_conversation":
            acp_emit({"jsonrpc": "2.0", "method": "_x.ai/session_notification", "params": {
                "sessionId": params["sessionId"], "update": {"sessionUpdate": "auto_compact_completed",
                "tokens_before": 100, "tokens_after": 50}}})
            acp_reply(rid, {})
        elif method == "session/list":
            acp_reply(rid, {"sessions": []})
        elif method == "session/cancel":
            cancelled.set()
        elif method == "session/prompt":
            text = params["prompt"][0]["text"]
            threading.Thread(target=acp_turn, args=(rid, params["sessionId"], text, os.getcwd()), daemon=True).start()
        elif rid is not None:
            acp_reply(rid, error={"code": -32601, "message": "Method not found"})


def arg(name):
    return sys.argv[sys.argv.index(name) + 1] if name in sys.argv else None


def turn():
    prompt = next(a.split("=", 1)[1] for a in sys.argv if a.startswith("--single="))
    text = prompt.rsplit("## 用户这一轮说\n", 1)[-1].strip()  # the app's message ends with the user's words
    sid = arg("--resume")
    if not sid or not os.path.exists(session_path(sid)):
        sys.stderr.write(f"session not found: {sid}\n")
        sys.exit(1)
    bootstrap = prompt.startswith("以下内容是压缩前")
    summarize = prompt.startswith("请把当前原生上下文")
    emit({"type": "system", "subtype": "init", "session_id": sid,
          "tools": [] if (bootstrap or summarize) and not (bootstrap and os.environ.get("FAKE_GROK_BOOTSTRAP_TOOLS")) and arg("--tools") == "todo_write" and
          "todo_write" in (arg("--disallowed-tools") or "").split(",") else ["search_tool", "use_tool", "web_search", "web_fetch"]})

    def assistant(content, tokens=1000):
        usage = {"input_tokens": min(tokens, 1), "cache_read_input_tokens": max(tokens - 1, 0),
                 "cache_creation_input_tokens": 0}
        emit({"type": "assistant", "uuid": str(uuid.uuid4()), "session_id": sid,
              "message": {"role": "assistant", "content": content, "usage": usage}})

    def x_search():
        tid = f"ctc_{uuid.uuid4().hex[:8]}"
        assistant([{"type": "tool_use", "id": tid, "name": "X search:",
                    "input": "{'variant': 'XSearch', 'backend': True}"}], tokens=0)
        return tid

    def reply(t):
        emit({"type": "stream_event", "session_id": sid,
              "event": {"type": "content_block_delta", "delta": {"type": "text_delta", "text": t[:3]}}})
        assistant([{"type": "text", "text": t}])

    def result(subtype="success", **extra):
        emit({"type": "result", "subtype": subtype, "is_error": subtype != "success", "total_cost_usd": 0.01,
              "session_id": sid, "modelUsage": {"grok-4.6-build": {"costUSD": 0.01}}, **extra})

    if bootstrap or summarize:
        if bootstrap and os.environ.get("FAKE_GROK_BOOTSTRAP_TOOL_USE"):
            assistant([{"type": "tool_use", "id": "unexpected", "name": "Read", "input": {"file_path": "bad"}}])
            return
        if ((bootstrap and os.environ.get("FAKE_GROK_BOOTSTRAP_FAIL"))
                or (summarize and os.environ.get("FAKE_GROK_SUMMARY_FAIL"))):
            result("error_during_execution", errors=["bootstrap failed"])
            return
        with open(session_path(sid)) as f:
            session = json.load(f)
        session.setdefault("prompts", []).append(prompt)
        with open(session_path(sid), "w") as f:
            json.dump(session, f)
        reply("SUMMARY: 必须保留现金流假设" if summarize else "已接收")
        result()
        return

    if text == "crash":
        sys.stderr.write("boom\n")
        sys.exit(3)
    if text == "slow":
        x_search()
        time.sleep(60)
        return
    if text == "search":
        tid = x_search()
        emit({"type": "user", "uuid": str(uuid.uuid4()), "session_id": sid, "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": tid, "content": '{"name":"x_keyword_search"}', "is_error": "False"}]}})
        reply("searched")
        result()
        return
    if text == "noresult":
        reply("half")
        return
    if text == "maxturns":
        reply("searched half the KOLs")
        result("error_max_turns", errors=["Reached maximum number of turns (40)"])
        return
    if text == "failed":
        result("error_during_execution", errors=["X search backend unavailable"])
        return
    if text == "linger":  # grok finishing up after its result, before it exits
        reply("echo: linger")
        result()
        time.sleep(1)
        return
    reply(CANDIDATES if text == "candidates" else f"echo: {text}")
    result()


if "agent" in sys.argv and "stdio" in sys.argv:
    acp()
else:
    turn()

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

Sessions live in $FAKE_GROK_SESSIONS/<id>.json ({model, window}); a turn on an unknown id fails as grok does.
Every launch is appended to $FAKE_GROK_LOG: argv, env names, and the GROK_* values.
"""
import json
import os
import sys
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
 {"ticker": "AXTI.US", "name": "AXT Inc", "sources": [{"handle": "bob", "stance": "bearish", "reason": "估值过高",
  "quote": "AXTI is stretched", "url": "https://x.com/bob/status/2", "posted_at": "2026-10-01T09:00:00Z"}]}
]}
```"""


def emit(obj):
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def session_path(sid):
    return os.path.join(SESSIONS, f"{sid}.json")


def acp():
    for line in sys.stdin:
        msg = json.loads(line)
        method, rid = msg.get("method"), msg.get("id")
        if os.environ.get("FAKE_GROK_ACP_EXIT"):
            sys.stderr.write("not logged in\n")
            sys.exit(1)
        if os.environ.get("FAKE_GROK_ACP_HANG"):
            continue
        if os.environ.get("FAKE_GROK_ACP_FAIL") == method:
            emit({"jsonrpc": "2.0", "id": rid, "error": {"code": -32602, "message": "Invalid params"}})
            continue
        params = msg.get("params") or {}
        if method == "initialize":
            result = {"protocolVersion": 1}
        elif method == "session/new":
            sid = str(uuid.uuid4())
            with open(session_path(sid), "w") as f:
                json.dump({"model": None, "window": 256000, "cwd": params.get("cwd")}, f)
            emit({"jsonrpc": "2.0", "method": "session/update", "params": {"sessionId": sid}})  # a notification first
            result = {"sessionId": sid}
        elif method == "session/set_model":
            with open(session_path(params["sessionId"])) as f:
                session = json.load(f)
            session.update(model=params["modelId"], window=(params.get("_meta") or {}).get("contextWindow"))
            with open(session_path(params["sessionId"]), "w") as f:
                json.dump(session, f)
            result = {}
        elif method == "session/close":
            result = {}
        else:
            emit({"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": "Method not found"}})
            continue
        emit({"jsonrpc": "2.0", "id": rid, "result": result})


def arg(name):
    return sys.argv[sys.argv.index(name) + 1] if name in sys.argv else None


def turn():
    text = next(a.split("=", 1)[1] for a in sys.argv if a.startswith("--single="))
    sid = arg("--resume")
    if not sid or not os.path.exists(session_path(sid)):
        sys.stderr.write(f"session not found: {sid}\n")
        sys.exit(1)
    emit({"type": "system", "subtype": "init", "session_id": sid,
          "tools": ["search_tool", "use_tool", "web_search", "web_fetch"]})

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


if sys.argv[1:3] == ["agent", "stdio"]:
    acp()
else:
    turn()

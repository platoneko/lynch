"""A strict JSONL app-server peer; session state is real files, generation is deterministic."""
from __future__ import annotations

import json
import os
import sys
import time
import uuid
from pathlib import Path

sessions = Path(os.environ["FAKE_CODEX_SESSIONS"])
sessions.mkdir(parents=True, exist_ok=True)
log = Path(os.environ["FAKE_CODEX_LOG"])
log.parent.mkdir(parents=True, exist_ok=True)
thread = None
turn = None
previous_turn = None
pending = None
initialized = False


def emit(obj):
    print(json.dumps(obj), flush=True)


def event(method, **params):
    identity = {"threadId": thread, **({} if method.startswith("turn/") else {"turnId": turn})}
    emit({"method": method, "params": {**identity, **params}})


def reply(request, result):
    emit({"id": request["id"], "result": result})


def finish(status="completed", error=None):
    event("turn/completed", turn={"id": turn, "status": status, "error": error})


for line in sys.stdin:
    request = json.loads(line)
    with log.open("a") as stream:
        stream.write(json.dumps(request) + "\n")
    method = request.get("method")
    params = request.get("params") or {}
    if method == "initialize":
        if os.environ.get("FAKE_CODEX_HANG_INIT"):
            continue
        if os.environ.get("FAKE_CODEX_FAIL_SETUP"):
            emit({"id": request["id"], "error": {"code": -1, "message": "no provider"}})
        else:
            reply(request, {"userAgent": "fake-codex"})
    elif method == "initialized":
        initialized = True
    elif method:
        if not initialized:
            emit({"id": request["id"], "error": {"code": -1, "message": "Not initialized"}})
            continue
        if method == "account/read":
            missing = os.environ.get("FAKE_CODEX_ACCOUNT") in ("missing", "external")
            reply(request, {"account": None if missing else {"type": "chatgpt"},
                            "requiresOpenaiAuth": os.environ.get("FAKE_CODEX_ACCOUNT") != "external"})
        elif method == "model/list":
            other = params.get("cursor") == "page2"
            reply(request, {"data": [{"id": "gpt-other" if other else "gpt-test",
                "model": "gpt-other" if other else "gpt-test", "displayName": "GPT Other" if other else "GPT Test",
                "defaultReasoningEffort": "medium" if other else "low", "isDefault": not other,
                "supportedReasoningEfforts": [{"reasoningEffort": e} for e in (["medium"] if other else ["low", "high"])]}],
                "nextCursor": None if other else "page2"})
        elif method in ("thread/start", "thread/resume"):
            thread = params.get("threadId") or f"thr_{uuid.uuid4().hex}"
            path = sessions / f"{thread}.json"
            if method == "thread/resume" and not path.exists():
                emit({"id": request["id"], "error": {"code": -1, "message": "thread not found"}})
            else:
                if not path.exists():
                    path.write_text("[]")
                reply(request, {"thread": {"id": thread}})
        elif method == "turn/start":
            previous_turn = turn
            turn = f"turn_{uuid.uuid4().hex}"
            text = params["input"][0]["text"]
            path = sessions / f"{thread}.json"
            history = json.loads(path.read_text())
            history.append(text)
            path.write_text(json.dumps(history))
            if text == "hang-turn":
                continue
            if text == "slow-start":
                time.sleep(0.4)
            reply(request, {"turn": {"id": turn, "status": "inProgress"}})
            if text == "stale" and previous_turn:
                event("turn/started", turn={"id": previous_turn, "status": "inProgress"})
            event("turn/started", turn={"id": turn, "status": "inProgress"})
            if text == "stale" and previous_turn:
                event("turn/completed", turn={"id": previous_turn, "status": "completed", "error": None})
            if os.environ.get("FAKE_CODEX_CRASH"):
                print("fake codex boom", file=sys.stderr, flush=True)
                sys.exit(1)
            if text == "wait":
                continue
            if text in ("skill", "move-skill") or text.startswith("patch:"):
                patch_path = ("frameworks/.claude/skills/sediment-framework/probe.md" if text in ("skill", "move-skill")
                              else text.removeprefix("patch:"))
                item = {"id": "patch1", "type": "fileChange", "status": "inProgress", "changes": [
                    {"path": str(Path.cwd() / patch_path),
                     "kind": {"type": "add"}, "diff": "+probe\n"}]}
                if text == "move-skill":
                    item["changes"][0].update({"path": str(Path.cwd() / "frameworks/source.md"),
                        "kind": {"type": "update", "move_path": str(Path.cwd() / patch_path)}})
                event("item/started", item=item)
                emit({"id": "approval1", "method": "item/fileChange/requestApproval", "params": {
                    "threadId": thread, "turnId": turn, "itemId": "patch1"}})
                pending = item
                continue
            if text.startswith("command:"):
                command = text.removeprefix("command:")
                item = {"id": "cmd1", "type": "commandExecution", "command": command, "cwd": str(Path.cwd()),
                        "status": "inProgress"}
                event("item/started", item=item)
                emit({"id": "approval1", "method": "item/commandExecution/requestApproval", "params": {
                    "threadId": thread, "turnId": turn, "itemId": "cmd1", "command": command}})
                pending = item
                continue
            if text == "tool":
                item = {"id": "cmd1", "type": "commandExecution", "command": "echo live", "cwd": str(Path.cwd()),
                        "status": "inProgress"}
                event("item/started", item=item)
                event("item/completed", item={**item, "status": "completed", "aggregatedOutput": "live\n", "exitCode": 0})
            if text == "error":
                finish("failed", {"message": "generation failed"})
                continue
            response = "echo: " + text
            event("item/started", item={"type": "agentMessage", "id": "message1", "text": ""})
            event("item/agentMessage/delta", itemId="message1", delta="echo: ")
            event("item/agentMessage/delta", itemId="message1", delta=text)
            event("item/completed", item={"type": "agentMessage", "id": "message1", "text": response})
            event("thread/tokenUsage/updated", tokenUsage={"last": {"inputTokens": 1200, "totalTokens": 1234},
                  "total": {"totalTokens": 2468}, "modelContextWindow": 1000000})
            finish()
        elif method == "turn/interrupt":
            reply(request, {})
            finish("interrupted")
        elif method == "thread/compact/start":
            assert params == {"threadId": thread}
            mode = os.environ.get("FAKE_CODEX_COMPACT", "ok")
            if mode == "reject":
                emit({"id": request["id"], "error": {"code": -1, "message": "compaction unavailable"}})
                continue
            if mode == "hang":
                continue
            if mode == "slow-ack":
                time.sleep(0.4)
            previous_turn = turn
            turn = f"compact_{uuid.uuid4().hex}"
            if mode != "before-ack":
                reply(request, {})
            if mode == "stale" and previous_turn:
                event("turn/started", turn={"id": previous_turn, "status": "inProgress"})
                emit({"id": "stale-approval", "method": "item/commandExecution/requestApproval", "params": {
                    "threadId": thread, "turnId": previous_turn, "itemId": "old-command", "command": "echo stale"}})
                event("turn/completed", turn={"id": previous_turn, "status": "completed", "error": None})
            event("turn/started", turn={"id": turn, "status": "inProgress"})
            item = {"id": "compact1", "type": "contextCompaction"}
            event("item/started", item=item)
            if mode == "wait":
                continue
            if mode == "crash":
                print("compaction crashed", file=sys.stderr, flush=True)
                sys.exit(1)
            if mode == "fail":
                finish("failed", {"message": "compaction failed"})
            else:
                event("item/completed", item=item)
                event("item/completed", item=item)
                finish()
            if mode == "before-ack":
                reply(request, {})
        else:
            emit({"id": request.get("id"), "error": {"code": -32601, "message": "Unsupported method"}})
    elif request.get("id") == "approval1" and pending:
        accepted = request.get("result", {}).get("decision") == "accept"
        if accepted and pending["type"] == "fileChange":
            change = pending["changes"][0]
            dest = Path(change["kind"].get("move_path") or change["path"])
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text("probe\n")
        event("item/completed", item={**pending, "status": "completed" if accepted else "declined"})
        pending = None
        finish()

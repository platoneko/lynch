"""Opt-in local app-server protocol check: LYNCH_LIVE_CODEX=1 python -m pytest tests/test_chat_codex_live.py.

Uses the user's configured Codex service and a disposable repository under pytest's base directory. Run with
--basetemp=../../_local/tmp/pytest-codex-live. No real framework is changed; model responses use the service quota.
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import uuid
from dataclasses import asdict
from types import SimpleNamespace

import pytest

from lynch_reader.chat.app_log import AppLog
from lynch_reader.chat.codex import CodexConfig, codex_env
from lynch_reader.chat.codex_link import CodexLink, discover_models
from tests.chat_helpers import LinkRecorder, conv

pytestmark = pytest.mark.skipif(os.environ.get("LYNCH_LIVE_CODEX") != "1", reason="real Codex, opt-in")


def test_local_codex_stream_patch_approval_and_resume(tmp_path, monkeypatch):
    async def main():
        cfg = CodexConfig((shutil.which("codex") or "codex",), tmp_path, tmp_path / "app-logs")
        env = codex_env(os.environ)
        models = await discover_models(cfg, env)
        assert models
        chosen = next((m for m in models if m.is_default), models[0])
        c = SimpleNamespace(**{**asdict(conv(agent="codex", kind="research", ticker=None,
                                             model=chosen.id, effort=chosen.default_effort)), "codex_thread": None})
        rec = LinkRecorder()
        link = CodexLink(cfg, c, env, AppLog(cfg.logs / "live.jsonl"), rec.events())
        child_requests, replies = set(), []
        on_request, reply = link._on_request, link._rpc.reply

        def record_request(rpc_id, method, params):
            if params.get("threadId") != link._thread and method == "item/commandExecution/requestApproval":
                child_requests.add(rpc_id)
            on_request(rpc_id, method, params)

        def record_reply(rpc_id, result=None, **kwargs):
            replies.append((rpc_id, result))
            reply(rpc_id, result, **kwargs)

        monkeypatch.setattr(link, "_on_request", record_request)
        monkeypatch.setattr(link._rpc, "reply", record_reply)

        async def turn(active, recorder, text):
            ends, answered = len(recorder.turn_ends), len(recorder.permissions)
            await active.prompt(text)
            async with asyncio.timeout(180):
                while len(recorder.turn_ends) == ends:
                    while answered < len(recorder.permissions):
                        await active.answer(recorder.permissions[answered][0], False)
                        answered += 1
                    await asyncio.sleep(0.05)
            assert recorder.turn_ends[-1][1] is None, recorder.turn_ends[-1]

        async def check_filings(active, recorder):
            before = len(recorder.entries)
            await turn(active, recorder, "这是临时目录协议测试。只运行 longbridge filing AAPL.US --count 1 --format json，"
                       "使用默认沙箱权限，不申请沙箱外执行，不重试，不读其他文件。然后简短报告结果。")
            outputs = [str(block.get("content", "")) for entry in recorder.entries[before:]
                       for block in entry["message"]["content"]
                       if isinstance(block, dict) and block.get("type") == "tool_result"]
            assert any('"title":' in output and '"file_urls":' in output for output in outputs), outputs

        async def check_cache(active, recorder):
            await turn(active, recorder, "这是临时目录协议测试。只运行 python3 frameworks/eval/probe.py，"
                       "这是演算脚本，正常运行会写缓存。不要主动申请额外权限，不读其他文件。简短报告输出。")
            assert (tmp_path / "frameworks/_local/tmp/cache.json").read_text() == "cache-ok"
            assert (tmp_path.parent / "tool-cache/probe.json").read_text() == "cache-ok"

        (tmp_path / "frameworks/_local/tmp").mkdir(parents=True)
        (tmp_path / "frameworks/eval").mkdir()
        (tmp_path / "frameworks/eval/probe.py").write_text(
            "from pathlib import Path\n"
            "root = Path(__file__).resolve().parents[2]\n"
            "for path in (root / 'frameworks/_local/tmp/cache.json', root.parent / 'tool-cache/probe.json'):\n"
            "    path.parent.mkdir(parents=True, exist_ok=True)\n"
            "    path.write_text('cache-ok')\n"
            "print('cache-ok')\n")
        (tmp_path / "frameworks/review.txt").write_text("review-token: LIME-47\n")
        skill = tmp_path / "frameworks/.claude/skills/sediment-framework"
        skill.mkdir(parents=True)
        (tmp_path / "frameworks/AGENTS.md").write_text("测试目录，不是真实投资框架。只做用户要求的协议探测。\n")
        (skill / "SKILL.md").write_text("---\nname: sediment-framework\ndescription: Test fixture\n---\n只做测试。\n")
        try:
            c.codex_thread = await link.start()
            await turn(link, rec, "这是临时目录协议测试。记住口令 MANGO-73。不要运行工具，仅回复该口令。")
            assert "MANGO-73" in "".join(rec.deltas)
            await turn(link, rec, "这是临时目录协议测试。只运行一条命令 python3 -c \"print('live-shell-ok')\"，"
                                  "然后简短回复输出，不要读其他文件。")
            assert any("live-shell-ok" in str(block.get("content", ""))
                       for entry in rec.entries for block in entry["message"]["content"]
                       if isinstance(block, dict) and block.get("type") == "tool_result")
            await check_filings(link, rec)
            await check_cache(link, rec)
            await turn(link, rec, "这是临时目录协议测试，可以落盘。仅用 apply_patch 创建 "
                                  "frameworks/_local/tmp/probe.txt，内容为 live-ok 一行；不要使用 shell 写入，不要提交。")
            assert (tmp_path / "frameworks/_local/tmp/probe.txt").read_text().strip() == "live-ok"
            await turn(link, rec, "这是临时目录协议测试，可以修改 skill。仅用 apply_patch 创建 "
                                  "frameworks/.claude/skills/sediment-framework/probe.txt，内容 x 一行。"
                                  "若审批拒绝就说明拒绝并结束，不要重试或绕过。")
            assert rec.permissions, "native skill patch did not ask the app"
            assert not (skill / "probe.txt").exists()
            await turn(link, rec, "这是临时目录协议测试。请派一个子 agent（用当前模型），只运行 "
                       "python3 -c \"from pathlib import Path; print(Path('frameworks/review.txt').read_text())\" "
                       "并回复文件里的口令。你自己不要读取文件。等子 agent 完成后简短报告口令。")
            assert "LIME-47" in "".join(rec.deltas)
            assert link._children, "the model did not create a child thread"
            assert any(rpc_id in child_requests and result == {"decision": "accept"}
                       for rpc_id, result in replies), "no child command approval was accepted"
            assert not rec.turn_ends[-1][1]
        finally:
            await link.close()
        rec2 = LinkRecorder()
        resumed = CodexLink(cfg, c, env, AppLog(cfg.logs / "live.jsonl"), rec2.events())
        try:
            assert await resumed.start() == c.codex_thread
            assert rec2.entries == []
            await turn(resumed, rec2, "我一开始说的口令是什么？不要运行工具，仅回复口令。")
            assert "MANGO-73" in "".join(rec2.deltas)
            await check_filings(resumed, rec2)
            (tmp_path / "frameworks/_local/tmp/cache.json").unlink()
            (tmp_path.parent / "tool-cache/probe.json").unlink()
            await check_cache(resumed, rec2)
        finally:
            await resumed.close()
        print(f"Codex {chosen.id}: stream, shell, live filings before/after resume, file approval and rejected skill patch verified")

    asyncio.run(main())


def test_local_codex_reused_child_reads_across_turns_and_resume(tmp_path, monkeypatch):
    async def main():
        cfg = CodexConfig((shutil.which("codex") or "codex",), tmp_path, tmp_path / "app-logs")
        env = codex_env(os.environ)
        models = await discover_models(cfg, env)
        chosen = next((m for m in models if m.is_default), models[0])
        c = SimpleNamespace(**{**asdict(conv(agent="codex", kind="research", ticker=None,
                                             model=chosen.id, effort=chosen.default_effort)), "codex_thread": None})
        (tmp_path / "frameworks").mkdir()
        tokens = [uuid.uuid4().hex for _ in range(3)]
        for n, token in enumerate(tokens):
            (tmp_path / f"frameworks/review-{n}.txt").write_text(token)
        trace_path = tmp_path / "child-events.jsonl"
        reader_thread = None

        def open_recorded_link():
            rec = LinkRecorder()
            link = CodexLink(cfg, c, env, AppLog(cfg.logs / "reuse.jsonl"), rec.events())
            requests, replies = {}, {}
            on_message, reply = link._on_message, link._rpc.reply

            def record_message(message):
                params = message.get("params") or {}
                if message.get("method", "").startswith(("thread/", "turn/", "item/")):
                    with trace_path.open("a") as stream:
                        stream.write(json.dumps(message, ensure_ascii=False) + "\n")
                if "id" in message and message.get("method") == "item/commandExecution/requestApproval":
                    requests[message["id"]] = params.get("threadId")
                on_message(message)

            def record_reply(rpc_id, result=None, **kwargs):
                replies[rpc_id] = (result, kwargs)
                reply(rpc_id, result, **kwargs)

            monkeypatch.setattr(link._rpc, "_message", record_message)
            monkeypatch.setattr(link._rpc, "reply", record_reply)
            return link, rec, requests, replies

        async def check_read(link, rec, requests, replies, n):
            nonlocal reader_thread
            ends, entries = len(rec.turn_ends), len(rec.entries)
            requests.clear()
            replies.clear()
            action = ("派一个名为 review_reader 的子 agent，沿用当前模型" if n == 0 else
                      "用 followup_task 再次唤起已有的 review_reader 子 agent，不要新建子 agent")
            await link.prompt(f"这是临时目录权限回归测试。{action}，让它只执行一条命令："
                              f"python3 -c \"from pathlib import Path; print(Path('frameworks/review-{n}.txt').read_text())\"。"
                              "你自己不要读取文件。等它完成，回复文件中的口令。若读取被拒绝，报告拒绝并结束，不绕过。")
            async with asyncio.timeout(180):
                while len(rec.turn_ends) == ends:
                    await asyncio.sleep(0.05)
            assert rec.turn_ends[-1][1] is None, rec.turn_ends[-1]
            approved = {thread for rpc_id, thread in requests.items()
                        if thread != link._thread and replies.get(rpc_id) == ({"decision": "accept"}, {})}
            if n == 0:
                assert len(approved) == 1, (n, requests, replies)
                reader_thread = approved.pop()
            else:
                assert approved == {reader_thread}, (n, reader_thread, requests, replies)
            assert any(tokens[n] in str(block.get("content", "")) and not block.get("is_error")
                       for entry in rec.entries[entries:] if entry.get("parent_tool_use_id")
                       for block in entry["message"]["content"] if block.get("type") == "tool_result")

        link, rec, requests, replies = open_recorded_link()
        try:
            c.codex_thread = await link.start()
            await check_read(link, rec, requests, replies, 0)
            await check_read(link, rec, requests, replies, 1)
        finally:
            await link.close()
        resumed, rec, requests, replies = open_recorded_link()
        try:
            assert await resumed.start() == c.codex_thread
            await check_read(resumed, rec, requests, replies, 2)
        finally:
            await resumed.close()

    asyncio.run(main())

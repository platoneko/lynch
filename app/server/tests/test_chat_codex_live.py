"""Opt-in local app-server protocol check: LYNCH_LIVE_CODEX=1 python -m pytest tests/test_chat_codex_live.py.

Uses the user's configured Codex service and a disposable repository under pytest's base directory. Run with
--basetemp=../../_local/tmp/pytest-codex-live. No real framework is changed; model responses use the service quota.
"""
from __future__ import annotations

import asyncio
import os
import shutil
from dataclasses import asdict
from types import SimpleNamespace

import pytest

from lynch_reader.chat.app_log import AppLog
from lynch_reader.chat.codex import CodexConfig, codex_env
from lynch_reader.chat.codex_link import CodexLink, discover_models
from tests.chat_helpers import LinkRecorder, conv

pytestmark = pytest.mark.skipif(os.environ.get("LYNCH_LIVE_CODEX") != "1", reason="real Codex, opt-in")


def test_local_codex_stream_patch_approval_and_resume(tmp_path):
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

        (tmp_path / "frameworks/_local/tmp").mkdir(parents=True)
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
            await turn(link, rec, "这是临时目录协议测试，可以落盘。仅用 apply_patch 创建 "
                                  "frameworks/_local/tmp/probe.txt，内容为 live-ok 一行；不要使用 shell 写入，不要提交。")
            assert (tmp_path / "frameworks/_local/tmp/probe.txt").read_text().strip() == "live-ok"
            await turn(link, rec, "这是临时目录协议测试，可以修改 skill。仅用 apply_patch 创建 "
                                  "frameworks/.claude/skills/sediment-framework/probe.txt，内容 x 一行。"
                                  "若审批拒绝就说明拒绝并结束，不要重试或绕过。")
            assert rec.permissions, "native skill patch did not ask the app"
            assert not (skill / "probe.txt").exists()
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
        finally:
            await resumed.close()
        print(f"Codex {chosen.id}: stream, shell, live filings before/after resume, file approval and rejected skill patch verified")

    asyncio.run(main())

"""Opt-in native compaction in a disposable thread; never operates on a real app conversation."""
from __future__ import annotations

import asyncio
import os
import shutil
from dataclasses import replace

import pytest

from lynch_reader.chat import transcript
from lynch_reader.chat.app_log import AppLog
from lynch_reader.chat.codex import DEFAULT_CODEX_MODEL, CodexConfig, codex_env
from lynch_reader.chat.codex_link import CodexLink, discover_models
from tests.chat_helpers import LinkRecorder, conv, until

pytestmark = pytest.mark.skipif(os.environ.get("LYNCH_LIVE_CODEX") != "1", reason="real Codex, opt-in")


def test_native_compaction_and_resumed_context(tmp_path):
    async def main():
        cfg = CodexConfig((shutil.which("codex") or "codex",), tmp_path, tmp_path / "app-logs")
        env = codex_env(os.environ)
        models = await discover_models(cfg, env)
        chosen = next((m for m in models if m.id == DEFAULT_CODEX_MODEL), models[0])
        c = conv(agent="codex", model=chosen.id, effort=chosen.default_effort)
        rec = LinkRecorder()
        path = cfg.logs / "compact.jsonl"
        link = CodexLink(cfg, c, env, AppLog(path), rec.events())
        try:
            thread = await link.start()
            await link.prompt("这是临时协议测试。请记住口令 CYAN-42，不运行工具，只回复已记住。")
            await until(lambda: len(rec.turn_ends) == 1, timeout=120)
            assert rec.turn_ends[-1][1] is None
            await link.compact()
            await until(lambda: len(rec.turn_ends) == 2, timeout=120)
            assert rec.turn_ends[-1] == ("completed", None)
            entries = transcript.read_entries(path)
            history = transcript.history(transcript.current_branch(entries), entries)
            assert history.items[-1]["kind"] == "compact"
            assert history.items[-1]["trigger"] == "manual"
        finally:
            await link.close()
        resumed_rec = LinkRecorder()
        resumed = CodexLink(cfg, replace(c, codex_thread=thread), env, AppLog(path), resumed_rec.events())
        try:
            assert await resumed.start() == thread
            await resumed.prompt("我刚才的口令是什么？不运行工具，只回复口令。")
            await until(lambda: bool(resumed_rec.turn_ends), timeout=120)
            assert resumed_rec.turn_ends[-1][1] is None
            assert "CYAN-42" in "".join(resumed_rec.deltas)
        finally:
            await resumed.close()
    asyncio.run(main())

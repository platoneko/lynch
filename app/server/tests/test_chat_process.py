from __future__ import annotations

import asyncio
import json
import logging
import os
import sys

import pytest

from lynch_reader.chat.process import ChatProcess
from tests.chat_helpers import FAKE_CMD, until


def spawn(tmp_path, **env):
    got, exits = [], []
    argv = [*FAKE_CMD, "-p", "--session-id", "s1"]
    proc = ChatProcess(argv, {**os.environ, "FAKE_CLAUDE_PROJECTS": str(tmp_path), **env}, tmp_path,
                       on_message=got.append, on_exit=lambda p, expected, err: exits.append((p, expected, err)))
    return proc, got, exits


def types(got):
    return [m["type"] for m in got]


def test_echo_round_trip_and_transcript(tmp_path):
    async def main():
        proc, got, _ = spawn(tmp_path)
        await proc.start()
        await proc.send_user("hello")
        await until(lambda: "result" in types(got))
        await proc.close()
        return got
    got = asyncio.run(main())
    assert got[1]["isReplay"] and got[1]["message"]["content"] == "hello"
    assert any(m["type"] == "assistant" and m["message"]["content"][0]["text"] == "echo: hello" for m in got)
    lines = (tmp_path / "s1.jsonl").read_text().splitlines()
    assert [json.loads(l)["type"] for l in lines] == ["user", "assistant"]


def test_interrupt_ends_the_turn(tmp_path):
    async def main():
        proc, got, _ = spawn(tmp_path)
        await proc.start()
        await proc.send_user("slow")
        await until(lambda: any(m["type"] == "assistant" for m in got))
        await proc.interrupt()
        await until(lambda: "result" in types(got))
        await proc.close()
        return got
    got = asyncio.run(main())
    assert got[-1]["subtype"] == "error_during_execution"
    assert "control_response" in types(got)


def test_crash_reports_unexpected_exit_with_stderr(tmp_path):
    async def main():
        proc, _, exits = spawn(tmp_path)
        await proc.start()
        await proc.send_user("crash")
        await until(lambda: bool(exits))
        return proc, exits
    proc, exits = asyncio.run(main())
    assert exits == [(proc, False, "boom")] and not proc.alive


def test_close_reports_expected_exit(tmp_path):
    async def main():
        proc, _, exits = spawn(tmp_path)
        await proc.start()
        await proc.close()
        return exits
    (_, expected, _), = asyncio.run(main())
    assert expected


def test_one_megabyte_line_is_read(tmp_path):
    async def main():
        proc, got, _ = spawn(tmp_path)
        await proc.start()
        await proc.send_user("big")
        await until(lambda: "result" in types(got))
        await proc.close()
        return got
    got = asyncio.run(main())
    results = [b for m in got if m["type"] == "user" and isinstance(m["message"]["content"], list)
               for b in m["message"]["content"]]
    assert len(results[0]["content"]) == 1_000_000


# A leader that leaves a child behind holding its stdout/stderr, the way an MCP server would.
LEADER_WITH_CHILD = ("import json, subprocess, sys; p = subprocess.Popen(['sleep', '30']); "
                     "print(json.dumps({'type': 'child', 'pid': p.pid}), flush=True); "
                     "sys.stderr.write('boom\\n'); sys.exit(3)")


def pid_alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def test_leader_exit_sweeps_the_group(tmp_path):
    async def main():
        got, exits = [], []
        proc = ChatProcess([sys.executable, "-c", LEADER_WITH_CHILD], dict(os.environ), tmp_path,
                           on_message=got.append, on_exit=lambda p, expected, err: exits.append((expected, err)))
        await proc.start()
        await until(lambda: bool(exits), timeout=2.0)
        await until(lambda: not pid_alive(got[0]["pid"]), timeout=2.0)
        return exits
    assert asyncio.run(main()) == [(False, "boom")]


def test_callback_error_does_not_stop_the_stream(tmp_path, caplog):
    async def main():
        got, exits = [], []

        def on_message(msg):
            got.append(msg)
            if len(got) == 1:
                raise RuntimeError("handler bug")

        proc = ChatProcess([*FAKE_CMD, "-p", "--session-id", "s1"],
                           {**os.environ, "FAKE_CLAUDE_PROJECTS": str(tmp_path)}, tmp_path,
                           on_message=on_message, on_exit=lambda p, expected, err: exits.append(expected))
        await proc.start()
        await proc.send_user("hello")
        await until(lambda: "result" in types(got))
        await proc.close()
        return exits
    with caplog.at_level(logging.ERROR):
        assert asyncio.run(main()) == [True]
    assert "handler bug" in caplog.text


def test_missing_binary_raises(tmp_path):
    proc = ChatProcess(["/nonexistent/claude"], dict(os.environ), tmp_path, lambda m: None, lambda *a: None)
    with pytest.raises(FileNotFoundError):
        asyncio.run(proc.start())

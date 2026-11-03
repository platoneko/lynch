from __future__ import annotations

import asyncio
import os
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

from lynch_reader.chat.service import Conflict
from tests.chat_helpers import state_of, until
from tests.test_chat_codex_service import codex_service

MODELS = {"claude": ("claude-opus-5-5", "medium"), "codex": ("gpt-test", "low"),
          "grok": ("grok-4.6", "high"), "opencode": ("0g/deepseek-v4.1-flash", "default")}


async def noop():
    pass


@pytest.mark.parametrize("agent", MODELS)
def test_research_process_updates_before_new_and_resume_but_not_reused_turn(tmp_path, agent):
    calls = []

    async def refresh():
        calls.append("updated")

    async def main():
        svc = codex_service(tmp_path, skills=SimpleNamespace(refresh=refresh, close=noop))
        try:
            c = await svc.create("research", None, "one", *MODELS[agent], agent=agent)
            await until(lambda: state_of(svc, c.id) == "idle")
            assert calls == ["updated"]
            await svc.send(c.id, "two")
            await until(lambda: state_of(svc, c.id) == "idle")
            assert calls == ["updated"]
            await svc.shutdown()
            await svc.send(c.id, "three")
            await until(lambda: state_of(svc, c.id) == "idle")
            assert calls == ["updated", "updated"]
        finally:
            await svc.shutdown()

    asyncio.run(main())


@pytest.mark.parametrize("agent", MODELS)
def test_update_failure_does_not_launch_research_agent(tmp_path, agent):
    from lynch_reader.chat.skills import FinanceSkillsError

    async def fail():
        raise FinanceSkillsError("finance-skills 更新失败")

    async def main():
        svc = codex_service(tmp_path, skills=SimpleNamespace(refresh=fail, close=noop))
        try:
            with pytest.raises(Conflict, match="finance-skills"):
                await svc.create("research", None, "one", *MODELS[agent], agent=agent)
            assert all(live.proc is None for live in svc._live.values())
        finally:
            await svc.shutdown()

    asyncio.run(main())


def test_search_only_grok_does_not_install_finance_skills(tmp_path):
    async def fail():
        raise AssertionError("search-only conversation must not install research skills")

    async def main():
        svc = codex_service(tmp_path, skills=SimpleNamespace(refresh=fail, close=noop))
        try:
            c = await svc.create("discover", None, "one", "grok-4.6", "high", 500000, agent="grok")
            await until(lambda: state_of(svc, c.id) == "dormant")
        finally:
            await svc.shutdown()

    asyncio.run(main())


def command(tmp_path: Path, body: str) -> tuple[str, ...]:
    script = tmp_path / "fake_update.py"
    script.write_text(body)
    return sys.executable, str(script)


def test_updater_preserves_full_output_and_serializes_refreshes(tmp_path):
    from lynch_reader.chat.skills import FinanceSkills

    marker = tmp_path / "calls"
    cmd = command(tmp_path, f"from pathlib import Path\nimport time\np=Path({str(marker)!r})\n"
                  "with p.open('a') as f: f.write('start\\n')\ntime.sleep(0.05)\n"
                  "print('first-output')\nprint('last-output')\n"
                  "with p.open('a') as f: f.write('end\\n')\n")
    updates = FinanceSkills(tmp_path, {}, update_cmd=cmd)

    async def main():
        await asyncio.gather(updates.refresh(), updates.refresh())

    asyncio.run(main())
    assert marker.read_text().splitlines() == ["start", "end", "start", "end"]
    logs = list((tmp_path / "_local/tmp").glob("finance-skills-update-*.log"))
    assert len(logs) == 2
    assert all("first-output\nlast-output\n" in p.read_text() for p in logs)


@pytest.mark.parametrize("cancel", [False, True])
def test_updater_timeout_and_cancel_reap_the_process(tmp_path, cancel):
    from lynch_reader.chat.skills import FinanceSkills, FinanceSkillsError

    marker = tmp_path / "started"
    cmd = command(tmp_path, f"from pathlib import Path\nimport time\nPath({str(marker)!r}).touch()\ntime.sleep(30)\n")
    updates = FinanceSkills(tmp_path, {}, update_cmd=cmd, timeout_s=1 if cancel else 0.2)

    async def main():
        task = asyncio.create_task(updates.refresh())
        await until(marker.exists)
        if cancel:
            task.cancel()
        with pytest.raises(asyncio.CancelledError if cancel else FinanceSkillsError):
            await task
        assert updates._process is None

    asyncio.run(main())


def test_shutdown_stops_pending_updates_and_rejects_new_ones(tmp_path):
    from lynch_reader.chat.skills import FinanceSkills, FinanceSkillsError

    marker = tmp_path / "started"
    cmd = command(tmp_path, f"from pathlib import Path\nimport time\nPath({str(marker)!r}).touch()\ntime.sleep(30)\n")
    updates = FinanceSkills(tmp_path, {}, update_cmd=cmd)

    async def main():
        task = asyncio.create_task(updates.refresh())
        await until(marker.exists)
        queued = asyncio.create_task(updates.refresh())
        await updates.close()
        for future in (task, queued):
            with pytest.raises(FinanceSkillsError):
                await future
        assert updates._process is None
        with pytest.raises(FinanceSkillsError):
            await updates.refresh()

    asyncio.run(main())


def test_cancel_kills_descendants_that_ignore_termination(tmp_path):
    from lynch_reader.chat.skills import FinanceSkills

    child = tmp_path / "descendant.py"
    marker = tmp_path / "descendant.pid"
    child.write_text("import os, signal, time\nfrom pathlib import Path\n"
                     "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
                     f"Path({str(marker)!r}).write_text(str(os.getpid()))\ntime.sleep(30)\n")
    cmd = command(tmp_path, "import subprocess, sys, time\n"
                  f"subprocess.Popen([sys.executable, {str(child)!r}])\ntime.sleep(30)\n")
    updates = FinanceSkills(tmp_path, {}, update_cmd=cmd)

    async def main():
        task = asyncio.create_task(updates.refresh())
        await until(marker.exists)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        pid = int(marker.read_text())
        try:
            stat = Path(f"/proc/{pid}/stat")
            await until(lambda: not stat.exists() or stat.read_text().split()[2] == "Z")
        finally:
            try:
                os.kill(pid, 9)  # fixture cleanup if the assertion catches the original bug
            except ProcessLookupError:
                pass

    asyncio.run(main())

"""Refresh shared finance skills before starting a research process."""
from __future__ import annotations

import asyncio
from collections.abc import Mapping
from pathlib import Path
import sys
import tempfile

from lynch_reader.chat.process import stop_process_group


class FinanceSkillsError(Exception):
    pass


class FinanceSkills:
    def __init__(self, repo_root: Path, env: Mapping[str, str], *, update_cmd: tuple[str, ...] | None = None,
                 timeout_s: float = 120.0) -> None:
        self._root, self._env, self._timeout = repo_root, dict(env), timeout_s
        self._cmd = update_cmd or (sys.executable, str(repo_root / "scripts/update_finance_skills.py"),
                                  "--repo-root", str(repo_root))
        self._lock = asyncio.Lock()
        self._process: asyncio.subprocess.Process | None = None
        self._closed = False

    async def refresh(self) -> None:
        async with self._lock:
            if self._closed:
                raise FinanceSkillsError("finance-skills 更新已停止")
            try:
                temporary = self._root / "_local/tmp"
                temporary.mkdir(parents=True, exist_ok=True)
                with tempfile.NamedTemporaryFile(prefix="finance-skills-update-", suffix=".log", dir=temporary,
                                                 delete=False) as output:
                    process = await asyncio.create_subprocess_exec(
                        *self._cmd, cwd=self._root, env=self._env, stdin=asyncio.subprocess.DEVNULL,
                        stdout=output, stderr=asyncio.subprocess.STDOUT, start_new_session=True)
                    self._process = process
                    try:
                        if self._closed:
                            raise FinanceSkillsError("finance-skills 更新已停止")
                        await asyncio.wait_for(process.wait(), self._timeout)
                    except BaseException:
                        await self._stop(process)
                        raise
                if process.returncode or self._closed:
                    raise FinanceSkillsError("finance-skills 更新失败，请在终端运行 just skills-update 查看原因")
            except TimeoutError:
                raise FinanceSkillsError("finance-skills 更新超时，请检查网络后重试") from None
            except OSError:
                raise FinanceSkillsError("finance-skills 更新程序无法启动，请检查 Node.js、npx 和目录权限") from None
            finally:
                self._process = None

    async def close(self) -> None:
        self._closed = True
        if self._process is not None:
            await self._stop(self._process)

    @staticmethod
    async def _stop(process: asyncio.subprocess.Process) -> None:
        await stop_process_group(process.pid)
        await process.wait()

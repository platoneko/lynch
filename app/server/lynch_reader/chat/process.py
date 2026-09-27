"""One long-lived `claude -p` process: stream-json lines in on stdin, out on stdout."""
from __future__ import annotations

import asyncio
import json
import os
import signal
from collections import deque
from collections.abc import Callable
from pathlib import Path

LINE_LIMIT = 64 * 1024 * 1024  # a tool result arrives as one line and can be large
STDERR_TAIL = 20
KILL_GRACE_S = 5.0


class ChatProcess:
    def __init__(self, argv: list[str], env: dict[str, str], cwd: Path,
                 on_message: Callable[[dict], None],
                 on_exit: Callable[["ChatProcess", bool, str], None]) -> None:
        self._argv, self._env, self._cwd = argv, env, cwd
        self._on_message, self._on_exit = on_message, on_exit
        self._proc: asyncio.subprocess.Process | None = None
        self._watcher: asyncio.Task | None = None
        self._stderr: deque[str] = deque(maxlen=STDERR_TAIL)
        self._closing = False
        self._requests = 0

    @property
    def alive(self) -> bool:
        return self._proc is not None and self._proc.returncode is None

    async def start(self) -> None:
        self._proc = await asyncio.create_subprocess_exec(
            *self._argv, cwd=self._cwd, env=self._env,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            start_new_session=True,  # own process group: close() takes MCP servers and tool children with it
            limit=LINE_LIMIT,
        )
        self._watcher = asyncio.create_task(self._watch())

    async def send_user(self, text: str) -> None:
        await self._write({"type": "user", "message": {"role": "user", "content": text}})

    async def interrupt(self) -> None:
        self._requests += 1
        await self._write({"type": "control_request", "request_id": f"interrupt-{self._requests}",
                           "request": {"subtype": "interrupt"}})

    async def close(self) -> None:
        """Ends the whole process group; the exit is then reported as expected."""
        if self._proc is None:
            return
        self._closing = True
        if self.alive:
            self._signal(signal.SIGTERM)
            try:
                await asyncio.wait_for(self._proc.wait(), KILL_GRACE_S)
            except TimeoutError:
                self._signal(signal.SIGKILL)
                await self._proc.wait()
        if self._watcher is not None:
            await self._watcher

    def _signal(self, sig: int) -> None:
        try:
            os.killpg(self._proc.pid, sig)
        except ProcessLookupError:
            pass

    async def _write(self, obj: dict) -> None:
        stdin = self._proc.stdin
        stdin.write((json.dumps(obj, ensure_ascii=False) + "\n").encode())
        await stdin.drain()

    async def _watch(self) -> None:
        await asyncio.gather(self._read_stdout(), self._read_stderr())
        await self._proc.wait()
        self._on_exit(self, self._closing, "\n".join(self._stderr))

    async def _read_stdout(self) -> None:
        async for raw in self._proc.stdout:
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue  # --verbose may print a non-JSON diagnostic line
            self._on_message(msg)

    async def _read_stderr(self) -> None:
        async for raw in self._proc.stderr:
            self._stderr.append(raw.decode(errors="replace").rstrip())

"""One long-lived `claude -p` process: stream-json lines in on stdin, out on stdout."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
from collections import deque
from collections.abc import Callable
from pathlib import Path

LINE_LIMIT = 64 * 1024 * 1024  # a tool result arrives as one line and can be large
STDERR_TAIL = 20
KILL_GRACE_S = 5.0
EXIT_POLL_S = 0.1

log = logging.getLogger(__name__)


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
        if self.alive:
            self._closing = True
            self._signal(signal.SIGTERM)
        # The watcher sweeps what survives the leader; SIGKILL covers a leader that ignores SIGTERM.
        try:
            await asyncio.wait_for(asyncio.shield(self._watcher), KILL_GRACE_S)
        except TimeoutError:
            self._signal(signal.SIGKILL)
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
        drained = asyncio.gather(self._read_stdout(), self._read_stderr())
        # Group members that outlive the leader (MCP servers, tool children) hold its pipes open,
        # so neither EOF nor wait() would come: watch the leader's exit code instead.
        while self.alive:
            await asyncio.sleep(EXIT_POLL_S)
        await self._sweep_group()
        await drained
        await self._proc.wait()
        self._on_exit(self, self._closing, "\n".join(self._stderr))

    async def _sweep_group(self) -> None:
        self._signal(signal.SIGTERM)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + KILL_GRACE_S
        while self._group_alive():
            if loop.time() >= deadline:
                self._signal(signal.SIGKILL)
                return
            await asyncio.sleep(EXIT_POLL_S)

    def _group_alive(self) -> bool:
        try:
            os.killpg(self._proc.pid, 0)
        except ProcessLookupError:
            return False
        return True

    async def _read_stdout(self) -> None:
        async for raw in self._proc.stdout:
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue  # --verbose may print a non-JSON diagnostic line
            try:
                self._on_message(msg)
            except Exception:
                # A handler bug must not stop the draining: a full pipe would stall claude, and on_exit would never fire.
                log.exception("on_message failed")

    async def _read_stderr(self) -> None:
        async for raw in self._proc.stderr:
            self._stderr.append(raw.decode(errors="replace").rstrip())

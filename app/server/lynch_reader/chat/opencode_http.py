"""Authenticated loopback client for the private OpenCode 2 server."""

from __future__ import annotations

import asyncio
import base64
import json
from urllib.parse import urlencode


class OpencodeHttpError(RuntimeError):
    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class OpencodeHttp:
    def __init__(self, port: int, password: str, directory: str) -> None:
        self.port = port
        self._authorization = base64.b64encode(f"opencode:{password}".encode()).decode()
        self._directory = directory

    async def _open(self, method: str, path: str, body=None):
        reader, writer = await asyncio.open_connection(
            "127.0.0.1", self.port, limit=64 * 1024 * 1024
        )
        payload = b"" if body is None else json.dumps(body, ensure_ascii=False).encode()
        path += ("&" if "?" in path else "?") + urlencode(
            {"location[directory]": self._directory}
        )
        headers = (
            f"{method} {path} HTTP/1.1\r\nHost: 127.0.0.1:{self.port}\r\n"
            f"Authorization: Basic {self._authorization}\r\nContent-Type: application/json\r\n"
            f"Content-Length: {len(payload)}\r\nConnection: close\r\n\r\n"
        )
        try:
            writer.write(headers.encode() + payload)
            await writer.drain()
            status = (await reader.readline()).decode().split()
            response_headers = {}
            while line := await reader.readline():
                if line == b"\r\n":
                    break
                key, value = line.decode().split(":", 1)
                response_headers[key.lower()] = value.strip()
            if len(status) < 2 or int(status[1]) >= 400:
                data = b"".join([chunk async for chunk in self._chunks(reader, response_headers)])
                try:
                    message = json.loads(data).get("message", "")
                except (ValueError, AttributeError):
                    message = ""
                raise OpencodeHttpError(
                    f"opencode HTTP {status[1] if len(status) > 1 else '连接中断'}"
                    + (f"：{message}" if message else ""),
                    status=int(status[1]) if len(status) > 1 else None,
                )
            return reader, writer, response_headers
        except BaseException:
            writer.close()
            await writer.wait_closed()
            raise

    async def _chunks(self, reader, headers):
        if headers.get("transfer-encoding", "").lower() == "chunked":
            while True:
                line = await reader.readline()
                if not line:
                    return
                size = int(line.split(b";", 1)[0].strip(), 16)
                if size == 0:
                    return
                yield await reader.readexactly(size)
                await reader.readexactly(2)
        elif "content-length" in headers:
            remaining = int(headers["content-length"])
            while remaining:
                chunk = await reader.read(min(remaining, 65536))
                if not chunk:
                    raise OpencodeHttpError("opencode HTTP 响应中断")
                remaining -= len(chunk)
                yield chunk
        else:
            while chunk := await reader.read(65536):
                yield chunk

    async def request(self, method: str, path: str, body=None, *, timeout: float = 30):
        async with asyncio.timeout(timeout):
            reader, writer, headers = await self._open(method, path, body)
            try:
                data = b"".join(
                    [chunk async for chunk in self._chunks(reader, headers)]
                )
                return json.loads(data) if data else None
            finally:
                writer.close()
                await writer.wait_closed()

    async def events(self):
        reader, writer, headers = await self._open("GET", "/api/event")
        pending = b""
        try:
            async for chunk in self._chunks(reader, headers):
                pending += chunk
                while b"\n" in pending:
                    line, pending = pending.split(b"\n", 1)
                    if line.startswith(b"data:"):
                        event = json.loads(line[5:].strip())
                        # V2's SSE schema encodes each event as a JSON string.
                        yield json.loads(event) if isinstance(event, str) else event
        finally:
            writer.close()
            await writer.wait_closed()

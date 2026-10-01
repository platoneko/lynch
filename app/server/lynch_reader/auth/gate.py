"""Every request needs a paired device's cookie, except the pairing page and its form post.

Anyone who reaches this server can start Claude Code on this host, and on the phone any app can reach a
forwarded loopback port, so neither the tailnet nor the phone's loopback is a boundary.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

from starlette.datastructures import MutableHeaders
from starlette.requests import HTTPConnection, Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.routing import Route
from starlette.types import ASGIApp, Message, Receive, Scope, Send
from starlette.websockets import WebSocketClose

from lynch_reader.auth.store import DeviceStore, InvalidCode, StoreCorrupt
from lynch_reader.chat.routes import is_json

log = logging.getLogger(__name__)

COOKIE = "lynch_device"
COOKIE_MAX_AGE_S = 315_360_000  # ten years: a device stays paired until revoked
PAIR_PAGE = "/pair"
PAIR_API = "/api/pair"
# The only ways in without a device cookie.
OPEN = {("GET", PAIR_PAGE), ("POST", PAIR_API)}
PAIR_BODY_MAX = 1024  # bytes; {"code": "XXXX-XXXX"} needs a few dozen, and anyone can post here

INVALID_CODE = "配对码无效或已过期"
UNPAIRED = "未配对或凭证已吊销"
PAIR_HTML = (Path(__file__).with_name("pair.html")).read_text(encoding="utf-8")


class DeviceAuth:
    """Pure ASGI, so it covers REST, the SSE streams and the static files alike."""

    def __init__(self, app: ASGIApp, devices: DeviceStore) -> None:
        self.app = app
        self.devices = devices

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "lifespan":
            await self.app(scope, receive, send)
            return
        if scope["type"] == "http" and (scope["method"], scope["path"]) in OPEN:
            await self.app(scope, receive, send)
            return
        token = self._paired_token(scope)
        if token is not None:
            if _is_page_load(scope):
                # Chromium (Android WebView too) caps a cookie's Max-Age at 400 days; renewing it on every
                # page load keeps a device in use paired until it is revoked.
                send = _with_header(send, "set-cookie", _device_cookie(token))
            await self.app(scope, receive, send)
            return
        if scope["type"] != "http":  # no websocket route exists; refuse the handshake rather than pass one
            await WebSocketClose(code=1008)(scope, receive, send)
            return
        await _refusal(scope)(scope, receive, send)

    def _paired_token(self, scope: Scope) -> str | None:
        """The request's device cookie if it belongs to a paired device."""
        token = HTTPConnection(scope).cookies.get(COOKIE)
        if not token:
            return None
        try:
            return token if self.devices.is_paired(token) else None
        except StoreCorrupt as e:
            log.error("device store unreadable, refusing every request: %s", e)
            return None


def _is_page_load(scope: Scope) -> bool:
    return (scope["type"] == "http" and scope["method"] == "GET"
            and "text/html" in HTTPConnection(scope).headers.get("accept", ""))


def _refusal(scope: Scope) -> Response:
    # A page load can show the pairing page; a fetch or EventSource gets a status the web UI acts on.
    if _is_page_load(scope):
        return RedirectResponse(PAIR_PAGE, status_code=302)
    return JSONResponse({"error": UNPAIRED}, status_code=401)


def _device_cookie(token: str) -> str:
    """The Set-Cookie value that pairs a device, for both the pairing and the renewal."""
    response = Response()
    # No Secure: the tailnet path is plain HTTP inside WireGuard.
    response.set_cookie(COOKIE, token, max_age=COOKIE_MAX_AGE_S, path="/", httponly=True, samesite="strict")
    return response.headers["set-cookie"]


def _with_header(send: Send, name: str, value: str) -> Send:
    async def wrapped(message: Message) -> None:
        if message["type"] == "http.response.start":
            MutableHeaders(scope=message).append(name, value)
        await send(message)

    return wrapped


def pair_routes(devices: DeviceStore) -> list[Route]:
    async def page(request: Request) -> Response:
        return HTMLResponse(PAIR_HTML, headers={"Cache-Control": "no-store"})

    async def pair(request: Request) -> Response:
        if not is_json(request):  # same CSRF guard as the chat commands
            return JSONResponse({"error": "Content-Type must be application/json"}, status_code=415)
        raw = await _read_at_most(request, PAIR_BODY_MAX)
        if raw is None:
            return JSONResponse({"error": "请求体过大"}, status_code=413)
        try:
            body = json.loads(raw)
        except ValueError:
            body = None
        code = body.get("code") if isinstance(body, dict) else None
        if not isinstance(code, str):
            return JSONResponse({"error": INVALID_CODE}, status_code=400)
        try:
            token = devices.redeem(code)
        except InvalidCode:
            return JSONResponse({"error": INVALID_CODE}, status_code=400)
        except StoreCorrupt as e:
            log.error("device store unreadable, pairing refused: %s", e)
            return JSONResponse({"error": "服务器上的设备文件损坏，请在服务器上处理"}, status_code=500)
        return Response(status_code=204, headers={"set-cookie": _device_cookie(token)})

    return [Route(PAIR_PAGE, page, methods=["GET"]), Route(PAIR_API, pair, methods=["POST"])]


async def _read_at_most(request: Request, limit: int) -> bytes | None:
    """The body, or None once it passes `limit` bytes; counts what arrives, since a chunked body has no
    Content-Length to trust."""
    body = b""
    async for chunk in request.stream():
        body += chunk
        if len(body) > limit:
            return None
    return body

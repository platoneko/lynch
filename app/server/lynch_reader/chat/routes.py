"""HTTP surface of the chats: REST commands plus one SSE stream per open conversation."""
from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path

from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from lynch_reader.catalog import framework_path
from lynch_reader.chat.config import DEFAULT_EFFORT, DEFAULT_MODEL
from lynch_reader.chat.service import ChatService, Conflict, Invalid, NotFound

HEARTBEAT_S = 15.0  # keeps the phone proxy and WebView from dropping a quiet stream
REAP_INTERVAL_S = 60.0


def sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


async def event_stream(service: ChatService, conv_id: str,
                       disconnected: Callable[[], Awaitable[bool]]) -> AsyncIterator[str]:
    # Subscribe, then snapshot, with no await in between: no event can land between the two, so none is
    # missed or repeated. `delta` events carry no key, so a repeat would double the partial text on the client.
    q = service.subscribe(conv_id)
    try:
        yield sse("snapshot", service.snapshot(conv_id))
        while not await disconnected():
            try:
                event, data = await asyncio.wait_for(q.get(), HEARTBEAT_S)
            except TimeoutError:
                yield ": ping\n\n"
                continue
            yield sse(event, data)
    finally:
        service.unsubscribe(conv_id, q)


def _guarded(handler: Callable[[Request], Awaitable[Response]]) -> Callable[[Request], Awaitable[Response]]:
    async def run(request: Request) -> Response:
        try:
            return await handler(request)
        except NotFound:
            return JSONResponse({"error": "对话不存在或已归档"}, status_code=404)
        except Invalid as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        except Conflict as e:
            return JSONResponse({"error": str(e)}, status_code=409)
    return run


async def _body(request: Request) -> dict:
    try:
        body = await request.json()
    except json.JSONDecodeError as e:
        raise Invalid("body is not JSON") from e
    if not isinstance(body, dict):
        raise Invalid("body must be an object")
    return body


def chat_routes(service: ChatService, frameworks_dir: Path) -> list[Route]:
    async def list_chats(request: Request) -> Response:
        return JSONResponse(service.list())

    async def options(request: Request) -> Response:
        return JSONResponse(service.options())

    async def create(request: Request) -> Response:
        body = await _body(request)
        ticker = body.get("ticker")
        if body.get("kind") == "ticker" and framework_path(str(ticker or ""), frameworks_dir) is None:
            raise Invalid("unknown ticker")
        conv = await service.create(body.get("kind"), ticker, str(body.get("text", "")),
                                    body.get("model") or DEFAULT_MODEL, body.get("effort") or DEFAULT_EFFORT)
        return JSONResponse({"id": conv.id})

    async def archive_ticker(request: Request) -> Response:
        ticker = (await _body(request)).get("ticker")
        if not ticker:
            raise Invalid("ticker required")
        return JSONResponse({"archived": await service.archive_ticker(ticker)})

    async def events(request: Request) -> Response:
        conv_id = request.path_params["id"]
        service.snapshot(conv_id)  # 404 before the stream starts
        return StreamingResponse(event_stream(service, conv_id, request.is_disconnected),
                                 media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    async def message(request: Request) -> Response:
        await service.send(request.path_params["id"], str((await _body(request)).get("text", "")))
        return Response(status_code=204)

    async def interrupt(request: Request) -> Response:
        await service.interrupt(request.path_params["id"])
        return Response(status_code=204)

    async def edit(request: Request) -> Response:
        body = await _body(request)
        await service.edit(request.path_params["id"], str(body.get("message_uuid", "")), str(body.get("text", "")))
        return Response(status_code=204)

    async def archive(request: Request) -> Response:
        await service.archive(request.path_params["id"])
        return Response(status_code=204)

    return [
        Route("/api/chats", _guarded(list_chats), methods=["GET"]),
        Route("/api/chats", _guarded(create), methods=["POST"]),
        Route("/api/chats/options", _guarded(options), methods=["GET"]),
        Route("/api/chats/archive", _guarded(archive_ticker), methods=["POST"]),
        Route("/api/chats/{id}/events", _guarded(events), methods=["GET"]),
        Route("/api/chats/{id}/messages", _guarded(message), methods=["POST"]),
        Route("/api/chats/{id}/interrupt", _guarded(interrupt), methods=["POST"]),
        Route("/api/chats/{id}/edit", _guarded(edit), methods=["POST"]),
        Route("/api/chats/{id}/archive", _guarded(archive), methods=["POST"]),
    ]


def chat_lifespan(service: ChatService):
    @asynccontextmanager
    async def lifespan(app):
        async def reap_forever():
            while True:
                await asyncio.sleep(REAP_INTERVAL_S)
                await service.reap_idle()

        reaper = asyncio.create_task(reap_forever())
        try:
            yield
        finally:
            reaper.cancel()
            await service.shutdown()  # a turn in flight is lost; history marks it (spec §3a)
    return lifespan

"""HTTP surface of the chats: REST commands plus one SSE stream per open conversation."""
from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager, suppress
from pathlib import Path

from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from lynch_reader.catalog import framework_path
from lynch_reader.chat.config import DEFAULT_EFFORT, DEFAULT_MODEL
from lynch_reader.chat.service import ChatService, Conflict, Invalid, NotFound

log = logging.getLogger(__name__)

# Keeps the phone's forward and WebView from dropping a quiet stream, and lets the page tell quiet from dead
# (web chat/api.ts STALE_MS allows three missed beats).
HEARTBEAT_S = 15.0
REAP_INTERVAL_S = 60.0


def sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


async def event_stream(service: ChatService, conv_id: str,
                       disconnected: Callable[[], Awaitable[bool]]) -> AsyncIterator[str]:
    # Subscribe, then snapshot, with no await in between: the in-memory state (partial text, state, context,
    # usage) is exact, so keyless `delta` events are never doubled. The snapshot's items come from the transcript
    # file, which claude writes at other moments than its stdout: a keyed item may be in the snapshot and then
    # arrive again as an `item` event; the client folds items by `key`.
    q = service.subscribe(conv_id)
    try:
        yield sse("snapshot", service.snapshot(conv_id))
        while not await disconnected():
            try:
                event, data = await asyncio.wait_for(q.get(), HEARTBEAT_S)
            except TimeoutError:
                yield sse("ping", {})  # an event, not an SSE comment: comments never reach the page
                continue
            yield sse(event, data)
    finally:
        service.unsubscribe(conv_id, q)


def is_json(request: Request) -> bool:
    return request.headers.get("content-type", "").split(";")[0].strip().lower() == "application/json"


def _guarded(handler: Callable[[Request], Awaitable[Response]]) -> Callable[[Request], Awaitable[Response]]:
    async def run(request: Request) -> Response:
        # CSRF guard: a cross-site form or text/plain POST is a "simple" request the browser sends without a
        # preflight; requiring JSON forces one, which this same-origin-only server never answers. Body-less
        # commands (interrupt, archive) included: they kill a running agent.
        if request.method == "POST" and not is_json(request):
            return JSONResponse({"error": "Content-Type must be application/json"}, status_code=415)
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
    except ValueError as e:  # JSONDecodeError, or UnicodeDecodeError on a non-UTF-8 body
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
        kind, ticker = body.get("kind"), body.get("ticker")
        if kind == "ticker" and framework_path(str(ticker or ""), frameworks_dir) is None:
            raise Invalid("unknown ticker")
        # A Claude chat may leave model and effort to the defaults; a Grok chat names model, effort, window and
        # how many days back to look.
        model, effort = (None, None) if kind == "discover" else (DEFAULT_MODEL, DEFAULT_EFFORT)
        conv = await service.create(kind, ticker, str(body.get("text", "")), body.get("model") or model,
                                    body.get("effort") or effort, body.get("context_window"), body.get("lookback_days"),
                                    body.get("agent"))
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

    async def rename(request: Request) -> Response:
        service.rename(request.path_params["id"], (await _body(request)).get("title"))
        return Response(status_code=204)

    async def approval(request: Request) -> Response:
        allow = (await _body(request)).get("allow")
        await service.answer_approval(request.path_params["id"], request.path_params["request_id"], allow)
        return Response(status_code=204)

    async def save_candidates(request: Request) -> Response:
        body = await _body(request)
        saved = service.save_candidates(request.path_params["id"], body.get("key"), body.get("tickers"))
        return JSONResponse({"saved": saved})

    async def list_candidates(request: Request) -> Response:
        return JSONResponse({"candidates": service.candidates.all()})

    async def archive_candidate(request: Request) -> Response:
        if not service.candidates.archive(request.path_params["ticker"]):
            raise Invalid("候选区没有这只票")
        return Response(status_code=204)

    async def restore_candidate(request: Request) -> Response:
        if not service.candidates.restore(request.path_params["ticker"]):
            raise Invalid("候选区没有这只票")
        return Response(status_code=204)

    routes = [
        Route("/api/chats", _guarded(list_chats), methods=["GET"]),
        Route("/api/chats", _guarded(create), methods=["POST"]),
        Route("/api/chats/options", _guarded(options), methods=["GET"]),
        Route("/api/chats/archive", _guarded(archive_ticker), methods=["POST"]),
        Route("/api/chats/{id}/events", _guarded(events), methods=["GET"]),
        Route("/api/chats/{id}/messages", _guarded(message), methods=["POST"]),
        Route("/api/chats/{id}/interrupt", _guarded(interrupt), methods=["POST"]),
        Route("/api/chats/{id}/edit", _guarded(edit), methods=["POST"]),
        Route("/api/chats/{id}/archive", _guarded(archive), methods=["POST"]),
        Route("/api/chats/{id}/title", _guarded(rename), methods=["POST"]),
        Route("/api/chats/{id}/approvals/{request_id}", _guarded(approval), methods=["POST"]),
        Route("/api/chats/{id}/candidates", _guarded(save_candidates), methods=["POST"]),
    ]
    if service.candidates is not None:
        routes += [
            Route("/api/candidates", _guarded(list_candidates), methods=["GET"]),
            Route("/api/candidates/{ticker}/archive", _guarded(archive_candidate), methods=["POST"]),
            Route("/api/candidates/{ticker}/restore", _guarded(restore_candidate), methods=["POST"]),
        ]
    return routes


def chat_lifespan(service: ChatService, *, reap_interval_s: float = REAP_INTERVAL_S):
    @asynccontextmanager
    async def lifespan(app):
        async def reap_forever():
            while True:
                await asyncio.sleep(reap_interval_s)
                try:
                    await service.reap_idle()
                except Exception:  # one failed sweep must not end idle recycling for the server's lifetime
                    log.exception("idle chat reaping failed")

        reaper = asyncio.create_task(reap_forever())
        try:
            yield
        finally:
            reaper.cancel()
            with suppress(asyncio.CancelledError):
                await reaper
            await service.shutdown()  # a turn in flight is lost; history marks it (spec §3a)
    return lifespan

"""HTTP surface: ticker catalog with zones on live quotes, raw framework and axis markdown, chats, and the built web UI.

Only paired devices get in (lynch_reader.auth): a one-time code from `just app-pair` buys a long-lived cookie.
Chats run Claude Code subprocesses that may write frameworks/ under a tool whitelist and the sediment-framework
skill, and read-only grok subprocesses that search X (AGENTS.md「app 对话」). The server itself writes only
output/chat/ and output/candidates/.
"""
from __future__ import annotations

import argparse
import logging
import os
import shutil
from pathlib import Path

import uvicorn
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from lynch_reader.auth.gate import DeviceAuth, pair_routes
from lynch_reader.auth.store import DEVICES_FILE, DeviceStore
from lynch_reader.catalog import FRAMEWORKS_DIR, REPO_ROOT, QuoteCache, axis_path_in, catalog, framework_path
from lynch_reader.chat.candidates import CandidateStore
from lynch_reader.chat.config import LaunchConfig
from lynch_reader.chat.grok import GrokConfig
from lynch_reader.chat.index import ChatIndex
from lynch_reader.chat.kol import INVEST_ROOT
from lynch_reader.chat.routes import chat_lifespan, chat_routes
from lynch_reader.chat.service import ChatService
from lynch_reader.chat.transcript import projects_dir_for

WEB_DIST = REPO_ROOT / "app" / "web" / "dist"
# The personal tailnet address of this host; other local users must not reach it via 0.0.0.0.
DEFAULT_HOST = "100.93.210.104"
DEFAULT_PORT = 3151


class WebDist(StaticFiles):
    """The built web UI; HTML is always revalidated.

    The phone's loopback origin is stable across launches, so a cached
    index.html would keep loading the previous build's hashed assets after a
    redeploy. The hashed assets themselves may stay cached.
    """

    async def get_response(self, path: str, scope) -> Response:
        response = await super().get_response(path, scope)
        if response.media_type == "text/html":
            response.headers["Cache-Control"] = "no-cache"
        return response


def create_app(frameworks_dir: Path, web_dist: Path, quotes: QuoteCache, devices: DeviceStore,
               chat: ChatService | None = None) -> Starlette:
    def tickers(request: Request) -> Response:
        refresh = request.query_params.get("refresh") == "1"
        return JSONResponse(catalog(frameworks_dir, quotes, refresh))

    def framework(request: Request) -> Response:
        path = framework_path(request.path_params["ticker"], frameworks_dir)
        if path is None:
            return PlainTextResponse("unknown ticker", status_code=404)
        return Response(path.read_text(encoding="utf-8"), media_type="text/markdown; charset=utf-8")

    def axis(request: Request) -> Response:
        path = axis_path_in(request.path_params["name"], frameworks_dir)
        if path is None:
            return PlainTextResponse("unknown axis", status_code=404)
        return Response(path.read_text(encoding="utf-8"), media_type="text/markdown; charset=utf-8")

    routes = [
        *pair_routes(devices),
        Route("/api/tickers", tickers),
        Route("/api/frameworks/{ticker}", framework),
        Route("/api/axes/{name}", axis),
    ]
    if chat is not None:
        routes += chat_routes(chat, frameworks_dir)
    if web_dist.is_dir():
        routes.append(Mount("/", WebDist(directory=web_dist, html=True)))
    return Starlette(routes=routes, middleware=[Middleware(DeviceAuth, devices=devices)],
                     lifespan=chat_lifespan(chat) if chat is not None else None)


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description="lynch mobile app server")
    ap.add_argument("--host", default=DEFAULT_HOST)
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    output = REPO_ROOT / "output"
    chat = ChatService(
        ChatIndex(output / "chat" / "index.json"),
        LaunchConfig((shutil.which("claude") or "claude",), REPO_ROOT),
        projects_dir_for(REPO_ROOT, Path.home()),
        dict(os.environ),
        grok=GrokConfig((shutil.which("grok") or "grok",), workdir=output / "chat" / "grok-workdir",
                        home=Path.home(), logs=output / "chat" / "grok", invest_root=INVEST_ROOT),
        candidates=CandidateStore(output / "candidates" / "candidates.json"),
    )
    # An open chat stream never ends by itself: without a graceful-shutdown timeout, SIGTERM waits on it
    # forever and the lifespan shutdown that stops the claude processes never runs.
    uvicorn.run(create_app(FRAMEWORKS_DIR, WEB_DIST, QuoteCache(), DeviceStore(DEVICES_FILE), chat=chat),
                host=args.host, port=args.port, timeout_graceful_shutdown=5)


if __name__ == "__main__":
    main()

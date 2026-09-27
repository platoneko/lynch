"""HTTP surface: ticker catalog with zones on live quotes, raw framework and axis markdown, and the built web UI.

Read-only by contract (AGENTS.md): nothing here writes frameworks/ or output/.
"""
from __future__ import annotations

import argparse
import logging
from pathlib import Path

import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from lynch_reader.catalog import FRAMEWORKS_DIR, REPO_ROOT, QuoteCache, axis_path_in, catalog, framework_path

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


def create_app(frameworks_dir: Path, web_dist: Path, quotes: QuoteCache) -> Starlette:
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
        Route("/api/tickers", tickers),
        Route("/api/frameworks/{ticker}", framework),
        Route("/api/axes/{name}", axis),
    ]
    if web_dist.is_dir():
        routes.append(Mount("/", WebDist(directory=web_dist, html=True)))
    return Starlette(routes=routes)


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description="lynch mobile reader server (read-only)")
    ap.add_argument("--host", default=DEFAULT_HOST)
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    uvicorn.run(create_app(FRAMEWORKS_DIR, WEB_DIST, QuoteCache()), host=args.host, port=args.port)


if __name__ == "__main__":
    main()

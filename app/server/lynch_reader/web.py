"""HTTP surface: ticker catalog, raw framework markdown, and the built web UI.

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

from lynch_reader.catalog import BANDS_DIR, FRAMEWORKS_DIR, REPO_ROOT, catalog, framework_path

WEB_DIST = REPO_ROOT / "app" / "web" / "dist"
# The personal tailnet address of this host; other local users must not reach it via 0.0.0.0.
DEFAULT_HOST = "100.93.210.104"
DEFAULT_PORT = 3151


def create_app(frameworks_dir: Path, bands_dir: Path, web_dist: Path) -> Starlette:
    def tickers(_: Request) -> Response:
        return JSONResponse(catalog(frameworks_dir, bands_dir))

    def framework(request: Request) -> Response:
        path = framework_path(request.path_params["ticker"], frameworks_dir)
        if path is None:
            return PlainTextResponse("unknown ticker", status_code=404)
        return Response(path.read_text(encoding="utf-8"), media_type="text/markdown; charset=utf-8")

    routes = [
        Route("/api/tickers", tickers),
        Route("/api/frameworks/{ticker}", framework),
    ]
    if web_dist.is_dir():
        routes.append(Mount("/", StaticFiles(directory=web_dist, html=True)))
    return Starlette(routes=routes)


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description="lynch mobile reader server (read-only)")
    ap.add_argument("--host", default=DEFAULT_HOST)
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    uvicorn.run(create_app(FRAMEWORKS_DIR, BANDS_DIR, WEB_DIST), host=args.host, port=args.port)


if __name__ == "__main__":
    main()

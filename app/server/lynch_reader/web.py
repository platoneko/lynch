"""HTTP surface: ticker catalog with zones on live quotes, raw framework and axis markdown, chats, and the built web UI.

Only paired devices get in (lynch_reader.auth): a one-time code from `just app-pair` buys a long-lived cookie.
Chats run Claude Code subprocesses that may write frameworks/ under a tool whitelist and the sediment-framework
skill, and read-only grok subprocesses that search X (AGENTS.md「app 对话」). The server itself writes only
output/chat/, output/candidates/, output/archive/, output/earnings/ and output/anomalies/.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import shutil
from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path

import uvicorn
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from lynch_reader.anomalies import BASELINE_FILE, EVENTS_FILE, Anomalies, AnomalyStore, BaselineCache
from lynch_reader.archive import ARCHIVE_FILE, FrameworkArchive
from lynch_reader.auth.gate import DeviceAuth, pair_routes
from lynch_reader.auth.store import DEVICES_FILE, DeviceStore
from lynch_reader.catalog import (
    FRAMEWORKS_DIR, REPO_ROOT, QuoteCache, QuotePoller, axis_path_in, catalog, framework_path, quote_failure,
    quote_pool,
)
from lynch_reader.chat.candidates import CANDIDATES_FILE, CandidateStore
from lynch_reader.chat.config import LaunchConfig
from lynch_reader.chat.grok import GrokConfig
from lynch_reader.chat.index import ChatIndex
from lynch_reader.chat.kol import INVEST_ROOT
from lynch_reader.chat.routes import _body, _guarded, chat_lifespan, chat_routes
from lynch_reader.chat.service import ChatService, Invalid
from lynch_reader.chat.transcript import projects_dir_for
from lynch_reader.earnings import CALENDAR_FILE, IGNORED_FILE, CalendarCache, Earnings, IgnoreStore

from eval.lib.frameworks import list_framework_paths  # noqa: E402 — after catalog, which puts the repo root on sys.path

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


def active_pool(frameworks_dir: Path, archive: FrameworkArchive) -> list[str]:
    """Frameworks the alerts cover: archived ones are left out."""
    hidden = archive.tickers()
    return [p.stem for p in list_framework_paths(frameworks_dir) if p.stem not in hidden]


def app_lifespan(chat: ChatService | None, poller: QuotePoller | None):
    @asynccontextmanager
    async def lifespan(app):
        if poller is not None:
            poller.start()
        try:
            if chat is not None:
                async with chat_lifespan(chat)(app):
                    yield
            else:
                yield
        finally:
            if poller is not None:
                await asyncio.to_thread(poller.stop)  # joins the thread: keep the event loop free meanwhile
    return lifespan


def create_app(frameworks_dir: Path, web_dist: Path, quotes: QuoteCache, devices: DeviceStore,
               archive: FrameworkArchive, chat: ChatService | None = None,
               earnings: Earnings | None = None, anomalies: Anomalies | None = None,
               poller: QuotePoller | None = None) -> Starlette:
    pool = lambda: active_pool(frameworks_dir, archive)  # noqa: E731
    if anomalies is not None:
        quotes.subscribe(lambda batch: anomalies.observe(batch, pool()))

    def tickers(request: Request) -> Response:
        refresh = request.query_params.get("refresh") == "1"
        return JSONResponse(catalog(frameworks_dir, quotes, refresh, archive.tickers()))

    def framework(request: Request) -> Response:
        path = framework_path(request.path_params["ticker"], frameworks_dir)
        if path is None:
            return PlainTextResponse("unknown ticker", status_code=404)
        return Response(path.read_text(encoding="utf-8"), media_type="text/markdown; charset=utf-8")

    def set_archived(archived: bool):
        async def run(request: Request) -> Response:
            ticker = request.path_params["ticker"]
            if framework_path(ticker, frameworks_dir) is None:
                raise Invalid("没有这只票的框架")
            if archived:
                archive.archive(ticker)
            else:
                archive.restore(ticker)
            return Response(status_code=204)
        return _guarded(run)  # its CSRF guard: a cross-site form must not archive anything

    def axis(request: Request) -> Response:
        path = axis_path_in(request.path_params["name"], frameworks_dir)
        if path is None:
            return PlainTextResponse("unknown axis", status_code=404)
        return Response(path.read_text(encoding="utf-8"), media_type="text/markdown; charset=utf-8")

    def earnings_list(request: Request) -> Response:
        # Sync: the first request after the daily calendar run falls due may wait on longbridge in the thread pool.
        tag = anomalies.earnings_tag if anomalies is not None else (lambda t: None)
        return JSONResponse(earnings.snapshot(pool(), chat.ticker_chat_starts() if chat is not None else [], tag))

    def set_ignored(ignored: bool):
        async def run(request: Request) -> Response:
            body = await _body(request)
            ticker, day = body.get("ticker"), body.get("date")
            if not isinstance(ticker, str) or not isinstance(day, str):
                raise Invalid("需要 ticker 和 date")
            try:
                date.fromisoformat(day)
            except ValueError:
                raise Invalid("date 须为 YYYY-MM-DD") from None
            if framework_path(ticker, frameworks_dir) is None:
                raise Invalid("没有这只票的框架")
            (earnings.ignored.ignore if ignored else earnings.ignored.unignore)(ticker, day)
            return Response(status_code=204)
        return _guarded(run)  # its CSRF guard: a cross-site form must not dismiss anything

    def anomaly_list(request: Request) -> Response:
        # Sync: right after start the first read waits on the first quote fetch, in the thread pool.
        snap = quotes.read(quote_pool(frameworks_dir))
        error = None if snap.error is None else quote_failure(snap.error, snap.batch)
        starts = chat.ticker_chat_starts() if chat is not None else []
        return JSONResponse(anomalies.snapshot(pool(), starts, error))

    def set_anomaly_ignored(ignored: bool):
        async def run(request: Request) -> Response:
            body = await _body(request)
            ticker, ref = body.get("ticker"), body.get("ref")
            if not isinstance(ticker, str) or not isinstance(ref, str):
                raise Invalid("需要 ticker 和 ref")
            if framework_path(ticker, frameworks_dir) is None:
                raise Invalid("没有这只票的框架")
            try:
                anomalies.store.set_ignored(ticker, ref, ignored)
            except KeyError:
                raise Invalid("没有这条异动") from None
            return Response(status_code=204)
        return _guarded(run)  # its CSRF guard: a cross-site form must not dismiss anything

    routes = [
        *pair_routes(devices),
        Route("/api/tickers", tickers),
        Route("/api/frameworks/{ticker}", framework),
        Route("/api/frameworks/{ticker}/archive", set_archived(True), methods=["POST"]),
        Route("/api/frameworks/{ticker}/restore", set_archived(False), methods=["POST"]),
        Route("/api/axes/{name}", axis),
    ]
    if earnings is not None:
        routes += [
            Route("/api/earnings", earnings_list),
            Route("/api/earnings/ignore", set_ignored(True), methods=["POST"]),
            Route("/api/earnings/unignore", set_ignored(False), methods=["POST"]),
        ]
    if anomalies is not None:
        routes += [
            Route("/api/anomalies", anomaly_list),
            Route("/api/anomalies/ignore", set_anomaly_ignored(True), methods=["POST"]),
            Route("/api/anomalies/unignore", set_anomaly_ignored(False), methods=["POST"]),
        ]
    if chat is not None:
        routes += chat_routes(chat, frameworks_dir)
    if web_dist.is_dir():
        routes.append(Mount("/", WebDist(directory=web_dist, html=True)))
    return Starlette(routes=routes, middleware=[Middleware(DeviceAuth, devices=devices)],
                     lifespan=app_lifespan(chat, poller))


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
                        home=Path.home(), logs=output / "chat" / "grok", invest_root=INVEST_ROOT,
                        skill=REPO_ROOT / ".agents" / "skills" / "discover-kol" / "references" / "search.md"),
        candidates=CandidateStore(CANDIDATES_FILE),
    )
    # An open chat stream never ends by itself: without a graceful-shutdown timeout, SIGTERM waits on it
    # forever and the lifespan shutdown that stops the claude processes never runs.
    quotes = QuoteCache()
    archive = FrameworkArchive(ARCHIVE_FILE)
    calendar = CalendarCache(CALENDAR_FILE)
    anomalies = Anomalies(AnomalyStore(EVENTS_FILE), BaselineCache(BASELINE_FILE), calendar)
    poller = QuotePoller(quotes, lambda: quote_pool(FRAMEWORKS_DIR),
                         after=lambda: anomalies.upkeep(active_pool(FRAMEWORKS_DIR, archive)))
    uvicorn.run(create_app(FRAMEWORKS_DIR, WEB_DIST, quotes, DeviceStore(DEVICES_FILE), archive, chat=chat,
                           earnings=Earnings(calendar, IgnoreStore(IGNORED_FILE)), anomalies=anomalies,
                           poller=poller),
                host=args.host, port=args.port, timeout_graceful_shutdown=5)


if __name__ == "__main__":
    main()

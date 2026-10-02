"""Frameworks put away from the app's home list. Only the list hides them: their documents, eval scripts,
the daily digest and `just check` are untouched (AGENTS.md「app 对话」).
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from lynch_reader.catalog import REPO_ROOT

ARCHIVE_FILE = REPO_ROOT / "output" / "archive" / "frameworks.json"


class FrameworkArchive:
    """The archived tickers, a sorted JSON list.

    The server is the only writer and calls it from async handlers on one event loop, so no lock.
    """

    def __init__(self, path: Path) -> None:
        self._path = path

    def tickers(self) -> set[str]:
        if not self._path.exists():
            return set()
        return set(json.loads(self._path.read_text(encoding="utf-8")))

    def archive(self, ticker: str) -> None:
        self._write(self.tickers() | {ticker})

    def restore(self, ticker: str) -> None:
        self._write(self.tickers() - {ticker})

    def _write(self, tickers: set[str]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps(sorted(tickers), ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, self._path)  # the list is read on every catalog load: never leave it torn

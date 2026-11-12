"""Frameworks put away from the app's home list. Only the list hides them: their documents, eval scripts,
the daily digest and `just check` are untouched (AGENTS.md「app 对话」).
"""
from __future__ import annotations

import json
import fcntl
import os
from contextlib import contextmanager
from pathlib import Path

from lynch_reader.catalog import REPO_ROOT

ARCHIVE_FILE = REPO_ROOT / "output" / "archive" / "frameworks.json"


class FrameworkArchive:
    """The archived tickers, a sorted JSON list.

    The stable file lock coordinates app writes with local migration commits.
    """

    def __init__(self, path: Path) -> None:
        # Different aliases of the same archive must share one lock identity.
        self._path = Path(path).resolve()

    def tickers(self) -> set[str]:
        with self.locked() as tickers:
            return tickers

    def _read(self) -> set[str]:
        if not self._path.exists():
            return set()
        tickers = json.loads(self._path.read_text(encoding="utf-8"))
        if not isinstance(tickers, list) or any(not isinstance(ticker, str) for ticker in tickers):
            raise ValueError('archive must contain a list of ticker strings')
        return set(tickers)

    def archive(self, ticker: str) -> None:
        with self.locked() as tickers:
            self._write(tickers | {ticker})

    def restore(self, ticker: str) -> None:
        with self.locked() as tickers:
            self._write(tickers - {ticker})

    @contextmanager
    def locked(self):
        """Hold the same lock used by archive writers until the caller finishes."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = self._path.with_name(self._path.name + '.lock')
        with lock_path.open('a+b') as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                yield self._read()
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    def _write(self, tickers: set[str]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps(sorted(tickers), ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, self._path)  # the list is read on every catalog load: never leave it torn

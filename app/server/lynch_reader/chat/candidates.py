"""KOL candidates: the block Grok ends a discover reply with (spec §3e) and the app's candidate list (§3f)."""
from __future__ import annotations

import fcntl
import json
import logging
import os
import re
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from lynch_reader.catalog import REPO_ROOT

log = logging.getLogger(__name__)

BLOCK = re.compile(r"```lynch-candidates[^\n]*\n(.*?)```", re.S)
TICKER = re.compile(r"^[A-Z0-9]+\.(US|HK|SH|SZ|SG)$")
# HK and A-share codes are bare numbers: without a name the reader cannot tell which company it is.
NAME_REQUIRED = ("HK", "SH", "SZ")
STANCES = ("bullish", "bearish", "neutral")
POST_URL = re.compile(r"^https://(?:x|twitter)\.com/[^/\s]+/status/(\d+)")
SOURCE_FIELDS = ("handle", "stance", "reason", "quote", "url", "posted_at")
CANDIDATES_FILE = REPO_ROOT / "output" / "candidates" / "candidates.json"
RETENTION_S = 30 * 86400  # how long an archived ticker, and a source not saved again, is kept


@dataclass(frozen=True)
class Parsed:
    text: str  # the reply without its block
    candidates: list[dict]
    dropped: int  # candidates and sources left out for failing the format


def parse_candidates(text: str) -> Parsed | None:
    """The reply's last candidate block, checked entry by entry; None when there is no usable block."""
    blocks = list(BLOCK.finditer(text))
    if not blocks:
        return None
    block = blocks[-1]
    try:
        data = json.loads(block.group(1))
    except ValueError:
        return None  # left in the text as Grok wrote it
    raw = data.get("candidates") if isinstance(data, dict) else None
    if not isinstance(raw, list):
        return None

    merged: dict[str, dict] = {}  # one card per ticker, even when Grok lists one twice
    dropped = 0
    for entry in raw:
        candidate, bad_sources = _candidate(entry)
        dropped += bad_sources
        if candidate is None:
            dropped += 1
            continue
        known = merged.setdefault(candidate["ticker"], {**candidate, "sources": []})
        known["name"] = known["name"] or candidate["name"]
        seen = {post_id(s["url"]) for s in known["sources"]}
        for source in candidate["sources"]:  # a post repeated inside one entry counts once too
            if post_id(source["url"]) not in seen:
                seen.add(post_id(source["url"]))
                known["sources"].append(source)
    return Parsed((text[:block.start()] + text[block.end():]).strip(), list(merged.values()), dropped)


def _candidate(entry: object) -> tuple[dict | None, int]:
    """A checked candidate (None when unusable) and how many of its sources were left out."""
    if not isinstance(entry, dict):
        return None, 0
    ticker, name = entry.get("ticker"), entry.get("name") or ""
    if not isinstance(ticker, str) or not TICKER.match(ticker) or not isinstance(name, str):
        return None, 0
    name = name.strip()
    if not name and ticker.rsplit(".", 1)[1] in NAME_REQUIRED:
        return None, 0
    raw = entry.get("sources") if isinstance(entry.get("sources"), list) else []
    sources = [s for s in map(_source, raw) if s is not None]
    if not sources:
        return None, len(raw)
    return {"ticker": ticker, "name": name, "sources": sources}, len(raw) - len(sources)


def _source(s: object) -> dict | None:
    if not isinstance(s, dict) or not all(isinstance(s.get(k), str) and s[k].strip() for k in SOURCE_FIELDS):
        return None
    out = {k: s[k].strip() for k in SOURCE_FIELDS}
    out["handle"] = out["handle"].lstrip("@")
    if out["stance"] not in STANCES or not POST_URL.match(out["url"]) or not _is_iso_time(out["posted_at"]):
        return None
    return out


def post_id(url: str) -> str:
    """The X post a link points at: one post may come as x.com or twitter.com, with or without a query."""
    m = POST_URL.match(url)
    return m.group(1) if m else url  # a stored row written by hand may hold another link


def _is_iso_time(s: str) -> bool:
    try:
        datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return False
    return True


class CandidateStore:
    """output/candidates/candidates.json, one row per ticker.

    Two writers share it: the server (存入候选区, 归档, 恢复) and `just candidates-save` from a terminal session. So
    every call reads the file, and each change is a read-modify-write under an exclusive flock on
    candidates.lock. Reads take no lock: the file is only ever replaced whole. Whether a ticker has landed is not
    stored: its framework file says so.

    Retention is lazy. A ticker archived for RETENTION_S is hidden at once and leaves the file with the next write.
    A ticker's sources older than RETENTION_S go only when that ticker is saved again.
    """

    def __init__(self, path: Path, wall: Callable[[], float] = time.time) -> None:
        self._path, self._wall = path, wall

    def all(self) -> list[dict]:
        return sorted(self._live(self._wall()).values(), key=lambda r: r["updated_at"], reverse=True)

    def save(self, conv_id: str, picked: list[dict]) -> list[str]:
        """Adds checked candidates (parse_candidates' shape). A ticker saved again drops its sources saved more than
        RETENTION_S ago, gains the new ones, and leaves the archive: the user picked it again."""
        with self._locked():
            now = self._wall()
            rows = self._live(now)
            for c in picked:
                row = rows.setdefault(c["ticker"], {"ticker": c["ticker"], "name": "", "sources": [],
                                                    "first_saved_at": now, "updated_at": now, "archived_at": None})
                row["name"] = c["name"] or row["name"]
                row["sources"] = [s for s in row["sources"] if now - s.get("saved_at", now) < RETENTION_S]
                seen = {post_id(s["url"]) for s in row["sources"]}
                row["sources"] += [{**s, "saved_at": now, "conv_id": conv_id}
                                   for s in c["sources"] if post_id(s["url"]) not in seen]
                row.update(updated_at=now, archived_at=None)
            self._write(rows)
        return [c["ticker"] for c in picked]

    def archive(self, ticker: str) -> bool:
        return self._set_archived(ticker, archived=True)

    def restore(self, ticker: str) -> bool:
        return self._set_archived(ticker, archived=False)

    def _set_archived(self, ticker: str, archived: bool) -> bool:
        with self._locked():
            now = self._wall()
            rows = self._live(now)
            row = rows.get(ticker)
            if row is None:
                return False
            row["archived_at"] = (row["archived_at"] or now) if archived else None
            self._write(rows)
        return True

    def _live(self, now: float) -> dict[str, dict]:
        """The rows, without those archived more than RETENTION_S ago."""
        return {t: r for t, r in self._read().items()
                if r["archived_at"] is None or now - r["archived_at"] < RETENTION_S}

    def _read(self) -> dict[str, dict]:
        if not self._path.exists():
            return {}
        rows = {}
        for row in json.loads(self._path.read_text(encoding="utf-8")):
            if isinstance(row, dict) and isinstance(row.get("ticker"), str) and isinstance(row.get("sources"), list):
                if row.pop("dismissed", False):  # 划掉, before 归档 replaced it
                    row.setdefault("archived_at", row.get("updated_at"))
                row.setdefault("archived_at", None)
                rows[row["ticker"]] = row
            else:  # one bad row must not take the whole list down
                log.warning("skipping malformed candidate row: %r", row)
        return rows

    @contextmanager
    def _locked(self) -> Iterator[None]:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with open(self._path.with_suffix(".lock"), "a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)  # released when the file closes
            yield

    def _write(self, rows: dict[str, dict]) -> None:
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps(list(rows.values()), ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, self._path)  # a crash mid-write must not leave a torn list

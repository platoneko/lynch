"""KOL candidates: the block Grok ends a discover reply with (spec §3e) and the app's candidate list (§3f)."""
from __future__ import annotations

import json
import logging
import os
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

log = logging.getLogger(__name__)

BLOCK = re.compile(r"```lynch-candidates[^\n]*\n(.*?)```", re.S)
TICKER = re.compile(r"^[A-Z0-9]+\.(US|HK|SH|SZ|SG)$")
# HK and A-share codes are bare numbers: without a name the reader cannot tell which company it is.
NAME_REQUIRED = ("HK", "SH", "SZ")
STANCES = ("bullish", "bearish", "neutral")
POST_URL = re.compile(r"^https://(?:x|twitter)\.com/[^/\s]+/status/\d+")
SOURCE_FIELDS = ("handle", "stance", "reason", "quote", "url", "posted_at")


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
        urls = {s["url"] for s in known["sources"]}
        for source in candidate["sources"]:  # a post repeated inside one entry counts once too
            if source["url"] not in urls:
                urls.add(source["url"])
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


def _is_iso_time(s: str) -> bool:
    try:
        datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return False
    return True


class CandidateStore:
    """output/candidates/candidates.json, one row per ticker.

    Single writer: only the server's event loop touches it, so no lock. Written only when the user taps
    存入候选区 (spec §3f). Whether a ticker has landed is not stored: its framework file says so.
    """

    def __init__(self, path: Path, wall: Callable[[], float] = time.time) -> None:
        self._path, self._wall = path, wall
        self._rows: dict[str, dict] = {}
        if path.exists():
            for row in json.loads(path.read_text(encoding="utf-8")):
                if isinstance(row, dict) and isinstance(row.get("ticker"), str) and isinstance(row.get("sources"), list):
                    self._rows[row["ticker"]] = row
                else:  # one bad row must not take the whole list down
                    log.warning("skipping malformed candidate row: %r", row)

    def all(self) -> list[dict]:
        return sorted(self._rows.values(), key=lambda r: r["updated_at"], reverse=True)

    def save(self, conv_id: str, picked: list[dict]) -> list[str]:
        """Adds checked candidates (parse_candidates' shape). A ticker saved again gains its new sources and
        leaves the dismissed group: the user picked it again."""
        now = self._wall()
        for c in picked:
            row = self._rows.setdefault(c["ticker"], {"ticker": c["ticker"], "name": "", "sources": [],
                                                      "first_saved_at": now, "updated_at": now, "dismissed": False})
            row["name"] = c["name"] or row["name"]
            urls = {s["url"] for s in row["sources"]}
            row["sources"] += [{**s, "saved_at": now, "conv_id": conv_id} for s in c["sources"] if s["url"] not in urls]
            row.update(updated_at=now, dismissed=False)
        self._save()
        return [c["ticker"] for c in picked]

    def dismiss(self, ticker: str) -> bool:
        row = self._rows.get(ticker)
        if row is None:
            return False
        row["dismissed"] = True
        self._save()
        return True

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps(list(self._rows.values()), ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, self._path)  # a crash mid-write must not leave a torn list

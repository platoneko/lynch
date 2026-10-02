"""KOL candidates: the block Grok ends a discover reply with (spec §3e) and the app's candidate list (§3f)."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime

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
        known["sources"] += [s for s in candidate["sources"] if s["url"] not in urls]
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

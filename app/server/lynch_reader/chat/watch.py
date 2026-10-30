"""The watch list: tickers whose framework update waits on later data (sell-side revisions, a 10-Q, an annual report,
a briefing, a peer's results), each with the day to look again.

A 讨论 chat that advises waiting ends its reply with a lynch-watch block (the rule is in chat/config.py); the user's
加入观察区 saves it. One card per ticker: saving again replaces the card, and only the user's × removes it.
"""
from __future__ import annotations

import json
import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

from lynch_reader.catalog import REPO_ROOT
from lynch_reader.chat.candidates import TICKER
from lynch_reader.daily_cache import BJT, write_json

log = logging.getLogger(__name__)

BLOCK = re.compile(r"```lynch-watch[^\n]*\n(.*?)```", re.S)
DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")
WATCH_FILE = REPO_ROOT / "output" / "watch" / "watch.json"


@dataclass(frozen=True)
class ParsedWatch:
    text: str  # the reply without its block
    card: dict  # {"ticker", "waits": [{"what", "date"}], "recheck"}


def parse_watch(text: str) -> ParsedWatch | None:
    """The reply's last watch block; None when there is none or it breaks the format, which leaves the block in the
    text for the reader to see."""
    blocks = list(BLOCK.finditer(text))
    if not blocks:
        return None
    block = blocks[-1]
    try:
        data = json.loads(block.group(1))
    except ValueError:
        return None
    card = _card(data)
    if card is None:
        return None
    return ParsedWatch((text[:block.start()] + text[block.end():]).strip(), card)


def _card(data: object) -> dict | None:
    if not isinstance(data, dict):
        return None
    ticker, waits, recheck = data.get("ticker"), data.get("waits"), data.get("recheck")
    if not isinstance(ticker, str) or not TICKER.match(ticker) or not isinstance(recheck, str) or not recheck.strip():
        return None
    if not isinstance(waits, list) or not waits:
        return None
    checked = [_wait(w) for w in waits]
    if any(w is None for w in checked):
        return None
    return {"ticker": ticker, "waits": sorted(checked, key=lambda w: w["date"]), "recheck": recheck.strip()}


def _wait(w: object) -> dict | None:
    if not isinstance(w, dict):
        return None
    what, day = w.get("what"), w.get("date")
    if not isinstance(what, str) or not what.strip() or not isinstance(day, str) or not DAY.match(day):
        return None
    try:
        date.fromisoformat(day)
    except ValueError:
        return None
    return {"what": what.strip(), "date": day}


class WatchStore:
    """output/watch/watch.json, one card per ticker.

    The server is the only writer and calls it from async handlers on one event loop, so no lock. A row that fails
    the card format (a hand edit) is skipped on read, so the next save or remove drops it from the file.
    """

    def __init__(self, path: Path, wall: Callable[[], float] = time.time) -> None:
        self._path, self._wall = path, wall

    def save(self, conv_id: str, card: dict) -> None:
        """Replaces the ticker's card: the new one lists everything it still waits on."""
        rows = self._read()
        rows[card["ticker"]] = {**card, "saved_at": self._wall(), "conv_id": conv_id}
        self._write(rows)

    def remove(self, ticker: str) -> bool:
        rows = self._read()
        if rows.pop(ticker, None) is None:
            return False
        self._write(rows)
        return True

    def snapshot(self, chat_starts: list[tuple[str, float]]) -> list[dict]:
        """The `GET /api/watch` items, soonest first. A card is due from its earliest wait's day (Beijing), and
        researched once a 讨论 chat on its ticker starts on or after that day and after the card was saved (the chat
        that saved it does not count)."""
        today = datetime.fromtimestamp(self._wall(), BJT).date().isoformat()
        items = []
        for row in self._read().values():
            due_date = row["waits"][0]["date"]
            due = due_date <= today
            due_day_start = datetime.combine(date.fromisoformat(due_date), datetime.min.time(), BJT).timestamp()
            since = max(row["saved_at"], due_day_start)
            researched = due and any(t == row["ticker"] and created >= since for t, created in chat_starts)
            items.append({**row, "due_date": due_date, "due": due, "researched": researched})
        return sorted(items, key=lambda it: (it["due_date"], it["ticker"]))

    def _read(self) -> dict[str, dict]:
        if not self._path.exists():
            return {}
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except ValueError:
            log.warning("unreadable watch list %s: read as empty", self._path)
            return {}
        rows = {}
        for row in raw if isinstance(raw, list) else []:
            # A hand-edited row must not take the whole list down, nor its × with it.
            card = _card(row)
            stamped = isinstance(row, dict) and isinstance(row.get("saved_at"), (int, float)) \
                and isinstance(row.get("conv_id"), str)
            if card is None or not stamped:
                log.warning("skipping malformed watch row: %r", row)
                continue
            rows[card["ticker"]] = {**card, "saved_at": row["saved_at"], "conv_id": row["conv_id"]}
        return rows

    def _write(self, rows: dict[str, dict]) -> None:
        write_json(self._path, sorted(rows.values(), key=lambda r: r["ticker"]))

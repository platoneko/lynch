"""Paired devices and the one pending pairing code, in a JSON file that both the server and the CLI change.

Only SHA-256 hashes are stored: neither a device cookie nor a pairing code can be read back from the file.
Every change is read-modify-write under an exclusive flock, written to a temp file and swapped in whole.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import secrets
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from lynch_reader.catalog import REPO_ROOT

DEVICES_FILE = REPO_ROOT / "output" / "app-auth" / "devices.json"

# 32 symbols without 0/O/1/I, so a code read off a screen has no look-alikes.
CODE_ALPHABET = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"
CODE_LEN = 8
CODE_TTL_S = 600.0
MAX_FAILURES = 5  # wrong guesses that void the pending code


class InvalidCode(Exception):
    """Wrong, expired, used, voided, or no pending code: one answer for all, so a guesser learns nothing."""


class StoreCorrupt(Exception):
    """The file exists but is not a readable device store; everything is refused until a human fixes it."""


@dataclass(frozen=True)
class Device:
    id: str
    name: str
    paired_at: str


def _sha256(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


def _normalize(code: str) -> str:
    return code.strip().replace("-", "").upper()


def _well_formed(data: object) -> bool:
    if not isinstance(data, dict) or not isinstance(data.get("devices"), list):
        return False
    devices_ok = all(isinstance(d, dict) and all(isinstance(d.get(k), str)
                                                 for k in ("id", "name", "token_sha256", "paired_at"))
                     for d in data["devices"])
    p = data.get("pending")
    pending_ok = p is None or (isinstance(p, dict) and isinstance(p.get("code_sha256"), str)
                               and isinstance(p.get("name"), str)
                               and isinstance(p.get("expires_at"), (int, float))
                               and isinstance(p.get("failures"), int))
    return devices_ok and pending_ok


class DeviceStore:
    def __init__(self, path: Path, clock: Callable[[], float] = time.time) -> None:
        self.path = path
        self._lock_path = path.with_suffix(".lock")
        self._clock = clock
        # The server checks every request; it rereads the file only when the file changed (a CLI revoke
        # replaces it, so the next request sees the revoke).
        self._cached_key: tuple[int, int, int] | None = None
        self._cached_tokens: frozenset[str] = frozenset()

    # ── pairing ──────────────────────────────────────────────────────────

    def create_pairing(self, name: str) -> str:
        """A fresh code for a device to be called `name`; replaces any pending code. Returns it as XXXX-XXXX."""
        code = "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LEN))
        with self._locked() as data:
            data["pending"] = {"code_sha256": _sha256(code), "name": name,
                               "expires_at": self._clock() + CODE_TTL_S, "failures": 0}
        return f"{code[:4]}-{code[4:]}"

    def redeem(self, code: str) -> str:
        """Trade the pending code for a new device; returns that device's cookie value."""
        token = None
        with self._locked() as data:
            name = self._consume_pending(data, _normalize(code))
            if name is not None:
                token = secrets.token_urlsafe(32)
                data["devices"].append({"id": self._new_id(data), "name": name,
                                        "token_sha256": _sha256(token), "paired_at": self._now_iso()})
        # Raised after the block so a wrong guess's failure count is still written.
        if token is None:
            raise InvalidCode
        return token

    def _consume_pending(self, data: dict, code: str) -> str | None:
        """The new device's name if `code` is the live pending code; voids the code once used, expired,
        or guessed at too often."""
        p = data["pending"]
        if p is None:
            return None
        if self._clock() >= p["expires_at"]:
            data["pending"] = None
            return None
        if not secrets.compare_digest(_sha256(code), p["code_sha256"]):
            p["failures"] += 1
            if p["failures"] >= MAX_FAILURES:
                data["pending"] = None
            return None
        data["pending"] = None
        return p["name"]

    # ── devices ──────────────────────────────────────────────────────────

    def is_paired(self, token: str) -> bool:
        return _sha256(token) in self._token_hashes()

    def devices(self) -> list[Device]:
        return [Device(d["id"], d["name"], d["paired_at"]) for d in self._read()["devices"]]

    def revoke(self, device_id: str) -> bool:
        with self._locked() as data:
            kept = [d for d in data["devices"] if d["id"] != device_id]
            removed = len(kept) != len(data["devices"])
            data["devices"] = kept
        return removed

    # ── file ─────────────────────────────────────────────────────────────

    def _token_hashes(self) -> frozenset[str]:
        try:
            st = self.path.stat()
        except FileNotFoundError:
            return frozenset()
        except OSError as e:
            raise StoreCorrupt(f"{self.path}: {e}") from e
        # The inode changes on every os.replace, so a rewrite within one mtime tick is still seen.
        key = (st.st_ino, st.st_mtime_ns, st.st_size)
        if key != self._cached_key:
            self._cached_tokens = frozenset(d["token_sha256"] for d in self._read()["devices"])
            self._cached_key = key
        return self._cached_tokens

    def _read(self) -> dict:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {"devices": [], "pending": None}
        except (OSError, ValueError) as e:
            raise StoreCorrupt(f"{self.path}: {e}") from e
        if not _well_formed(data):
            raise StoreCorrupt(f"{self.path}: not a device store")
        data.setdefault("pending", None)
        return data

    @contextmanager
    def _locked(self) -> Iterator[dict]:
        """Read-modify-write: the yielded dict is written back unless the block raises.

        A corrupt file raises StoreCorrupt from the read, before anything is written.
        """
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(self._lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            data = self._read()
            yield data
            self._write(data)
        finally:
            os.close(fd)  # releases the flock

    def _write(self, data: dict) -> None:
        tmp = self.path.with_name(self.path.name + ".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self.path)

    def _new_id(self, data: dict) -> str:
        taken = {d["id"] for d in data["devices"]}
        while (device_id := secrets.token_hex(4)) in taken:
            pass
        return device_id

    def _now_iso(self) -> str:
        return datetime.fromtimestamp(self._clock()).astimezone().isoformat(timespec="seconds")

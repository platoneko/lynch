"""Shared, bounded reads of explicitly configured external history files."""
from __future__ import annotations

import os
from pathlib import Path
import re
import stat

from eval.lib.valuation_drafts import _json_bytes, _load_json

FORBIDDEN = re.compile(r'(?:^|[._-])(config|secrets?|credentials?|\.env)(?:[._-]|$)', re.I)
MAX_SOURCE = 4 * 1024 * 1024


def validate_history_path(source: Path) -> None:
    if (not source.is_absolute() or '..' in source.parts or source.suffix.lower() != '.json'
            or any(FORBIDDEN.search(part) for part in source.parts)):
        raise ValueError('external history must be an explicitly named JSON file')


def open_regular(root: Path, relative: str) -> int:
    parts = Path(relative).parts
    if not parts or Path(relative).is_absolute() or any(p in ('.', '..') or FORBIDDEN.search(p) for p in parts):
        raise ValueError('unsafe declared path')
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in parts[:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        # Nonblocking open prevents a FIFO from stalling the whole pool.
        child = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
        if not stat.S_ISREG(os.fstat(child).st_mode):
            os.close(child)
            raise ValueError('declared input must be a regular file')
        return child
    finally:
        os.close(fd)


def read_history(source: Path, parent: Path) -> bytes:
    """Read one stable, bounded JSON object without following a file symlink."""
    validate_history_path(source)
    if source.parent.resolve(strict=True) != parent:
        raise ValueError('configured external source parent changed')
    with os.fdopen(open_regular(parent, source.name), 'rb') as stream:
        before = os.fstat(stream.fileno())
        data = stream.read(MAX_SOURCE + 1)
        after = os.fstat(stream.fileno())
    with os.fdopen(open_regular(parent, source.name), 'rb') as current:
        path_version = os.fstat(current.fileno())
    def version(info):
        return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns
    if (len(data) > MAX_SOURCE or len(data) != before.st_size or version(before) != version(after)
            or version(before) != version(path_version)):
        raise ValueError('external history changed during read or exceeds size limit')
    value = _load_json(data)
    if not isinstance(value, dict):
        raise ValueError('external history must be an object')
    _json_bytes(value)
    # The selected parent may have been redirected after the first check.
    if source.parent.resolve(strict=True) != parent:
        raise ValueError('configured external source parent changed')
    return data

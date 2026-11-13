"""Budgeted discovery of existing files, without importing or interpreting them."""
from __future__ import annotations

import os
from pathlib import Path
import re

from eval.lib.research_store import ResearchError


def discover_local(roots: list[Path], *, symbol: str | None = None, name: str | None = None,
                   max_files: int = 2000, max_depth: int = 6, limit: int = 20) -> dict:
    if not roots or not (symbol or name) or min(max_files, limit) < 1 or max_depth < 0:
        raise ResearchError('input', '本地发现需要明确目录、标的或文件名及有效扫描范围')
    normalized = []
    for root in roots:
        resolved = root.resolve()
        if not root.is_absolute() or resolved in (Path('/'), Path.home()) or not resolved.is_dir():
            raise ResearchError('input', '本地发现需要具体、可读取的绝对资料目录')
        normalized.append(resolved)
    needle = (symbol.split('.')[0] if symbol else name).casefold()
    pattern = re.compile(r'(?<![a-z0-9])' + re.escape(needle) + r'(?![a-z0-9])')
    items, scanned, partial = [], 0, False
    visited = set()
    errors = []
    for root in normalized:
        for directory, dirs, files in os.walk(root, followlinks=False, onerror=lambda _error: errors.append(True)):
            parent = Path(directory)
            depth = len(parent.relative_to(root).parts)
            dirs[:] = sorted(d for d in dirs if not (parent / d).is_symlink())
            if depth >= max_depth:
                partial = partial or bool(dirs)
                dirs[:] = []
            for filename in sorted(files):
                path = parent / filename
                if path.is_symlink() or path in visited:
                    continue
                if scanned >= max_files:
                    return {'items': items, 'scanned': scanned, 'partial': True, 'roots': list(map(str, normalized))}
                visited.add(path)
                scanned += 1
                relative = str(Path(root.name) / path.relative_to(root))
                if not pattern.search(relative.casefold()):
                    continue
                if len(items) >= limit:
                    partial = True
                    continue
                try:
                    stat = path.stat()
                except OSError:
                    errors.append(True)
                    continue
                items.append({'path': str(path), 'size': stat.st_size, 'observed_file_mtime': stat.st_mtime,
                              'content_checked': False})
    return {'items': items, 'scanned': scanned, 'partial': partial or bool(errors), 'roots': list(map(str, normalized))}

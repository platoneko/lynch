"""The KOL sources invest tracks on X, read through invest's own loaders (spec §3c).

invest decides which config wins (its gitignored override over the checked-in template) and how far back to
look; calling its loaders keeps those rules in invest alone.
"""
from __future__ import annotations

import asyncio
import json
import sys
from dataclasses import dataclass
from pathlib import Path

READ_TIMEOUT_S = 10.0
DEFAULT_LOOKBACK_HOURS = 36  # invest's own fallback when [fetch.tweets] sets none (scripts/fetch-tweets.py)

_READ = """\
import json, sys
sys.path.insert(0, sys.argv[1])
import load_kol_handles
from load_policies import POLICIES
tweets = (POLICIES.get("fetch") or {}).get("tweets") or {}
print(json.dumps({"list_id": load_kol_handles.load_list_id(), "handles": load_kol_handles.load_handles(),
                  "lookback_hours": tweets.get("lookback_hours")}))
"""


class KolUnavailable(Exception):
    pass


@dataclass(frozen=True)
class KolSources:
    list_id: str | None  # an X List id
    handles: tuple[str, ...]  # without "@"
    lookback_hours: int


async def kol_sources(invest_root: Path, python: str = sys.executable) -> KolSources:
    scripts = invest_root / "scripts"
    if not (scripts / "load_kol_handles.py").is_file():
        raise KolUnavailable(f"找不到 invest 的 KOL 名单加载脚本：{scripts}")
    proc = await asyncio.create_subprocess_exec(
        python, "-B", "-c", _READ, str(scripts), cwd=invest_root,  # -B: leave no __pycache__ in invest
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        out, err = await asyncio.wait_for(proc.communicate(), READ_TIMEOUT_S)
    except TimeoutError as e:
        proc.kill()
        await proc.wait()
        raise KolUnavailable("读取 invest 的 KOL 名单超时") from e
    if proc.returncode != 0:
        last = (err.decode(errors="replace").strip().splitlines() or [f"exit {proc.returncode}"])[-1]
        raise KolUnavailable(f"读取 invest 的 KOL 名单失败：{last}")
    try:
        data = json.loads(out)
    except ValueError as e:
        raise KolUnavailable("invest 的 KOL 名单加载脚本输出无法解析") from e

    hours = data.get("lookback_hours")
    if not isinstance(hours, int) or isinstance(hours, bool) or hours <= 0:
        hours = DEFAULT_LOOKBACK_HOURS
    sources = KolSources(data.get("list_id") or None, tuple(data.get("handles") or ()), hours)
    if sources.list_id is None and not sources.handles:
        raise KolUnavailable("invest 的 KOL 名单为空（config/kol_handles.toml 里 list_id 和 handles 都没填）")
    return sources

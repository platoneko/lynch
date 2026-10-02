"""The KOL source invest tracks on X, read through invest's own loaders (spec §3c): its X List, whose members
the user keeps on X, and how far back to look. invest's handle list is not used: discover searches the List only.

invest decides which config wins (its gitignored override over the checked-in template) and how far back to
look; calling its loaders keeps those rules in invest alone.
"""
from __future__ import annotations

import asyncio
import json
import sys
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from lynch_reader.catalog import REPO_ROOT

READ_TIMEOUT_S = 10.0
DEFAULT_LOOKBACK_HOURS = 36  # invest's own fallback when [fetch.tweets] sets none (scripts/fetch-tweets.py)
# invest is checked out next to the main lynch checkout. frameworks/ (a real dir there, a symlink in a worktree)
# always lives in that checkout, so it finds the place from a worktree too.
INVEST_ROOT = (REPO_ROOT / "frameworks").resolve().parent.parent / "invest"

_READ = """\
import json, sys
sys.path.insert(0, sys.argv[1])
import load_kol_handles
from load_policies import POLICIES
tweets = (POLICIES.get("fetch") or {}).get("tweets") or {}
print(json.dumps({"list_id": load_kol_handles.load_list_id(), "lookback_hours": tweets.get("lookback_hours")}))
"""


class KolUnavailable(Exception):
    pass


@dataclass(frozen=True)
class KolSources:
    list_id: str  # an X List id
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
    list_id = data.get("list_id")
    if not isinstance(list_id, str) or not list_id.strip():
        raise KolUnavailable("invest 的 KOL 配置里没有 X 列表（config/kol_handles.toml 的 list_id）")
    return KolSources(list_id.strip(), hours)


def kol_brief(kol: KolSources, now: datetime) -> str:
    """The KOL source as the search rules expect it: which List, and the exact UTC moment the lookback starts.

    X's `since:` filters by date only, so the start moment is spelled out: posts before it do not count and paging
    stops there. The app sends this to Grok every turn and `just kol-sources` prints it, so both read the same words.
    """
    start = now.astimezone(UTC) - timedelta(hours=kol.lookback_hours)
    moment, day = start.strftime("%Y-%m-%d %H:%M UTC"), start.date().isoformat()
    return "\n".join([
        f"- X 列表：{kol.list_id}（https://x.com/i/lists/{kol.list_id}，搜索写 list:{kol.list_id}）",
        f"- 回看范围：最近 {kol.lookback_hours} 小时，即 {moment} 之后发的帖子；搜索写 since:{day}"
        f"（since 只按日期过滤，早于 {moment} 的帖子不算，翻到这里就停）",
    ])

from __future__ import annotations

import asyncio

import pytest

from lynch_reader.chat.kol import KolSources, KolUnavailable, kol_sources
from tests.chat_helpers import write_invest


def test_reads_list_and_lookback(tmp_path):
    root = write_invest(tmp_path / "invest", list_id="123", handles=("alice", "bob"), hours=48)
    assert asyncio.run(kol_sources(root)) == KolSources("123", 48)


def test_lookback_falls_back_to_invest_default(tmp_path):
    root = write_invest(tmp_path / "invest", hours=None)
    assert asyncio.run(kol_sources(root)).lookback_hours == 36


def test_handles_without_a_list_are_refused(tmp_path):
    root = write_invest(tmp_path / "invest", list_id=None, handles=("alice",))  # the search goes by the X list only
    with pytest.raises(KolUnavailable, match="没有 X 列表"):
        asyncio.run(kol_sources(root))


def test_empty_list_is_refused(tmp_path):
    root = write_invest(tmp_path / "invest", list_id=None, handles=())
    with pytest.raises(KolUnavailable, match="没有 X 列表"):
        asyncio.run(kol_sources(root))


def test_missing_invest_is_refused(tmp_path):
    with pytest.raises(KolUnavailable, match="找不到"):
        asyncio.run(kol_sources(tmp_path / "nowhere"))


def test_loader_failure_is_reported(tmp_path):
    root = write_invest(tmp_path / "invest", broken=True)
    with pytest.raises(KolUnavailable, match="bad policies.toml"):
        asyncio.run(kol_sources(root))


from datetime import UTC, datetime, timedelta, timezone

from lynch_reader.chat.kol import kol_brief

NOW = datetime(2026, 10, 3, 1, 0, tzinfo=UTC)


def test_brief_names_the_list_window_and_start_date():
    assert kol_brief(KolSources("123", 36), NOW).splitlines() == [
        "- X 列表：123（https://x.com/i/lists/123，搜索写 list:123）",
        "- 回看范围：最近 36 小时，即 2026-10-01 13:00 UTC 之后发的帖子；搜索写 since:2026-10-01"
        "（since 只按日期过滤，早于 2026-10-01 13:00 UTC 的帖子不算，翻到这里就停）",
    ]


def test_brief_start_date_is_utc():
    east8 = datetime(2026, 10, 3, 9, 0, tzinfo=timezone(timedelta(hours=8)))  # 01:00 UTC
    assert "即 2026-10-01 13:00 UTC 之后" in kol_brief(KolSources("1", 36), east8)

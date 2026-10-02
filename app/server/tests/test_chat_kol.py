from __future__ import annotations

import asyncio

import pytest

from lynch_reader.chat.kol import KolSources, KolUnavailable, kol_sources
from tests.chat_helpers import write_invest


def test_reads_list_handles_and_lookback(tmp_path):
    root = write_invest(tmp_path / "invest", list_id="123", handles=("alice", "bob"), hours=48)
    assert asyncio.run(kol_sources(root)) == KolSources("123", ("alice", "bob"), 48)


def test_lookback_falls_back_to_invest_default(tmp_path):
    root = write_invest(tmp_path / "invest", hours=None)
    assert asyncio.run(kol_sources(root)).lookback_hours == 36


def test_handles_alone_are_enough(tmp_path):
    root = write_invest(tmp_path / "invest", list_id=None, handles=("alice",))
    assert asyncio.run(kol_sources(root)) == KolSources(None, ("alice",), 36)


def test_empty_list_is_refused(tmp_path):
    root = write_invest(tmp_path / "invest", list_id=None, handles=())
    with pytest.raises(KolUnavailable, match="为空"):
        asyncio.run(kol_sources(root))


def test_missing_invest_is_refused(tmp_path):
    with pytest.raises(KolUnavailable, match="找不到"):
        asyncio.run(kol_sources(tmp_path / "nowhere"))


def test_loader_failure_is_reported(tmp_path):
    root = write_invest(tmp_path / "invest", broken=True)
    with pytest.raises(KolUnavailable, match="bad policies.toml"):
        asyncio.run(kol_sources(root))

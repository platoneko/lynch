from __future__ import annotations

import json
from dataclasses import asdict

from lynch_reader.chat.index import TITLE_MAX, ChatIndex, make_title
from tests.chat_helpers import conv


def test_put_persists_and_reloads(tmp_path):
    path = tmp_path / "chat" / "index.json"
    ChatIndex(path).put(conv())
    assert ChatIndex(path).get("c1") == conv()


def test_update_replaces_fields_and_persists(tmp_path):
    path = tmp_path / "index.json"
    idx = ChatIndex(path)
    idx.put(conv())
    out = idx.update("c1", archived=True, context_window=1000)
    assert out.archived and out.context_window == 1000
    assert ChatIndex(path).get("c1").archived


def test_all_lists_every_row(tmp_path):
    idx = ChatIndex(tmp_path / "index.json")
    idx.put(conv(id="a"))
    idx.put(conv(id="b", archived=True))
    assert sorted(c.id for c in idx.all()) == ["a", "b"]


def test_unknown_fields_are_ignored_on_load(tmp_path):
    path = tmp_path / "index.json"
    path.write_text(json.dumps([{**asdict(conv()), "future_field": 1}]), encoding="utf-8")
    assert ChatIndex(path).get("c1") == conv()


def test_make_title_collapses_whitespace_and_truncates():
    assert make_title("  研究一下\n宁德时代  ") == "研究一下 宁德时代"
    title = make_title("x" * 100)
    assert len(title) == TITLE_MAX and title.endswith("…")

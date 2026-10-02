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


def test_malformed_rows_are_skipped_on_load(tmp_path, caplog):
    path = tmp_path / "index.json"
    good = asdict(conv())
    missing = {k: v for k, v in asdict(conv(id="c2")).items() if k != "title"}
    wrong_type = {**asdict(conv(id="c3")), "updated_at": "yesterday"}
    path.write_text(json.dumps([good, missing, wrong_type, "not a row"]), encoding="utf-8")
    idx = ChatIndex(path)
    assert [c.id for c in idx.all()] == ["c1"]
    assert sum("malformed" in r.message for r in caplog.records) == 3


def test_make_title_collapses_whitespace_and_truncates():
    assert make_title("  研究一下\n宁德时代  ") == "研究一下 宁德时代"
    title = make_title("x" * 100)
    assert len(title) == TITLE_MAX and title.endswith("…")


def test_discover_fields_default_and_are_type_checked(tmp_path):
    path = tmp_path / "index.json"
    base = {"id": "a", "kind": "research", "ticker": None, "title": "t", "model": "m", "effort": "e",
            "created_at": 1.0, "updated_at": 1.0}
    path.write_text(json.dumps([
        base,
        {**base, "id": "b", "kind": "discover", "grok_session": "s1", "cost_usd": 0.25},
        {**base, "id": "c", "cost_usd": "free"},
        {**base, "id": "d", "grok_session": 7},
        {**base, "id": "e", "kind": "discover", "lookback_days": 3},
        {**base, "id": "f", "lookback_days": "2"},
        {**base, "id": "g", "lookback_days": True},
    ]), encoding="utf-8")
    index = ChatIndex(path)
    assert [c.id for c in index.all()] == ["a", "b", "e"]
    assert (index.get("a").lookback_days, index.get("e").lookback_days) == (None, 3)
    assert (index.get("a").grok_session, index.get("a").cost_usd) == (None, None)
    assert (index.get("b").grok_session, index.get("b").cost_usd) == ("s1", 0.25)

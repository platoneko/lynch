from __future__ import annotations

import asyncio
import json
from datetime import datetime

import pytest

from lynch_reader.chat.service import Conflict, Invalid
from lynch_reader.chat.watch import WatchStore, parse_watch
from lynch_reader.daily_cache import BJT
from tests.chat_helpers import make_service, state_of, until


def block(card, extra=""):
    return f"先不落盘。\n\n```lynch-watch\n{json.dumps(card, ensure_ascii=False)}\n```{extra}"


def card(**over):
    return {"ticker": "SOFI.US", "recheck": "分部营销费；FY27 每股收益",
            "waits": [{"what": "卖方刷新 FY27 每股收益（约一两周）", "date": "2026-11-10"},
                      {"what": "10-Q（预计 11 月上旬）", "date": "2026-11-07"}], **over}


def bjt(day: str, hour: int = 12) -> float:
    return datetime.fromisoformat(f"{day}T{hour:02d}:00:00").replace(tzinfo=BJT).timestamp()


def test_valid_block_is_stripped_and_waits_sorted_by_date():
    p = parse_watch(block(card()))
    assert p.text == "先不落盘。"
    assert p.card == {"ticker": "SOFI.US", "recheck": "分部营销费；FY27 每股收益",
                      "waits": [{"what": "10-Q（预计 11 月上旬）", "date": "2026-11-07"},
                                {"what": "卖方刷新 FY27 每股收益（约一两周）", "date": "2026-11-10"}]}


def test_no_block_or_a_broken_one_leaves_the_text_alone():
    assert parse_watch("just text") is None
    assert parse_watch("x\n```lynch-watch\n{oops\n```") is None
    for bad in (card(ticker="sofi"), card(recheck=" "), card(waits=[]), card(waits="10-Q"),
                card(waits=[{"what": "10-Q", "date": "11月上旬"}]), card(waits=[{"what": "10-Q", "date": "2026-02-30"}]),
                card(waits=[{"what": "", "date": "2026-11-07"}]), ["not", "a", "card"]):
        assert parse_watch(block(bad)) is None, bad


def test_save_replaces_the_tickers_card_and_remove_drops_it(tmp_path):
    store = WatchStore(tmp_path / "watch.json", wall=lambda: bjt("2026-10-30"))
    store.save("c1", parse_watch(block(card())).card)
    store.save("c1", parse_watch(block(card(ticker="MSFT.US", waits=[{"what": "卖方", "date": "2026-11-01"}]))).card)
    store.save("c2", parse_watch(block(card(waits=[{"what": "卖方刷新", "date": "2026-11-12"}]))).card)
    [msft, sofi] = store.snapshot([])
    assert (msft["ticker"], sofi["ticker"]) == ("MSFT.US", "SOFI.US")  # soonest first
    assert sofi["waits"] == [{"what": "卖方刷新", "date": "2026-11-12"}] and sofi["conv_id"] == "c2"
    assert store.remove("SOFI.US") and not store.remove("SOFI.US")
    assert [it["ticker"] for it in store.snapshot([])] == ["MSFT.US"]


def test_due_from_the_earliest_day_and_researched_by_a_chat_started_since(tmp_path):
    now = [bjt("2026-11-06", 23)]
    store = WatchStore(tmp_path / "watch.json", wall=lambda: now[0])
    store.save("c1", parse_watch(block(card())).card)
    [it] = store.snapshot([])
    assert (it["due_date"], it["due"], it["researched"]) == ("2026-11-07", False, False)
    now[0] = bjt("2026-11-07", 0)  # midnight in Beijing
    early_chat, due_day_chat = ("SOFI.US", bjt("2026-11-06")), ("SOFI.US", bjt("2026-11-07", 9))
    [it] = store.snapshot([early_chat, ("MSFT.US", bjt("2026-11-07", 9))])
    assert (it["due"], it["researched"]) == (True, False)
    [it] = store.snapshot([early_chat, due_day_chat])
    assert it["researched"]


def test_only_a_ticker_chat_saves_a_card_on_its_own_ticker(tmp_path):
    async def main():
        svc, _ = make_service(tmp_path)
        research = await svc.create("research", None, block(card()), "claude-opus-5-5", "medium")
        other = await svc.create("ticker", "MSFT.US", block(card()), "claude-opus-5-5", "medium")
        own = await svc.create("ticker", "SOFI.US", block(card()), "claude-opus-5-5", "medium")
        for conv in (research, other, own):
            await until(lambda: state_of(svc, conv.id) == "idle")
        key = lambda conv: [i for i in svc.snapshot(conv.id)["items"] if i.get("watch")][0]["key"]
        with pytest.raises(Invalid, match="讨论已有框架"):
            svc.save_watch(research.id, key(research))
        with pytest.raises(Conflict, match="MSFT.US"):
            svc.save_watch(other.id, key(other))
        svc.save_watch(own.id, key(own))
        rows = svc.watch_list()
        await svc.shutdown()
        return own, rows
    own, rows = asyncio.run(main())
    assert [(r["ticker"], r["conv_id"]) for r in rows] == [("SOFI.US", own.id)]


def test_a_malformed_row_is_skipped_not_fatal(tmp_path):
    path = tmp_path / "watch.json"
    good = {**parse_watch(block(card())).card, "saved_at": 1.0, "conv_id": "c1"}
    path.write_text(json.dumps([{"ticker": "MSFT.US"}, {**good, "ticker": "AAPL.US", "waits": []}, "x", good]))
    store = WatchStore(path, wall=lambda: bjt("2026-10-30"))
    assert [it["ticker"] for it in store.snapshot([])] == ["SOFI.US"]
    assert store.remove("SOFI.US") and store.snapshot([]) == []


def test_the_chat_that_saved_a_due_card_does_not_count_as_research(tmp_path):
    store = WatchStore(tmp_path / "watch.json", wall=lambda: bjt("2026-11-08", 10))
    store.save("c1", parse_watch(block(card())).card)  # due since 11-07
    [it] = store.snapshot([("SOFI.US", bjt("2026-11-08", 9))])
    assert (it["due"], it["researched"]) == (True, False)
    [it] = store.snapshot([("SOFI.US", bjt("2026-11-08", 11))])
    assert it["researched"]

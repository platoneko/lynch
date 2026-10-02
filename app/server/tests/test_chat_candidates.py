from __future__ import annotations

import json

from lynch_reader.chat.candidates import parse_candidates
from lynch_reader.chat.grok import discover_rules
from lynch_reader.chat.kol import KolSources


def src(handle="alice", url="https://x.com/alice/status/1", **over):
    return {"handle": handle, "stance": "bullish", "reason": "便宜", "quote": "cheap", "url": url,
            "posted_at": "2026-10-01T08:00:00Z", **over}


def block(*candidates, extra=""):
    return f"正文\n\n```lynch-candidates\n{json.dumps({'candidates': list(candidates)}, ensure_ascii=False)}\n```{extra}"


def test_valid_block_is_stripped_and_parsed():
    p = parse_candidates(block({"ticker": "0700.HK", "name": "腾讯控股", "sources": [src(handle="@alice")]}))
    assert p.text == "正文" and p.dropped == 0
    assert p.candidates == [{"ticker": "0700.HK", "name": "腾讯控股", "sources": [src()]}]


def test_no_block_or_bad_json_leaves_the_text_alone():
    assert parse_candidates("just text") is None
    assert parse_candidates("x\n```lynch-candidates\n{oops\n```") is None
    assert parse_candidates('```lynch-candidates\n{"candidates": "nope"}\n```') is None


def test_hk_and_a_shares_need_a_name_us_does_not():
    p = parse_candidates(block({"ticker": "600519.SH", "name": "", "sources": [src()]},
                               {"ticker": "0700.HK", "sources": [src()]},
                               {"ticker": "AXTI.US", "sources": [src(url="https://x.com/a/status/9")]}))
    assert [c["ticker"] for c in p.candidates] == ["AXTI.US"] and p.candidates[0]["name"] == ""
    assert p.dropped == 2


def test_bad_tickers_stances_urls_and_times_are_dropped():
    p = parse_candidates(block(
        {"ticker": "axti.us", "name": "x", "sources": [src()]},
        {"ticker": "AXTI.NYSE", "name": "x", "sources": [src()]},
        {"ticker": "AAPL.US", "name": "Apple", "sources": [
            src(stance="bullish-ish"), src(url="https://example.com/1"), src(posted_at="yesterday"),
            src(quote=""), src(url="https://twitter.com/c/status/3")]},
    ))
    assert [c["ticker"] for c in p.candidates] == ["AAPL.US"]
    assert [s["url"] for s in p.candidates[0]["sources"]] == ["https://twitter.com/c/status/3"]
    assert p.dropped == 2 + 4


def test_same_ticker_twice_merges_sources_by_url():
    p = parse_candidates(block({"ticker": "AXTI.US", "name": "", "sources": [src()]},
                               {"ticker": "AXTI.US", "name": "AXT Inc", "sources": [
                                   src(), src(handle="bob", url="https://x.com/bob/status/2")]}))
    [c] = p.candidates
    assert c["name"] == "AXT Inc" and [s["handle"] for s in c["sources"]] == ["alice", "bob"]


def test_last_block_wins():
    text = block({"ticker": "AAPL.US", "name": "Apple", "sources": [src()]}) + "\n" + \
        block({"ticker": "MSFT.US", "name": "Microsoft", "sources": [src()]})
    assert [c["ticker"] for c in parse_candidates(text).candidates] == ["MSFT.US"]


def test_the_example_in_grok_rules_is_a_valid_block():
    p = parse_candidates(discover_rules(KolSources("1", ("a",), 36)))
    assert p is not None and p.dropped == 0 and p.candidates[0]["ticker"] == "0700.HK"

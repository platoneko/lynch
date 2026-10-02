from __future__ import annotations

import fcntl
import json
import threading

from lynch_reader.catalog import REPO_ROOT
import pytest

from lynch_reader.chat.candidates import CandidateStore, configured_source_days, parse_candidates

SEARCH_MD = REPO_ROOT / ".agents" / "skills" / "discover-kol" / "references" / "search.md"


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


def test_same_url_twice_in_one_entry_keeps_one_source():
    p = parse_candidates(block({"ticker": "AXTI.US", "name": "", "sources": [src(), src()]}))
    [c] = p.candidates
    assert [s["url"] for s in c["sources"]] == ["https://x.com/alice/status/1"] and p.dropped == 0

def test_last_block_wins():
    text = block({"ticker": "AAPL.US", "name": "Apple", "sources": [src()]}) + "\n" + \
        block({"ticker": "MSFT.US", "name": "Microsoft", "sources": [src()]})
    assert [c["ticker"] for c in parse_candidates(text).candidates] == ["MSFT.US"]


def test_the_example_in_the_skill_is_a_valid_block():
    text = SEARCH_MD.read_text(encoding="utf-8")
    assert text.splitlines()[0] == "# lynch 发现新票：搜索规则与候选格式"
    p = parse_candidates(text)
    assert p is not None and p.dropped == 0 and p.candidates[0]["ticker"] == "0700.HK"


class Clock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


def cand(ticker="AXTI.US", name="AXT Inc", *sources):
    return {"ticker": ticker, "name": name, "sources": list(sources) or [src()]}


def test_save_then_reload(tmp_path):
    clock = Clock()
    store = CandidateStore(tmp_path / "c" / "candidates.json", clock)
    assert store.save("conv1", [cand()]) == ["AXTI.US"]
    [row] = CandidateStore(tmp_path / "c" / "candidates.json").all()
    assert row["ticker"] == "AXTI.US" and row["name"] == "AXT Inc" and row["archived_at"] is None
    assert row["first_saved_at"] == row["updated_at"] == 100.0
    assert row["sources"] == [{**src(), "saved_at": 100.0, "conv_id": "conv1"}]


def test_saving_twice_merges_sources_without_duplicates(tmp_path):
    clock = Clock()
    store = CandidateStore(tmp_path / "candidates.json", clock)
    store.save("conv1", [cand()])
    clock.t = 200.0
    store.save("conv2", [cand("AXTI.US", "", src(), src(handle="bob", url="https://x.com/bob/status/2"))])
    [row] = store.all()
    assert [s["handle"] for s in row["sources"]] == ["alice", "bob"]
    assert row["name"] == "AXT Inc" and (row["first_saved_at"], row["updated_at"]) == (100.0, 200.0)


def test_archive_and_restore(tmp_path):
    clock = Clock()
    store = CandidateStore(tmp_path / "candidates.json", clock)
    store.save("conv1", [cand()])
    clock.t = 150.0
    assert store.archive("AXTI.US") is True and store.all()[0]["archived_at"] == 150.0
    assert store.restore("AXTI.US") is True and store.all()[0]["archived_at"] is None
    assert store.archive("NOPE.US") is False and store.restore("NOPE.US") is False


def test_an_archived_ticker_saved_again_returns_to_todo(tmp_path):
    store = CandidateStore(tmp_path / "candidates.json", Clock())
    store.save("conv1", [cand()])
    store.archive("AXTI.US")
    store.save("conv2", [cand("AXTI.US", "", src(handle="bob", url="https://x.com/bob/status/2"))])
    [row] = store.all()
    assert row["archived_at"] is None and [s["handle"] for s in row["sources"]] == ["alice", "bob"]


DAY = 86400.0


def test_an_archived_ticker_is_gone_after_30_days(tmp_path):
    path = tmp_path / "candidates.json"
    clock = Clock()
    store = CandidateStore(path, clock)
    store.save("conv1", [cand()])
    store.archive("AXTI.US")
    clock.t += 30 * DAY - 1
    assert [r["ticker"] for r in store.all()] == ["AXTI.US"]
    clock.t += 1
    assert store.all() == []  # hidden at once
    assert "AXTI.US" in path.read_text(encoding="utf-8")  # removed from the file on the next write
    store.save("conv1", [cand("MSFT.US", "Microsoft")])
    assert "AXTI.US" not in path.read_text(encoding="utf-8")


def post(handle, day):
    return src(handle=handle, url=f"https://x.com/{handle}/status/{day}", posted_at=f"2026-10-{day:02d}T08:00:00Z")


def test_saving_again_drops_old_sources_except_the_five_kols_shown(tmp_path):
    clock = Clock()
    store = CandidateStore(tmp_path / "candidates.json", clock)
    seven = [post("alice", 1), post("alice", 2), post("bob", 3), post("carol", 4), post("dave", 5), post("erin", 6),
             post("frank", 7)]
    store.save("conv1", [cand("AXTI.US", "AXT Inc", *seven), cand("MSFT.US", "Microsoft", post("alice", 1))])
    clock.t += 8 * DAY
    assert len({r["ticker"]: r for r in store.all()}["AXTI.US"]["sources"]) == 7  # lazy: nothing goes before a save
    store.save("conv2", [cand("AXTI.US", "", post("gina", 8))])
    rows = {r["ticker"]: r for r in store.all()}
    # Shown: the latest post of the 5 newest KOLs (gina, frank, erin, dave, carol), kept however old. The rest were
    # saved over 7 days ago: alice's two posts and bob's go.
    assert sorted(s["handle"] for s in rows["AXTI.US"]["sources"]) == ["carol", "dave", "erin", "frank", "gina"]
    assert [s["handle"] for s in rows["MSFT.US"]["sources"]] == ["alice"]  # not saved again: untouched


def test_sources_within_7_days_all_stay(tmp_path):
    clock = Clock()
    store = CandidateStore(tmp_path / "candidates.json", clock)
    store.save("conv1", [cand("AXTI.US", "AXT Inc", post("alice", 1), post("alice", 2))])
    clock.t += 6 * DAY
    store.save("conv2", [cand("AXTI.US", "", post("bob", 3))])
    assert len(store.all()[0]["sources"]) == 3


def test_source_retention_is_configurable(tmp_path):
    clock = Clock()
    store = CandidateStore(tmp_path / "candidates.json", clock, source_days=14)
    store.save("conv1", [cand("AXTI.US", "AXT Inc", post("alice", 1), post("alice", 2))])
    clock.t += 13 * DAY
    store.save("conv2", [cand("AXTI.US", "", post("bob", 3))])
    assert len(store.all()[0]["sources"]) == 3
    clock.t += 2 * DAY
    store.save("conv3", [cand("AXTI.US", "", post("bob", 4))])
    assert [s["posted_at"][8:10] for s in store.all()[0]["sources"]] == ["02", "03", "04"]  # alice's older post goes


def test_source_days_reads_the_environment():
    assert configured_source_days({}) == 7
    assert configured_source_days({"LYNCH_CANDIDATE_SOURCE_DAYS": "14"}) == 14
    for bad in ("0", "-1", "7d", ""):
        with pytest.raises(ValueError):
            configured_source_days({"LYNCH_CANDIDATE_SOURCE_DAYS": bad})


def test_a_row_dismissed_before_archiving_existed_reads_as_archived(tmp_path):
    path = tmp_path / "candidates.json"
    path.write_text(json.dumps([{"ticker": "OLD.US", "name": "", "sources": [], "first_saved_at": 1.0,
                                 "updated_at": 5.0, "dismissed": True}]), encoding="utf-8")
    [row] = CandidateStore(path, Clock()).all()
    assert row["archived_at"] == 5.0 and "dismissed" not in row


def test_newest_first_and_malformed_rows_skipped(tmp_path):
    path = tmp_path / "candidates.json"
    path.write_text(json.dumps([{"ticker": 5}, {"ticker": "OLD.US", "name": "", "sources": [], "first_saved_at": 1.0,
                                                 "updated_at": 1.0, "dismissed": False}]), encoding="utf-8")
    clock = Clock()
    store = CandidateStore(path, clock)
    store.save("c", [cand("NEW.US", "")])
    assert [r["ticker"] for r in store.all()] == ["NEW.US", "OLD.US"]


def test_two_stores_on_one_file_keep_each_others_rows(tmp_path):
    path = tmp_path / "candidates.json"
    server, script = CandidateStore(path, Clock()), CandidateStore(path, Clock())
    server.save("conv1", [cand("AAPL.US", "Apple")])
    script.save("terminal", [cand("MSFT.US", "Microsoft")])
    server.save("conv1", [cand("AXTI.US")])  # the server's next save must not drop the script's row
    assert sorted(r["ticker"] for r in server.all()) == ["AAPL.US", "AXTI.US", "MSFT.US"]
    assert script.archive("AAPL.US") is True
    assert {r["ticker"]: r["archived_at"] for r in server.all()}["AAPL.US"] is not None


def test_save_waits_for_another_writers_lock(tmp_path):
    path = tmp_path / "candidates.json"
    store = CandidateStore(path, Clock())
    with open(tmp_path / "candidates.lock", "a") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        t = threading.Thread(target=store.save, args=("conv1", [cand()]))
        t.start()
        t.join(0.3)
        assert t.is_alive() and not path.exists()  # blocked behind the other writer
    t.join(5)
    assert not t.is_alive() and [r["ticker"] for r in store.all()] == ["AXTI.US"]


def test_a_missing_file_is_an_empty_list(tmp_path):
    assert CandidateStore(tmp_path / "nope" / "candidates.json").all() == []


def test_one_post_under_two_url_forms_is_one_source():
    other_form = src(url="https://twitter.com/alice/status/1?s=20")
    p = parse_candidates(block({"ticker": "AXTI.US", "name": "", "sources": [src(), other_form]}))
    assert [s["url"] for s in p.candidates[0]["sources"]] == ["https://x.com/alice/status/1"]


def test_saving_the_same_post_again_by_another_url_adds_nothing(tmp_path):
    store = CandidateStore(tmp_path / "candidates.json", Clock())
    store.save("conv1", [cand()])
    store.save("conv2", [cand("AXTI.US", "", src(url="https://twitter.com/alice/status/1?s=20"))])
    assert [s["url"] for s in store.all()[0]["sources"]] == ["https://x.com/alice/status/1"]


def test_bearish_views_are_left_out_without_counting_as_bad_format():
    bear = src(handle="bob", url="https://x.com/bob/status/2", stance="bearish")
    calm = src(handle="carol", url="https://x.com/carol/status/3", stance="neutral")
    p = parse_candidates(block({"ticker": "AXTI.US", "name": "", "sources": [src(), bear, calm]},
                               {"ticker": "MSFT.US", "name": "Microsoft", "sources": [bear]}))
    [c] = p.candidates
    assert c["ticker"] == "AXTI.US" and [s["handle"] for s in c["sources"]] == ["alice", "carol"]
    assert p.dropped == 0

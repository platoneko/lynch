from __future__ import annotations

import json
import os
import subprocess
from datetime import date, datetime

import pytest

from lynch_reader.earnings import (
    RETRY_S, CalendarCache, Earnings, IgnoreStore, file_changed_at, judge, last_release, next_release, release_at,
    updated_since,
)
from lynch_reader.market_hours import MARKET_TZ
from eval.lib.earnings_lb import CalendarEvent, CalendarFetch  # after lynch_reader, which puts the repo root on sys.path

# 2026-10-14 is a Wednesday.


def ev(day: str, slot: str = "after", period: str = "2026 财年第 3 季度") -> CalendarEvent:
    return CalendarEvent(day, slot, period)


def at(market: str, month: int, day: int, hh: int, mm: int = 0) -> datetime:
    return datetime(2026, month, day, hh, mm, tzinfo=MARKET_TZ[market])


def reported(day: str, slot: str, call: datetime, period: str = "2026 财年第 3 季度") -> CalendarEvent:
    """An entry Longbridge rewrote once results were in: `day` may be off, the call start is not."""
    return CalendarEvent(day, slot, period, call_at=int(call.timestamp()), reported=True)


@pytest.mark.parametrize("ticker, slot, local", [
    ("TSM.US", "before", (9, 30)),
    ("TSM.US", "during", (16, 0)),
    ("TSM.US", "after", (16, 0)),
    ("TSM.US", "unknown", (16, 0)),
    ("0700.HK", "before", (9, 30)),
    ("0700.HK", "after", (16, 0)),
    ("600036.SH", "before", (9, 30)),
    ("600036.SH", "after", (15, 0)),
    ("000933.SZ", "during", (15, 0)),
])
def test_release_moment_by_slot_and_market(ticker, slot, local):
    market = {"US": "US", "HK": "HK", "SH": "A", "SZ": "A"}[ticker.rsplit(".", 1)[1]]
    assert release_at(ticker, ev("2026-10-14", slot)) == at(market, 10, 14, *local)


def test_unknown_market_has_no_moment():
    assert release_at("D05.SG", ev("2026-10-14")) is None
    assert judge("D05.SG", [ev("2026-10-14")], at("US", 10, 14, 12)) is None


@pytest.mark.parametrize("slot, before_flip, after_flip", [
    ("before", (9, 29), (9, 30)),
    ("after", (15, 59), (16, 0)),
    ("during", (15, 59), (16, 0)),
    ("unknown", (15, 59), (16, 0)),
])
def test_flips_to_released_at_the_moment(slot, before_flip, after_flip):
    events = [ev("2026-10-14", slot)]
    assert judge("TSM.US", events, at("US", 10, 14, *before_flip)).status == "upcoming"
    assert judge("TSM.US", events, at("US", 10, 14, *after_flip)).status == "released"


def test_a_share_after_close_releases_at_15_beijing():
    events = [ev("2026-10-20", "after")]
    assert judge("300750.SZ", events, at("A", 10, 20, 14, 59)).status == "upcoming"
    assert judge("300750.SZ", events, at("A", 10, 20, 15, 0)).status == "released"


def test_judged_in_the_markets_own_day():
    # 2026-10-15 01:00 Beijing is still 10-14 13:00 in New York: TSM's 10-14 after-close release is today, upcoming.
    row = judge("TSM.US", [ev("2026-10-14", "after")], at("A", 10, 15, 1))
    assert (row.status, row.day_offset) == ("upcoming", 0)


def test_upcoming_window_is_three_days():
    now = at("US", 10, 11, 12)
    assert judge("TSM.US", [ev("2026-10-14")], now).day_offset == 3
    assert judge("TSM.US", [ev("2026-10-15")], now) is None


def test_released_window_is_seven_days():
    events = [ev("2026-10-14", "after")]
    assert judge("TSM.US", events, at("US", 10, 21, 23)).day_offset == -7
    assert judge("TSM.US", events, at("US", 10, 22, 0)) is None


def test_two_entries_make_one_row():
    # The calendar lists a quarter and the half-year report a day apart.
    events = [ev("2026-07-27", "after", "2026 财年第 2 季度"), ev("2026-07-28", "after", "2026 财年半年报")]
    row = judge("000933.SZ", events, at("A", 7, 28, 10))
    assert (row.status, row.event.period) == ("released", "2026 财年第 2 季度")
    row = judge("000933.SZ", events, at("A", 7, 28, 16))
    assert (row.status, row.event.period) == ("released", "2026 财年半年报")


def test_released_wins_over_upcoming_and_nearest_upcoming_wins():
    assert judge("TSM.US", [ev("2026-10-14"), ev("2026-10-16")], at("US", 10, 14, 17)).event.date == "2026-10-14"
    assert judge("TSM.US", [ev("2026-10-17"), ev("2026-10-15")], at("US", 10, 14, 12)).event.date == "2026-10-15"


def test_reported_entry_counts_from_its_call_when_earlier():
    # A 10-20 after-close release the calendar moved to 10-21 once results were in.
    e = reported("2026-10-21", "after", at("US", 10, 20, 16, 30))
    assert release_at("SPCX.US", e) == at("US", 10, 20, 16, 30)
    row = judge("SPCX.US", [e], at("US", 10, 21, 10))
    assert (row.status, row.day_offset, row.dates) == ("released", -1, ("2026-10-20",))


def test_call_only_corrects_a_reported_entry_and_never_delays_it():
    early = int(at("US", 10, 20, 16, 30).timestamp())
    assert release_at("TSM.US", CalendarEvent("2026-10-21", "after", "q", call_at=early)) == at("US", 10, 21, 16)
    assert release_at("TSM.US", reported("2026-10-21", "after", at("US", 10, 21, 17))) == at("US", 10, 21, 16)


# ---- calendar cache, ignores, research, response


def test_last_release_is_the_latest_out_within_30_days():
    events = [ev("2026-07-15", "after", "2026 财年第 2 季度"), ev("2026-10-14", "after"), ev("2027-01-15", "after")]
    row = last_release("TSM.US", events, at("US", 11, 13, 12))
    assert (row.event.date, row.day_offset, row.since) == ("2026-10-14", -30, at("US", 10, 14, 16))
    assert last_release("TSM.US", events, at("US", 11, 14, 12)) is None  # 31 days on: no longer flagged
    assert last_release("D05.SG", events, at("US", 10, 15, 12)) is None


def test_last_release_counts_its_paired_entry_as_the_same_release():
    events = [ev("2026-07-27", "after", "2026 财年第 2 季度"), ev("2026-07-28", "after", "2026 财年半年报")]
    row = last_release("000933.SZ", events, at("A", 8, 10, 10))
    assert (row.event.period, row.since) == ("2026 财年半年报", at("A", 7, 27, 15))


def test_a_rewritten_year_to_date_entry_does_not_move_the_release():
    # Once results are in, longbridge adds a 前三季度 entry a day early with the same call.
    call = at("A", 10, 28, 15, 30)
    events = [reported("2026-10-28", "after", call), reported("2026-10-27", "after", call, "2026 财年前三季度")]
    row = last_release("600036.SH", events, at("A", 11, 2, 10))
    assert (row.event.period, row.since) == ("2026 财年第 3 季度", at("A", 10, 28, 15))


def test_a_scheduled_call_alone_does_not_merge_entries():
    # Before results, a quarter and its half-year report a day apart may already share the scheduled call.
    call = int(at("A", 7, 29, 9).timestamp())
    events = [CalendarEvent("2026-07-27", "after", "2026 财年第 2 季度", call_at=call),
              CalendarEvent("2026-07-28", "after", "2026 财年半年报", call_at=call)]
    row = judge("000933.SZ", events, at("A", 7, 27, 16))
    assert (row.status, row.event.period) == ("released", "2026 财年第 2 季度")


def test_releases_days_apart_are_separate():
    # An A-share annual report and its Q1 report a few days later: the framework must follow the later one.
    events = [ev("2027-04-20", "after", "2026 财年年报"), ev("2027-04-25", "after", "2027 财年第 1 季度")]
    row = last_release("600036.SH", events, datetime(2027, 4, 26, 10, tzinfo=MARKET_TZ["A"]))
    assert (row.event.period, row.since) == ("2027 财年第 1 季度", datetime(2027, 4, 25, 15, tzinfo=MARKET_TZ["A"]))


def test_next_release_is_the_nearest_still_to_come():
    events = [ev("2026-10-14", "after"), ev("2027-04-15", "after"), ev("2027-01-15", "after")]
    row = next_release("TSM.US", events, at("US", 10, 14, 17))
    assert (row.event.date, row.day_offset) == ("2027-01-15", 93)
    assert next_release("TSM.US", events[:1], at("US", 10, 14, 17)) is None


class Changed:
    """Stands in for `file_changed_at`: a fixed answer and whether it was asked."""

    def __init__(self, t: float | None) -> None:
        self.t, self.asked = t, False

    def __call__(self) -> float | None:
        self.asked = True
        return self.t


@pytest.mark.parametrize("updated, expected", [("2026-10-16", True), ("2026-10-14", False)])
def test_revised_by_the_header_day_on_other_days(updated, expected):
    # A US after-close release on 10-14 is Beijing's 10-15.
    changed = Changed(None)
    assert updated_since(date.fromisoformat(updated), at("US", 10, 14, 16), changed) is expected
    assert not changed.asked


def test_revised_on_the_release_day_only_when_the_file_changed_after_it():
    since = at("A", 10, 28, 15)  # an A-share after-close release
    assert updated_since(date(2026, 10, 28), since, Changed(at("A", 10, 28, 20).timestamp())) is True
    assert updated_since(date(2026, 10, 28), since, Changed(at("A", 10, 28, 10).timestamp())) is False
    assert updated_since(date(2026, 10, 28), since, Changed(None)) is False
    assert updated_since(None, since, Changed(None)) is False


def _git(repo, *args: str) -> None:
    # Committed later than authored, as a rebase leaves it: the edit's own time is the author's.
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True,
                   env={**os.environ, "GIT_AUTHOR_DATE": "2026-10-28T20:00:00+08:00",
                        "GIT_COMMITTER_DATE": "2026-10-29T09:00:00+08:00"})


def test_file_changed_at_is_the_last_commit_or_an_uncommitted_edit(tmp_path):
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "base")
    p = tmp_path / "TSM.US.md"
    p.write_text("a", encoding="utf-8")
    _git(tmp_path, "add", p.name)
    _git(tmp_path, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "x")
    os.utime(p, (0, 0))
    assert file_changed_at(p) == at("A", 10, 28, 20).timestamp()
    p.write_text("b", encoding="utf-8")
    os.utime(p, (123.0, 123.0))
    assert file_changed_at(p) == 123.0
    assert file_changed_at(tmp_path / "NONE.US.md") is None
    assert file_changed_at(tmp_path.parent / "elsewhere" / "X.US.md") is None  # not a repo


class Clock:
    def __init__(self, t: datetime) -> None:
        self.now = t.timestamp()

    def __call__(self) -> float:
        return self.now

    def set(self, t: datetime) -> None:
        self.now = t.timestamp()


class FakeCalendar:
    """Stands in for `fetch_calendar`: fixed events, a call log, and optional failing tickers or a raise."""

    def __init__(self, events: dict[str, list[CalendarEvent]]) -> None:
        self.events, self.calls, self.fail, self.error = events, [], set(), None

    def __call__(self, tickers, start, end) -> CalendarFetch:
        self.calls.append((list(tickers), start, end))
        if self.error is not None:
            raise self.error
        failed = [t for t in tickers if t in self.fail]
        # Like the CLI: releases before `start` are not returned.
        since = {t: [e for e in self.events.get(t, []) if e.date >= start.isoformat()] for t in tickers}
        return CalendarFetch({t: since[t] for t in tickers if t not in failed}, failed,
                             "longbridge failed (exit 1): token expired" if failed else None)


@pytest.fixture
def cal():
    return FakeCalendar({"TSM.US": [ev("2026-10-14", "after")], "0700.HK": [ev("2026-11-12", "after")]})


def test_one_full_fetch_a_day_at_1637_beijing(tmp_path, cal):
    clk = Clock(at("A", 10, 14, 8))
    cache = CalendarCache(tmp_path / "calendar.json", fetch=cal, wall=clk)
    events, day, error = cache.get(["TSM.US", "0700.HK"])
    assert (day, error) == ("2026-10-14", None)
    assert events["TSM.US"] == [ev("2026-10-14", "after")]
    _, start, end = cal.calls[0]
    assert (start.isoformat(), end.isoformat()) == ("2026-10-06", "2026-11-13")  # 8 days back: see the US test
    clk.set(at("A", 10, 14, 16, 36))
    cache.get(["TSM.US", "0700.HK"])
    assert len(cal.calls) == 1
    clk.set(at("A", 10, 14, 16, 37))
    cache.get(["TSM.US", "0700.HK"])
    assert len(cal.calls) == 2 and cal.calls[1][0] == ["TSM.US", "0700.HK"]
    clk.set(at("A", 10, 15, 0, 1))  # Beijing midnight is no trigger
    cache.get(["TSM.US", "0700.HK"])
    assert len(cal.calls) == 2
    clk.set(at("A", 10, 17, 16, 40))  # a Saturday counts too: no session is involved
    cache.get(["TSM.US", "0700.HK"])
    assert len(cal.calls) == 3


def test_new_ticker_is_fetched_alone_and_removed_one_filtered(tmp_path, cal):
    clk = Clock(at("A", 10, 14, 8))
    cache = CalendarCache(tmp_path / "calendar.json", fetch=cal, wall=clk)
    cache.get(["TSM.US"])
    events, _, _ = cache.get(["TSM.US", "0700.HK"])
    assert cal.calls[1][0] == ["0700.HK"] and "0700.HK" in events
    events, _, _ = cache.get(["0700.HK"])
    assert len(cal.calls) == 2 and set(events) == {"0700.HK"}


def test_ticker_without_events_is_not_refetched(tmp_path, cal):
    clk = Clock(at("A", 10, 14, 8))
    cache = CalendarCache(tmp_path / "calendar.json", fetch=cal, wall=clk)
    cache.get(["NOPE.US"])
    events, _, _ = cache.get(["NOPE.US"])
    assert events == {"NOPE.US": []} and len(cal.calls) == 1


def test_cache_survives_a_restart(tmp_path, cal):
    clk = Clock(at("A", 10, 14, 8))
    CalendarCache(tmp_path / "calendar.json", fetch=cal, wall=clk).get(["TSM.US"])
    events, day, _ = CalendarCache(tmp_path / "calendar.json", fetch=cal, wall=clk).get(["TSM.US"])
    assert len(cal.calls) == 1 and day == "2026-10-14" and events["TSM.US"][0].slot == "after"


def test_corrupt_cache_file_starts_empty(tmp_path, cal):
    (tmp_path / "calendar.json").write_text("{not json", encoding="utf-8")
    clk = Clock(at("A", 10, 14, 8))
    events, _, _ = CalendarCache(tmp_path / "calendar.json", fetch=cal, wall=clk).get(["TSM.US"])
    assert events["TSM.US"] and len(cal.calls) == 1


def test_partial_failure_keeps_old_events_and_retries_only_the_failed(tmp_path, cal):
    clk = Clock(at("A", 10, 14, 8))
    cache = CalendarCache(tmp_path / "calendar.json", fetch=cal, wall=clk)
    cache.get(["TSM.US", "0700.HK"])
    clk.set(at("A", 10, 15, 8))
    cal.fail = {"TSM.US"}
    events, day, error = cache.get(["TSM.US", "0700.HK"])
    assert day == "2026-10-15" and "token expired" in error  # the rest of the pool answered: the day is done
    assert events["TSM.US"] == [ev("2026-10-14", "after")]  # yesterday's events kept
    clk.now += RETRY_S - 1
    cache.get(["TSM.US", "0700.HK"])
    assert len(cal.calls) == 2  # inside the back-off: no call
    cal.fail = set()
    clk.now += 1
    events, day, error = cache.get(["TSM.US", "0700.HK"])
    assert cal.calls[-1][0] == ["TSM.US"] and (day, error) == ("2026-10-15", None)  # only the failed one again


def test_total_failure_holds_the_day_for_a_full_refetch(tmp_path, cal):
    clk = Clock(at("A", 10, 14, 8))
    cache = CalendarCache(tmp_path / "calendar.json", fetch=cal, wall=clk)
    cache.get(["TSM.US", "0700.HK"])
    clk.set(at("A", 10, 15, 8))
    cal.fail = {"TSM.US", "0700.HK"}  # e.g. logged out
    _, day, _ = cache.get(["TSM.US", "0700.HK"])
    assert day == "2026-10-14"
    cal.fail = set()
    clk.now += RETRY_S
    _, day, error = cache.get(["TSM.US", "0700.HK"])
    assert cal.calls[-1][0] == ["TSM.US", "0700.HK"] and (day, error) == ("2026-10-15", None)


def test_release_dropped_once_out_stays_through_the_window(tmp_path):
    # Longbridge has dropped releases from the calendar the day after they came out.
    cal = FakeCalendar({"TSM.US": [ev("2026-10-14", "after")]})
    clk = Clock(at("A", 10, 14, 17))  # New York 05:00, before the release
    cache = CalendarCache(tmp_path / "c.json", fetch=cal, wall=clk)
    cache.get(["TSM.US"])
    cal.events = {"TSM.US": [ev("2027-01-15", "after")]}
    clk.set(at("A", 10, 15, 17))
    assert cache.get(["TSM.US"])[0]["TSM.US"] == [ev("2026-10-14", "after"), ev("2027-01-15", "after")]
    clk.set(at("A", 10, 22, 17))
    assert ev("2026-10-14", "after") in cache.get(["TSM.US"])[0]["TSM.US"]
    clk.set(at("A", 11, 14, 17))  # past the fetch window, still inside the 30-day flag
    assert ev("2026-10-14", "after") in cache.get(["TSM.US"])[0]["TSM.US"]
    clk.set(at("A", 11, 15, 17))  # 10-14 is past 30 days, plus the US day's lag
    assert cache.get(["TSM.US"])[0]["TSM.US"] == [ev("2027-01-15", "after")]


def test_release_before_the_fetch_window_is_kept_beside_a_new_answer(tmp_path):
    # The daily fetch looks back 8 days only: an older release is cached, whatever today's answer holds.
    cal = FakeCalendar({"TSM.US": [ev("2026-10-14", "after")]})
    clk = Clock(at("A", 10, 15, 17))
    cache = CalendarCache(tmp_path / "c.json", fetch=cal, wall=clk)
    cache.get(["TSM.US"])
    cal.events = {"TSM.US": [ev("2026-10-14", "after"), ev("2026-10-27", "before")]}
    clk.set(at("A", 10, 28, 17))
    assert cache.get(["TSM.US"])[0]["TSM.US"] == [ev("2026-10-14", "after"), ev("2026-10-27", "before")]


def test_answer_with_a_release_out_replaces_the_cached_ones(tmp_path):
    cal = FakeCalendar({"TSM.US": [ev("2026-10-14", "after")]})
    clk = Clock(at("A", 10, 14, 17))
    CalendarCache(tmp_path / "c.json", fetch=cal, wall=clk).get(["TSM.US"])
    moved = reported("2026-10-15", "after", at("US", 10, 14, 16, 30))
    cal.events = {"TSM.US": [moved]}
    clk.set(at("A", 10, 15, 17))
    assert CalendarCache(tmp_path / "c.json", fetch=cal, wall=clk).get(["TSM.US"])[0]["TSM.US"] == [moved]
    assert CalendarCache(tmp_path / "c.json", fetch=cal, wall=clk).get(["TSM.US"])[0]["TSM.US"] == [moved]  # persisted


def test_fetch_that_raises_is_a_failure_not_a_crash(tmp_path, cal):
    clk = Clock(at("A", 10, 14, 8))
    cal.error = RuntimeError("boom")
    events, day, error = CalendarCache(tmp_path / "calendar.json", fetch=cal, wall=clk).get(["TSM.US"])
    assert (events, day) == ({"TSM.US": []}, None) and "boom" in error


def test_ignore_is_per_release_and_pruned(tmp_path):
    clk = Clock(at("A", 10, 14, 8))
    store = IgnoreStore(tmp_path / "ignored.json", wall=clk)
    store.ignore("TSM.US", "2026-10-14")
    store.ignore("TSM.US", "2026-10-14")
    assert store.keys() == {("TSM.US", "2026-10-14")}
    store.unignore("TSM.US", "2026-10-14")
    assert store.keys() == set()
    store.ignore("AAA.US", "2026-10-10")
    assert store.keys() == {("AAA.US", "2026-10-10")}  # still inside the window on 10-14
    clk.set(at("A", 10, 20, 8))
    store.ignore("BBB.US", "2026-10-20")  # writing prunes keys past the released window
    assert store.keys() == {("BBB.US", "2026-10-20")}
    assert json.loads((tmp_path / "ignored.json").read_text()) == [["BBB.US", "2026-10-20"]]


def never(ticker: str, since: datetime) -> bool:
    return False


def revised_after(t: datetime):
    """A `revised` stand-in: every framework last changed at `t`."""
    return lambda ticker, since: t > since


def snapshot(tmp_path, cal, now: datetime, revised=never, ignore=()):
    clk = Clock(now)
    store = IgnoreStore(tmp_path / "ignored.json", wall=clk)
    for key in ignore:
        store.ignore(*key)
    e = Earnings(CalendarCache(tmp_path / "calendar.json", fetch=cal, wall=clk), store, wall=clk)
    return e.snapshot(["TSM.US", "0700.HK"], revised)


def test_snapshot_shape(tmp_path, cal):
    body = snapshot(tmp_path, cal, at("US", 10, 14, 17))
    assert body["fetched_day"] == "2026-10-15" and body["error"] is None  # 17:00 New York is 05:00 next day Beijing
    assert body["items"] == [{
        "ticker": "TSM.US", "status": "released", "date": "2026-10-14", "day_offset": 0, "slot": "after",
        "release_at": "2026-10-14T16:00:00-04:00", "period": "2026 财年第 3 季度",
        "researched": False, "ignored": False, "anomaly": None,
    }]


def test_researched_means_the_framework_was_revised_after_the_release(tmp_path, cal):
    body = snapshot(tmp_path, cal, at("US", 10, 14, 17), revised=revised_after(at("US", 10, 14, 15)))
    assert body["items"][0]["researched"] is False
    body = snapshot(tmp_path, cal, at("US", 10, 14, 17), revised=revised_after(at("US", 10, 14, 16, 30)))
    assert body["items"][0]["researched"] is True


def test_upcoming_row_is_never_researched(tmp_path):
    cal = FakeCalendar({"TSM.US": [ev("2026-10-15", "after")]})
    body = snapshot(tmp_path, cal, at("US", 10, 14, 12), revised=lambda t, since: True)
    assert [(i["status"], i["researched"]) for i in body["items"]] == [("upcoming", False)]


def test_ignored_release_is_flagged(tmp_path, cal):
    body = snapshot(tmp_path, cal, at("US", 10, 14, 17), ignore=[("TSM.US", "2026-10-14")])
    assert body["items"][0]["ignored"] is True


def test_rewritten_release_shows_and_ignores_on_its_true_day(tmp_path):
    cal = FakeCalendar({"TSM.US": [reported("2026-10-15", "after", at("US", 10, 14, 16, 30))]})
    [item] = snapshot(tmp_path, cal, at("US", 10, 15, 10), ignore=[("TSM.US", "2026-10-14")])["items"]
    assert (item["date"], item["day_offset"], item["ignored"]) == ("2026-10-14", -1, True)
    assert item["release_at"] == "2026-10-14T16:30:00-04:00"


def test_us_release_is_fetched_through_its_seventh_local_day(tmp_path):
    # Beijing 10-21 10:00 is New York 10-20 22:00: a 10-13 release is New York's 7th day, Beijing's 8th.
    cal = FakeCalendar({"TSM.US": [ev("2026-10-13", "after")]})
    clk = Clock(at("A", 10, 21, 10))
    e = Earnings(CalendarCache(tmp_path / "c.json", fetch=cal, wall=clk), IgnoreStore(tmp_path / "i.json", wall=clk),
                 wall=clk)
    items = e.snapshot(["TSM.US"], never)["items"]
    assert [(i["status"], i["day_offset"]) for i in items] == [("released", -7)]


def test_paired_entries_keep_research_and_ignore(tmp_path):
    # The quarter and the half-year report a day apart are one release: handling the first covers the second.
    cal = FakeCalendar({"000933.SZ": [ev("2026-07-27", "after", "2026 财年第 2 季度"),
                                      ev("2026-07-28", "after", "2026 财年半年报")]})
    clk = Clock(at("A", 7, 28, 16))
    store = IgnoreStore(tmp_path / "i.json", wall=clk)
    e = Earnings(CalendarCache(tmp_path / "c.json", fetch=cal, wall=clk), store, wall=clk)
    item = e.snapshot(["000933.SZ"], revised_after(at("A", 7, 27, 15, 1)))["items"][0]
    assert (item["period"], item["researched"]) == ("2026 财年半年报", True)
    store.ignore("000933.SZ", "2026-07-27")
    item = e.snapshot(["000933.SZ"], never)["items"][0]
    assert item["ignored"] is True
    store.unignore("000933.SZ", item["date"])  # the page sends the row's own (latest) date
    assert e.snapshot(["000933.SZ"], never)["items"][0]["ignored"] is False


def test_released_newest_first_then_upcoming_soonest_first(tmp_path):
    cal = FakeCalendar({
        "AAA.US": [ev("2026-10-12", "after")], "BBB.US": [ev("2026-10-13", "after")],
        "CCC.US": [ev("2026-10-16", "after")], "DDD.US": [ev("2026-10-15", "before")],
    })
    clk = Clock(at("US", 10, 14, 12))
    e = Earnings(CalendarCache(tmp_path / "c.json", fetch=cal, wall=clk), IgnoreStore(tmp_path / "i.json", wall=clk),
                 wall=clk)
    items = e.snapshot(["AAA.US", "BBB.US", "CCC.US", "DDD.US"], never)["items"]
    assert [i["ticker"] for i in items] == ["BBB.US", "AAA.US", "DDD.US", "CCC.US"]


def test_snapshot_carries_each_rows_anomaly_tag(tmp_path, cal):
    clk = Clock(at("US", 10, 15, 10))
    store = IgnoreStore(tmp_path / "ignored.json", wall=clk)
    e = Earnings(CalendarCache(tmp_path / "calendar.json", fetch=cal, wall=clk), store, wall=clk)
    [item] = e.snapshot(["TSM.US"], never, tag=lambda t: {"move": 0.1} if t == "TSM.US" else None)["items"]
    assert item["anomaly"] == {"move": 0.1}
    assert e.snapshot(["TSM.US"], never)["items"][0]["anomaly"] is None


def test_read_only_get_never_fetches(tmp_path, cal):
    clk = Clock(at("A", 10, 14, 8))
    cache = CalendarCache(tmp_path / "calendar.json", fetch=cal, wall=clk)
    assert cache.get(["TSM.US"], refresh=False) == ({"TSM.US": []}, None, None)
    assert cal.calls == []
    cache.get(["TSM.US"])
    clk.set(at("A", 10, 15, 8))  # past 10-14 16:37: a refreshing get would refetch
    events, day, _ = cache.get(["TSM.US"], refresh=False)
    assert len(cal.calls) == 1 and day == "2026-10-14" and events["TSM.US"]


def test_ticker_dates_give_last_next_and_the_stale_flag(tmp_path):
    cal = FakeCalendar({"TSM.US": [ev("2026-10-14", "after"), ev("2027-01-15", "after")],
                        "0700.HK": [ev("2026-11-12", "after")]})
    clk = Clock(at("A", 10, 16, 10))
    cache = CalendarCache(tmp_path / "c.json", fetch=cal, wall=clk)
    cache.get(["TSM.US", "0700.HK"])
    e = Earnings(cache, IgnoreStore(tmp_path / "i.json", wall=clk), wall=clk)
    calls = len(cal.calls)
    dates = e.ticker_dates(["TSM.US", "0700.HK"], never)
    assert len(cal.calls) == calls  # the home list only reads the cache
    assert dates["TSM.US"] == {
        "last_earnings": {"date": "2026-10-14", "slot": "after", "period": "2026 财年第 3 季度",
                          "release_at": "2026-10-14T16:00:00-04:00", "day_offset": -1},
        "next_earnings": {"date": "2027-01-15", "slot": "after", "period": "2026 财年第 3 季度",
                          "release_at": "2027-01-15T16:00:00-05:00", "day_offset": 92},
        "stale_after_earnings": True,
    }
    assert dates["0700.HK"]["last_earnings"] is None and dates["0700.HK"]["stale_after_earnings"] is False
    assert e.ticker_dates(["TSM.US"], lambda t, since: True)["TSM.US"]["stale_after_earnings"] is False

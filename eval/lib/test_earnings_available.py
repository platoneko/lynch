"""Unit tests for earnings-available pure logic."""
from __future__ import annotations

import unittest
from datetime import date

from eval.lib.earnings_available import (
    Hit,
    TickerState,
    UniverseItem,
    evaluate,
    filter_universe,
    format_message,
    normalize_symbol,
    pool_of,
)


def _item(t: str, name: str = "") -> UniverseItem:
    return UniverseItem(ticker=t, name=name or t)


class NormalizeTests(unittest.TestCase):
    def test_hk_strip_zeros(self) -> None:
        self.assertEqual(normalize_symbol("0700.HK"), "700.HK")
        self.assertEqual(normalize_symbol("0189.HK"), "189.HK")
        self.assertEqual(normalize_symbol("700.HK"), "700.HK")

    def test_others_unchanged(self) -> None:
        self.assertEqual(normalize_symbol("NVDA.US"), "NVDA.US")
        self.assertEqual(normalize_symbol("601899.SH"), "601899.SH")

    def test_pool(self) -> None:
        self.assertEqual(pool_of("0700.HK"), "ha")
        self.assertEqual(pool_of("601899.SH"), "ha")
        self.assertEqual(pool_of("NVDA.US"), "us")
        ha = filter_universe([_item("0700.HK"), _item("NVDA.US"), _item("000933.SZ")], "ha")
        self.assertEqual([x.ticker for x in ha], ["0700.HK", "000933.SZ"])


class EvaluateTests(unittest.TestCase):
    def test_bootstrap_no_notify(self) -> None:
        r = evaluate(
            [_item("NVDA.US", "英伟达")],
            earn_dates={"NVDA.US": "2026-09-08"},
            latest_periods={"NVDA.US": "2027.H1"},
            state={},
            today_by_ticker={"NVDA.US": date(2026, 9, 8)},
        )
        self.assertEqual(r.hits, [])
        self.assertEqual(r.state["NVDA.US"].last_seen_period, "2027.H1")

    def test_regular_t0_and_t1_every_run(self) -> None:
        st = {"NVDA.US": TickerState(last_seen_period="2026.Q1")}
        r0 = evaluate(
            [_item("NVDA.US")],
            earn_dates={"NVDA.US": "2026-09-08"},
            latest_periods={"NVDA.US": "2027.H1"},
            state=st,
            today_by_ticker={"NVDA.US": date(2026, 9, 8)},
        )
        self.assertEqual(len(r0.hits), 1)
        self.assertEqual(r0.hits[0].window, "regular")
        self.assertEqual(r0.hits[0].days, 0)
        self.assertTrue(r0.state["NVDA.US"].pushed_regular)

        r1 = evaluate(
            [_item("NVDA.US")],
            earn_dates={"NVDA.US": "2026-09-08"},
            latest_periods={"NVDA.US": "2027.H1"},
            state=r0.state,
            today_by_ticker={"NVDA.US": date(2026, 9, 9)},
        )
        self.assertEqual(len(r1.hits), 1)
        self.assertEqual(r1.hits[0].days, 1)
        self.assertEqual(r1.hits[0].window, "regular")

    def test_extend_first_confirm_once(self) -> None:
        st = {"NVDA.US": TickerState(last_seen_period="2026.Q1")}
        # First seen on T+3
        r3 = evaluate(
            [_item("NVDA.US")],
            earn_dates={"NVDA.US": "2026-09-05"},
            latest_periods={"NVDA.US": "2027.H1"},
            state=st,
            today_by_ticker={"NVDA.US": date(2026, 9, 8)},
        )
        self.assertEqual(len(r3.hits), 1)
        self.assertEqual(r3.hits[0].window, "extend")
        self.assertEqual(r3.hits[0].days, 3)
        self.assertTrue(r3.state["NVDA.US"].pushed_extend)

        r4 = evaluate(
            [_item("NVDA.US")],
            earn_dates={"NVDA.US": "2026-09-05"},
            latest_periods={"NVDA.US": "2027.H1"},
            state=r3.state,
            today_by_ticker={"NVDA.US": date(2026, 9, 9)},
        )
        self.assertEqual(r4.hits, [])

    def test_no_extend_nag_after_regular(self) -> None:
        st = {"NVDA.US": TickerState(last_seen_period="2026.Q1")}
        r0 = evaluate(
            [_item("NVDA.US")],
            earn_dates={"NVDA.US": "2026-09-08"},
            latest_periods={"NVDA.US": "2027.H1"},
            state=st,
            today_by_ticker={"NVDA.US": date(2026, 9, 8)},
        )
        self.assertEqual(len(r0.hits), 1)
        r2 = evaluate(
            [_item("NVDA.US")],
            earn_dates={"NVDA.US": "2026-09-08"},
            latest_periods={"NVDA.US": "2027.H1"},
            state=r0.state,
            today_by_ticker={"NVDA.US": date(2026, 9, 10)},
        )
        self.assertEqual(r2.hits, [])

    def test_outside_window_silent_updates_baseline(self) -> None:
        st = {"NVDA.US": TickerState(last_seen_period="2026.Q1")}
        r = evaluate(
            [_item("NVDA.US")],
            earn_dates={"NVDA.US": "2026-08-01"},
            latest_periods={"NVDA.US": "2027.H1"},
            state=st,
            today_by_ticker={"NVDA.US": date(2026, 9, 8)},
        )
        self.assertEqual(r.hits, [])
        self.assertEqual(r.state["NVDA.US"].last_seen_period, "2027.H1")

    def test_unchanged_period_no_hit(self) -> None:
        st = {"NVDA.US": TickerState(last_seen_period="2027.H1")}
        r = evaluate(
            [_item("NVDA.US")],
            earn_dates={"NVDA.US": "2026-09-08"},
            latest_periods={"NVDA.US": "2027.H1"},
            state=st,
            today_by_ticker={"NVDA.US": date(2026, 9, 8)},
        )
        self.assertEqual(r.hits, [])

    def test_max_d_inclusive(self) -> None:
        st = {"NVDA.US": TickerState(last_seen_period="2026.Q1")}
        # d=6 still in; d=7 out
        r6 = evaluate(
            [_item("NVDA.US")],
            earn_dates={"NVDA.US": "2026-09-02"},
            latest_periods={"NVDA.US": "2027.H1"},
            state=st,
            today_by_ticker={"NVDA.US": date(2026, 9, 8)},
        )
        self.assertEqual(len(r6.hits), 1)
        self.assertEqual(r6.hits[0].days, 6)

        st2 = {"NVDA.US": TickerState(last_seen_period="2026.Q1")}
        r7 = evaluate(
            [_item("NVDA.US")],
            earn_dates={"NVDA.US": "2026-09-01"},
            latest_periods={"NVDA.US": "2027.H1"},
            state=st2,
            today_by_ticker={"NVDA.US": date(2026, 9, 8)},
        )
        self.assertEqual(r7.hits, [])


class FormatTests(unittest.TestCase):
    def test_format(self) -> None:
        title, lines = format_message(
            [
                Hit(
                    ticker="NVDA.US",
                    name="英伟达",
                    earn_date="2026-09-08",
                    days=0,
                    period="2027.H1",
                    window="regular",
                )
            ],
            as_of=date(2026, 9, 8),
            pool="us",
        )
        self.assertIn("美股", title)
        self.assertIn("1 只", title)
        self.assertEqual(len(lines), 1)
        self.assertIn("NVDA.US 英伟达", lines[0])
        self.assertIn("T+0", lines[0])


if __name__ == "__main__":
    unittest.main()

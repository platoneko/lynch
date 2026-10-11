"""Tests for the longbridge earnings-calendar fetcher (no CLI: the runner is faked)."""
from __future__ import annotations

import unittest
from datetime import date

from eval.lib.earnings_lb import CalendarEvent, fetch_calendar, normalize_symbol, parse_calendar


def info(symbol: str, local_date: str | None, market_time: str | None, content: str) -> dict:
    ext: dict = {"financial_report": {"fiscal_year": "2026", "market_time": market_time, "period": "3",
                                      "period_type": "qf"}}
    if local_date is not None:
        ext["local_date"] = local_date
    return {"symbol": symbol, "content": content, "ext": ext}


def payload(*infos: dict) -> dict:
    # The CLI groups infos by day; grouping does not matter to the parser.
    return {"list": [{"infos": list(infos)}]}


class NormalizeTests(unittest.TestCase):
    def test_hk_strips_leading_zeros_only(self) -> None:
        self.assertEqual(normalize_symbol("0700.HK"), "700.HK")
        self.assertEqual(normalize_symbol("700.HK"), "700.HK")
        self.assertEqual(normalize_symbol("NVDA.US"), "NVDA.US")
        self.assertEqual(normalize_symbol("600036.SH"), "600036.SH")


class ParseTests(unittest.TestCase):
    def test_slot_period_and_date(self) -> None:
        got = parse_calendar(payload(
            info("TSM.US", "2026-10-14", "U", "2026 财年第 3 季度业绩披露"),
            info("1088.HK", "2026-10-23", "before", "2026 财年第 3 季度业绩披露"),
            info("NOK.US", "2026-10-22", "during", "2026 财年第 3 季度业绩披露"),
            info("MSFT.US", "2026-10-28", "after", "2027 财年第 1 季度业绩披露"),
        ))
        self.assertEqual(got["TSM.US"], [CalendarEvent("2026-10-14", "unknown", "2026 财年第 3 季度")])
        self.assertEqual(got["1088.HK"][0].slot, "before")
        self.assertEqual(got["NOK.US"][0].slot, "during")
        self.assertEqual(got["MSFT.US"][0], CalendarEvent("2026-10-28", "after", "2027 财年第 1 季度"))

    def test_entries_without_iso_date_are_dropped(self) -> None:
        got = parse_calendar(payload(
            info("AAA.US", None, "after", "x"),
            info("BBB.US", "10/14", "after", "x"),
            info("CCC.US", "2026-13-01", "after", "x"),
        ))
        self.assertEqual(got, {})

    def test_garbage_payload_is_empty(self) -> None:
        self.assertEqual(parse_calendar(None), {})
        self.assertEqual(parse_calendar({"list": "nope"}), {})


class FakeRun:
    """Stands in for `_run_json`: answers by the --symbol flags, or raises for listed symbols."""

    def __init__(self, infos: list[dict], fail: set[str] = frozenset()) -> None:
        self.infos, self.fail, self.calls = infos, fail, []

    def __call__(self, cmd: list[str]) -> dict:
        symbols = [cmd[i + 1] for i, a in enumerate(cmd) if a == "--symbol"]
        self.calls.append(symbols)
        if self.fail & set(symbols):
            raise RuntimeError("longbridge failed (exit 1): token expired")
        wanted = {normalize_symbol(s) for s in symbols}
        return payload(*[i for i in self.infos if normalize_symbol(i["symbol"]) in wanted])


class FetchTests(unittest.TestCase):
    START, END = date(2026, 10, 4), date(2026, 11, 10)

    def test_batches_of_ten_with_every_ticker_keyed(self) -> None:
        tickers = [f"T{i:02d}.US" for i in range(23)]
        run = FakeRun([info("T05.US", "2026-10-20", "after", "2026 财年第 3 季度业绩披露")])
        got = fetch_calendar(tickers, self.START, self.END, run=run, sleep=lambda s: None)
        self.assertEqual([len(c) for c in run.calls], [10, 10, 3])
        self.assertEqual(set(got.events), set(tickers))
        self.assertEqual(got.events["T05.US"], [CalendarEvent("2026-10-20", "after", "2026 财年第 3 季度")])
        self.assertEqual(got.events["T00.US"], [])
        self.assertEqual((got.failed, got.error), ([], None))

    def test_hk_codes_map_back_to_framework_tickers(self) -> None:
        run = FakeRun([info("700.HK", "2026-11-12", "after", "2026 财年第 3 季度业绩披露")])
        got = fetch_calendar(["0700.HK"], self.START, self.END, run=run, sleep=lambda s: None)
        self.assertEqual(got.events["0700.HK"][0].date, "2026-11-12")

    def test_failed_batch_is_reported_and_others_kept(self) -> None:
        tickers = [f"T{i:02d}.US" for i in range(12)]
        run = FakeRun([info("T11.US", "2026-10-20", "after", "x")], fail={"T03.US"})
        got = fetch_calendar(tickers, self.START, self.END, run=run, sleep=lambda s: None)
        self.assertEqual(got.failed, tickers[:10])
        self.assertIn("token expired", got.error)
        self.assertEqual(set(got.events), {"T10.US", "T11.US"})

    def test_passes_the_window_to_the_cli(self) -> None:
        seen: list[list[str]] = []
        fetch_calendar(["AAA.US"], self.START, self.END, run=lambda c: seen.append(c) or payload(),
                       sleep=lambda s: None)
        cmd = seen[0]
        self.assertEqual(cmd[:3], ["longbridge", "finance-calendar", "report"])
        self.assertEqual(cmd[cmd.index("--start") + 1], "2026-10-04")
        self.assertEqual(cmd[cmd.index("--end") + 1], "2026-11-10")


if __name__ == "__main__":
    unittest.main()

"""Quote parsing: the regular last stays the price; US extended-hours blocks ride along. Daily bars for baselines."""
from __future__ import annotations

import json
import subprocess
import unittest
from datetime import datetime, timezone
from unittest import mock

from eval.lib.price import DailyBar, SessionQuote, fetch_daily_bars, fetch_quotes

ROWS = [
    {
        "symbol": "AAPL.US", "last": "341.070", "prev_close": "335.920", "status": "Normal", "volume": 41234567,
        "pre_market": {"last": "336.010", "timestamp": "2026-09-25T13:30:01Z", "prev_close": "335.920"},
        "post_market": {"last": "341.460", "timestamp": "2026-09-25T23:59:58Z", "prev_close": "341.070"},
        "overnight": {"last": "336.150", "timestamp": None},  # no trade time: dropped
    },
    {"symbol": "700.HK", "last": "436.600", "prev_close": "438.400", "status": "Normal", "volume": 20422500,
     "pre_market": None, "post_market": None, "overnight": None},
]


def fetch(rows):
    done = subprocess.CompletedProcess([], 0, stdout=json.dumps(rows), stderr="")
    with mock.patch("eval.lib.price.shutil.which", return_value="/bin/longbridge"), \
            mock.patch("eval.lib.price.subprocess.run", return_value=done):
        return fetch_quotes([r["symbol"] for r in rows])


def run_cli(stdout: str, code: int = 0):
    done = subprocess.CompletedProcess([], code, stdout=stdout, stderr="boom" if code else "")
    return mock.patch("eval.lib.price.subprocess.run", return_value=done), \
        mock.patch("eval.lib.price.shutil.which", return_value="/bin/longbridge")


class PriceTests(unittest.TestCase):
    def test_regular_last_is_unchanged(self) -> None:
        q = fetch(ROWS)
        self.assertEqual(q["AAPL.US"].last, 341.07)
        self.assertEqual(q["700.HK"].last, 436.6)

    def test_extended_blocks_with_a_trade_time(self) -> None:
        q = fetch(ROWS)
        self.assertEqual(q["AAPL.US"].extended, (
            SessionQuote("pre", 336.01, datetime(2026, 9, 25, 13, 30, 1, tzinfo=timezone.utc), 335.92),
            SessionQuote("post", 341.46, datetime(2026, 9, 25, 23, 59, 58, tzinfo=timezone.utc), 341.07),
        ))
        self.assertEqual(q["700.HK"].extended, ())

    def test_volume_and_session_base_close(self) -> None:
        q = fetch(ROWS)
        self.assertEqual(q["AAPL.US"].volume, 41234567)
        self.assertEqual(q["700.HK"].volume, 20422500)
        self.assertEqual([x.prev_close for x in q["AAPL.US"].extended], [335.92, 341.07])


BARS = [
    {"time": "2026-10-08T04:00:00Z", "open": "1", "high": "1", "low": "1", "close": "31.370", "volume": "100"},
    {"time": "2026-10-09T04:00:00Z", "open": "1", "high": "1", "low": "1", "close": "31.020", "volume": "700"},
    {"time": "2026-10-10T04:00:00Z", "close": None, "volume": "1"},  # unparseable: dropped
]


class DailyBarTests(unittest.TestCase):
    def test_bars_oldest_first_with_their_own_time(self) -> None:
        run, which = run_cli(json.dumps(BARS))
        with run as r, which:
            bars = fetch_daily_bars("WOLF.US", 62)
        self.assertEqual(bars, [
            DailyBar(datetime(2026, 10, 8, 4, tzinfo=timezone.utc), 31.37, 100.0),
            DailyBar(datetime(2026, 10, 9, 4, tzinfo=timezone.utc), 31.02, 700.0),
        ])
        self.assertEqual(r.call_args.args[0], ["longbridge", "kline", "WOLF.US", "--period", "day", "--count", "62",
                                               "--adjust", "forward", "--format", "json"])

    def test_cli_failure_raises(self) -> None:
        run, which = run_cli("", code=1)
        with run, which, self.assertRaises(RuntimeError):
            fetch_daily_bars("WOLF.US", 62)

    def test_non_list_payload_raises(self) -> None:
        run, which = run_cli(json.dumps({"message": "token expired"}))
        with run, which, self.assertRaises(RuntimeError):
            fetch_daily_bars("WOLF.US", 62)


if __name__ == "__main__":
    unittest.main()

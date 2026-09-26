"""Quote parsing: the regular last stays the price; US extended-hours blocks ride along."""
from __future__ import annotations

import json
import subprocess
import unittest
from datetime import datetime, timezone
from unittest import mock

from eval.lib.price import SessionQuote, fetch_quotes

ROWS = [
    {
        "symbol": "AAPL.US", "last": "341.070", "prev_close": "335.920", "status": "Normal",
        "pre_market": {"last": "336.010", "timestamp": "2026-09-25T13:30:01Z"},
        "post_market": {"last": "341.460", "timestamp": "2026-09-25T23:59:58Z"},
        "overnight": {"last": "336.150", "timestamp": None},  # no trade time: dropped
    },
    {"symbol": "700.HK", "last": "436.600", "prev_close": "438.400", "status": "Normal",
     "pre_market": None, "post_market": None, "overnight": None},
]


def fetch(rows):
    done = subprocess.CompletedProcess([], 0, stdout=json.dumps(rows), stderr="")
    with mock.patch("eval.lib.price.shutil.which", return_value="/bin/longbridge"), \
            mock.patch("eval.lib.price.subprocess.run", return_value=done):
        return fetch_quotes([r["symbol"] for r in rows])


class PriceTests(unittest.TestCase):
    def test_regular_last_is_unchanged(self) -> None:
        q = fetch(ROWS)
        self.assertEqual(q["AAPL.US"].last, 341.07)
        self.assertEqual(q["700.HK"].last, 436.6)

    def test_extended_blocks_with_a_trade_time(self) -> None:
        q = fetch(ROWS)
        self.assertEqual(q["AAPL.US"].extended, (
            SessionQuote("pre", 336.01, datetime(2026, 9, 25, 13, 30, 1, tzinfo=timezone.utc)),
            SessionQuote("post", 341.46, datetime(2026, 9, 25, 23, 59, 58, tzinfo=timezone.utc)),
        ))
        self.assertEqual(q["700.HK"].extended, ())


if __name__ == "__main__":
    unittest.main()

"""Synthetic HTTP handler test; no market or model code is executed."""
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest

from lynch_reader.catalog import QuoteSnapshot
from lynch_reader.web import create_app


class MutableProvider:
    def __init__(self):
        self.middle = 20

    def current(self, ticker):
        return {"status": "confirmed", "review_id": "one", "output": {
            "ticker": ticker, "currency": "USD", "anchors": {"L": 10, "M": self.middle, "H": 30},
            "candidate": None, "option": None}}


class ConfirmedHttp(unittest.TestCase):
    def test_result_update_changes_response_etag_without_document_edit(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "FAKE.US.md").write_text("# FAKE.US — Fake\n\n> 林奇：`stalwart` | 更新：2026-01-01\n", encoding="utf-8")
            provider = MutableProvider()
            quotes = SimpleNamespace(read=lambda symbols: QuoteSnapshot(None, None))
            archive = SimpleNamespace(tickers=lambda: set())
            app = create_app(root, root / "absent-dist", quotes, None, archive, valuation_provider=provider)
            handler = next(r.endpoint for r in app.routes if getattr(r, "path", None) == "/api/tickers")
            first = handler(SimpleNamespace(headers={}))
            etag = first.headers["etag"]
            self.assertEqual(json.loads(first.body)["tickers"][0]["M"], 20)
            self.assertEqual(handler(SimpleNamespace(headers={"if-none-match": etag})).status_code, 304)
            provider.middle = 21
            changed = handler(SimpleNamespace(headers={"if-none-match": etag}))
            self.assertEqual(changed.status_code, 200)
            self.assertNotEqual(changed.headers["etag"], etag)
            self.assertEqual(json.loads(changed.body)["tickers"][0]["M"], 21)


if __name__ == "__main__":
    unittest.main()

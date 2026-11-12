"""Synthetic-only tests: the catalog must not run models or read price anchors from prose."""
from __future__ import annotations

import math
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from eval.lib.price import Quote
from lynch_reader.catalog import QuoteBatch, QuoteSnapshot, catalog


class Provider:
    def __init__(self, result):
        self.result = result

    def current(self, ticker):
        return self.result


class Quotes:
    def __init__(self, price):
        self.price = price

    def read(self, symbols):
        if self.price is None:
            return QuoteSnapshot(None, None)
        quote = Quote(symbols[0], self.price, None, None)
        return QuoteSnapshot(QuoteBatch(tuple(symbols), {symbols[0]: quote}, 1700000000), None)


def confirmed():
    return {"status": "confirmed", "review_id": "review-1", "output": {
        "ticker": "FAKE.US", "currency": "USD", "units": {"price": "USD_PER_SHARE"},
        "anchors": {"L": 10, "M": 20, "H": 30},
        "option": {"value": 4, "value_unit": "USD_PER_SHARE", "anchors": {"L": 10, "M": 22, "H": 34}},
        "candidate": {"label": "新业务", "anchors": {"L": 12, "M": 24, "H": 36}},
    }}


class ConfirmedResultCatalog(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        # Deliberately plausible legacy values: a fallback would display 900/901/902.
        (self.root / "FAKE.US.md").write_text(
            "# FAKE.US — Example\n\n> 林奇：`stalwart` | 更新：2026-01-01\n\n"
            "L = **900** M = **901** H = **902**\n", encoding="utf-8")

    def read(self, result, price=20, earnings=None):
        return catalog(self.root, Quotes(price), earnings_dates=earnings,
                       valuation_provider=Provider(result))["tickers"][0]

    def test_confirmed_result_supplies_each_band_and_zone(self):
        row = self.read(confirmed())
        self.assertEqual((row["L"], row["M"], row["H"]), (10, 20, 30))
        self.assertEqual(row["candidate"]["label"], "新业务")
        self.assertEqual(row["option"]["H"], 34)
        self.assertEqual(row["band"]["zone"], "neutral")
        self.assertEqual(row["band"]["option_zone"], "neutral")
        self.assertEqual(row["band"]["candidate_zone"], "neutral")
        self.assertEqual(row["valuation_status"], "confirmed")

    def test_option_short_label_reaches_homepage(self):
        value = confirmed()
        value['output']['option']['label'] = '新产线'
        self.assertEqual(self.read(value)['option']['label'], '新产线')

    def test_missing_and_unconfirmed_never_read_numbers_from_markdown(self):
        for status in ("missing", "unconfirmed", "stale", "proof_unavailable"):
            with self.subTest(status=status):
                row = self.read({"status": status, "output": confirmed()["output"]})
                self.assertEqual((row["L"], row["M"], row["H"]), (None, None, None))
                self.assertEqual(row["valuation_status"], status)
                self.assertEqual(row["band"]["price"], 20)
                self.assertIsNone(row["band"]["zone"])
                self.assertEqual(row["name"], "Example")

    def test_invalid_output_is_not_exposed(self):
        for value in (True, math.inf, -2, 20):
            with self.subTest(value=value):
                result = confirmed()
                result["output"]["anchors"]["L"] = value
                row = self.read(result)
                self.assertEqual(row["valuation_status"], "invalid")
                self.assertIsNone(row["L"])

    def test_bad_identity_or_candidate_is_not_exposed(self):
        for key, value in (("ticker", "OTHER.US"), ("currency", "ABC"), ("candidate", {"label": "", "anchors": {"L": 1, "M": 2, "H": 3}})):
            with self.subTest(key=key):
                result = confirmed()
                result["output"][key] = value
                self.assertEqual(self.read(result)["valuation_status"], "invalid")

    def test_legacy_unknown_state_cannot_hide_uncertainty(self):
        record = confirmed()
        record.update(status='legacy_accepted', origin='local_operator_attestation', batch_id='batch',
                      historical_evidence_source={'reference': 'old.json', 'rationale': '历史材料'},
                      historical_evidence_flags=[], historical_comparison={
                          'candidate': {'state': 'unknown'}, 'option': {'state': 'unknown'}})
        row = self.read(record)
        self.assertIn('candidate_unknown', row['historical_evidence_flags'])
        self.assertIn('option_unknown', row['historical_evidence_flags'])

    def test_legacy_known_state_conflict_is_visible(self):
        record = confirmed()
        record.update(status='legacy_accepted', origin='local_operator_attestation', batch_id='batch',
                      historical_evidence_source={'reference': 'old.json', 'rationale': '历史材料'},
                      historical_evidence_flags=[], historical_comparison={
                          'candidate': {'state': 'absent'}, 'option': {'state': 'present'}})
        row = self.read(record)
        self.assertEqual(row['valuation_status'], 'legacy_accepted')
        self.assertIn('historical_values_differ', row['historical_evidence_flags'])
        self.assertEqual(row['historical_evidence_differences'][0]['field'], 'candidate.presence')

    def test_mixed_share_and_fund_units_are_invalid(self):
        result = confirmed()
        result["output"]["units"] = {"price": "USD_PER_SHARE", "candidate": "USD_PER_UNIT"}
        self.assertEqual(self.read(result)["valuation_status"], "invalid")

    def test_conflicting_unit_on_candidate_wrapper_is_invalid(self):
        result = confirmed()
        result["output"]["candidate"]["unit"] = "USD_PER_UNIT"
        self.assertEqual(self.read(result)["valuation_status"], "invalid")

    def test_provider_status_with_wrong_type_is_invalid(self):
        self.assertEqual(self.read({"status": []})["valuation_status"], "invalid")

    def test_missing_candidate_or_option_contract_is_invalid(self):
        for key in ("candidate", "option"):
            with self.subTest(key=key):
                result = confirmed()
                del result["output"][key]
                self.assertEqual(self.read(result)["valuation_status"], "invalid")

    def test_no_quote_keeps_confirmed_bands_and_earnings(self):
        def earnings(updates, versions):
            self.assertEqual(updates["FAKE.US"].isoformat(), "2026-01-01")
            return {"FAKE.US": {"stale_after_earnings": True}}
        row = self.read(confirmed(), price=None, earnings=earnings)
        self.assertEqual(row["M"], 20)
        self.assertIsNone(row["band"])
        self.assertTrue(row["stale_after_earnings"])

    def test_changed_confirmed_result_updates_without_document_change(self):
        provider = Provider(confirmed())
        first = catalog(self.root, Quotes(20), valuation_provider=provider)["tickers"][0]
        provider.result = confirmed()
        provider.result["output"]["anchors"]["M"] = 21
        second = catalog(self.root, Quotes(20), valuation_provider=provider)["tickers"][0]
        self.assertEqual(first["M"], 20)
        self.assertEqual(second["M"], 21)

    def test_provider_failure_or_untrusted_record_does_not_use_document_prices(self):
        class BrokenProvider:
            def current(self, ticker):
                raise OSError("unavailable")
        row = catalog(self.root, Quotes(20), valuation_provider=BrokenProvider())["tickers"][0]
        self.assertEqual(row["valuation_status"], "unavailable")
        self.assertIsNone(row["M"])
        record = confirmed()
        record.pop("review_id")
        self.assertEqual(self.read(record)["valuation_status"], "invalid")

    def test_migrated_result_with_disclosed_old_concerns_can_be_displayed(self):
        record = confirmed()
        record["output"]["legacy_concerns"] = ["historical issue retained in the document"]
        row = self.read(record)
        self.assertEqual(row["valuation_status"], "confirmed")
        self.assertEqual(row["M"], 20)

    def test_zero_value_option_has_no_additional_band(self):
        record = confirmed()
        record["output"]["option"]["value"] = 0
        row = self.read(record)
        self.assertIsNone(row["option"])
        self.assertIsNone(row["band"]["option_zone"])


if __name__ == "__main__":
    unittest.main()

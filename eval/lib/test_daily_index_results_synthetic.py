"""Only fabricated results/quotes; never execute a stock model or send a message."""
import copy
from pathlib import Path
import tempfile
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from eval.lib.frameworks import FrameworkIdentity
from scripts import daily_bands, gen_index
from eval.lib.result_view import map_result


def record(ticker="FAKE.US", status="confirmed"):
    return {
        "status": status, "review_id": "review-1", "batch_id": "batch-1",
        "origin": "local_operator_attestation",
        "historical_comparison": {"candidate": {"state": "present"}, "option": {"state": "present"}},
        "historical_evidence_flags": [],
        "historical_evidence_source": {"reference": "历史来源", "rationale": "核对来源"},
        "output": {
            "ticker": ticker, "currency": "USD", "units": {"price": "USD_PER_SHARE"},
            "anchors": {"L": 10, "M": 20, "H": 30},
            "candidate": {"label": "扩产", "anchors": {"L": 12, "M": 24, "H": 36}},
            "option": {"value": 5, "value_unit": "USD_PER_SHARE",
                       "anchors": {"L": 11, "M": 22, "H": 35}, "status": "requires_reconciliation"},
            "review_flags": ["legacy_issue"],
            "rate_sensitivity": {
                "inputs": {"delta_cons": .0075, "rf_cap": .005},
                "consensus": {"L": 9, "M": 19, "H": 29},
                "pressure": {"L": 8, "M": 20, "H": 32},
                "price_currency": "USD", "price_unit": "per_share",
            },
            "baskets": {"r_perp": 4},
        },
    }


class Provider:
    def __init__(self, records):
        self.records = records
        self.calls = []

    def current(self, ticker):
        self.calls.append(ticker)
        value = self.records[ticker]
        if isinstance(value, Exception):
            raise value
        return copy.deepcopy(value)

    def calculate(self, ticker):
        raise AssertionError("stock model execution is forbidden")


def identity(ticker="FAKE.US"):
    return FrameworkIdentity(ticker=ticker, name="假公司 / Fake", lynch_primary="stalwart",
                             lynch_secondary=None, updated=None)


class ResultViewTest(unittest.TestCase):
    def test_option_short_label_is_shared_with_numeric_consumers(self):
        value = record()
        value['output']['option']['label'] = '新产线'
        status, result = map_result('FAKE.US', value)
        self.assertEqual(status, 'confirmed')
        self.assertEqual(result.option_label, '新产线')
        rows, errors = daily_bands.build_rows_from_results([identity()], Provider({'FAKE.US': value}),
                                                           {'FAKE.US': 20}, {}, archived=set())
        self.assertEqual(errors, [])
        self.assertTrue(any('新产线' in line for line in daily_bands.format_pool_lines(rows[0])))

    def test_shared_view_exposes_old_candidate_only_as_historical_reference(self):
        value = record(status='legacy_accepted')
        value['output']['candidate'] = {'status': 'unavailable'}
        value['historical_comparison']['candidate'] = {
            'state': 'present', 'source': 'synthetic',
            'data': {'label': '旧候选', 'anchors': {'L': 12, 'M': 24, 'H': 36}},
        }
        status, result = map_result('FAKE.US', value)
        self.assertEqual(status, 'legacy_accepted')
        self.assertIsNone(result.candidate)
        self.assertEqual(result.historical_reference_bands[0]['label'], '旧候选')
        self.assertEqual(result.historical_reference_bands[0]['anchors']['M'], 24)

    def test_verified_result_preserves_label_option_flags_and_rate_scenarios(self):
        status, result = map_result("FAKE.US", record())
        self.assertEqual(status, "confirmed")
        self.assertEqual(result.candidate_label, "扩产")
        self.assertEqual(result.option_value, 5)
        self.assertEqual(result.option_status, "requires_reconciliation")
        self.assertEqual(result.review_flags, ("legacy_issue",))
        self.assertEqual(result.rate_consensus, (9, 19, 29))
        self.assertEqual(result.rate_pressure, (8, 20, 32))

    def test_unconfirmed_stale_and_missing_are_not_results(self):
        for state in ("missing", "unconfirmed", "stale", "invalid"):
            with self.subTest(state=state):
                self.assertEqual(map_result("FAKE.US", {"status": state})[0], state)
                self.assertIsNone(map_result("FAKE.US", {"status": state})[1])
        self.assertEqual(map_result("FAKE.US", {"confirmation_status": "confirmed", "output": record()["output"]})[0], "invalid")
        self.assertEqual(map_result("FAKE.US", {"status": []})[0], "invalid")

    def test_history_requires_batch_identity_not_fake_script_confirmation(self):
        historical = record(status="legacy_accepted")
        self.assertEqual(map_result("FAKE.US", historical)[0], "legacy_accepted")
        del historical["batch_id"]
        self.assertEqual(map_result("FAKE.US", historical)[0], "invalid")

    def test_history_requires_comparison_and_keeps_uncertainty(self):
        value = record(status="legacy_accepted")
        del value["historical_comparison"]
        self.assertEqual(map_result("FAKE.US", value)[0], "invalid")
        value = record(status="legacy_accepted")
        value["historical_comparison"] = {"candidate": {"state": "unknown"}, "option": {"state": "unknown"}}
        status, result = map_result("FAKE.US", value)
        self.assertEqual(status, "legacy_accepted")
        self.assertIn("candidate_unknown", result.historical_evidence_flags)
        self.assertIn("option_unknown", result.historical_evidence_flags)
        value["historical_comparison"]["candidate"]["state"] = "absent"
        status, result = map_result("FAKE.US", value)
        self.assertEqual(status, "legacy_accepted")
        self.assertIn("historical_values_differ", result.historical_evidence_flags)
        self.assertEqual(result.historical_evidence_differences[0]["field"], "candidate.presence")

    def test_historical_review_and_fund_metadata_reach_the_shared_view(self):
        value = record(status="legacy_accepted")
        value["historical_comparison"].update({"review_flags": {"state": "known", "values": ["old_issue"]},
                                                  "option_status": {"state": "known", "value": "unavailable"},
                                                  "fund_reference_status": {"historical": "stale"}})
        value["historical_evidence_differences"] = [{"field": "anchors.M", "kind": "changed",
                                                      "historical": 19, "current": 20}]
        value["output"]["fund_reference_status"] = "unavailable"
        status, view = map_result("FAKE.US", value)
        self.assertEqual(status, "legacy_accepted")
        self.assertEqual(view.historical_review_flags, ("old_issue",))
        self.assertEqual(view.historical_option_status, "unavailable")
        self.assertEqual(view.historical_fund_reference_status, "stale")
        self.assertEqual(view.fund_reference_status, "unavailable")
        self.assertIn("historical_values_differ", view.historical_evidence_flags)
        self.assertEqual(view.historical_evidence_source["reference"], "历史来源")

    def test_unavailable_optional_scenarios_do_not_become_tradeable_bands(self):
        value = record()
        value["output"]["option"]["status"] = "unavailable"
        value["output"]["candidate"]["status"] = "unavailable"
        del value["output"]["option"]["anchors"]
        del value["output"]["candidate"]["anchors"]
        status, result = map_result("FAKE.US", value)
        self.assertEqual(status, "confirmed")
        self.assertIsNone(result.option_band)
        self.assertIsNone(result.candidate)
        self.assertEqual(result.option_status, "unavailable")
        self.assertEqual(result.candidate_status, "unavailable")
        value["output"]["option"]["anchors"] = {"L": 11, "M": 22, "H": 35}
        self.assertEqual(map_result("FAKE.US", value)[0], "invalid")

    def test_old_rate_shape_is_marked_for_migration_without_hiding_base(self):
        value = record("FAKE.HK")
        output = value["output"]
        output["currency"] = "HKD"
        output["units"]["price"] = "HKD_PER_SHARE"
        output["option"]["value_unit"] = "HKD_PER_SHARE"
        output["rate"] = {"inputs": {"delta_cons": .0075, "rf_cap": .005},
                          "consensus": [9, 19, 29], "pressure": [8, 20, 32]}
        del output["rate_sensitivity"]
        status, result = map_result("FAKE.HK", value)
        self.assertEqual(status, "confirmed")
        self.assertEqual(result.anchors, (10, 20, 30))
        self.assertIsNone(result.rate_consensus)
        self.assertTrue(result.rate_error)
        output["rate_sensitivity"] = {"inputs": {"delta_cons": .0075, "rf_cap": .005}}
        self.assertTrue(map_result("FAKE.HK", value)[1].rate_error)

    def test_home_and_daily_use_the_same_mapper(self):
        app = str(Path(__file__).resolve().parents[2] / "app/server")
        sys.path.insert(0, app)
        try:
            from lynch_reader.valuation_catalog import map_result as home_map
            self.assertIs(home_map, map_result)
            value = record(status="legacy_accepted")
            value["historical_comparison"]["candidate"]["state"] = "unknown"
            status, view = home_map("FAKE.US", value)
            self.assertEqual(status, "legacy_accepted")
            self.assertEqual((view.L, view.M, view.H), (10, 20, 30))
            self.assertEqual(view.v_opt, 5)
            self.assertEqual(view.historical_evidence_states["candidate"], "unknown")
            self.assertIn("candidate_unknown", view.historical_evidence_flags)
        finally:
            sys.path.remove(app)

    def test_real_home_catalog_does_not_price_unavailable_optional_bands(self):
        app = str(Path(__file__).resolve().parents[2] / "app/server")
        sys.path.insert(0, app)
        try:
            from eval.lib.price import Quote
            from lynch_reader.catalog import QuoteBatch, QuoteSnapshot, catalog
            class Quotes:
                def read(self, tickers):
                    return QuoteSnapshot(QuoteBatch(tuple(tickers), {"FAKE.US": Quote("FAKE.US", 32, None, None)}, 1700000000), None)
            with tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                (root / "FAKE.US.md").write_text("# FAKE.US — Fake\n\n> 林奇：`stalwart` | 更新：2026-01-01\n")
                value = record()
                value["output"]["option"]["status"] = "unavailable"
                value["output"]["candidate"]["status"] = "unavailable"
                del value["output"]["option"]["anchors"]
                del value["output"]["candidate"]["anchors"]
                row = catalog(root, Quotes(), valuation_provider=Provider({"FAKE.US": value}))["tickers"][0]
                self.assertIsNone(row["option"])
                self.assertIsNone(row["candidate"])
                self.assertEqual(row["option_review_status"], "unavailable")
                self.assertEqual(row["candidate_review_status"], "unavailable")
                self.assertIsNone(row["band"]["option_zone"])
                self.assertIsNone(row["band"]["candidate_zone"])
        finally:
            sys.path.remove(app)

    def test_bad_units_and_currency_do_not_become_price_bands(self):
        for mutate in (lambda r: r["output"].update(currency="CNY"),
                       lambda r: r["output"]["anchors"].update(unit="USD_PER_100_SHARES"),
                       lambda r: r["output"]["candidate"].update(label="")):
            value = record()
            mutate(value)
            self.assertEqual(map_result("FAKE.US", value)[0], "invalid")

    def test_option_amount_without_per_share_unit_is_not_assumed(self):
        for unit in ("USD_MILLION", None, {"unexpected": "USD_PER_SHARE"}):
            with self.subTest(unit=unit):
                value = record()
                value["output"]["option"]["value_unit"] = unit
                status, result = map_result("FAKE.US", value)
                self.assertEqual(status, "confirmed")
                self.assertIsNone(result.option_value)
                self.assertEqual(result.option_band, (11, 22, 35))

    def test_bad_rate_data_is_visible_without_discarding_base(self):
        value = record()
        value["output"]["rate_sensitivity"]["price_currency"] = "CNY"
        status, result = map_result("FAKE.US", value)
        self.assertEqual(status, "confirmed")
        self.assertEqual(result.anchors, (10, 20, 30))
        self.assertTrue(result.rate_error)
        self.assertIsNone(result.rate_consensus)

    def test_fund_unit_keeps_per_unit_semantics(self):
        value = record("159307.SZ")
        output = value["output"]
        output["currency"] = "CNY"
        output["units"]["price"] = "CNY_PER_FUND_UNIT"
        output["option"]["value_unit"] = "CNY_PER_FUND_UNIT"
        output["rate_sensitivity"]["price_currency"] = "CNY"
        output["rate_sensitivity"]["price_unit"] = "per_fund_unit"
        status, result = map_result("159307.SZ", value)
        self.assertEqual(status, "confirmed")
        self.assertEqual(result.option_value, 5)
        self.assertEqual(result.rate_consensus, (9, 19, 29))

    def test_invalid_optional_basket_does_not_erase_valid_prices(self):
        value = record()
        value["output"]["baskets"] = {"r_perp": True}
        status, result = map_result("FAKE.US", value)
        self.assertEqual(status, "confirmed")
        self.assertEqual(result.anchors, (10, 20, 30))
        self.assertIsNone(result.baskets)
        self.assertTrue(result.basket_error)


class DigestTest(unittest.TestCase):
    def rows(self, price, previous=None, item=None):
        provider = Provider({"FAKE.US": item or record()})
        return daily_bands.build_rows_from_results([identity()], provider, {"FAKE.US": price},
                                                   previous or {}, archived=set())

    def test_boundaries_match_existing_five_tier_semantics(self):
        for price, expected in ((9, "deep_discount"), (10, "conservative"), (15, "neutral"),
                                (25, "optimistic"), (30, "optimistic"), (31, "stretched")):
            with self.subTest(price=price):
                rows, errors = self.rows(price)
                self.assertEqual(errors, [])
                self.assertEqual(rows[0].zone, expected)

    def test_all_bands_cross_independently_and_rates_never_trigger_cross(self):
        rows, errors = self.rows(32, {"FAKE.US": {"zone": "neutral", "option_zone": "neutral",
                                                     "candidate_zone": "neutral"}})
        row = rows[0]
        self.assertEqual(errors, [])
        self.assertTrue(row.crossed)
        self.assertTrue(row.option_crossed)
        self.assertTrue(row.candidate_crossed)
        lines = daily_bands.format_pool_lines(row)
        self.assertTrue(any("候选(扩产)" in line for line in lines))
        self.assertTrue(any("利率·共识" in line for line in lines))
        self.assertTrue(any("利率·压力" in line for line in lines))
        self.assertTrue(any("待核" in line for line in lines))
        rows, _ = self.rows(20, {"FAKE.US": {"zone": "neutral", "option_zone": "neutral",
                                                  "candidate_zone": "neutral"}})
        self.assertFalse(rows[0].any_crossed)

    def test_cross_messages_keep_short_labels_for_both_extra_bands(self):
        value = record()
        value['output']['option']['label'] = '新产线'
        rows, _ = self.rows(32, {'FAKE.US': {'zone': 'neutral', 'option_zone': 'neutral',
                                              'candidate_zone': 'neutral'}}, value)
        _, lines = daily_bands.build_cross_post(rows, as_of_local='2040-01-01')
        self.assertTrue(any('新产线 「' in line for line in lines))
        self.assertTrue(any('候选(扩产) 「' in line for line in lines))

    def test_missing_option_amount_still_shows_confirmed_option_band_without_guessing_amount(self):
        value = record()
        value["output"]["option"]["value_unit"] = "USD_MILLION"
        rows, _ = self.rows(20, item=value)
        lines = daily_bands.format_pool_lines(rows[0])
        self.assertTrue(any("新业务" in line for line in lines))
        self.assertFalse(any("+V(5)" in line for line in lines))
        rows, _ = self.rows(32, {"FAKE.US": {"zone": "neutral", "option_zone": "neutral"}}, value)
        _, cross = daily_bands.build_cross_post(rows, as_of_local="2026-01-01")
        self.assertTrue(any("新业务 「中性」 → 「乐观」" in line for line in cross))

    def test_missing_results_quotes_and_archives_are_explicit_and_no_fallback_occurs(self):
        provider = Provider({"FAKE.US": {"status": "missing"}, "BAD.US": RuntimeError("offline"),
                             "ARCH.US": {"status": "missing"}})
        rows, errors = daily_bands.build_rows_from_results(
            [identity(), identity("BAD.US"), identity("ARCH.US")], provider,
            {"FAKE.US": 20, "BAD.US": 20, "ARCH.US": 20}, {}, archived={"ARCH.US"})
        self.assertEqual(len(rows), 1)
        self.assertIn("已归档，未迁移估值", " ".join(daily_bands.format_pool_lines(rows[0])))
        self.assertIn("20", " ".join(daily_bands.format_pool_lines(rows[0])))
        self.assertEqual(len(errors), 2)
        self.assertTrue(any("FAKE.US" in e for e in errors))
        self.assertTrue(any("BAD.US" in e for e in errors))
        self.assertIn("ARCH.US", provider.calls)

    def test_archived_missing_quote_remains_visible_and_never_crosses(self):
        provider = Provider({"ARCH.US": {"status": "missing"}})
        rows, errors = daily_bands.build_rows_from_results([identity("ARCH.US")], provider, {}, {}, archived={"ARCH.US"})
        self.assertEqual(errors, [])
        self.assertFalse(rows[0].any_crossed)
        self.assertIn("暂无报价", " ".join(daily_bands.format_pool_lines(rows[0])))
        self.assertEqual(provider.calls, ["ARCH.US"])

    def test_archived_with_existing_result_keeps_band_and_cross_without_recalculation(self):
        provider = Provider({"FAKE.US": record()})
        rows, errors = daily_bands.build_rows_from_results(
            [identity()], provider, {"FAKE.US": 32},
            {"FAKE.US": {"zone": "neutral", "option_zone": "neutral", "candidate_zone": "neutral"}},
            archived={"FAKE.US"})
        self.assertEqual(errors, [])
        self.assertTrue(rows[0].any_crossed)
        text = " ".join(daily_bands.format_pool_lines(rows[0]))
        self.assertIn("已归档", text)
        self.assertNotIn("未迁移估值", text)
        self.assertIn("10/20/30", text)
        self.assertIn("候选(扩产)", text)
        self.assertEqual(provider.calls, ["FAKE.US"])

    def test_archived_with_existing_result_but_no_quote_keeps_all_result_bands(self):
        provider = Provider({"FAKE.US": record()})
        rows, errors = daily_bands.build_rows_from_results([identity()], provider, {}, {}, archived={"FAKE.US"})
        self.assertEqual(errors, [])
        text = " ".join(daily_bands.format_pool_lines(rows[0]))
        self.assertIn("暂无报价", text)
        self.assertIn("10/20/30", text)
        self.assertIn("候选(扩产)", text)
        self.assertIn("11/22/35", text)
        self.assertIn("利率·共识", text)
        self.assertFalse(rows[0].any_crossed)

    def test_archived_stale_or_unreadable_does_not_claim_never_migrated(self):
        for value, message in (({"status": "stale"}, "估值依据已变化"),
                               (RuntimeError("offline"), "估值结果暂不可读取")):
            with self.subTest(value=value):
                provider = Provider({"FAKE.US": value})
                rows, errors = daily_bands.build_rows_from_results([identity()], provider, {"FAKE.US": 20}, {}, archived={"FAKE.US"})
                self.assertEqual(errors, [])
                text = " ".join(daily_bands.format_pool_lines(rows[0]))
                self.assertIn("已归档", text)
                self.assertIn(message, text)
                self.assertNotIn("未迁移估值", text)

    def test_restoring_an_archived_existing_result_preserves_its_band(self):
        provider = Provider({"FAKE.US": record()})
        archived, errors = daily_bands.build_rows_from_results([identity()], provider, {"FAKE.US": 20}, {}, archived={"FAKE.US"})
        restored, restored_errors = daily_bands.build_rows_from_results([identity()], provider, {"FAKE.US": 20}, {}, archived=set())
        self.assertEqual(errors + restored_errors, [])
        self.assertEqual(archived[0].record["anchors"], restored[0].record["anchors"])
        self.assertIn("已归档", daily_bands.format_pool_lines(archived[0]))
        self.assertNotIn("已归档", daily_bands.format_pool_lines(restored[0]))
        self.assertEqual(provider.calls, ["FAKE.US", "FAKE.US"])

    def test_real_archive_store_and_restore_use_only_private_fixture(self):
        app = str(Path(__file__).resolve().parents[2] / "app/server")
        sys.path.insert(0, app)
        try:
            from lynch_reader.archive import FrameworkArchive
            with tempfile.TemporaryDirectory() as temp:
                archive = FrameworkArchive(Path(temp) / "archive.json")
                archive.archive("ARCH.US")
                provider = Provider({"ARCH.US": {"status": "missing"}})
                rows, errors = daily_bands.build_rows_from_results([identity("ARCH.US")], provider,
                                                                    {"ARCH.US": 20}, {}, archived=archive.tickers())
                self.assertEqual(errors, [])
                self.assertIn("已归档，未迁移估值", " ".join(daily_bands.format_pool_lines(rows[0])))
                self.assertEqual(provider.calls, ["ARCH.US"])
                archive.restore("ARCH.US")
                rows, errors = daily_bands.build_rows_from_results([identity("ARCH.US")], provider,
                                                                    {"ARCH.US": 20}, {}, archived=archive.tickers())
                self.assertEqual(rows, [])
                self.assertTrue(any("待迁移" in error for error in errors))
        finally:
            sys.path.remove(app)

    def test_history_uncertainty_and_unavailable_option_are_visible_without_false_cross(self):
        value = record(status="legacy_accepted")
        value["historical_comparison"] = {"candidate": {"state": "unknown"}, "option": {"state": "unknown"}}
        value["output"]["candidate"]["status"] = "unavailable"
        value["output"]["option"]["status"] = "unavailable"
        del value["output"]["candidate"]["anchors"]
        del value["output"]["option"]["anchors"]
        rows, errors = self.rows(32, {"FAKE.US": {"zone": "neutral", "candidate_zone": "neutral", "option_zone": "neutral"}}, value)
        self.assertEqual(errors, [])
        row = rows[0]
        lines = " ".join(daily_bands.format_pool_lines(row))
        self.assertIn("历史框架沿用", lines)
        self.assertIn("候选价带是否完整仍待核对", lines)
        self.assertIn("新业务价带是否完整仍待核对", lines)
        self.assertIn("候选价带暂不可用", lines)
        self.assertIn("新业务估值暂不可用", lines)
        self.assertFalse(row.option_crossed)
        self.assertFalse(row.candidate_crossed)

    def test_historical_differences_and_fund_status_are_not_lost_in_digest(self):
        value = record(status="legacy_accepted")
        value["historical_comparison"].update({"review_flags": {"state": "known", "values": ["old_issue"]},
                                                  "option_status": {"state": "known", "value": "unavailable"},
                                                  "fund_reference_status": {"historical": "stale"}})
        value["historical_evidence_differences"] = [{"field": "anchors.M", "kind": "changed",
                                                      "historical": 19, "current": 20}]
        value["output"]["review_flags"] = []
        value["output"]["fund_reference_status"] = "unavailable"
        rows, errors = self.rows(20, item=value)
        self.assertEqual(errors, [])
        text = " ".join(daily_bands.format_pool_lines(rows[0]))
        self.assertIn("历史材料与当前估值结果有差异", text)
        self.assertIn("估值假设有待核对事项", text)
        self.assertIn("旧材料的新业务估值状态仍需核对", text)
        self.assertIn("基金穿透对照存在待核对事项", text)

    def test_known_double_counting_is_explicit_in_digest(self):
        value = record(status="legacy_accepted")
        value["output"]["review_flags"] = ["historical_double_counting"]
        rows, errors = self.rows(20, item=value)
        self.assertEqual(errors, [])
        text = " ".join(daily_bands.format_pool_lines(rows[0]))
        self.assertIn("旧估值存在重复计价，仅供历史对照", text)
        self.assertIn("L/M/H = 10/20/30", text)
        self.assertIsNone(rows[0].record['zone'])
        self.assertNotIn('anchors', rows[0].record)
        self.assertFalse(rows[0].any_crossed)

    def test_rate_errors_are_reported_without_hiding_main_band(self):
        value = record()
        value["output"]["rate_sensitivity"]["price_currency"] = "CNY"
        rows, errors = self.rows(20, item=value)
        self.assertEqual(rows[0].zone, "neutral")
        self.assertIsNone(rows[0].rf)
        self.assertTrue(any("利率情景结果无效" in e for e in errors))

    def test_bad_quotes_preserve_bands_without_fabricating_a_zone(self):
        for price in (None, True, float("inf"), float("nan"), -1, 0, 10 ** 1000):
            with self.subTest(price=str(price)[:12]):
                rows, errors = self.rows(price)
                self.assertTrue(any('FAKE.US' in error and '报价' in error for error in errors))
                self.assertEqual(rows[0].meta.M, 20)
                self.assertIsNone(rows[0].price)
                text = ' '.join(daily_bands.format_pool_lines(rows[0]))
                self.assertIn('暂无报价', text)
                self.assertNotIn('已归档', text)

    def test_pure_path_never_calls_quotes_network_writes_or_old_price_readers(self):
        with patch.object(daily_bands, "fetch_quotes", side_effect=AssertionError("network")), \
             patch.object(daily_bands, "send_post", side_effect=AssertionError("message")), \
             patch.object(daily_bands, "write_bands", side_effect=AssertionError("write")), \
             patch.object(daily_bands, "read_rf_readings", side_effect=AssertionError("old rate file"), create=True), \
             patch.object(daily_bands, "load_all_frameworks", side_effect=AssertionError("old document prices"), create=True):
            rows, errors = self.rows(20)
        self.assertEqual(errors, [])
        self.assertEqual(rows[0].record["inputs_used"]["valuation_ref"], "review-1")

    def test_existing_quote_object_status_is_preserved(self):
        quote = SimpleNamespace(last=20, status="NORMAL")
        rows, errors = self.rows(quote)
        self.assertEqual(errors, [])
        self.assertEqual(rows[0].price, 20)
        self.assertEqual(rows[0].record["inputs_used"]["quote_status"], "NORMAL")


class IndexTest(unittest.TestCase):
    def test_index_collects_axis_metadata_from_result_snapshots(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / 'FAKE.US.md').write_text('# FAKE.US — Fake\n\n> 林奇：`stalwart` | 更新：2026-01-01\n')
            value = record()
            value['output']['axis_metadata'] = {'gold': {
                'axis_id': 'gold', 'logical_id': '_AXIS.gold', 'axis_type': 'shared_scenario_axis',
                'label': '黄金', 'title': '黄金情景轴', 'role': 'priced',
                'priced_mounts': ['FAKE.US'], 'reference_mounts': [], 'historical_consensus': None}}
            value['output']['source_versions'] = {'axis:gold': 'synthetic'}
            text = gen_index.build_index_from_results(root, Provider({'FAKE.US': value}), archived=set())
            self.assertIn('_AXIS.gold', text)
            self.assertIn('挂载：FAKE.US', text)

    def test_index_reads_identity_and_provider_not_markdown_prices(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "FAKE.US.md").write_text("# FAKE.US — Fake\n\n> 林奇：`stalwart` | 更新：2026-01-01\n\n**乱写价带** 1/2/3\n")
            provider = Provider({"FAKE.US": record()})
            text = gen_index.build_index_from_results(root, provider, archived=set(), axis_metadata=[])
            self.assertIn("[FAKE.US](./FAKE.US.md)", text)
            self.assertIn("篮 4", text)
            self.assertNotIn("10%", text)
            self.assertNotIn("1/2/3", text)
            self.assertEqual(provider.calls, ["FAKE.US"])

    def test_index_reports_missing_machine_basket_instead_of_parsing_old_inputs(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "FAKE.US.md").write_text("# FAKE.US — Fake\n\n> 林奇：`stalwart` | 更新：2026-01-01\n\n篮 7 20%\n")
            value = record()
            del value["output"]["baskets"]
            text = gen_index.build_index_from_results(root, Provider({"FAKE.US": value}), archived=set(), axis_metadata=[])
            self.assertIn("篮子：未提供", text)
            self.assertNotIn("篮 7", text)

    def test_index_real_axis_metadata_sorting_and_standalone_chinese_name(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "000100.SZ.md").write_text("# 000100.SZ — TCL 科技\n\n> 林奇：`cyclical` | 更新：2026-01-01\n")
            value = record("000100.SZ")
            axes = [{"axis_id": "z-axis", "logical_id": "_AXIS.z-axis", "axis_type": "shared_scenario_axis",
                     "priced_mounts": ["B.US", "A.US"], "reference_mounts": []},
                    {"axis_id": "a-axis", "logical_id": "_AXIS.a-axis", "axis_type": "shared_scenario_axis",
                     "priced_mounts": [], "reference_mounts": ["Z.US", "X.US"]}]
            first = gen_index.build_index_from_results(root, Provider({"000100.SZ": value}), archived=set(), axis_metadata=axes)
            second = gen_index.build_index_from_results(root, Provider({"000100.SZ": value}), archived=set(), axis_metadata=list(reversed(axes)))
            self.assertEqual(first, second)
            self.assertIn("TCL 科技", first)
            self.assertLess(first.index("_AXIS.a-axis"), first.index("_AXIS.z-axis"))
            self.assertIn("A.US、B.US", first)
            self.assertIn("X.US、Z.US", first)

    def test_duplicate_axis_from_stock_snapshots_is_deduplicated_and_conflicts_are_marked(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "FAKE.US.md").write_text("# FAKE.US — Fake\n\n> 林奇：`stalwart` | 更新：2026-01-01\n")
            base = {"axis_id": "gold", "logical_id": "_AXIS.gold", "axis_type": "shared_scenario_axis",
                    "priced_mounts": ["B.US", "A.US"], "reference_mounts": ["C.US"], "role": "priced"}
            same = {**base, "priced_mounts": ["A.US", "B.US"], "role": "reference", "historical_consensus": None}
            text = gen_index.build_index_from_results(root, Provider({"FAKE.US": record()}),
                                                     archived=set(), axis_metadata=[base, same])
            self.assertEqual(text.count("[`_AXIS.gold`]"), 1)
            bad = {**same, "priced_mounts": ["A.US"]}
            text = gen_index.build_index_from_results(root, Provider({"FAKE.US": record()}),
                                                     archived=set(), axis_metadata=[base, bad])
            self.assertIn('轴资料冲突，待核对', text)

    def test_index_archives_visible_and_statuses_do_not_claim_missing_basket(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for ticker in ("A.US", "B.US", "C.US", "D.US"):
                (root / f"{ticker}.md").write_text(f"# {ticker} — Example\n\n> 林奇：`stalwart` | 更新：2026-01-01\n")
            provider = Provider({"A.US": RuntimeError("unavailable"), "B.US": {"status": "stale"},
                                 "C.US": {"status": "missing"}, "D.US": {"status": "missing"}})
            text = gen_index.build_index_from_results(root, provider, archived={"D.US"}, axis_metadata=[])
            self.assertIn("估值结果暂不可读取", text)
            self.assertIn("估值依据已变化", text)
            self.assertIn("待迁移", text)
            self.assertIn("已归档，未迁移估值", text)
            self.assertIn("D.US", provider.calls)

    def test_index_distinguishes_archived_available_missing_and_stale(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for ticker in ("A.US", "B.US", "C.US"):
                (root / f"{ticker}.md").write_text(f"# {ticker} — Example\n\n> 林奇：`stalwart` | 更新：2026-01-01\n")
            provider = Provider({"A.US": record("A.US"), "B.US": {"status": "missing"}, "C.US": {"status": "stale"}})
            text = gen_index.build_index_from_results(root, provider, archived={"A.US", "B.US", "C.US"}, axis_metadata=[])
            lines = {ticker: next(line for line in text.splitlines() if f"[{ticker}]" in line) for ticker in ("A.US", "B.US", "C.US")}
            self.assertIn("已归档", lines["A.US"])
            self.assertIn("篮 4", lines["A.US"])
            self.assertNotIn("未迁移", lines["A.US"])
            self.assertIn("已归档，未迁移估值", lines["B.US"])
            self.assertIn("已归档，估值依据已变化", lines["C.US"])

    def test_multiple_basket_order_is_deterministic(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "FAKE.US.md").write_text("# FAKE.US — Fake\n\n> 林奇：`stalwart` | 更新：2026-01-01\n")
            first = record()
            first["output"]["baskets"] = {"r_z": 7, "r_a": 4}
            second = record()
            second["output"]["baskets"] = {"r_a": 4, "r_z": 7}
            one = gen_index.build_index_from_results(root, Provider({"FAKE.US": first}), archived=set(), axis_metadata=[])
            two = gen_index.build_index_from_results(root, Provider({"FAKE.US": second}), archived=set(), axis_metadata=[])
            self.assertEqual(one, two)


if __name__ == "__main__":
    unittest.main()

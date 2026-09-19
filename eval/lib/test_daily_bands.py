"""Formatter tests for the Feishu daily / cross-band digest."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPTS = REPO_ROOT / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import daily_bands  # noqa: E402
from eval.lib.frameworks import FrameworkMeta  # noqa: E402


def _meta(**kwargs) -> FrameworkMeta:
    base = dict(
        ticker="603993.SH",
        path=Path("frameworks/603993.SH.md"),
        name="洛阳钼业 / CMOC Group Limited",
        L=13.51,
        M=19.63,
        H=23.24,
        currency="CNY",
        eval_script=None,
        lynch_primary="cyclical",
        lynch_secondary=None,
    )
    base.update(kwargs)
    return FrameworkMeta(**base)


def _row(meta: FrameworkMeta, price: float, zone: str, **kwargs) -> daily_bands.Row:
    return daily_bands.Row(
        meta=meta,
        price=price,
        zone=zone,
        previous_zone=kwargs.get("previous_zone"),
        crossed=kwargs.get("crossed", False),
        notes=kwargs.get("notes", []),
        record={},
        option_zone=kwargs.get("option_zone"),
        previous_option_zone=kwargs.get("previous_option_zone"),
        option_crossed=kwargs.get("option_crossed", False),
    )


class PoolLinesTests(unittest.TestCase):
    def test_blocked_ticker_prints_no_zone_and_never_crosses(self) -> None:
        meta = _meta(ticker="X.US", path=Path("frameworks/X.US.md"), name="x / X Inc.", L=57.35, M=77.23,
                     H=136.83, currency="USD", lynch_primary="stalwart", blocked="③ 出带；解封 = ROIC ≥14%")
        r = _row(meta, 150.0, None, previous_zone="stretched")
        lines = daily_bands.format_pool_lines(r)
        self.assertEqual(lines[0], "⛔ X.US 150.0 USD")
        self.assertEqual(lines[1], "BLOCKED（③ 出带；解封 = ROIC ≥14%）· 对照带 L/M/H = 57.35/77.23/136.83")
        self.assertFalse(r.any_crossed)

    def test_us_header_omits_name(self) -> None:
        r = _row(
            _meta(
                ticker="NVDA.US",
                path=Path("frameworks/NVDA.US.md"),
                name="英伟达 / NVIDIA Corporation",
                L=154.01,
                M=304.53,
                H=359.88,
                currency="USD",
                lynch_primary="fast_grower",
            ),
            price=218.3,
            zone="conservative",
        )
        self.assertEqual(
            daily_bands.format_pool_lines(r)[0],
            "🟢 NVDA.US 218.3 USD",
        )

    def test_no_option_is_header_plus_base_lmh(self) -> None:
        r = _row(_meta(), price=19.01, zone="neutral")
        self.assertEqual(
            daily_bands.format_pool_lines(r),
            [
                "🟡 603993.SH 洛阳钼业 19.01 CNY",
                "「中性」 [16.57, 21.43) · L/M/H = 13.51/19.63/23.24",
            ],
        )

    def test_zone_bounds_open_ends(self) -> None:
        self.assertEqual(
            daily_bands.format_zone_bounds("deep_discount", 13.51, 19.63, 23.24),
            "P < 13.51",
        )
        self.assertEqual(
            daily_bands.format_zone_bounds("stretched", 13.51, 19.63, 23.24),
            "P > 23.24",
        )
        self.assertEqual(
            daily_bands.format_zone_bounds("optimistic", 13.51, 19.63, 23.24),
            "[21.43, 23.24]",
        )

    def test_v_opt_zero_skips_plus_v_line(self) -> None:
        meta = _meta(v_opt=0.0, p_tiers=(0.0, 0.25, 1.0))
        r = _row(meta, price=19.01, zone="neutral", option_zone="neutral")
        lines = daily_bands.format_pool_lines(r)
        self.assertEqual(len(lines), 2)
        self.assertFalse(any("+V" in line for line in lines))

    def test_plus_v_line_uses_credited_band_and_option_zone(self) -> None:
        meta = _meta(
            ticker="INTC.US",
            path=Path("frameworks/INTC.US.md"),
            name="英特尔 / Intel Corporation",
            L=27.2,
            M=36.8,
            H=46.4,
            currency="USD",
            lynch_primary="cyclical",
            lynch_secondary="asset_play",
            v_opt=66.6,
            p_tiers=(0.0, 0.516, 1.0),
        )
        r = _row(
            meta,
            price=106.24,
            zone="stretched",
            option_zone="optimistic",
        )
        self.assertEqual(
            daily_bands.format_pool_lines(r),
            [
                "🔴 INTC.US 106.2 USD",
                "「叙事溢价」 P > 46.4 · L/M/H = 27.2/36.8/46.4",
                "「乐观」 +V(66.6) [92.08, 113] · L/M/H = 27.2/71.17/113",
            ],
        )


class DailyPostTests(unittest.TestCase):
    def test_blank_line_between_tickers(self) -> None:
        rows = [
            _row(_meta(), price=19.01, zone="neutral"),
            _row(
                _meta(
                    ticker="0700.HK",
                    path=Path("frameworks/0700.HK.md"),
                    name="腾讯控股 / Tencent Holdings Ltd.",
                    L=400.0,
                    M=500.0,
                    H=600.0,
                    currency="HKD",
                    lynch_primary="stalwart",
                ),
                price=435.4,
                zone="deep_discount",
            ),
        ]
        title, lines = daily_bands.build_daily_post(rows, as_of_local="2026-09-11 07:30 CST")
        self.assertEqual(title, "lynch 价带日报 · 2026-09-11 07:30 CST")
        self.assertEqual(
            lines,
            [
                "🟡 603993.SH 洛阳钼业 19.01 CNY",
                "「中性」 [16.57, 21.43) · L/M/H = 13.51/19.63/23.24",
                "",
                "🟢 0700.HK 腾讯控股 435.4 HKD",
                "「深度折价」 P < 400 · L/M/H = 400/500/600",
            ],
        )


class ZoneChangedTests(unittest.TestCase):
    def test_none_previous_is_first_observation(self) -> None:
        self.assertFalse(daily_bands.zone_changed(None, "neutral"))

    def test_none_current_does_not_alert(self) -> None:
        self.assertFalse(daily_bands.zone_changed("neutral", None))

    def test_same_is_not_a_cross(self) -> None:
        self.assertFalse(daily_bands.zone_changed("neutral", "neutral"))

    def test_different_is_a_cross(self) -> None:
        self.assertTrue(daily_bands.zone_changed("neutral", "optimistic"))


class CreditedOptionZoneTests(unittest.TestCase):
    def test_v_opt_zero_is_none(self) -> None:
        meta = _meta(v_opt=0.0, p_tiers=(0.0, 0.25, 1.0))
        self.assertIsNone(daily_bands.credited_option_zone(19.01, meta))

    def test_missing_p_tiers_is_none(self) -> None:
        meta = _meta(v_opt=2.3)
        self.assertIsNone(daily_bands.credited_option_zone(26.83, meta))

    def test_intc_price_on_credited_band_is_optimistic(self) -> None:
        meta = _meta(
            ticker="INTC.US",
            L=27.2,
            M=36.8,
            H=46.4,
            currency="USD",
            v_opt=66.6,
            p_tiers=(0.0, 0.516, 1.0),
        )
        self.assertEqual(daily_bands.credited_option_zone(106.24, meta), "optimistic")


class CrossPostTests(unittest.TestCase):
    def test_appends_chinese_zone_transition_not_duplicate_lmh(self) -> None:
        r = _row(
            _meta(),
            price=22.00,
            zone="optimistic",
            previous_zone="neutral",
            crossed=True,
        )
        title, lines = daily_bands.build_cross_post(
            [r], as_of_local="2026-09-11 07:30 CST"
        )
        self.assertEqual(title, "lynch 跨带 · 2026-09-11 07:30 CST")
        self.assertEqual(
            lines,
            [
                "🟠 603993.SH 洛阳钼业 22.00 CNY",
                "「乐观」 [21.43, 23.24] · L/M/H = 13.51/19.63/23.24",
                "「中性」 → 「乐观」",
            ],
        )

    def test_cross_keeps_plus_v_snapshot_then_base_transition(self) -> None:
        meta = _meta(
            ticker="INTC.US",
            path=Path("frameworks/INTC.US.md"),
            name="英特尔 / Intel Corporation",
            L=27.2,
            M=36.8,
            H=46.4,
            currency="USD",
            v_opt=66.6,
            p_tiers=(0.0, 0.516, 1.0),
        )
        r = _row(
            meta,
            price=106.24,
            zone="stretched",
            previous_zone="optimistic",
            crossed=True,
            option_zone="optimistic",
        )
        _, lines = daily_bands.build_cross_post([r], as_of_local="x")
        self.assertEqual(
            lines,
            [
                "🔴 INTC.US 106.2 USD",
                "「叙事溢价」 P > 46.4 · L/M/H = 27.2/36.8/46.4",
                "「乐观」 +V(66.6) [92.08, 113] · L/M/H = 27.2/71.17/113",
                "「乐观」 → 「叙事溢价」",
            ],
        )

    def test_option_only_cross_emits_plus_v_transition(self) -> None:
        meta = _meta(
            ticker="INTC.US",
            path=Path("frameworks/INTC.US.md"),
            name="英特尔 / Intel Corporation",
            L=27.2,
            M=36.8,
            H=46.4,
            currency="USD",
            v_opt=66.6,
            p_tiers=(0.0, 0.516, 1.0),
        )
        r = _row(
            meta,
            price=106.24,
            zone="stretched",
            previous_zone="stretched",
            crossed=False,
            option_zone="optimistic",
            previous_option_zone="neutral",
            option_crossed=True,
        )
        _, lines = daily_bands.build_cross_post([r], as_of_local="x")
        self.assertEqual(
            lines,
            [
                "🔴 INTC.US 106.2 USD",
                "「叙事溢价」 P > 46.4 · L/M/H = 27.2/36.8/46.4",
                "「乐观」 +V(66.6) [92.08, 113] · L/M/H = 27.2/71.17/113",
                "+V 「中性」 → 「乐观」",
            ],
        )

    def test_both_crosses_emit_two_transition_lines(self) -> None:
        r = _row(
            _meta(),
            price=22.00,
            zone="optimistic",
            previous_zone="neutral",
            crossed=True,
            option_zone="neutral",
            previous_option_zone="conservative",
            option_crossed=True,
        )
        # no V_opt on this meta → snapshot has no +V line, but the
        # transition still reports the option-deck move when flagged.
        _, lines = daily_bands.build_cross_post([r], as_of_local="x")
        self.assertEqual(
            lines,
            [
                "🟠 603993.SH 洛阳钼业 22.00 CNY",
                "「乐观」 [21.43, 23.24] · L/M/H = 13.51/19.63/23.24",
                "「中性」 → 「乐观」",
                "+V 「保守」 → 「中性」",
            ],
        )


if __name__ == "__main__":
    unittest.main()

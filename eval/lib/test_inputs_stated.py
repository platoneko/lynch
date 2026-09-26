"""check_against_stated while framework docs drop the current price and its zone."""
from __future__ import annotations

import unittest

from eval.lib.inputs import check_against_stated

ANCHORS = {"L": 10.0, "M": 20.0, "H": 40.0, "A": 15.0, "B": 30.0}
DECK = "### Outputs\n\n- 底牌 L = **10** · M = **20** · H = **40** · 切点 A = **15** · B = **30**\n"


class StatedZoneTests(unittest.TestCase):
    def test_price_free_doc_skips_the_zone_check(self) -> None:
        self.assertEqual(check_against_stated(DECK, ANCHORS, None), [])
        # An eval not yet migrated still passes a zone; the doc has none to compare.
        self.assertEqual(check_against_stated(DECK, ANCHORS, "neutral"), [])

    def test_stated_zone_still_compared(self) -> None:
        md = DECK + "- 现价 **25** → zone = **`neutral`**\n"
        self.assertEqual(check_against_stated(md, ANCHORS, "neutral"), [])
        self.assertEqual(check_against_stated(md, ANCHORS, "optimistic"),
                         ["zone: stated neutral vs recomputed optimistic"])

    def test_stated_zone_without_a_computed_one_fails(self) -> None:
        # The eval dropped the price but the doc still carries a zone: the two halves disagree.
        md = DECK + "- 现价 **25** → zone = **`neutral`**\n"
        self.assertEqual(check_against_stated(md, ANCHORS, None),
                         ["zone: stated neutral but the eval computes none"])

    def test_anchors_still_checked(self) -> None:
        self.assertEqual(check_against_stated(DECK, {**ANCHORS, "M": 21.0}, None),
                         ["M: stated 20.0 vs recomputed 21.0000"])


if __name__ == "__main__":
    unittest.main()

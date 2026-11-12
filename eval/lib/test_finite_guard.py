"""Purely fictional inputs for common rule guards; no stock or axis code."""
import unittest

from eval.lib.gordon import check_g_cap, check_r


class FiniteRuleBoundaries(unittest.TestCase):
    def test_named_basket_rejects_nonfinite_and_boolean_discount(self):
        for value in (float('nan'), float('inf'), float('-inf'), True, False):
            with self.subTest(value=value):
                self.assertTrue(check_r(value, 'fictional discount', 4))

    def test_perpetual_growth_guard_rejects_nonfinite_and_boolean_inputs(self):
        for growth, discount in ((float('nan'), .1), (float('-inf'), .1),
                                 (.02, float('nan')), (.02, float('inf')),
                                 (True, .1), (.02, False)):
            with self.subTest(growth=growth, discount=discount):
                self.assertTrue(check_g_cap(growth, discount, 'fictional growth'))

    def test_valid_declining_growth_and_cap_boundary_remain_valid(self):
        self.assertEqual(check_r(.1, 'fictional discount', 4), [])
        self.assertEqual(check_g_cap(-.02, .1, 'fictional decline'), [])
        self.assertEqual(check_g_cap(.06, .1, 'fictional cap'), [])
        self.assertTrue(check_g_cap(.061, .1, 'fictional excess'))


if __name__ == '__main__':
    unittest.main()

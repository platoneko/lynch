import math
import unittest

from eval.lib.inputs import require_whole_years


class WholeYearGuardTest(unittest.TestCase):
    def test_accepts_integer_and_equivalent_integral_float(self):
        require_whole_years({'first': 2028, 'last': 2032.0}, ('first', 'last'))

    def test_rejects_invalid_values_before_int_conversion(self):
        for value in (2028.5, True, False, math.nan, math.inf, -math.inf, '2028', None):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, 'year'):
                require_whole_years({'year': value}, ('year',))

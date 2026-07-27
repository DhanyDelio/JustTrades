import unittest

from services.chart_analyzer import _fmt_price


class FmtPriceTests(unittest.TestCase):
    def test_precision_ranges(self):
        cases = [
            (60000, "60,000.00"),
            (1.95, "1.9500"),
            (0.33, "0.33000"),
            (0.0098, "0.009800"),
            (0.000003, "0.0000030000"),
        ]
        for value, expected in cases:
            with self.subTest(value=value):
                self.assertEqual(_fmt_price(value).strip(), expected)


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import unittest
from datetime import date, timedelta

from legal_agent_core.errors import DomainError
from legal_agent_core.jalali import (
    gregorian_to_jalali,
    jalali_to_gregorian,
    parse_jalali_date,
)

KNOWN_PAIRS = (
    ((1396, 9, 5), date(2017, 11, 26)),
    ((1399, 9, 25), date(2020, 12, 15)),
    ((1395, 8, 1), date(2016, 10, 22)),
    ((1399, 12, 20), date(2021, 3, 10)),
    ((1399, 12, 30), date(2021, 3, 20)),
    ((1400, 1, 14), date(2021, 4, 3)),
    ((1384, 6, 1), date(2005, 8, 23)),
    ((1403, 12, 30), date(2025, 3, 20)),
    ((1404, 1, 1), date(2025, 3, 21)),
)


class JalaliConversionTests(unittest.TestCase):
    def test_known_dates(self) -> None:
        for jalali, expected in KNOWN_PAIRS:
            with self.subTest(jalali=jalali):
                self.assertEqual(jalali_to_gregorian(*jalali), expected)

    def test_roundtrip_over_leap_boundaries(self) -> None:
        current = date(2020, 1, 1)
        end = date(2030, 1, 1)
        while current < end:
            jalali = gregorian_to_jalali(current)
            self.assertEqual(jalali_to_gregorian(*jalali), current)
            current += timedelta(days=1)

    def test_parse_accepts_persian_digits_and_separators(self) -> None:
        self.assertEqual(parse_jalali_date("۱۴۰۰/۰۱/۱۴"), date(2021, 4, 3))
        self.assertEqual(parse_jalali_date("1396-9-5"), date(2017, 11, 26))

    def test_invalid_inputs_raise_domain_error(self) -> None:
        with self.assertRaises(DomainError):
            jalali_to_gregorian(1400, 13, 1)
        with self.assertRaises(DomainError):
            jalali_to_gregorian(1400, 1, 0)
        with self.assertRaises(DomainError):
            parse_jalali_date("نه")


if __name__ == "__main__":
    unittest.main()

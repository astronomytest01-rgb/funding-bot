import unittest
from unittest.mock import patch

import longterm


class LongtermHistoryTests(unittest.TestCase):
    def leg(self, rows, exchange="xt"):
        with patch.object(longterm, "_market_meta", return_value=(None, None)):
            return longterm._leg_from_rows(exchange, "TEST", rows, 7)

    def test_reversed_and_shuffled_api_histories_have_same_latest_signal(self):
        # Old negative payouts must not hide the now-positive SHORT trend.
        rows = list(enumerate([-0.01] * 4 + [0.10] * 17))
        expected = self.leg(rows)
        for exchange in ("xt", "toobit", "coinw"):
            for data in (rows, rows[::-1], rows[::2] + rows[1::2]):
                with self.subTest(exchange=exchange, data=data):
                    leg = self.leg(data, exchange)
                    self.assertEqual(leg.rates, expected.rates)
                    self.assertEqual(leg.latest, 0.10)
                    self.assertEqual(longterm._paying_signal(leg), ("SHORT", "full"))

    def test_old_positive_payouts_cannot_mask_recent_reversal(self):
        rows = list(enumerate([0.10] * 17 + [-0.01] * 4))
        for data in (rows, rows[::-1]):
            self.assertIsNone(longterm._paying_signal(self.leg(data)))

    def test_recent_pair_income_uses_newest_six_payouts(self):
        payer = self.leg(list(enumerate([0.12] * 15 + [0.02] * 6)))
        hedge = self.leg(list(enumerate([0.03] * 21)), "binance")
        self.assertIsNotNone(longterm._paying_signal(payer))
        # Positive historical income, but latest six payouts lose money.
        self.assertIsNone(longterm._pair_candidate(
            "TEST", hedge, payer, paying_exchange="xt",
            paying_direction="SHORT", paying_category="partial",
        ))


if __name__ == "__main__":
    unittest.main()

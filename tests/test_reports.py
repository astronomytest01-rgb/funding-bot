import unittest
from unittest.mock import patch

import reports


class EveningReportPairTests(unittest.TestCase):
    def test_daily_income_filter_hides_low_or_negative_routes(self):
        signal = {"exchange": "phemex", "direction": "LONG"}
        fake = {
            "phemex": {
                "exchange": "phemex",
                "sym": ".ACEUSDTFR8H",
                "rates": (-0.01, -0.01, -0.01),
                "avg": -0.01,
                "std": 0.0,
                "payments_per_day": 3.0,
            },
            "kucoin": {
                "exchange": "kucoin",
                "sym": "ACEUSDTM",
                "rates": (-0.01, -0.01, -0.01),
                "avg": -0.01,
                "std": 0.0,
                "payments_per_day": 3.0,
            },
        }

        with patch.object(reports, "fetch_exchange_average", side_effect=lambda _coin, ex, _start, _end: fake[ex]), \
             patch.object(reports, "is_oi_allowed", return_value=True), \
             patch.object(reports, "is_volume_allowed", return_value=True):
            pair = reports.find_delta_pair_for_signal("ACE", signal, 1, ["phemex", "kucoin"])

        self.assertIsNone(pair)

    def test_pair_selection_normalizes_different_funding_intervals(self):
        signal = {"exchange": "phemex", "direction": "LONG"}
        fake = {
            "phemex": {
                "exchange": "phemex",
                "sym": ".COTIUSDTFR8H",
                "rates": (-0.3594, -0.3594, -0.3594),
                "avg": -0.3594,
                "std": 0.0,
                "payments_per_day": 3.0,
            },
            "kucoin": {
                "exchange": "kucoin",
                "sym": "COTIUSDTM",
                "rates": tuple([-0.047] * 24),
                "avg": -0.047,
                "std": 0.0,
                "payments_per_day": 24.0,
            },
        }

        with patch.object(reports, "AUTO_REPORT_MIN_DAILY_USD", 0), \
             patch.object(reports, "fetch_exchange_average", side_effect=lambda _coin, ex, _start, _end: fake[ex]), \
             patch.object(reports, "is_oi_allowed", return_value=True), \
             patch.object(reports, "is_volume_allowed", return_value=True):
            pair = reports.find_delta_pair_for_signal("COTI", signal, 1, ["phemex", "kucoin"])

        self.assertIsNotNone(pair)
        self.assertEqual(pair["long_ex"], "kucoin")
        self.assertEqual(pair["short_ex"], "phemex")
        self.assertAlmostEqual(pair["net_daily_pct"], 0.0498, places=4)


if __name__ == "__main__":
    unittest.main()

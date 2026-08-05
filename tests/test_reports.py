import unittest
from unittest.mock import patch

import reports


class EveningReportPairTests(unittest.TestCase):
    def test_best_book_prices_reads_phemex_orderbook_payload(self):
        quote = reports.best_book_prices({
            "result": {
                "orderbook_p": {
                    "bids": [["0.010034", "11959"]],
                    "asks": [["0.010062", "427"]],
                }
            }
        })

        self.assertEqual(quote, {"bid": 0.010034, "ask": 0.010062})

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
        quotes = {
            "phemex": {"bid": 100.0, "ask": 100.1},
            "kucoin": {"bid": 100.0, "ask": 100.1},
        }

        with patch.object(reports, "fetch_exchange_average", side_effect=lambda _coin, ex, _start, _end: fake[ex]), \
             patch.object(reports, "fetch_market_quote", side_effect=lambda _coin, ex: quotes[ex]), \
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
        quotes = {
            "phemex": {"bid": 100.0, "ask": 100.1},
            "kucoin": {"bid": 101.0, "ask": 101.1},
        }

        with patch.object(reports, "AUTO_REPORT_MIN_DAILY_USD", 0), \
             patch.object(reports, "AUTO_REPORT_MIN_ENTRY_SPREAD_PCT", -2.0), \
             patch.object(reports, "fetch_exchange_average", side_effect=lambda _coin, ex, _start, _end: fake[ex]), \
             patch.object(reports, "fetch_market_quote", side_effect=lambda _coin, ex: quotes[ex]), \
             patch.object(reports, "is_oi_allowed", return_value=True), \
             patch.object(reports, "is_volume_allowed", return_value=True):
            pair = reports.find_delta_pair_for_signal("COTI", signal, 1, ["phemex", "kucoin"])

        self.assertIsNotNone(pair)
        self.assertEqual(pair["long_ex"], "kucoin")
        self.assertEqual(pair["short_ex"], "phemex")
        self.assertAlmostEqual(pair["net_daily_pct"], 0.0498, places=4)
        self.assertAlmostEqual(pair["entry_spread_pct"], -1.088031651829882, places=6)

    def test_pair_selection_rejects_entry_spread_below_threshold(self):
        signal = {"exchange": "okx", "direction": "LONG"}
        fake = {
            "okx": {
                "exchange": "okx",
                "sym": "HOME-USDT-SWAP",
                "rates": (-0.2, -0.2, -0.2),
                "avg": -0.2,
                "std": 0.0,
                "payments_per_day": 3.0,
            },
            "phemex": {
                "exchange": "phemex",
                "sym": ".HOMEUSDTFR8H",
                "rates": (-0.1, -0.1, -0.1),
                "avg": -0.1,
                "std": 0.0,
                "payments_per_day": 3.0,
            },
            "kucoin": {
                "exchange": "kucoin",
                "sym": "HOMEUSDTM",
                "rates": (0.01, 0.01, 0.01),
                "avg": 0.01,
                "std": 0.0,
                "payments_per_day": 3.0,
            },
        }
        quotes = {
            "okx": {"bid": 99.9, "ask": 100.0},
            "phemex": {"bid": 98.5, "ask": 98.6},
            "kucoin": {"bid": 99.4, "ask": 99.5},
        }

        with patch.object(reports, "AUTO_REPORT_MIN_DAILY_USD", 0), \
             patch.object(reports, "AUTO_REPORT_MIN_ENTRY_SPREAD_PCT", -0.8), \
             patch.object(reports, "fetch_exchange_average", side_effect=lambda _coin, ex, _start, _end: fake[ex]), \
             patch.object(reports, "fetch_market_quote", side_effect=lambda _coin, ex: quotes[ex]), \
             patch.object(reports, "is_oi_allowed", return_value=True), \
             patch.object(reports, "is_volume_allowed", return_value=True):
            pair = reports.find_delta_pair_for_signal("HOME", signal, 1, ["okx", "phemex", "kucoin"])

        self.assertIsNotNone(pair)
        self.assertEqual(pair["long_ex"], "okx")
        self.assertEqual(pair["short_ex"], "kucoin")
        self.assertGreaterEqual(pair["entry_spread_pct"], -0.8)


if __name__ == "__main__":
    unittest.main()

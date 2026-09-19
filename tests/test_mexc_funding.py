import unittest
from unittest.mock import patch

import bot
import exchanges


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


class MexcFundingTests(unittest.TestCase):
    def test_symbol_normalization(self):
        self.assertEqual(exchanges.mexc_symbol_for_coin("btc"), "BTC_USDT")
        self.assertEqual(exchanges.mexc_symbol_for_coin("BTCUSDT"), "BTC_USDT")
        self.assertEqual(exchanges.mexc_symbol_for_coin("BTC_USD"), "BTC_USDT")

    def test_fetch_converts_rates_filters_period_and_paginates(self):
        pages = [
            {
                "success": True,
                "code": 0,
                "data": {
                    "totalPage": 2,
                    "resultList": [
                        {"settleTime": 3000, "fundingRate": 0.0001},
                        {"settleTime": 2000, "fundingRate": -0.0002},
                    ],
                },
            },
            {
                "success": True,
                "code": 0,
                "data": {
                    "totalPage": 2,
                    "resultList": [
                        {"settleTime": 1000, "fundingRate": 0.0003},
                    ],
                },
            },
        ]

        with patch.object(exchanges.requests, "get", side_effect=[FakeResponse(p) for p in pages]) as get:
            rows, symbol = exchanges.mexc_fetch("btc", 1500, 3000)

        self.assertEqual(symbol, "BTC_USDT")
        self.assertEqual(rows, [(2000, -0.02), (3000, 0.01)])
        self.assertEqual(get.call_count, 2)
        self.assertEqual(get.call_args_list[1].kwargs["params"]["page_num"], 2)

    def test_mexc_is_available_to_funding_but_not_global_scans(self):
        self.assertIn("mexc", bot.funding_exchange_keys())
        self.assertNotIn("mexc", bot.active_exchange_keys())
        coins, days, exchange = bot.parse_tokens("BTC mexc 7")
        self.assertEqual((coins, days, exchange), (["BTC"], 7, "mexc"))

        keyboard = bot.make_exchange_keyboard("fr", exchange_keys=bot.funding_exchange_keys())
        labels = [button.text for row in keyboard.inline_keyboard for button in row]
        self.assertIn("MEXC", labels)


if __name__ == "__main__":
    unittest.main()

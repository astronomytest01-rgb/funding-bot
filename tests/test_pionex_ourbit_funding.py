import unittest
from unittest.mock import patch

import bot
import exchanges


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self.payload


class PionexOurbitFundingTests(unittest.TestCase):
    def test_symbols_and_funding_only_routing(self):
        self.assertEqual(exchanges.pionex_symbol_for_coin("BTC_USDT_PERP"), "BTC_USDT_PERP")
        self.assertEqual(exchanges.pionex_symbol_for_coin("btcusdt"), "BTC_USDT_PERP")
        for exchange, label in (("pionex", "Pionex"), ("ourbit", "Ourbit")):
            with self.subTest(exchange=exchange):
                self.assertIn(exchange, bot.funding_exchange_keys())
                self.assertNotIn(exchange, bot.active_exchange_keys())
                self.assertEqual(bot.parse_tokens(f"BTC {exchange} 7"), (["BTC"], 7, exchange))
                keyboard = bot.make_exchange_keyboard("fr", exchange_keys=bot.funding_exchange_keys())
                self.assertIn(label, [button.text for row in keyboard.inline_keyboard for button in row])

    def test_ourbit_uses_v1_history(self):
        page = {"success": True, "code": 0, "data": {"totalPage": 1, "resultList": [
            {"settleTime": 2000, "fundingRate": "0.0001"},
            {"settleTime": 1000, "fundingRate": "0.0002"},
        ]}}
        with patch.object(exchanges.requests, "get", return_value=FakeResponse(page)) as get:
            rows, symbol = exchanges.ourbit_fetch("BTC", 1500, 2500)
        self.assertEqual((rows, symbol), ([(2000, 0.01)], "BTC_USDT"))
        self.assertEqual(get.call_args.args[0], "https://futures.ourbit.com/api/v1/contract/funding_rate/history")

    def test_pionex_paginates_filters_and_converts_percent(self):
        first = {"result": True, "data": {"rates": [
            {"fundingTime": ts, "fundingRate": "0.0001"} for ts in range(3000, 2500, -1)
        ]}}
        second = {"result": True, "data": {"rates": [
            {"fundingTime": 2400, "fundingRate": "-0.0002"},
            {"fundingTime": 1000, "fundingRate": "0.0003"},
        ]}}
        with patch.object(exchanges.requests, "get", side_effect=[FakeResponse(first), FakeResponse(second)]) as get:
            rows, symbol = exchanges.pionex_fetch("BTC", 2000, 3000)
        self.assertEqual(symbol, "BTC_USDT_PERP")
        self.assertEqual(len(rows), 501)
        self.assertEqual(rows[0], (2400, -0.02))
        self.assertEqual(rows[-1], (3000, 0.01))
        self.assertEqual(get.call_args_list[1].kwargs["params"]["endTime"], 2500)

    def test_pionex_rejects_error_response(self):
        with patch.object(exchanges.requests, "get", return_value=FakeResponse({"result": False, "message": "bad symbol"})):
            rows, error = exchanges.pionex_fetch("BAD", 1000, 2000)
        self.assertEqual(rows, [])
        self.assertIn("bad symbol", error)


if __name__ == "__main__":
    unittest.main()

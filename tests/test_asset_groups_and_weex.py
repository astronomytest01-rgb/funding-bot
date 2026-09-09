import unittest
from unittest.mock import Mock, patch

import bot
import exchanges
from asset_groups import asset_group, canonical_asset_symbol, group_items


class AssetGroupTests(unittest.TestCase):
    def test_stock_aliases_are_canonical_and_stay_out_of_crypto_groups(self):
        self.assertEqual(canonical_asset_symbol("GOOGLX_USDT"), "GOOGL")
        self.assertEqual(canonical_asset_symbol("NCSKGOOGL2USD-USDT"), "GOOGL")
        self.assertEqual(asset_group("GOOGL"), "tradfi")
        self.assertEqual(asset_group("GOOGLX"), "tradfi")
        self.assertEqual(asset_group("SPX"), "tradfi")

    def test_operator_tier_one_assets_are_explicit(self):
        for symbol in ("SUI", "DASH", "BCH", "GRAM"):
            self.assertEqual(asset_group(symbol), "tier1")
        self.assertEqual(asset_group("RANDOMMICROCAP"), "other")

    def test_group_items_preserves_mathematical_order_inside_each_section(self):
        rows = ["MEME123", "GOOGLX", "DASH", "AAPL", "SUI"]
        grouped = group_items(rows, lambda item: item)
        self.assertEqual(grouped["tradfi"], ["GOOGLX", "AAPL"])
        self.assertEqual(grouped["tier1"], ["DASH", "SUI"])
        self.assertEqual(grouped["other"], ["MEME123"])


class WeexFundingTests(unittest.TestCase):
    def test_weex_is_available_in_funding_exchange_presets(self):
        labels = [
            button.text
            for row in bot.make_exchange_keyboard("fr").inline_keyboard
            for button in row
        ]
        self.assertIn("WEEX", labels)

    @patch("exchanges.requests.get")
    def test_weex_funding_history_uses_v3_and_converts_rate_to_percent(self, get):
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = [
            {"symbol": "BTCUSDT", "fundingRate": "0.00025", "fundingTime": 2000},
            {"symbol": "BTCUSDT", "fundingRate": "0.00010", "fundingTime": 500},
        ]
        get.return_value = response

        rows, symbol = exchanges.weex_fetch("BTC", 1000, 3000)

        self.assertEqual(symbol, "BTCUSDT")
        self.assertEqual(rows, [(2000, 0.025)])
        self.assertIn("/capi/v3/market/fundingRate", get.call_args.args[0])

    @patch("exchanges.requests.get")
    def test_weex_symbol_list_uses_api_trading_symbols(self, get):
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = ["BTCUSDT", "GOOGLUSDT", "INVALIDUSD"]
        get.return_value = response

        self.assertEqual(exchanges.weex_get_all_symbols(), ["BTC", "GOOGL"])
        self.assertIn("/capi/v3/market/apiTradingSymbols", get.call_args.args[0])


if __name__ == "__main__":
    unittest.main()

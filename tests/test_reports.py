import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import reports


def report_pair(symbol):
    return {
        "coin": symbol,
        "direction": "LONG",
        "long_ex": "xt",
        "short_ex": "phemex",
        "long_avg": -0.1,
        "short_avg": 0.01,
        "long_payments_per_day": 3.0,
        "short_payments_per_day": 3.0,
        "net_rate": 0.33,
        "net_daily_pct": 0.33,
        "daily_income_usd": 66.0,
        "entry_spread_pct": -0.1,
    }


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

    def test_first_payload_reads_bybit_result_list(self):
        item = reports.first_payload({
            "retCode": 0,
            "result": {
                "list": [
                    {"bid1Price": "64103.7", "ask1Price": "64103.8"}
                ]
            }
        })

        self.assertEqual(item, {"bid1Price": "64103.7", "ask1Price": "64103.8"})

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
             patch.object(reports, "fetch_market_quote", side_effect=lambda _coin, ex: quotes[ex]):
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
             patch.object(reports, "fetch_market_quote", side_effect=lambda _coin, ex: quotes[ex]):
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
             patch.object(reports, "fetch_market_quote", side_effect=lambda _coin, ex: quotes[ex]):
            pair = reports.find_delta_pair_for_signal("HOME", signal, 1, ["okx", "phemex", "kucoin"])

        self.assertIsNotNone(pair)
        self.assertEqual(pair["long_ex"], "okx")
        self.assertEqual(pair["short_ex"], "kucoin")
        self.assertGreaterEqual(pair["entry_spread_pct"], -0.8)


class EveningReportTelegramTests(unittest.IsolatedAsyncioTestCase):
    async def test_grouped_pages_have_independent_load_more_buttons(self):
        pairs = [report_pair("GOOGLX"), report_pair("DASH")] + [
            report_pair(f"MEME{i}") for i in range(6)
        ]
        bot = Mock(send_message=AsyncMock())
        with (
            patch.dict(reports._evening_sessions, {}, clear=True),
            patch.object(reports, "EVENING_PAGE_SIZE", 5),
            patch.object(reports, "format_oi_status", return_value="OI"),
            patch.object(reports, "format_volume_status", return_value="VOL"),
        ):
            token = await reports.send_evening_pages(bot, 1, pairs)

        messages = [call.args[1] for call in bot.send_message.call_args_list]
        self.assertTrue(any("Фондовый рынок / TradFi — 1" in text for text in messages))
        self.assertTrue(any("Tier-1 — 1" in text for text in messages))
        other_call = next(
            call for call in bot.send_message.call_args_list
            if call.args[1].startswith("🧪 Остальные / щиткоины — 6")
        )
        button = other_call.kwargs["reply_markup"].inline_keyboard[0][0]
        self.assertEqual(button.text, "Показать ещё 1")
        self.assertEqual(button.callback_data, f"ev_more:{token}:other:5")

    async def test_evening_math_report_is_sent_before_gemini_starts(self):
        events = []
        pair = report_pair("DASH")
        context = SimpleNamespace(bot=Mock(send_message=AsyncMock()))

        async def send_math(*_args, **_kwargs):
            events.append("math")

        async def send_instructions(*_args, **_kwargs):
            events.append("instructions")

        async def send_gemini(*_args, **_kwargs):
            events.append("gemini")

        with (
            patch.object(reports, "get_active_exchanges", return_value=["xt"]),
            patch.object(reports, "get_scan_symbols_for_exchange", return_value=["DASH"]),
            patch.object(reports, "EXCHANGE_FETCHERS", {"xt": lambda *_args: ([(1, -0.1)] * 4, "DASH")}),
            patch.object(reports, "find_delta_pair_for_signal", return_value=pair),
            patch.object(reports, "send_evening_pages", side_effect=send_math),
            patch.object(reports, "send_entry_instructions", side_effect=send_instructions),
            patch.object(reports, "send_evening_gemini_recommendations", side_effect=send_gemini),
            patch.object(reports.time, "sleep"),
        ):
            await reports.run_evening_report(context, 1)

        self.assertEqual(events, ["math", "instructions", "gemini"])


if __name__ == "__main__":
    unittest.main()

import unittest
from unittest.mock import Mock, patch

import longterm


def _rows(rate, count=21):
    return [(index, rate) for index in range(count)]


class LongTermPayingExchangeTests(unittest.TestCase):
    def test_hedge_only_exchanges_never_discover_payer_but_remain_hedges(self):
        xt_data = {
            "PAYLONG": _rows(-0.10),
            "PAYSHORT": _rows(0.10),
            "NOALLOW": _rows(0.0),
        }
        binance_data = {
            "PAYLONG": _rows(0.01),
            "PAYSHORT": _rows(-0.01),
            "NOALLOW": _rows(0.10),
            "EXCLUDEDONLY": _rows(0.10),
        }
        fetched = []

        def fetcher(data, exchange):
            def fetch(symbol, start_ms, now_ms):
                fetched.append((exchange, symbol))
                return data.get(symbol, []), symbol

            return fetch

        fetchers = {
            "xt": fetcher(xt_data, "xt"),
            "binance": fetcher(binance_data, "binance"),
        }
        symbol_fetchers = {
            "xt": lambda: ["PAYLONG", "PAYSHORT", "NOALLOW"],
            "binance": lambda: ["PAYLONG", "PAYSHORT", "NOALLOW", "EXCLUDEDONLY"],
        }

        with (
            patch.object(longterm, "EXCHANGE_FETCHERS", fetchers),
            patch.object(longterm, "EXCHANGE_SYMBOL_FETCHERS", symbol_fetchers),
            patch.object(longterm, "LONGTERM_EXAMPLE_SYMBOLS", ()),
            patch.object(longterm, "_fetch_cg_market", return_value={}),
            patch.object(longterm, "_market_meta", return_value=(None, None)),
            patch.object(longterm.time, "sleep"),
        ):
            result = longterm.scan_longterm_funding(
                days=7,
                active_exchanges=["xt", "binance"],
            )

        groups = dict(result["groups"])
        self.assertEqual(set(groups), {"PAYLONG", "PAYSHORT"})
        self.assertEqual(result["paying_exchanges"], ["xt"])
        self.assertEqual(result["hedge_only_exchanges"], ["binance"])
        self.assertNotIn(("binance", "EXCLUDEDONLY"), fetched)
        self.assertTrue(
            all(
                pair.paying_exchange not in longterm.LONGTERM_HEDGE_ONLY_EXCHANGES
                for pairs in groups.values()
                for pair in pairs
            )
        )

        long_pair = groups["PAYLONG"][0]
        self.assertEqual(long_pair.paying_exchange, "xt")
        self.assertEqual(long_pair.paying_direction, "LONG")
        self.assertEqual(long_pair.short.exchange, "binance")

        short_pair = groups["PAYSHORT"][0]
        self.assertEqual(short_pair.paying_exchange, "xt")
        self.assertEqual(short_pair.paying_direction, "SHORT")
        self.assertEqual(short_pair.long.exchange, "binance")

    def test_telegram_text_uses_compact_report_layout(self):
        leg = longterm.LongTermLeg("xt", (-0.1,) * 21, -0.1, 0.0, -0.1, 3.0, 2_000_000, 3_000_000)
        hedge = longterm.LongTermLeg("binance", (0.01,) * 21, 0.01, 0.0, 0.01, 3.0, 500_000, 600_000)
        pair = longterm._pair_candidate(
            "PAYLONG",
            leg,
            hedge,
            paying_exchange="xt",
            paying_direction="LONG",
            paying_category="full",
        )
        pair = longterm.replace(pair, entry_spread_pct=-0.128)

        text = longterm.format_longterm_coin("PAYLONG", [pair])

        self.assertIn("🟢 Лонг:", text)
        self.assertIn("🔴 Шорт:", text)
        self.assertIn("выплат/день", text)
        self.assertIn("✅ OI $2,000,000 | $15k = 0.75%", text)
        self.assertIn("✅ Vol24h $3,000,000", text)
        self.assertIn("Чистый фандинг: `+0.3300%` / день", text)
        self.assertIn("`$26.40`", text)
        self.assertIn("`$8,000`", text)
        self.assertIn("`$66.00`", text)
        self.assertIn("`$20,000`", text)
        self.assertIn("Спред входа: `-0.128%`", text)
        self.assertNotIn("Платящая", text)
        self.assertNotIn("FULL", text)

    def test_okx_is_hedge_only_for_longterm(self):
        self.assertIn("okx", longterm.LONGTERM_HEDGE_ONLY_EXCHANGES)

        paying_rows = {"PAYLONG": _rows(-0.10)}
        okx_rows = {"PAYLONG": _rows(0.01), "OKXONLY": _rows(0.10)}

        def fetcher(data):
            return lambda symbol, start_ms, now_ms: (data.get(symbol, []), symbol)

        with (
            patch.object(longterm, "EXCHANGE_FETCHERS", {
                "xt": fetcher(paying_rows),
                "okx": fetcher(okx_rows),
            }),
            patch.object(longterm, "EXCHANGE_SYMBOL_FETCHERS", {
                "xt": lambda: ["PAYLONG"],
                "okx": lambda: ["PAYLONG", "OKXONLY"],
            }),
            patch.object(longterm, "LONGTERM_EXAMPLE_SYMBOLS", ()),
            patch.object(longterm, "_fetch_cg_market", return_value={}),
            patch.object(longterm, "_market_meta", return_value=(None, None)),
            patch.object(longterm.time, "sleep"),
        ):
            result = longterm.scan_longterm_funding(days=7, active_exchanges=["xt", "okx"])

        groups = dict(result["groups"])
        self.assertEqual(set(groups), {"PAYLONG"})
        self.assertEqual(result["paying_exchanges"], ["xt"])
        self.assertEqual(result["hedge_only_exchanges"], ["okx"])
        self.assertEqual(groups["PAYLONG"][0].short.exchange, "okx")

    def test_entry_spread_enrichment_is_display_only(self):
        long_leg = longterm.LongTermLeg("xt", (-0.1,) * 21, -0.1, 0.0, -0.1, 3.0, None, None)
        short_leg = longterm.LongTermLeg("okx", (0.01,) * 21, 0.01, 0.0, 0.01, 3.0, None, None)
        pair = longterm._pair_candidate(
            "PAYLONG",
            long_leg,
            short_leg,
            paying_exchange="xt",
            paying_direction="LONG",
            paying_category="full",
        )

        quotes = {
            ("PAYLONG", "xt"): {"bid": 99.0, "ask": 100.0},
            ("PAYLONG", "okx"): {"bid": 99.5, "ask": 100.5},
        }
        with patch.object(longterm, "_cached_market_quote", side_effect=lambda symbol, exchange: quotes[(symbol, exchange)]):
            enriched = longterm._enrich_entry_spreads([("PAYLONG", [pair])])

        enriched_pair = enriched[0][1][0]
        self.assertAlmostEqual(enriched_pair.entry_spread_pct, -0.5)
        self.assertIsNone(pair.entry_spread_pct)
        self.assertEqual(enriched_pair.paying_exchange, pair.paying_exchange)
        self.assertEqual(enriched_pair.daily_usd, pair.daily_usd)

    def test_scan_stops_when_only_hedge_only_exchanges_are_active(self):
        fetcher = Mock()
        with (
            patch.object(longterm, "EXCHANGE_FETCHERS", {"binance": fetcher}),
            patch.object(longterm, "_fetch_cg_market") as market_fetch,
        ):
            result = longterm.scan_longterm_funding(days=7, active_exchanges=["binance"])

        self.assertEqual(result["groups"], [])
        self.assertEqual(result["symbols_scanned"], 0)
        fetcher.assert_not_called()
        market_fetch.assert_not_called()


class LongTermTelegramReminderTests(unittest.IsolatedAsyncioTestCase):
    async def test_start_message_lists_every_hedge_only_exchange(self):
        class Bot:
            def __init__(self):
                self.messages = []

            async def send_message(self, chat_id, text, **kwargs):
                self.messages.append(text)

        empty_result = {
            "days": 7,
            "symbols_scanned": 0,
            "pairs_found": 0,
            "groups": [],
            "groups_total": 0,
            "errors": {},
            "active_exchanges": [],
            "paying_exchanges": [],
            "hedge_only_exchanges": [],
        }
        bot = Bot()
        with patch.object(longterm, "scan_longterm_funding", return_value=empty_result):
            await longterm.send_longterm_report(bot, 1, manual=True)

        reminder = bot.messages[0].lower()
        for exchange in ("kucoin", "gate", "mexc", "binance", "bybit", "okx"):
            label = longterm.EXCHANGE_LABELS.get(exchange, exchange).lower()
            self.assertIn(label, reminder)
        self.assertIn("не ищутся как платящая нога", reminder)
        self.assertIn("остаются доступны как хедж", reminder)


if __name__ == "__main__":
    unittest.main()

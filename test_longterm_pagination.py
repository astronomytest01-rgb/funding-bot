import unittest
from unittest.mock import AsyncMock, Mock, patch

import longterm


def pair_for(symbol):
    long_leg = longterm.LongTermLeg("xt", (-0.1,) * 21, -0.1, 0, -0.1, 3, None, None)
    short_leg = longterm.LongTermLeg("okx", (0.01,) * 21, 0.01, 0, 0.01, 3, None, None)
    return longterm._pair_candidate(
        symbol, long_leg, short_leg,
        paying_exchange="xt", paying_direction="LONG", paying_category="full",
    )


class LongTermDisplayTests(unittest.TestCase):
    def test_only_first_ranked_route_is_displayed(self):
        best = pair_for("TEST")
        alternative = longterm.replace(best, short=longterm.replace(best.short, exchange="binance"))
        text = longterm.format_longterm_coin("TEST", [best, alternative])
        self.assertEqual(text.count("🟢 Лонг:"), 1)
        self.assertEqual(text.count("🔴 Шорт:"), 1)
        self.assertIn("OKX", text)
        self.assertNotIn("Binance", text)
        self.assertNotIn("Вариантов", text)
        self.assertNotIn("#1", text)
        self.assertIn("$8,000", text)
        self.assertIn("$20,000", text)
        self.assertNotIn("Gemini", text)

    def test_quotes_only_for_visible_route_and_missing_quote_keeps_coin(self):
        best = pair_for("TEST")
        alternative = longterm.replace(best, short=longterm.replace(best.short, exchange="binance"))
        with patch.object(longterm, "_cached_market_quote", side_effect=RuntimeError("offline")) as fetch:
            groups = longterm._enrich_entry_spreads([("TEST", [best, alternative])])
        self.assertEqual({call.args for call in fetch.call_args_list}, {("TEST", "xt"), ("TEST", "okx")})
        self.assertEqual(groups, [("TEST", [best, alternative])])
        self.assertIn("Спред входа: `нет данных`", longterm.format_longterm_coin(*groups[0]))

    def test_summary_distinguishes_routes_from_coins_after_gemini(self):
        result = dict(
            active_exchanges=["xt", "okx"], days=7, symbols_scanned=180,
            pairs_found=100, groups_total=20, groups_before_gemini=20,
            groups=[("TEST", [pair_for("TEST")])] * 3, errors={},
            gemini_recommended_symbols=["TEST"], gemini_reviewed_symbols=["TEST", "TWO", "THREE"],
        )
        text = longterm.format_longterm_summary(result)
        self.assertIn("Маршрутов до Gemini (включая альтернативы): `100`", text)
        self.assertIn("Gemini рекомендует: `1` из `3`", text)
        self.assertIn("Доступно к показу: `3` монет", text)

    def test_gemini_is_separate_and_preserves_mathematical_order(self):
        groups = [(symbol, [pair_for(symbol)]) for symbol in ("LOW", "KEEPER", "OTHER")]
        result = {"groups": groups}
        with (
            patch.object(longterm, "GEMINI_API_KEY", "test"),
            patch.object(longterm, "LONGTERM_GEMINI_FILTER", True),
            patch.object(longterm, "gemini_generate", return_value="KEEP KEEPER — quality"),
            patch.object(longterm.time, "sleep"),
        ):
            reviewed, note = longterm.apply_longterm_gemini_filter(result)

        self.assertEqual([symbol for symbol, _pairs in reviewed["groups"]], ["LOW", "KEEPER", "OTHER"])
        self.assertEqual(len(reviewed["groups"]), 3)
        self.assertFalse(reviewed["groups"][0][1][0].ai_recommended)
        self.assertTrue(reviewed["groups"][1][1][0].ai_recommended)
        recommendation = "\n".join(longterm.format_longterm_gemini_recommendations(reviewed))
        self.assertIn("KEEPER — quality", recommendation)
        self.assertNotIn("• LOW", recommendation)
        self.assertNotIn("Gemini", longterm.format_longterm_coin(*reviewed["groups"][1]))
        self.assertIn("Все 3 математических кандидатов сохранены", note)


class LongTermPaginationTests(unittest.IsolatedAsyncioTestCase):
    async def test_auto_report_sends_math_page_before_separate_gemini_block(self):
        groups = [(symbol, [pair_for(symbol)]) for symbol in ("LOW", "KEEPER")]
        scan_result = {"groups": groups}

        async def send_math_page(bot, chat_id, token, offset):
            events.append("math")
            await bot.send_message(chat_id, "MATH PAGE")

        events = []

        def apply_review(result):
            events.append("gemini")
            return reviewed, "internal note"

        bot = Mock(send_message=AsyncMock())
        with (
            patch.object(longterm, "GEMINI_API_KEY", "test"),
            patch.object(longterm, "LONGTERM_GEMINI_FILTER", True),
            patch.object(longterm, "scan_longterm_funding", return_value=scan_result),
            patch.object(longterm, "apply_longterm_gemini_filter") as apply_gemini,
            patch.object(longterm, "format_longterm_summary", return_value="SUMMARY"),
            patch.object(longterm, "_save_session", return_value="token"),
            patch.object(longterm, "send_longterm_page", side_effect=send_math_page),
        ):
            reviewed = dict(scan_result)
            reviewed.update(
                gemini_reviewed_symbols=["LOW", "KEEPER"],
                gemini_recommended_symbols=["KEEPER"],
                gemini_recommendation_rows=["• KEEPER — quality"],
                gemini_errors=[],
            )
            apply_gemini.side_effect = apply_review
            await longterm.send_longterm_report(bot, 1, manual=False)

        messages = [call.args[1] for call in bot.send_message.call_args_list]
        self.assertLess(messages.index("SUMMARY"), messages.index("MATH PAGE"))
        gemini_index = next(i for i, message in enumerate(messages) if message.startswith("🤖 Gemini —"))
        self.assertLess(messages.index("MATH PAGE"), gemini_index)
        self.assertNotIn("internal note", messages)
        self.assertEqual(events, ["math", "gemini"])

    async def test_twelve_coins_paginate_without_rescan_or_duplicates(self):
        groups = [(f"COIN{i}", [pair_for(f"COIN{i}")]) for i in range(12)]
        bot = Mock(send_message=AsyncMock())
        with (
            patch.dict(longterm._longterm_sessions, {}, clear=True),
            patch.object(longterm, "LONGTERM_PAGE_SIZE", 5),
            patch.object(longterm, "_enrich_entry_spreads", side_effect=lambda page: page),
            patch.object(longterm, "scan_longterm_funding") as scan,
        ):
            token = longterm._save_session({"groups": groups})
            await longterm.send_longterm_page(bot, 1, token, 0)
            first_button = bot.send_message.call_args.kwargs["reply_markup"].inline_keyboard[0][0]
            self.assertEqual(first_button.callback_data, f"lt_more:{token}:other:5")

            query = Mock(data=first_button.callback_data, answer=AsyncMock(), message=Mock(chat_id=1))
            await longterm.longterm_more_callback(Mock(callback_query=query), Mock(bot=bot))
            second_button = bot.send_message.call_args.kwargs["reply_markup"].inline_keyboard[0][0]
            self.assertEqual(second_button.callback_data, f"lt_more:{token}:other:10")
            self.assertEqual(second_button.text, "Показать ещё 2")

            query.data = second_button.callback_data
            await longterm.longterm_more_callback(Mock(callback_query=query), Mock(bot=bot))
            self.assertNotIn("reply_markup", bot.send_message.call_args.kwargs)
            self.assertIn("Показано 12 из 12", bot.send_message.call_args.args[1])
            coin_messages = [c.args[1].splitlines()[0] for c in bot.send_message.call_args_list if c.args[1].startswith("🧲")]
            self.assertEqual(coin_messages, [f"🧲 *COIN{i}*" for i in range(12)])
            scan.assert_not_called()

    async def test_one_page_explicitly_reports_no_more_results(self):
        bot = Mock(send_message=AsyncMock())
        with (
            patch.dict(longterm._longterm_sessions, {}, clear=True),
            patch.object(longterm, "LONGTERM_PAGE_SIZE", 5),
            patch.object(longterm, "_enrich_entry_spreads", side_effect=lambda page: page),
        ):
            for count in (1, 5):
                token = longterm._save_session({"groups": [(str(i), [pair_for(str(i))]) for i in range(count)]})
                await longterm.send_longterm_page(bot, 1, token, 0)
                self.assertNotIn("reply_markup", bot.send_message.call_args.kwargs)
                self.assertIn("следующей страницы нет", bot.send_message.call_args.args[1])


if __name__ == "__main__":
    unittest.main()

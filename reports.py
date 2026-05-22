import time

import requests
from telegram.ext import ContextTypes

from ai import gemini_analyze_bulk
from analysis import analyze_rates, calc_std, get_active_exchanges, recent_trend_ok
from config import AUTO_SCAN_AMOUNT, AUTO_SCAN_DAYS, GEMINI_API_KEY, REPORT_CHAT_ID
from exchanges import EXCHANGE_FETCHERS, EXCHANGE_LABELS, EXCHANGE_SYMBOL_FETCHERS, phemex_get_all_symbols
from oi import format_oi_status, is_oi_allowed

ENTRY_INSTRUCTIONS = """🛡️ *Главная задача — не потерять депозит.* Заработок вторичен.

📊 *Фильтры и анализ:*

• 🐋 *Open Interest (OI):* В боте монеты с OI ниже *$500,000* скрываются автоматически. От *$500,000* до *$1,000,000* — только ⚠️ ручная осторожность. Для входа приоритет — *$1,000,000+* на конкретной бирже. Зайдя на свои *$15,000* в слабый OI, ты сам проломишь пустой стакан (как было с FLOW на Toobit), оторвешь цену фьючерса от спота и алгоритм может влепить штрафной отрицательный фандинг. Твой ордер не должен превышать *1–1.5%* от всего OI.
• *Прибыльность:* Фандинг стабильно *0.03% – 0.08%* (если меньше — невыгодно).
• *Спред входа:* Строго *до -0.5%*.
• *Ликвидность:* Смотреть суточный объем и сумму первых 5 ордеров в стакане.
• *arcways.io:* Проверить slippage под свой объем и как долго держится фандинг.
• Обязательно проверять *Predicted Rate* (прогноз следующей ставки).

⏳ *Набор позиции (TWAP):*

• Никаких входов всем объемом. Дробить по *$500–$1000* раз в час.

🪤 *Защита от ловушек алгоритмов:*

• Залил 50% объема → проверил ставку с ПК и телефона (обновил пару раз) → переждал 1-2 часа → долил остаток.
• *Избегай ловушек пустого стакана:* Это касается как неликвидных альткоинов (FLOW на Toobit), так и токенизированных акций (GOOGL на KuCoin вне торговой сессии). Если бросить рыночный ордер в пустой стакан, цена фьючерса (Mark Price) резко оторвется от спотового индекса (Index Price). Чтобы вернуть баланс, алгоритм биржи алгоритмически выкрутит тебе огромный штрафной фандинг (например, -0.3%).
• Часто пустоту стакана можно понять только *после* того, как зашел и сломал ставку об себя же. Поэтому до входа жестко проверяй стакан, спред и глубину — твой объем не должен размазать цену и спровоцировать штраф биржи.

🕸️ *Защитная сетка (SL/TP):*

• Ставить 4–10 ордеров в диапазоне *60% – 72% изменения позиции* (на Binance максимум 4).
• Указывать триггеры по *% изменения позиции* (как на Phemex) или *% цены монеты*, а не по фиксированным ценам.

🤝 *Верификация:*

• Выбор плеча и монеты предварительно обсуждать с Gemini.

⚠️ *Акции (Осторожно!):* На выходных не торгуются. При открытии рынка в понедельник цена может резко прыгнуть на *20-40%* из-за новостей — это риск мгновенной ликвидации."""


async def send_entry_instructions(context, chat_id):
    for chunk in [ENTRY_INSTRUCTIONS[i:i+4000] for i in range(0, len(ENTRY_INSTRUCTIONS), 4000)]:
        await context.bot.send_message(chat_id, chunk, parse_mode="Markdown")


def get_scan_symbols_for_exchange(exchange):
    """Список монет для скана: CoinW/KuCoin/Bitunix берём из их API, остальные — из Phemex universe."""
    if exchange == "coinw":
        r = requests.get("https://api.coinw.com/v1/perpum/instruments", timeout=8)
        r.raise_for_status()
        return sorted({x["base"].upper() for x in r.json().get("data", []) if x.get("base")})
    symbol_fetcher = EXCHANGE_SYMBOL_FETCHERS.get(exchange)
    return symbol_fetcher() if symbol_fetcher else phemex_get_all_symbols()


def fetch_exchange_average(coin, exchange, start_ms, end_ms):
    fetcher = EXCHANGE_FETCHERS.get(exchange)
    if not fetcher:
        return None
    data, sym = fetcher(coin, start_ms, end_ms)
    if not data:
        return None
    rates = [r for _, r in data]
    return {
        "exchange": exchange,
        "sym": sym,
        "rates": rates,
        "avg": sum(rates) / len(rates),
        "std": calc_std(rates),
    }


def find_delta_pair_for_signal(coin, signal, days, active_exchanges):
    """Подбирает пару для монеты, которая уже прошла full-фильтр LONG или SHORT."""
    now_ms = int(time.time() * 1000)
    start_ms = now_ms - days * 24 * 60 * 60 * 1000
    exchange_data = {}
    for ex in active_exchanges:
        try:
            info = fetch_exchange_average(coin, ex, start_ms, now_ms)
            if info:
                exchange_data[ex] = info
        except Exception:
            pass
        time.sleep(0.1)

    signal_ex = signal["exchange"]
    if signal_ex not in exchange_data:
        return None

    main_avg = exchange_data[signal_ex]["avg"]
    candidates = []
    if signal["direction"] == "LONG":
        long_ex = signal_ex
        long_avg = main_avg
        for short_ex, info in exchange_data.items():
            if short_ex == long_ex:
                continue
            short_avg = info["avg"]
            if not is_oi_allowed(long_ex, coin) or not is_oi_allowed(short_ex, coin):
                continue
            net = -long_avg + short_avg
            candidates.append((net, long_ex, short_ex, long_avg, short_avg, info["std"]))
    else:
        short_ex = signal_ex
        short_avg = main_avg
        for long_ex, info in exchange_data.items():
            if long_ex == short_ex:
                continue
            long_avg = info["avg"]
            if not is_oi_allowed(long_ex, coin) or not is_oi_allowed(short_ex, coin):
                continue
            net = -long_avg + short_avg
            candidates.append((net, long_ex, short_ex, long_avg, short_avg, info["std"]))

    if not candidates:
        return None
    candidates.sort(key=lambda x: (-x[0], x[5]))
    net, long_ex, short_ex, long_avg, short_avg, _ = candidates[0]
    return {
        "coin": coin,
        "direction": signal["direction"],
        "long_ex": long_ex,
        "short_ex": short_ex,
        "long_avg": long_avg,
        "short_avg": short_avg,
        "net_rate": net,
    }


async def run_evening_report(context: ContextTypes.DEFAULT_TYPE, chat_id: int, manual=False):
    """Вечерний отчёт: full-фильтр → Gemini-рекомендация → лучшая delta-neutral пара."""
    active = get_active_exchanges()
    if not active:
        await context.bot.send_message(chat_id, "❌ Вечерний отчёт: нет активных бирж.")
        return

    await context.bot.send_message(
        chat_id,
        ("🕗 *Вечерний авто-скан запущен*\n" if not manual else "🕗 *Вечерний отчёт запущен вручную*\n")
        + "Ищу FULL-подходящие монеты, подбираю пару, а Gemini показываю как рекомендацию.",
        parse_mode="Markdown",
    )

    now_ms = int(time.time() * 1000)
    start_ms = now_ms - AUTO_SCAN_DAYS * 24 * 60 * 60 * 1000
    full_signals = []

    for ex in active:
        fetcher = EXCHANGE_FETCHERS.get(ex)
        if not fetcher:
            continue
        try:
            coins = get_scan_symbols_for_exchange(ex)
        except Exception as e:
            await context.bot.send_message(chat_id, f"⚠️ {EXCHANGE_LABELS.get(ex, ex)}: не получил список монет: {e}")
            continue

        found_before = len(full_signals)
        await context.bot.send_message(chat_id, f"🔎 {EXCHANGE_LABELS.get(ex, ex)}: начинаю скан {len(coins)} монет...")
        for idx, coin in enumerate(coins, start=1):
            try:
                rows, _sym = fetcher(coin, start_ms, now_ms)
            except Exception:
                rows = []
            if idx % 50 == 0:
                await context.bot.send_message(chat_id, f"⏳ {EXCHANGE_LABELS.get(ex, ex)}: {idx}/{len(coins)} | FULL {len(full_signals) - found_before}")
            if not rows:
                time.sleep(0.1)
                continue
            ordered_rates = [rate for _, rate in sorted(rows, key=lambda x: x[0])]
            metrics = analyze_rates(ordered_rates)
            if metrics and metrics["category"] == "full" and recent_trend_ok(ordered_rates, metrics["direction"]):
                if not is_oi_allowed(ex, coin):
                    time.sleep(0.1)
                    continue
                key_avg = metrics["neg_avg"] if metrics["direction"] == "LONG" else metrics["pos_avg"]
                full_signals.append({
                    "coin": coin,
                    "exchange": ex,
                    "direction": metrics["direction"],
                    "avg": metrics["avg"],
                    "key_avg": key_avg,
                    "outlier_pct": metrics["outlier_pct"],
                })
            time.sleep(0.1)

        await context.bot.send_message(
            chat_id,
            f"⏳ {EXCHANGE_LABELS.get(ex, ex)}: найдено FULL {len(full_signals) - found_before}, всего {len(full_signals)}"
        )

    if not full_signals:
        await context.bot.send_message(chat_id, "✅ Вечерний авто-скан завершён: FULL-подходящих монет нет.")
        await send_entry_instructions(context, chat_id)
        return

    best_signal_by_coin = {}
    for signal in full_signals:
        coin = signal["coin"]
        prev = best_signal_by_coin.get(coin)
        if not prev or abs(signal["key_avg"]) > abs(prev["key_avg"]):
            best_signal_by_coin[coin] = signal

    report_coins = list(best_signal_by_coin.keys())
    gemini_chunks = []
    if GEMINI_API_KEY:
        await context.bot.send_message(chat_id, f"🤖 Gemini готовит рекомендацию по {len(report_coins)} монетам, но не удаляет их из отчёта...")
        signals = list(best_signal_by_coin.values())
        for i in range(0, len(signals), 15):
            part = signals[i:i+15]
            text_rows = []
            for s in part:
                direction = "ЛОНГ" if s["direction"] == "LONG" else "ШОРТ"
                text_rows.append(
                    f"- {s['coin']} ({direction}, {EXCHANGE_LABELS.get(s['exchange'], s['exchange'])}, avg {s['avg']:+.4f}%)"
                )
            answer = gemini_analyze_bulk("\n".join(text_rows), AUTO_SCAN_DAYS)
            if answer:
                gemini_chunks.append(answer)
            time.sleep(3)
    else:
        await context.bot.send_message(chat_id, "⚠️ GEMINI_API_KEY не задан: блок рекомендации Gemini пропущен.")

    pairs = []
    for coin in report_coins:
        pair = find_delta_pair_for_signal(coin, best_signal_by_coin[coin], AUTO_SCAN_DAYS, active)
        if pair:
            pairs.append(pair)

    if not pairs:
        await context.bot.send_message(chat_id, "⚠️ Не нашёл дельта-нейтральных пар по FULL-монетам.")
        if gemini_chunks:
            gemini_report = "🤖 *Gemini-рекомендация* — не фильтр, а ручная проверка:\n\n" + "\n".join(gemini_chunks)
            for chunk in [gemini_report[i:i+4000] for i in range(0, len(gemini_report), 4000)]:
                await context.bot.send_message(chat_id, chunk, parse_mode="Markdown")
        await send_entry_instructions(context, chat_id)
        return

    lines = [
        f"🤖 *ВЕЧЕРНИЙ ОТЧЁТ* — база {AUTO_SCAN_DAYS} дня, сумма ${AUTO_SCAN_AMOUNT:,.0f}\n"
    ]
    for p in sorted(pairs, key=lambda x: x["net_rate"], reverse=True)[:20]:
        long_label = EXCHANGE_LABELS.get(p["long_ex"], p["long_ex"])
        short_label = EXCHANGE_LABELS.get(p["short_ex"], p["short_ex"])
        approx_income = AUTO_SCAN_AMOUNT * (p["net_rate"] / 100) * 3
        long_oi = format_oi_status(p["long_ex"], p["coin"])
        short_oi = format_oi_status(p["short_ex"], p["coin"])
        lines.append(
            f"*{p['coin']}*\n"
            f"  🟢 Лонг: `{long_label}` avg `{p['long_avg']:+.4f}%` | {long_oi}\n"
            f"  🔴 Шорт: `{short_label}` avg `{p['short_avg']:+.4f}%` | {short_oi}\n"
            f"  📈 Чистый фандинг: `{p['net_rate']:+.4f}%` / ставку\n"
            f"  💰 Оценка: `${approx_income:.2f}` за 1 день\n"
        )

    report = "\n".join(lines)
    for chunk in [report[i:i+4000] for i in range(0, len(report), 4000)]:
        await context.bot.send_message(chat_id, chunk, parse_mode="Markdown")


    if gemini_chunks:
        gemini_report = "🤖 *Gemini-рекомендация* — не фильтр, а ручная проверка:\n\n" + "\n".join(gemini_chunks)
        for chunk in [gemini_report[i:i+4000] for i in range(0, len(gemini_report), 4000)]:
            await context.bot.send_message(chat_id, chunk, parse_mode="Markdown")

    await send_entry_instructions(context, chat_id)


async def auto_scan_job(context: ContextTypes.DEFAULT_TYPE):
    if not REPORT_CHAT_ID:
        return
    await run_evening_report(context, int(REPORT_CHAT_ID))


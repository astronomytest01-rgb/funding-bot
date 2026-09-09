import asyncio
import os
import secrets
import time

import requests
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from ai import gemini_analyze_bulk, get_last_gemini_error
from analysis import analyze_rates, calc_std, get_active_exchanges, recent_trend_ok
from asset_groups import GROUP_LABELS, GROUP_ORDER, group_items
from config import AUTO_REPORT_MIN_DAILY_USD, AUTO_REPORT_MIN_ENTRY_SPREAD_PCT, AUTO_SCAN_AMOUNT, AUTO_SCAN_DAYS, GEMINI_API_KEY, REPORT_CHAT_ID, TEMPORARILY_DISABLED_EXCHANGES
from exchanges import EXCHANGE_FETCHERS, EXCHANGE_LABELS, EXCHANGE_SYMBOL_FETCHERS, _proxies, phemex_get_all_symbols
from oi import format_oi_status, format_volume_status, is_oi_allowed, is_volume_allowed


EVENING_PAGE_SIZE = int(os.getenv("EVENING_PAGE_SIZE", "5"))
EVENING_GEMINI_TIMEOUT_SECONDS = int(os.getenv("EVENING_GEMINI_TIMEOUT_SECONDS", "90"))
_evening_sessions = {}
_EVENING_SESSION_TTL = 2 * 60 * 60


def temporary_disabled_text():
    labels = [EXCHANGE_LABELS.get(e, e.upper()) for e in TEMPORARILY_DISABLED_EXCHANGES]
    return ", ".join(sorted(labels))


async def gemini_report_recommendation(text_rows, days, chunk_index, total_chunks):
    """Run Gemini outside the Telegram event loop with a hard report timeout."""
    prompt_text = "\n".join(text_rows)
    try:
        return await asyncio.wait_for(
            asyncio.to_thread(gemini_analyze_bulk, prompt_text, days, 20),
            timeout=75,
        )
    except asyncio.TimeoutError:
        return None
    except Exception:
        return None

ENTRY_INSTRUCTIONS = """🛡️ *Главная задача — не потерять депозит.* Заработок вторичен.

📊 *Фильтры и анализ:*

• 🐋 *Open Interest (OI):* В ручном `/analyze` монеты с OI ниже *$500,000* скрываются автоматически; для XT порог *$150,000*, Aster не фильтруется. В автоотчётах 20:00/22:00 OI не скрывает монеты, а показывается как риск-статус ✅/⚠️/🚫. Для входа приоритет — *$1,000,000+* на конкретной бирже. Твой ордер не должен превышать *1–1.5%* от всего OI.
• 📊 *24h Volume:* В ручном `/analyze` подтверждённый оборот ниже *$400,000* скрывается автоматически; для XT порог *$150,000*, Aster не фильтруется. В автоотчётах 20:00/22:00 Vol24h не скрывает монеты, а показывается как риск-статус ✅/⚠️/🚫. Дальше нужна ручная проверка стакана, spread и slippage.
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
    """Список монет для скана: XT/CoinW/Toobit/KuCoin/Bitunix берём из их API, остальные — из Phemex universe."""
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
        "payments_per_day": len(rates) / max((end_ms - start_ms) / (24 * 60 * 60 * 1000), 1e-9),
    }


def number(value, default=0.0):
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def first_payload(data):
    if isinstance(data, dict):
        for key in ("data", "result", "list"):
            payload = data.get(key)
            if isinstance(payload, list):
                return payload[0] if payload else {}
            if isinstance(payload, dict):
                return first_payload(payload)
        return data
    if isinstance(data, list):
        return data[0] if data else {}
    return {}


def best_book_prices(data):
    payload = first_payload(data)
    if isinstance(payload, dict):
        payload = payload.get("orderbook_p") or payload.get("book") or payload
    bids = payload.get("bids") or payload.get("b") or []
    asks = payload.get("asks") or payload.get("a") or []
    if not bids or not asks:
        return None
    bid = bids[0]
    ask = asks[0]
    if isinstance(bid, dict):
        best_bid = number(bid.get("p") or bid.get("price") or bid.get("px"))
    else:
        best_bid = number(bid[0] if isinstance(bid, (list, tuple)) and bid else None)
    if isinstance(ask, dict):
        best_ask = number(ask.get("p") or ask.get("price") or ask.get("px"))
    else:
        best_ask = number(ask[0] if isinstance(ask, (list, tuple)) and ask else None)
    if best_bid <= 0 or best_ask <= 0:
        return None
    return {"bid": best_bid, "ask": best_ask}


def coin_base(coin):
    coin = coin.upper()
    if coin.endswith("USDT"):
        return coin[:-4]
    if coin.endswith("USD"):
        return coin[:-3]
    return coin


def public_get_json(base_url, endpoint, params, timeout=6, proxies=None):
    r = requests.get(f"{base_url}{endpoint}", params=params, timeout=timeout, proxies=proxies)
    r.raise_for_status()
    return r.json()


def fetch_market_quote(coin, exchange):
    """Returns top-of-book bid/ask for report entry spread filtering."""
    base = coin_base(coin)
    exchange = exchange.lower()
    if exchange == "binance":
        from config import BINANCE_PROXY_URL
        data = public_get_json("https://fapi.binance.com", "/fapi/v1/depth", {"symbol": f"{base}USDT", "limit": "20"}, proxies=_proxies(BINANCE_PROXY_URL))
        return best_book_prices(data)
    if exchange == "bybit":
        from config import BYBIT_PROXY_URL
        data = public_get_json("https://api.bybit.com", "/v5/market/tickers", {"category": "linear", "symbol": f"{base}USDT"}, proxies=_proxies(BYBIT_PROXY_URL))
        item = first_payload(data)
        return {"bid": number(item.get("bid1Price")), "ask": number(item.get("ask1Price"))}
    if exchange == "phemex":
        data = public_get_json("https://api.phemex.com", "/md/v2/ticker/24hr", {"symbol": f"{base}USDT"})
        item = first_payload(data)
        quote = {"bid": number(item.get("bidRp")), "ask": number(item.get("askRp"))}
        if quote["bid"] > 0 and quote["ask"] > 0:
            return quote
        book = public_get_json("https://api.phemex.com", "/md/v2/orderbook", {"symbol": f"{base}USDT"})
        return best_book_prices(book)
    if exchange == "okx":
        data = public_get_json("https://www.okx.com", "/api/v5/market/ticker", {"instId": f"{base}-USDT-SWAP"})
        item = first_payload(data)
        return {"bid": number(item.get("bidPx")), "ask": number(item.get("askPx"))}
    if exchange == "gate":
        data = public_get_json("https://api.gateio.ws/api/v4", "/futures/usdt/order_book", {"contract": f"{base}_USDT", "limit": "20"})
        return best_book_prices(data)
    if exchange == "xt":
        data = public_get_json("https://fapi.xt.com", "/future/market/v1/public/q/depth", {"symbol": f"{base.lower()}_usdt", "level": "20"})
        return best_book_prices(data)
    if exchange == "toobit":
        data = public_get_json("https://api.toobit.com", "/quote/v1/ticker/24hr", {"symbol": f"{base}-SWAP-USDT"})
        item = first_payload(data)
        return {"bid": number(item.get("b") or item.get("bid")), "ask": number(item.get("a") or item.get("ask"))}
    if exchange == "bingx":
        data = public_get_json("https://open-api.bingx.com", "/openApi/swap/v2/quote/depth", {"symbol": f"{base}-USDT", "limit": "20"})
        return best_book_prices(data)
    if exchange == "kucoin":
        symbol = "XBTUSDTM" if base == "BTC" else f"{base}USDTM"
        data = public_get_json("https://api-futures.kucoin.com", "/api/v1/ticker", {"symbol": symbol})
        item = first_payload(data)
        return {"bid": number(item.get("bestBidPrice")), "ask": number(item.get("bestAskPrice"))}
    if exchange == "coinw":
        data = public_get_json("https://api.coinw.com", "/v1/perpumPublic/depth", {"base": base})
        return best_book_prices(data)
    if exchange == "weex":
        data = public_get_json(
            "https://api-contract.weex.com",
            "/capi/v3/market/depth",
            {"symbol": f"{base}USDT", "limit": "15"},
        )
        return best_book_prices(data)
    return None


def entry_spread_pct(long_quote, short_quote):
    if not long_quote or not short_quote:
        return None
    long_ask = number(long_quote.get("ask"))
    short_bid = number(short_quote.get("bid"))
    if long_ask <= 0 or short_bid <= 0:
        return None
    return (short_bid / long_ask * 100) - 100


def calc_pair_daily_income_pct(long_avg, long_payments_per_day, short_avg, short_payments_per_day):
    """Daily funding PnL % for equal notional legs.

    Long earns when funding is negative, short earns when funding is positive.
    Funding intervals differ by exchange, so per-payment averages must be
    multiplied by observed payments per day before comparing two venues.
    """
    return (-long_avg * long_payments_per_day) + (short_avg * short_payments_per_day)


def calc_pair_daily_income_usd(amount_usd, long_avg, long_payments_per_day, short_avg, short_payments_per_day):
    return amount_usd * calc_pair_daily_income_pct(long_avg, long_payments_per_day, short_avg, short_payments_per_day) / 100


def find_delta_pair_for_signal(coin, signal, days, active_exchanges):
    """Подбирает лучшую пару для монеты с учетом разных funding intervals."""
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

    if signal["exchange"] not in exchange_data:
        return None

    candidates = []
    quote_cache = {}
    for ex in exchange_data:
        try:
            quote = fetch_market_quote(coin, ex)
            if quote and quote.get("bid", 0) > 0 and quote.get("ask", 0) > 0:
                quote_cache[ex] = quote
        except Exception:
            pass
        time.sleep(0.05)

    for long_ex, long_info in exchange_data.items():
        for short_ex, short_info in exchange_data.items():
            if short_ex == long_ex:
                continue
            entry_spread = entry_spread_pct(quote_cache.get(long_ex), quote_cache.get(short_ex))
            if entry_spread is None or entry_spread < AUTO_REPORT_MIN_ENTRY_SPREAD_PCT:
                continue
            net_daily_pct = calc_pair_daily_income_pct(
                long_info["avg"],
                long_info["payments_per_day"],
                short_info["avg"],
                short_info["payments_per_day"],
            )
            daily_income_usd = AUTO_SCAN_AMOUNT * net_daily_pct / 100
            if daily_income_usd < AUTO_REPORT_MIN_DAILY_USD:
                continue
            candidates.append((
                daily_income_usd,
                net_daily_pct,
                long_ex,
                short_ex,
                long_info,
                short_info,
                entry_spread,
            ))

    if not candidates:
        return None
    candidates.sort(key=lambda x: (-x[0], x[4]["std"] + x[5]["std"], -x[6]))
    daily_income_usd, net_daily_pct, long_ex, short_ex, long_info, short_info, entry_spread = candidates[0]
    return {
        "coin": coin,
        "direction": signal["direction"],
        "long_ex": long_ex,
        "short_ex": short_ex,
        "long_avg": long_info["avg"],
        "short_avg": short_info["avg"],
        "long_payments_per_day": long_info["payments_per_day"],
        "short_payments_per_day": short_info["payments_per_day"],
        "net_rate": net_daily_pct,
        "net_daily_pct": net_daily_pct,
        "daily_income_usd": daily_income_usd,
        "entry_spread_pct": entry_spread,
    }


def format_evening_pair(pair):
    long_label = EXCHANGE_LABELS.get(pair["long_ex"], pair["long_ex"])
    short_label = EXCHANGE_LABELS.get(pair["short_ex"], pair["short_ex"])
    return (
        f"*{pair['coin']}*\n"
        f"🟢 Лонг: `{long_label}` avg `{pair['long_avg']:+.4f}%` | "
        f"`{pair['long_payments_per_day']:.1f}` выплат/день | "
        f"{format_oi_status(pair['long_ex'], pair['coin'])} | "
        f"{format_volume_status(pair['long_ex'], pair['coin'])}\n"
        f"🔴 Шорт: `{short_label}` avg `{pair['short_avg']:+.4f}%` | "
        f"`{pair['short_payments_per_day']:.1f}` выплат/день | "
        f"{format_oi_status(pair['short_ex'], pair['coin'])} | "
        f"{format_volume_status(pair['short_ex'], pair['coin'])}\n"
        f"📈 Чистый фандинг: `{pair['net_daily_pct']:+.4f}%` / день\n"
        f"💰 Оценка: `${pair['daily_income_usd']:.2f}` за 1 день на позицию "
        f"`${AUTO_SCAN_AMOUNT:,.0f}` на ногу\n"
        f"🚪 Спред входа: `{pair['entry_spread_pct']:+.3f}%`"
    )


def _cleanup_evening_sessions():
    cutoff = time.time() - _EVENING_SESSION_TTL
    for token, session in list(_evening_sessions.items()):
        if session["created_at"] < cutoff:
            _evening_sessions.pop(token, None)


def _save_evening_session(pairs):
    _cleanup_evening_sessions()
    token = secrets.token_urlsafe(6)
    _evening_sessions[token] = {
        "created_at": time.time(),
        "groups": group_items(pairs, lambda pair: pair["coin"]),
    }
    return token


def _evening_more_markup(token, group_key, next_offset, total):
    if next_offset >= total:
        return None
    count = min(EVENING_PAGE_SIZE, total - next_offset)
    return InlineKeyboardMarkup([[
        InlineKeyboardButton(
            f"Показать ещё {count}",
            callback_data=f"ev_more:{token}:{group_key}:{next_offset}",
        )
    ]])


async def send_evening_group_page(bot, chat_id, token, group_key, offset):
    session = _evening_sessions.get(token)
    if not session:
        await bot.send_message(chat_id, "Сессия вечернего отчёта устарела. Запусти /report ещё раз.")
        return
    pairs = session["groups"].get(group_key, [])
    total = len(pairs)
    end = min(offset + EVENING_PAGE_SIZE, total)
    page = pairs[offset:end]
    if not page:
        return
    text = f"{GROUP_LABELS[group_key]} — {total}\n\n" + "\n\n".join(format_evening_pair(pair) for pair in page)
    await bot.send_message(
        chat_id,
        text,
        parse_mode="Markdown",
        reply_markup=_evening_more_markup(token, group_key, end, total),
    )


async def send_evening_pages(bot, chat_id, pairs):
    token = _save_evening_session(pairs)
    grouped = _evening_sessions[token]["groups"]
    counts = " · ".join(f"{GROUP_LABELS[key]}: {len(grouped[key])}" for key in GROUP_ORDER)
    await bot.send_message(
        chat_id,
        f"🤖 *ВЕЧЕРНИЙ ОТЧЁТ* — база {AUTO_SCAN_DAYS} дня, "
        f"позиция ${AUTO_SCAN_AMOUNT:,.0f} на ногу\n\n{counts}",
        parse_mode="Markdown",
    )
    for group_key in GROUP_ORDER:
        await send_evening_group_page(bot, chat_id, token, group_key, 0)
    return token


async def evening_more_callback(update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    try:
        _prefix, token, group_key, offset_raw = query.data.split(":", 3)
        if group_key not in GROUP_ORDER:
            raise ValueError("unknown group")
        offset = int(offset_raw)
    except Exception:
        await query.message.reply_text("Не смог открыть следующую группу. Запусти /report ещё раз.")
        return
    await send_evening_group_page(context.bot, query.message.chat_id, token, group_key, offset)


async def _collect_evening_gemini(signals):
    chunks = []
    errors = []
    total_chunks = (len(signals) + 14) // 15
    for i in range(0, len(signals), 15):
        part = signals[i:i + 15]
        chunk_index = i // 15 + 1
        text_rows = []
        for signal in part:
            direction = "ЛОНГ" if signal["direction"] == "LONG" else "ШОРТ"
            text_rows.append(
                f"- {signal['coin']} ({direction}, "
                f"{EXCHANGE_LABELS.get(signal['exchange'], signal['exchange'])}, "
                f"avg {signal['avg']:+.4f}%)"
            )
        answer = await gemini_report_recommendation(text_rows, AUTO_SCAN_DAYS, chunk_index, total_chunks)
        if answer:
            chunks.append(answer)
        else:
            errors.append(
                f"порция {chunk_index}/{total_chunks}: "
                f"{get_last_gemini_error() or 'timeout/no response'}"
            )
        await asyncio.sleep(1)
    return chunks, errors


async def send_evening_gemini_recommendations(context, chat_id, signals):
    if not GEMINI_API_KEY:
        await context.bot.send_message(chat_id, "⚠️ Gemini не настроен; основной отчёт уже отправлен.")
        return
    await context.bot.send_message(
        chat_id,
        f"🤖 Gemini отдельно проверяет {len(signals)} найденных монет. Основной список уже доступен.",
    )
    try:
        chunks, errors = await asyncio.wait_for(
            _collect_evening_gemini(signals),
            timeout=EVENING_GEMINI_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        chunks, errors = [], [f"общий timeout {EVENING_GEMINI_TIMEOUT_SECONDS} сек."]
    if chunks:
        report = "🤖 *Gemini — отдельная рекомендация*\n\n" + "\n".join(chunks)
        for chunk in [report[i:i + 4000] for i in range(0, len(report), 4000)]:
            await context.bot.send_message(chat_id, chunk, parse_mode="Markdown")
    if errors:
        await context.bot.send_message(
            chat_id,
            "⚠️ Gemini не завершил весь анализ. Основной отчёт не затронут.\n"
            + "\n".join(errors[:3]),
        )


async def run_evening_report(context: ContextTypes.DEFAULT_TYPE, chat_id: int, manual=False):
    """Вечерний отчёт: full-фильтр → пары и страницы → отдельная Gemini-рекомендация."""
    active = get_active_exchanges()
    if not active:
        await context.bot.send_message(chat_id, "❌ Вечерний отчёт: нет активных бирж.")
        return

    await context.bot.send_message(
        chat_id,
        ("🕗 *Вечерний авто-скан запущен*\n" if not manual else "🕗 *Вечерний отчёт запущен вручную*\n")
        + "Ищу FULL-подходящие монеты, подбираю пару, а Gemini показываю как рекомендацию.\n"
        + f"❌ {temporary_disabled_text()} временно отключены; API-код сохранён, их можно быстро вернуть.",
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
        await context.bot.send_message(chat_id, f"🔎 {EXCHANGE_LABELS.get(ex, ex)}: скан {len(coins)} монет...")
        for idx, coin in enumerate(coins, start=1):
            try:
                rows, _sym = fetcher(coin, start_ms, now_ms)
            except Exception:
                rows = []
            if not rows:
                time.sleep(0.1)
                continue
            ordered_rates = [rate for _, rate in sorted(rows, key=lambda x: x[0])]
            metrics = analyze_rates(ordered_rates)
            if metrics and metrics["category"] == "full" and recent_trend_ok(ordered_rates, metrics["direction"]):
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

        found_now = len(full_signals) - found_before
        await context.bot.send_message(
            chat_id,
            f"✅ {EXCHANGE_LABELS.get(ex, ex)}: FULL {found_now}, всего {len(full_signals)}"
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
    pairs = []
    for coin in report_coins:
        pair = find_delta_pair_for_signal(coin, best_signal_by_coin[coin], AUTO_SCAN_DAYS, active)
        if pair:
            pairs.append(pair)

    if not pairs:
        await context.bot.send_message(
            chat_id,
            f"⚠️ Не нашёл дельта-нейтральных пар по FULL-монетам с доходом от ${AUTO_REPORT_MIN_DAILY_USD:.0f}/день."
        )
        await send_entry_instructions(context, chat_id)
        return

    pairs.sort(key=lambda pair: pair["net_rate"], reverse=True)
    await send_evening_pages(context.bot, chat_id, pairs)
    await send_entry_instructions(context, chat_id)
    visible_symbols = {pair["coin"] for pair in pairs}
    gemini_signals = [
        best_signal_by_coin[symbol]
        for symbol in report_coins
        if symbol in visible_symbols
    ]
    await send_evening_gemini_recommendations(context, chat_id, gemini_signals)


async def auto_scan_job(context: ContextTypes.DEFAULT_TYPE):
    if not REPORT_CHAT_ID:
        return
    await run_evening_report(context, int(REPORT_CHAT_ID))

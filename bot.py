"""
Funding Rate Analyzer Telegram Bot.

Main Telegram handlers live here. Exchange fetchers, filter logic, Gemini AI,
and evening report jobs are split into modules to keep future AI edits safer.
"""

import time
import requests
from datetime import datetime, time as dt_time, timezone

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    filters, ContextTypes, ConversationHandler, CallbackQueryHandler
)

from ai import gemini_analyze_single, send_gemini_scan_review
from analysis import (
    analyze_coin_multi,
    analyze_delta,
    analyze_rates,
    fmt_delta_result,
    get_active_exchanges,
    recent_trend_label,
    recent_trend_ok,
)
from config import (
    BOT_TOKEN,
    DEFAULT_DAYS,
    EXCHANGES_ENABLED,
    GEMINI_API_KEY,
    MAX_OUTLIER_PCT,
    MIN_NEG_RATIO,
    MIN_POS_RATIO,
    NEG_AVG_THRESHOLD,
    REPORT_CHAT_ID,
    STABILITY_THRESHOLD,
)
from exchanges import EXCHANGE_FETCHERS, EXCHANGE_LABELS, phemex_fetch, phemex_get_all_symbols
from reports import auto_scan_job

WAIT_ANALYZE_COINS = 1
WAIT_SHOW_COIN = 3
WAIT_CALC_COIN = 5
WAIT_DELTA_COIN = 7
WAIT_DELTACALC = 9
WAIT_AI_COIN = 10

ACF_COIN = 20
ACF_DAYS = 21
ACF_DAYS_NUM = 22
ACF_EXCH = 23
FR_COIN = 30
FR_DAYS = 31
FR_DAYS_NUM = 32
FR_EXCH = 33
PC_COIN = 40
PC_AMT = 41
PC_AMT_NUM = 42
PC_DAYS = 43
PC_DAYS_NUM = 44
PC_EXCH = 45
AN_METHOD = 50
AN_AMT = 51
AN_AMT_NUM = 52
AN_THRESH = 53
AN_THRESH_NUM = 54
AN_DAYS = 55
AN_DAYS_NUM = 56

SCAN_DAYS = 7
SCAN_BATCH = 20
AN_ANOMALY_THRESHOLD = 0.8
_scan_running = {}

# ─────────────────────────────────────────────
# ПАРСИНГ АРГУМЕНТОВ
# ─────────────────────────────────────────────

def parse_tokens(text):
    """'BTC ETH /days 14 /exchange xt' -> (coins, days, exchange)
    Также поддерживает быстрый ввод без префиксов:
    'BTC coinw 7' -> (['BTC'], 7, 'coinw')
    'BTC ETH phemex 14' -> (['BTC', 'ETH'], 14, 'phemex')
    """
    KNOWN_EXCHANGES = set(EXCHANGES_ENABLED.keys())
    DISABLED_EXCHANGE_ALIASES = {"kucoin", "gate", "gateio", "gate.io", "blofin", "weex"}

    parts = text.strip().split()
    days = DEFAULT_DAYS
    exchange = None
    coins = []
    i = 0
    while i < len(parts):
        p = parts[i].lower()
        # /days N или --days N
        if p in ("/days", "--days") and i + 1 < len(parts):
            try:
                days = int(parts[i + 1]); i += 2; continue
            except ValueError:
                pass
        # /exchange NAME или --exchange NAME
        if p in ("/exchange", "--exchange") and i + 1 < len(parts):
            exchange = parts[i + 1].lower(); i += 2; continue
        # пропускаем команды вида /filter если вдруг попали в текст
        if p in ("/filter", "/funding", "/calculator", "/start", "/help", "/settings", "/cancel"):
            i += 1; continue
        # Распознаём название активной биржи без префикса.
        if p in KNOWN_EXCHANGES:
            exchange = p; i += 1; continue
        # Старые/отключённые биржи не считаем тикерами монет.
        if p in EXCHANGE_FETCHERS or p in DISABLED_EXCHANGE_ALIASES:
            i += 1; continue
        # Распознаём число как количество дней
        try:
            days = int(parts[i]); i += 1; continue
        except ValueError:
            pass
        coins.append(parts[i].upper())
        i += 1
    return coins, days, exchange


# ─────────────────────────────────────────────
# ФОРМАТИРОВАНИЕ РЕЗУЛЬТАТОВ
# ─────────────────────────────────────────────

def fmt_coin_line(coin, ex_results, active_exchanges):
    """Форматирует одну строку монеты для всех бирж."""
    lines = []
    # Определяем общую категорию и направление (лучшая из бирж)
    cats_dirs = [(r.get("category"), r.get("direction", "LONG"))
                 for r in ex_results.values() if not r.get("error")]
    if any(c == "full" for c, _ in cats_dirs):
        overall = "✅"
    elif any(c == "partial" for c, _ in cats_dirs):
        overall = "⚡"
    elif cats_dirs:
        overall = "❌"
    else:
        overall = "⚠️"

    # Определяем преобладающее направление
    dirs = [d for _, d in cats_dirs if d]
    direction = max(set(dirs), key=dirs.count) if dirs else "LONG"
    dir_icon  = "🟢 ЛОНГ" if direction == "LONG" else "🔴 ШОРТ"

    lines.append(f"{overall} *{coin}* {dir_icon}")
    for ex in active_exchanges:
        r = ex_results.get(ex, {})
        label = EXCHANGE_LABELS.get(ex, ex.upper())
        if r.get("error"):
            lines.append(f"  `{label}`: ошибка — {r['error'][:40]}")
            continue
        cat      = {"full": "✅", "partial": "⚡", "fail": "❌"}.get(r.get("category", "fail"), "❌")
        r_dir    = r.get("direction", "LONG")
        key_avg  = r["neg_avg"] if r_dir == "LONG" else r.get("pos_avg", 0.0)
        trend = f"  тренд `{r.get('trend_label')}`" if r.get("trend_label") else ""
        lines.append(
            f"  `{label}` {cat}  avg `{r['avg']:+.4f}%`  key\\_avg `{key_avg:+.4f}%`  выбр `{r['outlier_pct']:.0f}%`{trend}"
        )
    return "\n".join(lines)


def build_analyze_reply(all_results, days, active_exchanges):
    """Строит итоговое сообщение анализа."""
    ex_labels = " + ".join(EXCHANGE_LABELS.get(e, e.upper()) for e in active_exchanges)
    lines = [f"📊 *Анализ* — {days} дней — {ex_labels}\n"]

    # Группируем монеты по лучшей категории
    def best_cat(ex_res):
        cats = [r.get("category") for r in ex_res.values() if not r.get("error")]
        if "full" in cats: return "full"
        if "partial" in cats: return "partial"
        if cats: return "fail"
        return "error"

    full    = [(c, r) for c, r in all_results.items() if best_cat(r) == "full"]
    partial = [(c, r) for c, r in all_results.items() if best_cat(r) == "partial"]
    fail    = [(c, r) for c, r in all_results.items() if best_cat(r) == "fail"]
    errors  = [(c, r) for c, r in all_results.items() if best_cat(r) == "error"]

    if full:
        lines.append(f"✅ *ПОДХОДЯТ* ({len(full)}):")
        for coin, ex_res in full:
            lines.append(fmt_coin_line(coin, ex_res, active_exchanges))
            lines.append("")

    if partial:
        lines.append(f"⚡ *ЧАСТИЧНО* ({len(partial)}):")
        for coin, ex_res in partial:
            lines.append(fmt_coin_line(coin, ex_res, active_exchanges))
            lines.append("")

    if fail:
        lines.append(f"❌ *НЕ ПОДХОДЯТ* ({len(fail)}):")
        for coin, ex_res in fail:
            lines.append(fmt_coin_line(coin, ex_res, active_exchanges))
            lines.append("")

    if errors:
        lines.append(f"⚠️ *Ошибки* ({len(errors)}):")
        for coin, _ in errors:
            lines.append(f"  `{coin}` — нет данных ни на одной бирже")

    return "\n".join(lines)


# ─────────────────────────────────────────────
# DO-функции
# ─────────────────────────────────────────────

async def do_analyze(update, coins, days, exchange_arg, selected_exchanges=None):
    # Получаем reply функцию независимо от типа (Update или CallbackQuery)
    if hasattr(update, 'message') and update.message:
        reply_fn = update.message.reply_text
    elif hasattr(update, 'reply_text'):
        reply_fn = update.reply_text
    else:
        reply_fn = update.message.reply_text

    # Если переданы выбранные биржи — используем их
    if selected_exchanges:
        active = [e for e in selected_exchanges if e in EXCHANGE_FETCHERS]
    else:
        active = get_active_exchanges(exchange_arg)
    if not active:
        await reply_fn("❌ Нет активных бирж. Проверь настройки EXCHANGES_ENABLED.")
        return

    ex_str = " + ".join(EXCHANGE_LABELS.get(e, e) for e in active)
    await reply_fn(f"🔍 Анализирую {len(coins)} монет на {ex_str} за {days} дней...")

    now_ms = int(time.time() * 1000)
    start_ms = now_ms - days * 24 * 60 * 60 * 1000

    all_results = {}
    for coin in coins:
        all_results[coin] = analyze_coin_multi(coin, start_ms, now_ms, active)

    reply = build_analyze_reply(all_results, days, active)
    # Telegram limit
    if len(reply) > 4000:
        chunks = [reply[i:i+4000] for i in range(0, len(reply), 4000)]
        for chunk in chunks:
            await reply_fn(chunk, parse_mode="Markdown")
    else:
        await reply_fn(reply, parse_mode="Markdown")


async def do_show(update, coin, days, exchange_arg):
    active = get_active_exchanges(exchange_arg)
    if not active:
        await update.message.reply_text("❌ Нет активных бирж.")
        return

    now_ms = int(time.time() * 1000)
    start_ms = now_ms - days * 24 * 60 * 60 * 1000

    for ex in active:
        fetcher = EXCHANGE_FETCHERS.get(ex)
        label = EXCHANGE_LABELS.get(ex, ex.upper())
        await update.message.reply_text(f"🔍 Загружаю {coin} с {label} за {days} дней...")

        try:
            data, sym_or_err = fetcher(coin, start_ms, now_ms)
        except Exception as e:
            await update.message.reply_text(f"❌ {label}: {e}")
            continue

        if not data:
            await update.message.reply_text(f"❌ {label}: {sym_or_err}")
            continue

        data_sorted = sorted(data, key=lambda x: x[0], reverse=True)
        rates = [r for _, r in data_sorted]
        neg = [r for r in rates if r < 0]
        avg = sum(rates) / len(rates)
        neg_avg = sum(neg) / len(neg) if neg else 0.0

        header = (
            f"📈 *{coin}* — {label} — `{sym_or_err}` — {days} дней\n"
            f"Ставок: `{len(rates)}`  |  Avg: `{avg:+.4f}%`  |  Neg avg: `{neg_avg:+.4f}%`\n"
            f"Min: `{min(rates):+.4f}%`  |  Max: `{max(rates):+.4f}%`\n\n"
            f"`{'Время (UTC)':<17} {'Ставка':>10}`\n"
            f"`{'─'*29}`\n"
        )

        rate_lines = []
        for ts, rate in data_sorted:
            dt = datetime.fromtimestamp(ts / 1000, tz=timezone.utc)
            marker = "  ◀" if rate <= STABILITY_THRESHOLD else ""
            rate_lines.append(f"`{dt.strftime('%m-%d %H:%M'):<12} {rate:>+10.4f}%{marker}`")

        chunk = header
        messages = []
        for line in rate_lines:
            if len(chunk) + len(line) + 1 > 4000:
                messages.append(chunk)
                chunk = ""
            chunk += line + "\n"
        if chunk:
            messages.append(chunk)

        for msg in messages:
            await update.message.reply_text(msg, parse_mode="Markdown")


async def do_calc(update, coin, amount_usd, days, exchange_arg):
    active = get_active_exchanges(exchange_arg)
    if not active:
        await update.message.reply_text("❌ Нет активных бирж.")
        return

    now_ms = int(time.time() * 1000)
    start_ms = now_ms - days * 24 * 60 * 60 * 1000

    for ex in active:
        fetcher = EXCHANGE_FETCHERS.get(ex)
        label = EXCHANGE_LABELS.get(ex, ex.upper())
        await update.message.reply_text(f"🔍 Считаю доход по {coin} на {label} за {days} дней...")

        try:
            data, sym_or_err = fetcher(coin, start_ms, now_ms)
        except Exception as e:
            await update.message.reply_text(f"❌ {label}: {e}")
            continue

        if not data:
            await update.message.reply_text(f"❌ {label}: {sym_or_err}")
            continue

        by_day = {}
        total_income = 0.0
        for ts, rate in data:
            if rate >= 0:
                continue
            payment = amount_usd * abs(rate / 100)
            total_income += payment
            day = datetime.fromtimestamp(ts / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
            by_day[day] = by_day.get(day, 0.0) + payment

        rates = [r for _, r in data]
        neg = [r for r in rates if r < 0]
        neg_avg = sum(neg) / len(neg) if neg else 0.0
        income_per_day = total_income / days if days > 0 else 0

        lines = [
            f"💰 *Калькулятор — {coin} — {label}*\n",
            f"Сумма позиции: `${amount_usd:,.0f}`",
            f"Период: `{days}` дней",
            f"Выплат получено: `{len(neg)}` из `{len(rates)}` ставок\n",
            f"📈 *Итого: `${total_income:.2f}`*",
            f"📅 В среднем в день: `${income_per_day:.2f}`",
            f"⚡ Avg neg ставка: `{neg_avg:+.4f}%`\n",
            f"`{'Дата':<12} {'Доход':>12}`",
            f"`{'─'*26}`",
        ]
        for day in sorted(by_day.keys(), reverse=True):
            lines.append(f"`{day:<12} ${by_day[day]:>10.2f}`")

        await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


# ─────────────────────────────────────────────
# CONVERSATION HANDLERS
# ─────────────────────────────────────────────

async def analyze_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.args:
        coins, days, exchange = parse_tokens(" ".join(context.args))
        if coins:
            await do_analyze(update, coins, days, exchange)
            return ConversationHandler.END
    await update.message.reply_text(
        "Введи монеты через пробел:\n\n"
        "`BTC ETH SOL ENJ`\n"
        "`BTC ETH /days 14`\n"
        "`BTC ETH /exchange xt`\n"
        "`BTC ETH /exchange phemex`\n"
        "`BTC ETH /exchange all`\n\n"
        "Отмена: /cancel",
        parse_mode="Markdown"
    )
    return WAIT_ANALYZE_COINS


async def analyze_got_coins(update: Update, context: ContextTypes.DEFAULT_TYPE):
    coins, days, exchange = parse_tokens(update.message.text.strip())
    if not coins:
        await update.message.reply_text("Не распознал монеты. Попробуй: `BTC ETH SOL`", parse_mode="Markdown")
        return WAIT_ANALYZE_COINS
    await do_analyze(update, coins, days, exchange)
    return ConversationHandler.END


async def show_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.args:
        coins, days, exchange = parse_tokens(" ".join(context.args))
        if coins:
            await do_show(update, coins[0], days, exchange)
            return ConversationHandler.END
    await update.message.reply_text(
        "Введи монету:\n\n"
        "`ENJ`\n"
        "`ENJ /days 14`\n"
        "`ENJ /exchange xt`\n\n"
        "Отмена: /cancel",
        parse_mode="Markdown"
    )
    return WAIT_SHOW_COIN


async def show_got_coin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    coins, days, exchange = parse_tokens(update.message.text.strip())
    if not coins:
        await update.message.reply_text("Не распознал монету. Попробуй: `ENJ`", parse_mode="Markdown")
        return WAIT_SHOW_COIN
    await do_show(update, coins[0], days, exchange)
    return ConversationHandler.END


async def calc_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.args:
        coins, days, exchange = parse_tokens(" ".join(context.args))
        amount = None
        remaining = []
        for p in coins:
            try:
                amount = float(p.replace("$", "").replace(",", ""))
            except ValueError:
                remaining.append(p)
        if remaining and amount:
            await do_calc(update, remaining[0], amount, days, exchange)
            return ConversationHandler.END
    await update.message.reply_text(
        "Введи монету и сумму позиции:\n\n"
        "`ENJ 25000`\n"
        "`ENJ 25000 /days 14`\n"
        "`ENJ 25000 /exchange xt`\n\n"
        "Отмена: /cancel",
        parse_mode="Markdown"
    )
    return WAIT_CALC_COIN


async def calc_got_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    coins, days, exchange = parse_tokens(update.message.text.strip())
    amount = None
    remaining = []
    for p in coins:
        try:
            amount = float(p.replace("$", "").replace(",", ""))
        except ValueError:
            remaining.append(p)
    if not remaining:
        await update.message.reply_text("Не распознал монету. Попробуй: `ENJ 25000`", parse_mode="Markdown")
        return WAIT_CALC_COIN
    if not amount or amount <= 0:
        await update.message.reply_text("Не распознал сумму. Попробуй: `ENJ 25000`", parse_mode="Markdown")
        return WAIT_CALC_COIN
    await do_calc(update, remaining[0], amount, days, exchange)
    return ConversationHandler.END


# ─────────────────────────────────────────────
# ПРОСТЫЕ КОМАНДЫ
# ─────────────────────────────────────────────

async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Отменено.")
    return ConversationHandler.END


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    active = [EXCHANGE_LABELS.get(e, e) for e, on in EXCHANGES_ENABLED.items() if on]
    ex_str = ", ".join(active)
    gemini_status = "включён" if GEMINI_API_KEY else "не настроен"
    report_status = "включён" if REPORT_CHAT_ID else "не настроен"
    text = (
        "👋 *Funding Rate Analyzer + Gemini AI*\n\n"
        f"Активные биржи: `{ex_str}`\n"
        f"Gemini AI: `{gemini_status}` | Вечерний отчёт: `{report_status}`\n\n"
        "Команды:\n"
        "/filter — анализ монет по эталонным фильтрам фандинга\n"
        "/funding — ставки фандинга по монете\n"
        "/calculator — калькулятор дохода от фандинга\n"
        "/analyze — скан рынка + Gemini-фильтр найденных монет\n"
        "/ai — фундаментальный AI-анализ монеты без анализа фандинга\n"
        "/findpair — дельта-нейтраль: найти пару лонг+шорт\n"
        "/settings — настройки и управление биржами\n"
        "/help — справка\n\n"
        "💡 *Быстрый ввод:*\n"
        "`/filter ENJ phemex 7`\n"
        "`/funding ENJ phemex 7`\n"
        "`/calculator ENJ 25000 7 phemex`\n"
        "`/ai SOL ENJ`"
    )
    await update.message.reply_text(text, parse_mode="Markdown")


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await cmd_start(update, context)


# cmd_exchanges удалена — используй /settings


# cmd_toggle удалена — используй /settings


async def cmd_settings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    active = [EXCHANGE_LABELS.get(e, e) for e, on in EXCHANGES_ENABLED.items() if on]
    text = (
        "⚙️ *Настройки*\n\n"
        f"Активные биржи: `{', '.join(active)}`\n"
        f"Период по умолчанию: `{DEFAULT_DAYS}` дней\n"
        f"Порог ставки: `{STABILITY_THRESHOLD}%`\n"
        f"Макс. выбросов: `{MAX_OUTLIER_PCT}%`\n"
        f"Neg avg порог: `{NEG_AVG_THRESHOLD}%`\n\n"
        "Категории:\n"
        "✅ *ПОДХОДИТ* — стабильность ок\n"
        "⚡ *ЧАСТИЧНО* — neg\\_avg сильный, нестабильно\n"
        "❌ *НЕ ПОДХОДИТ* — не прошла"
    )
    await update.message.reply_text(text, parse_mode="Markdown")


async def unknown(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Не знаю такой команды. Напиши /help.")




# ─────────────────────────────────────────────
# /analyze — скан монет с выбором биржи
# ─────────────────────────────────────────────

SCAN_EXCHANGE_FETCHERS = {
    "phemex": None,       # особая обработка — список монет с Phemex
    "xt":     "xt",
    "toobit": "toobit",
    "okx":    "okx",
    "bingx":  "bingx",
    "coinw":  "coinw",
    "zoomex": "zoomex",
}


# ─────────────────────────────────────────────
# /analyze — скан монет с выбором биржи, метода и периода
# ─────────────────────────────────────────────

AN_ANOMALY_THRESHOLD = 0.8   # аномалии для метода "Средний доход" > ±0.8%

async def cmd_analyze_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Шаг 1: выбор биржи."""
    context.user_data.clear()
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("Phemex",  callback_data="an_ex_phemex"),
         InlineKeyboardButton("XT",      callback_data="an_ex_xt")],
        [InlineKeyboardButton("Toobit",  callback_data="an_ex_toobit"),
         InlineKeyboardButton("CoinW",   callback_data="an_ex_coinw")],
        [InlineKeyboardButton("OKX",     callback_data="an_ex_okx"),
         InlineKeyboardButton("BingX",   callback_data="an_ex_bingx")],
        [InlineKeyboardButton("Zoomex",  callback_data="an_ex_zoomex")],
        [InlineKeyboardButton("Отмена",  callback_data="an_cancel")],
    ])
    await update.message.reply_text(
        "🔍 Скан монет по фандингу\n\nШаг 1/3: Выбери биржу:",
        reply_markup=keyboard,
    )
    return AN_METHOD


async def an_exchange_btn(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Шаг 2: биржа выбрана → выбор метода."""
    q = update.callback_query
    await q.answer()
    if q.data == "an_cancel":
        await q.edit_message_text("Отменено.")
        return ConversationHandler.END
    exchange = q.data.replace("an_ex_", "")
    context.user_data["an_exchange"] = exchange
    labels = {ex: EXCHANGE_LABELS.get(ex, ex.upper()) for ex in EXCHANGES_ENABLED}
    label = labels.get(exchange, exchange)
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("Средняя ставка",  callback_data="an_method_rate"),
         InlineKeyboardButton("Средний доход",   callback_data="an_method_income")],
        [InlineKeyboardButton("Отмена", callback_data="an_cancel")],
    ])
    await q.edit_message_text(
        f"Биржа: {label}\n\nШаг 2/3: Выбери метод анализа:\n\n"
        "Средняя ставка — стандартный фильтр\n"
        "Средний доход — расчёт прибыли в день",
        reply_markup=keyboard,
    )
    return AN_METHOD


async def an_method_btn(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Шаг 3a: метод выбран."""
    q = update.callback_query
    await q.answer()
    if q.data == "an_cancel":
        await q.edit_message_text("Отменено.")
        return ConversationHandler.END
    method = q.data.replace("an_method_", "")
    context.user_data["an_method"] = method
    if method == "income":
        # Метод "Средний доход" → спрашиваем сумму
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("$15,000", callback_data="an_amt_15000"),
             InlineKeyboardButton("$20,000", callback_data="an_amt_20000")],
            [InlineKeyboardButton("$25,000", callback_data="an_amt_25000"),
             InlineKeyboardButton("Другая",  callback_data="an_amt_other")],
            [InlineKeyboardButton("Отмена",  callback_data="an_cancel")],
        ])
        await q.edit_message_text(
            "Метод: Средний доход\n\nШаг: Введи сумму позиции (USDT):",
            reply_markup=keyboard,
        )
        return AN_AMT
    else:
        # Метод "Средняя ставка" → сразу к периоду
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("3 дня",  callback_data="an_days_3"),
             InlineKeyboardButton("7 дней", callback_data="an_days_7")],
            [InlineKeyboardButton("14 дней", callback_data="an_days_14"),
             InlineKeyboardButton("Другой", callback_data="an_days_other")],
            [InlineKeyboardButton("Отмена", callback_data="an_cancel")],
        ])
        await q.edit_message_text(
            "Метод: Средняя ставка\n\nШаг 3/3: Выбери период анализа:",
            reply_markup=keyboard,
        )
        return AN_DAYS


async def an_amt_btn(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Шаг: сумма выбрана кнопкой."""
    q = update.callback_query
    await q.answer()
    if q.data == "an_cancel":
        await q.edit_message_text("Отменено.")
        return ConversationHandler.END
    if q.data == "an_amt_other":
        await q.edit_message_text("Введи сумму в USDT, например `30000`:", parse_mode="Markdown")
        return AN_AMT_NUM
    amount = float(q.data.replace("an_amt_", ""))
    context.user_data["an_amount"] = amount
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("$20",  callback_data="an_thr_20"),
         InlineKeyboardButton("$25",  callback_data="an_thr_25")],
        [InlineKeyboardButton("$40",  callback_data="an_thr_40"),
         InlineKeyboardButton("$50",  callback_data="an_thr_50")],
        [InlineKeyboardButton("Другое", callback_data="an_thr_other")],
        [InlineKeyboardButton("Отмена", callback_data="an_cancel")],
    ])
    await q.edit_message_text(
        f"Сумма: ${amount:,.0f}\n\nМинимальный доход в день:",
        reply_markup=keyboard,
    )
    return AN_THRESH


async def an_amt_num(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Шаг: сумма введена вручную."""
    try:
        amount = float(update.message.text.strip().replace("$","").replace(",",""))
        if amount <= 0: raise ValueError
    except ValueError:
        await update.message.reply_text("Введи число, например `30000`:", parse_mode="Markdown")
        return AN_AMT_NUM
    context.user_data["an_amount"] = amount
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("$20",  callback_data="an_thr_20"),
         InlineKeyboardButton("$25",  callback_data="an_thr_25")],
        [InlineKeyboardButton("$40",  callback_data="an_thr_40"),
         InlineKeyboardButton("$50",  callback_data="an_thr_50")],
        [InlineKeyboardButton("Другое", callback_data="an_thr_other")],
        [InlineKeyboardButton("Отмена", callback_data="an_cancel")],
    ])
    await update.message.reply_text(
        f"Сумма: ${amount:,.0f}\n\nМинимальный доход в день:",
        reply_markup=keyboard,
    )
    return AN_THRESH


async def an_thresh_btn(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Шаг: порог дохода выбран кнопкой."""
    q = update.callback_query
    await q.answer()
    if q.data == "an_cancel":
        await q.edit_message_text("Отменено.")
        return ConversationHandler.END
    if q.data == "an_thr_other":
        await q.edit_message_text("Введи минимальный доход в день ($), например `30`:", parse_mode="Markdown")
        return AN_THRESH_NUM
    threshold = float(q.data.replace("an_thr_", ""))
    context.user_data["an_threshold"] = threshold
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("3 дня",  callback_data="an_days_3"),
         InlineKeyboardButton("7 дней", callback_data="an_days_7")],
        [InlineKeyboardButton("14 дней", callback_data="an_days_14"),
         InlineKeyboardButton("Другой", callback_data="an_days_other")],
        [InlineKeyboardButton("Отмена", callback_data="an_cancel")],
    ])
    await q.edit_message_text(
        f"Порог: ≥${threshold:.0f}/день\n\nШаг 3/3: Выбери период анализа:",
        reply_markup=keyboard,
    )
    return AN_DAYS


async def an_thresh_num(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Шаг: порог введён вручную."""
    try:
        threshold = float(update.message.text.strip().replace("$",""))
        if threshold <= 0: raise ValueError
    except ValueError:
        await update.message.reply_text("Введи число, например `30`:", parse_mode="Markdown")
        return AN_THRESH_NUM
    context.user_data["an_threshold"] = threshold
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("3 дня",  callback_data="an_days_3"),
         InlineKeyboardButton("7 дней", callback_data="an_days_7")],
        [InlineKeyboardButton("14 дней", callback_data="an_days_14"),
         InlineKeyboardButton("Другой", callback_data="an_days_other")],
        [InlineKeyboardButton("Отмена", callback_data="an_cancel")],
    ])
    await update.message.reply_text(
        f"Порог: ≥${threshold:.0f}/день\n\nШаг 3/3: Выбери период анализа:",
        reply_markup=keyboard,
    )
    return AN_DAYS


async def an_days_btn(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Шаг: период выбран кнопкой."""
    q = update.callback_query
    await q.answer()
    if q.data == "an_cancel":
        await q.edit_message_text("Отменено.")
        return ConversationHandler.END
    if q.data == "an_days_other":
        await q.edit_message_text("Введи количество дней числом, например `30`:", parse_mode="Markdown")
        return AN_DAYS_NUM
    days = int(q.data.replace("an_days_", ""))
    context.user_data["an_days"] = days
    await q.edit_message_text("Запускаю скан...")
    await an_run_scan(q, context)
    return ConversationHandler.END


async def an_days_num(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Шаг: период введён вручную."""
    try:
        days = int(update.message.text.strip())
        if days <= 0: raise ValueError
    except ValueError:
        await update.message.reply_text("Введи число, например `30`:", parse_mode="Markdown")
        return AN_DAYS_NUM
    context.user_data["an_days"] = days
    await update.message.reply_text("Запускаю скан...")
    await an_run_scan(update, context)
    return ConversationHandler.END


async def an_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if q:
        await q.answer()
        await q.edit_message_text("Отменено.")
    else:
        await update.message.reply_text("Отменено.")
    return ConversationHandler.END


async def an_run_scan(trigger, context: ContextTypes.DEFAULT_TYPE):
    """Запускает скан с выбранными параметрами."""
    # Получаем message для ответов
    if hasattr(trigger, 'message'):
        msg = trigger.message
    else:
        msg = trigger

    exchange  = context.user_data.get("an_exchange", "phemex")
    method    = context.user_data.get("an_method", "rate")
    days      = context.user_data.get("an_days", SCAN_DAYS)
    amount    = context.user_data.get("an_amount", 0)
    threshold = context.user_data.get("an_threshold", 0)
    chat_id   = msg.chat_id

    if _scan_running.get(chat_id):
        await msg.reply_text("⏳ Скан уже запущен.")
        return

    labels = {ex: EXCHANGE_LABELS.get(ex, ex.upper()) for ex in EXCHANGES_ENABLED}
    label = labels.get(exchange, exchange)
    method_label = "Средняя ставка" if method == "rate" else f"Средний доход (${amount:,.0f}, ≥${threshold:.0f}/день)"

    # Получаем список монет
    try:
        if exchange == "coinw":
            r = requests.get("https://api.coinw.com/v1/perpum/instruments", timeout=15)
            all_coins = [x["base"].upper() for x in r.json().get("data", [])]
        else:
            all_coins = phemex_get_all_symbols()
    except Exception as e:
        await msg.reply_text(f"❌ Ошибка получения списка монет: {e}")
        return

    total = len(all_coins)
    await msg.reply_text(
        f"📋 Скан *{label}* — {total} монет\n"
        f"Метод: *{method_label}*\n"
        f"Период: *{days} дней*\n",
        parse_mode="Markdown"
    )

    _scan_running[chat_id] = True
    now_ms   = int(time.time() * 1000)
    start_ms = now_ms - days * 24 * 60 * 60 * 1000

    fetcher = phemex_fetch if exchange == "phemex" else EXCHANGE_FETCHERS.get(exchange)
    if not fetcher:
        await msg.reply_text(f"❌ Нет fetcher для биржи {exchange}")
        _scan_running[chat_id] = False
        return

    passed  = []
    batches = [all_coins[i:i+SCAN_BATCH] for i in range(0, total, SCAN_BATCH)]

    for batch_idx, batch in enumerate(batches):
        if not _scan_running.get(chat_id):
            await msg.reply_text(
                f"⛔ Скан остановлен на порции {batch_idx+1}/{len(batches)}"
            )
            return

        batch_results = []
        for coin in batch:
            if not _scan_running.get(chat_id):
                break
            try:
                rows, sym = fetcher(coin, start_ms, now_ms)
            except Exception:
                rows = []
            if not rows:
                time.sleep(0.15)
                continue

            rates = [r for _, r in rows]
            if not rates:
                time.sleep(0.15)
                continue

            if method == "rate":
                # ── Стандартный метод ─────────────────────────────────────
                r = analyze_rates(rates)
                if not r or r["category"] == "fail":
                    time.sleep(0.15)
                    continue
                direction = r["direction"]
                ordered_rates = [rate for _, rate in sorted(rows, key=lambda x: x[0])]
                if not recent_trend_ok(ordered_rates, direction):
                    time.sleep(0.15)
                    continue
                key_avg   = r["neg_avg"] if direction == "LONG" else r["pos_avg"]
                outlier   = r["outlier_pct"]
                category  = r["category"]
                batch_results.append((coin, key_avg, outlier, direction, category, None))
                passed.append((coin, key_avg, outlier, direction, category, None))

            else:
                # ── Метод "Средний доход" ─────────────────────────────────
                # Фильтр аномалий > ±0.8%
                clean = [r for r in rates if abs(r) <= AN_ANOMALY_THRESHOLD]
                if not clean:
                    time.sleep(0.15)
                    continue

                neg       = [r for r in clean if r < 0]
                pos       = [r for r in clean if r > 0]
                neg_ratio = len(neg) / len(clean)
                pos_ratio = len(pos) / len(clean)

                avg_rate = sum(clean) / len(clean)
                if avg_rate < 0 and neg_ratio >= MIN_NEG_RATIO:
                    direction = "LONG"
                    key_avg = avg_rate
                    below = sum(1 for r in clean if r <= STABILITY_THRESHOLD)
                    outlier = (len(clean) - below) / len(clean) * 100
                elif avg_rate > 0 and pos_ratio >= MIN_POS_RATIO:
                    direction = "SHORT"
                    key_avg = avg_rate
                    above = sum(1 for r in clean if r >= -STABILITY_THRESHOLD)
                    outlier = (len(clean) - above) / len(clean) * 100
                else:
                    time.sleep(0.15)
                    continue

                # payments_per_day = кол-во выплат за период / кол-во дней
                payments_per_day = len(clean) / days
                daily_income     = amount * abs(key_avg) / 100 * payments_per_day

                if daily_income < threshold:
                    time.sleep(0.15)
                    continue
                ordered_rates = [rate for _, rate in sorted(rows, key=lambda x: x[0])]
                if not recent_trend_ok(ordered_rates, direction):
                    time.sleep(0.15)
                    continue

                batch_results.append((coin, key_avg, outlier, direction, "income", daily_income))
                passed.append((coin, key_avg, outlier, direction, "income", daily_income))

            time.sleep(0.15)

        scanned   = min((batch_idx + 1) * SCAN_BATCH, total)
        remaining = total - scanned

        if batch_results:
            lines = [f"📊 Порция {batch_idx+1}/{len(batches)} [{scanned}/{total} | осталось {remaining}]\n"]
            for coin, avg, op, direction, cat, income in batch_results:
                dir_icon = "🟢" if direction == "LONG" else "🔴"
                if cat == "income":
                    lines.append(f"💰 {dir_icon} `{coin}` avg `{avg:+.4f}%` ~${income:.1f}/день")
                else:
                    cat_icon = "✅" if cat == "full" else "⚡"
                    lines.append(f"{cat_icon} {dir_icon} `{coin}` avg `{avg:+.4f}%` выбр `{op:.0f}%`")
            await msg.reply_text("\n".join(lines), parse_mode="Markdown")
        else:
            await msg.reply_text(
                f"⏳ {scanned}/{total} | найдено: {len(passed)} | осталось {remaining}"
            )

    _scan_running[chat_id] = False

    if not passed:
        await msg.reply_text(
            f"Скан {label} завершён: {total} монет\n\nНичего не найдено за {days} дней.",
        )
        return

    # Итоговый отчёт
    lines = [f"✅ *Скан {label} завершён* — {total} монет за {days} дней\n"]

    if method == "income":
        passed_sorted = sorted(passed, key=lambda x: -(x[5] or 0))
        lines.append(f"💰 *Средний доход* ≥${threshold:.0f}/день ({len(passed_sorted)}):")
        for coin, avg, op, direction, cat, income in passed_sorted:
            dir_icon = "🟢" if direction == "LONG" else "🔴"
            lines.append(f"  {dir_icon} `{coin}` avg `{avg:+.4f}%` ~${income:.1f}/день выбр `{op:.0f}%`")
    else:
        full_longs  = [(c,a,o) for c,a,o,d,cat,_ in passed if d=="LONG"  and cat=="full"]
        full_shorts = [(c,a,o) for c,a,o,d,cat,_ in passed if d=="SHORT" and cat=="full"]
        part_longs  = [(c,a,o) for c,a,o,d,cat,_ in passed if d=="LONG"  and cat=="partial"]
        part_shorts = [(c,a,o) for c,a,o,d,cat,_ in passed if d=="SHORT" and cat=="partial"]
        if full_longs:
            lines.append(f"\n✅ 🟢 *ЛОНГ — ПОДХОДЯТ* ({len(full_longs)}):")
            for c,a,o in sorted(full_longs, key=lambda x: x[1]):
                lines.append(f"  `{c}` avg `{a:+.4f}%` выбр `{o:.0f}%`")
        if full_shorts:
            lines.append(f"\n✅ 🔴 *ШОРТ — ПОДХОДЯТ* ({len(full_shorts)}):")
            for c,a,o in sorted(full_shorts, key=lambda x: -x[1]):
                lines.append(f"  `{c}` avg `{a:+.4f}%` выбр `{o:.0f}%`")
        if part_longs:
            lines.append(f"\n⚡ 🟢 *ЛОНГ — ЧАСТИЧНО* ({len(part_longs)}):")
            for c,a,o in sorted(part_longs, key=lambda x: x[1]):
                lines.append(f"  `{c}` avg `{a:+.4f}%` выбр `{o:.0f}%`")
        if part_shorts:
            lines.append(f"\n⚡ 🔴 *ШОРТ — ЧАСТИЧНО* ({len(part_shorts)}):")
            for c,a,o in sorted(part_shorts, key=lambda x: -x[1]):
                lines.append(f"  `{c}` avg `{a:+.4f}%` выбр `{o:.0f}%`")

    reply = "\n".join(lines)
    if len(reply) > 4000:
        for chunk in [reply[i:i+4000] for i in range(0, len(reply), 4000)]:
            await msg.reply_text(chunk, parse_mode="Markdown")
    else:
        await msg.reply_text(reply, parse_mode="Markdown")

    await send_gemini_scan_review(msg, passed, days)



# ─────────────────────────────────────────────
# HELPERS ДЛЯ КНОПОК
# ─────────────────────────────────────────────

def make_days_keyboard(cb_prefix, extra_short=False):
    """Кнопки выбора периода.
    extra_short=True — добавляет 1 день и 3 дня (для /funding).
    """
    if extra_short:
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("1 день",        callback_data=f"{cb_prefix}_days_1"),
             InlineKeyboardButton("3 дня",         callback_data=f"{cb_prefix}_days_3")],
            [InlineKeyboardButton("7 дней",        callback_data=f"{cb_prefix}_days_7"),
             InlineKeyboardButton("14 дней",       callback_data=f"{cb_prefix}_days_14")],
            [InlineKeyboardButton("Другой период", callback_data=f"{cb_prefix}_days_other")],
            [InlineKeyboardButton("Отмена",        callback_data=f"{cb_prefix}_cancel")],
        ])
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("7 дней",       callback_data=f"{cb_prefix}_days_7"),
         InlineKeyboardButton("14 дней",      callback_data=f"{cb_prefix}_days_14")],
        [InlineKeyboardButton("Другой период", callback_data=f"{cb_prefix}_days_other")],
        [InlineKeyboardButton("Отмена",        callback_data=f"{cb_prefix}_cancel")],
    ])


def make_exchange_keyboard(cb_prefix, selected=None):
    """Кнопки выбора бирж с мультивыбором.
    selected — set выбранных бирж (чекбоксы).
    Внизу кнопки: Все / Подтвердить / Отмена.
    """
    if selected is None:
        selected = set()
    buttons = []
    row = []
    for ex, label in EXCHANGE_LABELS.items():
        if not EXCHANGES_ENABLED.get(ex, False):
            continue
        icon = "✅ " if ex in selected else ""
        row.append(InlineKeyboardButton(
            f"{icon}{label}",
            callback_data=f"{cb_prefix}_ex_{ex}"
        ))
        if len(row) == 3:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    buttons.append([
        InlineKeyboardButton("Все биржи",   callback_data=f"{cb_prefix}_ex_all"),
        InlineKeyboardButton("Подтвердить", callback_data=f"{cb_prefix}_ex_confirm"),
    ])
    buttons.append([InlineKeyboardButton("Отмена", callback_data=f"{cb_prefix}_cancel")])
    return InlineKeyboardMarkup(buttons)


def make_amount_keyboard(cb_prefix):
    """Кнопки выбора суммы."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("15000",  callback_data=f"{cb_prefix}_amt_15000"),
         InlineKeyboardButton("20000",  callback_data=f"{cb_prefix}_amt_20000")],
        [InlineKeyboardButton("25000",  callback_data=f"{cb_prefix}_amt_25000"),
         InlineKeyboardButton("Другое", callback_data=f"{cb_prefix}_amt_other")],
        [InlineKeyboardButton("Отмена", callback_data=f"{cb_prefix}_cancel")],
    ])


# ─────────────────────────────────────────────
# /analyze-coin-match-filter  (ACF)
# ─────────────────────────────────────────────

async def acf_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Шаг 0: быстрый ввод или пошаговый диалог."""
    if context.args:
        coins, days, exchange = parse_tokens(" ".join(context.args))
        if coins:
            await do_analyze(update, coins, days, exchange)
            return ConversationHandler.END
    await update.message.reply_text(
        "🔍 Анализ монет по фильтрам\n\n"
        "Шаг 1/3: Введи название монеты или несколько через пробел\n\n"
        "Отмена: /cancel",
        parse_mode="Markdown"
    )
    return ACF_COIN


async def acf_got_coin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Шаг 1: получили монету(ы), спрашиваем период."""
    coins, _, _ = parse_tokens(update.message.text.strip())
    if not coins:
        await update.message.reply_text("Не распознал монеты. Попробуй: `ENJ` или `ENJ RON`", parse_mode="Markdown")
        return ACF_COIN
    context.user_data["acf_coins"] = coins
    await update.message.reply_text(
        f"Шаг 2/3: Выбери период анализа.",
        reply_markup=make_days_keyboard("acf"),
        parse_mode="Markdown"
    )
    return ACF_DAYS


async def acf_days_btn(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Шаг 2: нажата кнопка периода."""
    q = update.callback_query
    await q.answer()
    if q.data == "acf_cancel":
        await q.edit_message_text("❌ Отменено.")
        return ConversationHandler.END
    if q.data == "acf_days_other":
        await q.edit_message_text("Введи количество дней числом, например `30`:", parse_mode="Markdown")
        return ACF_DAYS_NUM
    days = int(q.data.split("_")[-1])
    context.user_data["acf_days"] = days
    await q.edit_message_text(
        "Шаг 3/3: Выбери биржу.",
        reply_markup=make_exchange_keyboard("acf"),
        parse_mode="Markdown"
    )
    return ACF_EXCH


async def acf_days_num(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Шаг 2b: ввели число дней вручную."""
    try:
        days = int(update.message.text.strip())
        if days <= 0: raise ValueError
    except ValueError:
        await update.message.reply_text("Введи число, например `30`:", parse_mode="Markdown")
        return ACF_DAYS_NUM
    context.user_data["acf_days"] = days
    await update.message.reply_text(
        "Шаг 3/3: Выбери биржу.",
        reply_markup=make_exchange_keyboard("acf"),
        parse_mode="Markdown"
    )
    return ACF_EXCH


async def acf_exchange_btn(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Шаг 3: мультивыбор бирж, запуск после Подтвердить."""
    q = update.callback_query
    await q.answer()

    if q.data == "acf_cancel":
        await q.edit_message_text("Отменено.")
        return ConversationHandler.END

    selected = context.user_data.get("acf_selected_ex", set())

    if q.data == "acf_ex_all":
        selected = set(ex for ex in EXCHANGES_ENABLED if EXCHANGES_ENABLED[ex])
        context.user_data["acf_selected_ex"] = selected
        await q.edit_message_text(
            "Шаг 3/3: Выбери биржу.",
            reply_markup=make_exchange_keyboard("acf", selected),
            parse_mode="Markdown"
        )
        return ACF_EXCH

    if q.data == "acf_ex_confirm":
        if not selected:
            selected = set(ex for ex in EXCHANGES_ENABLED if EXCHANGES_ENABLED[ex])
        coins    = context.user_data.get("acf_coins", [])
        days     = context.user_data.get("acf_days", DEFAULT_DAYS)
        ex_label = ", ".join(EXCHANGE_LABELS.get(e, e) for e in selected)
        await q.edit_message_text(
            f"Анализирую {' '.join(coins)} за {days}д на {ex_label}...",
        )
        exchange = list(selected)[0] if len(selected) == 1 else None
        await do_analyze(q, coins, days, exchange, selected if len(selected) > 1 else None)
        return ConversationHandler.END

    # Переключаем выбор биржи
    ex = q.data.replace("acf_ex_", "")
    if ex in selected:
        selected.discard(ex)
    else:
        selected.add(ex)
    context.user_data["acf_selected_ex"] = selected
    await q.edit_message_text(
        "Шаг 3/3: Выбери биржу.",
        reply_markup=make_exchange_keyboard("acf", selected),
        parse_mode="Markdown"
    )
    return ACF_EXCH


# ─────────────────────────────────────────────
# /funding-rates  (FR)
# ─────────────────────────────────────────────

async def fr_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Шаг 0: быстрый ввод или пошаговый."""
    if context.args:
        coins, days, exchange = parse_tokens(" ".join(context.args))
        if coins:
            await do_show(update, coins[0], days, exchange)
            return ConversationHandler.END
    await update.message.reply_text(
        "📈 Ставки фандинга по монете\n\n"
        "Шаг 1/3: Введи название монеты\n\n"
        "Отмена: /cancel",
        parse_mode="Markdown"
    )
    return FR_COIN


async def fr_got_coin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    coins, _, _ = parse_tokens(update.message.text.strip())
    if not coins:
        await update.message.reply_text("Не распознал монету. Попробуй: `ENJ`", parse_mode="Markdown")
        return FR_COIN
    context.user_data["fr_coin"] = coins[0]
    await update.message.reply_text(
        "Шаг 2/3: Выбери период анализа.",
        reply_markup=make_days_keyboard("fr", extra_short=True),
        parse_mode="Markdown"
    )
    return FR_DAYS


async def fr_days_btn(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if q.data == "fr_cancel":
        await q.edit_message_text("❌ Отменено.")
        return ConversationHandler.END
    if q.data == "fr_days_other":
        await q.edit_message_text("Введи количество дней числом, например `30`:", parse_mode="Markdown")
        return FR_DAYS_NUM
    days = int(q.data.split("_")[-1])
    context.user_data["fr_days"] = days
    await q.edit_message_text(
        "Шаг 3/3: Выбери биржу.",
        reply_markup=make_exchange_keyboard("fr"),
        parse_mode="Markdown"
    )
    return FR_EXCH


async def fr_days_num(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        days = int(update.message.text.strip())
        if days <= 0: raise ValueError
    except ValueError:
        await update.message.reply_text("Введи число, например `30`:", parse_mode="Markdown")
        return FR_DAYS_NUM
    context.user_data["fr_days"] = days
    await update.message.reply_text(
        "Шаг 3/3: Выбери биржу.",
        reply_markup=make_exchange_keyboard("fr"),
        parse_mode="Markdown"
    )
    return FR_EXCH


async def fr_exchange_btn(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    if q.data == "fr_cancel":
        await q.edit_message_text("Отменено.")
        return ConversationHandler.END

    selected = context.user_data.get("fr_selected_ex", set())

    if q.data == "fr_ex_all":
        selected = set(ex for ex in EXCHANGES_ENABLED if EXCHANGES_ENABLED[ex])
        context.user_data["fr_selected_ex"] = selected
        await q.edit_message_text(
            "Шаг 3/3: Выбери биржу.",
            reply_markup=make_exchange_keyboard("fr", selected),
            parse_mode="Markdown"
        )
        return FR_EXCH

    if q.data == "fr_ex_confirm":
        if not selected:
            selected = set(ex for ex in EXCHANGES_ENABLED if EXCHANGES_ENABLED[ex])
        coin     = context.user_data.get("fr_coin", "")
        days     = context.user_data.get("fr_days", DEFAULT_DAYS)
        ex_label = ", ".join(EXCHANGE_LABELS.get(e, e) for e in selected)
        await q.edit_message_text(f"Загружаю ставки {coin} за {days}д на {ex_label}...")
        exchange = list(selected)[0] if len(selected) == 1 else None
        await do_show(q, coin, days, exchange)
        return ConversationHandler.END

    ex = q.data.replace("fr_ex_", "")
    if ex in selected:
        selected.discard(ex)
    else:
        selected.add(ex)
    context.user_data["fr_selected_ex"] = selected
    await q.edit_message_text(
        "Шаг 3/3: Выбери биржу.",
        reply_markup=make_exchange_keyboard("fr", selected),
        parse_mode="Markdown"
    )
    return FR_EXCH


# ─────────────────────────────────────────────
# /profit-calculator  (PC)
# ─────────────────────────────────────────────

async def pc_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Шаг 0: быстрый ввод или пошаговый."""
    if context.args:
        coins, days, exchange = parse_tokens(" ".join(context.args))
        amount = None
        remaining = []
        for p in coins:
            try:
                amount = float(p.replace("$","").replace(",",""))
            except ValueError:
                remaining.append(p)
        if remaining and amount:
            await do_calc(update, remaining[0], amount, days, exchange)
            return ConversationHandler.END
    await update.message.reply_text(
        "💰 Калькулятор дохода от фандинга\n\n"
        "Шаг 1/4: Введи название монеты\n\n"
        "Отмена: /cancel",
        parse_mode="Markdown"
    )
    return PC_COIN


async def pc_got_coin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    coins, _, _ = parse_tokens(update.message.text.strip())
    if not coins:
        await update.message.reply_text("Не распознал монету. Попробуй: `ENJ`", parse_mode="Markdown")
        return PC_COIN
    context.user_data["pc_coin"] = coins[0]
    await update.message.reply_text(
        "Шаг 2/4: Выбери сумму позиции (USDT).",
        reply_markup=make_amount_keyboard("pc"),
        parse_mode="Markdown"
    )
    return PC_AMT


async def pc_amt_btn(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if q.data == "pc_cancel":
        await q.edit_message_text("❌ Отменено.")
        return ConversationHandler.END
    if q.data == "pc_amt_other":
        await q.edit_message_text("Введи сумму в USDT, например `50000`:", parse_mode="Markdown")
        return PC_AMT_NUM
    amount = float(q.data.split("_")[-1])
    context.user_data["pc_amount"] = amount
    await q.edit_message_text(
        f"Сумма: *${amount:,.0f}*\\n\\nШаг 3/4: Выбери период:",
        reply_markup=make_days_keyboard("pc"),
        parse_mode="Markdown"
    )
    return PC_DAYS


async def pc_amt_num(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        amount = float(update.message.text.strip().replace("$","").replace(",",""))
        if amount <= 0: raise ValueError
    except ValueError:
        await update.message.reply_text("Введи сумму числом, например `50000`:", parse_mode="Markdown")
        return PC_AMT_NUM
    context.user_data["pc_amount"] = amount
    await update.message.reply_text(
        f"Сумма: *${amount:,.0f}*\\n\\nШаг 3/4: Выбери период:",
        reply_markup=make_days_keyboard("pc"),
        parse_mode="Markdown"
    )
    return PC_DAYS


async def pc_days_btn(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if q.data == "pc_cancel":
        await q.edit_message_text("❌ Отменено.")
        return ConversationHandler.END
    if q.data == "pc_days_other":
        await q.edit_message_text("Введи количество дней числом, например `30`:", parse_mode="Markdown")
        return PC_DAYS_NUM
    days = int(q.data.split("_")[-1])
    context.user_data["pc_days"] = days
    await q.edit_message_text(
        f"Период: *{days} дн.*\\n\\nШаг 4/4: Выбери биржу:",
        reply_markup=make_exchange_keyboard("pc"),
        parse_mode="Markdown"
    )
    return PC_EXCH


async def pc_days_num(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        days = int(update.message.text.strip())
        if days <= 0: raise ValueError
    except ValueError:
        await update.message.reply_text("Введи число, например `30`:", parse_mode="Markdown")
        return PC_DAYS_NUM
    context.user_data["pc_days"] = days
    await update.message.reply_text(
        f"Период: *{days} дн.*\\n\\nШаг 4/4: Выбери биржу:",
        reply_markup=make_exchange_keyboard("pc"),
        parse_mode="Markdown"
    )
    return PC_EXCH


async def pc_exchange_btn(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    if q.data == "pc_cancel":
        await q.edit_message_text("Отменено.")
        return ConversationHandler.END

    selected = context.user_data.get("pc_selected_ex", set())

    if q.data == "pc_ex_all":
        selected = set(ex for ex in EXCHANGES_ENABLED if EXCHANGES_ENABLED[ex])
        context.user_data["pc_selected_ex"] = selected
        await q.edit_message_text(
            "Шаг 4/4: Выбери биржу.",
            reply_markup=make_exchange_keyboard("pc", selected),
            parse_mode="Markdown"
        )
        return PC_EXCH

    if q.data == "pc_ex_confirm":
        if not selected:
            selected = set(ex for ex in EXCHANGES_ENABLED if EXCHANGES_ENABLED[ex])
        coin   = context.user_data.get("pc_coin", "")
        amount = context.user_data.get("pc_amount", 0)
        days   = context.user_data.get("pc_days", DEFAULT_DAYS)
        ex_label = ", ".join(EXCHANGE_LABELS.get(e, e) for e in selected)
        await q.edit_message_text(f"Считаю доход {coin} ${amount:,.0f} за {days}д на {ex_label}...")
        exchange = list(selected)[0] if len(selected) == 1 else None
        await do_calc(q, coin, amount, days, exchange)
        return ConversationHandler.END

    ex = q.data.replace("pc_ex_", "")
    if ex in selected:
        selected.discard(ex)
    else:
        selected.add(ex)
    context.user_data["pc_selected_ex"] = selected
    await q.edit_message_text(
        "Шаг 4/4: Выбери биржу.",
        reply_markup=make_exchange_keyboard("pc", selected),
        parse_mode="Markdown"
    )
    return PC_EXCH


# ─────────────────────────────────────────────
# /settings — объединённые настройки + биржи
# ─────────────────────────────────────────────

def make_settings_keyboard():
    """Кнопки настроек: каждая биржа + Все ВКЛ/ВЫКЛ."""
    buttons = []
    row = []
    for ex, enabled in EXCHANGES_ENABLED.items():
        label = EXCHANGE_LABELS.get(ex, ex.upper())
        icon  = "✅" if enabled else "❌"
        row.append(InlineKeyboardButton(f"{icon} {label}", callback_data=f"set_ex_{ex}"))
        if len(row) == 3:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    buttons.append([
        InlineKeyboardButton("✅ Все ВКЛ",  callback_data="set_ex_all_on"),
        InlineKeyboardButton("❌ Все ВЫКЛ", callback_data="set_ex_all_off"),
    ])
    buttons.append([InlineKeyboardButton("✖️ Закрыть", callback_data="set_close")])
    return InlineKeyboardMarkup(buttons)


def settings_text():
    active = [EXCHANGE_LABELS.get(e, e) for e, on in EXCHANGES_ENABLED.items() if on]
    return (
        "⚙️ *Настройки*\\n\\n"
        f"Период по умолчанию: `{DEFAULT_DAYS}` дней\\n"
        f"Порог ставки: `{STABILITY_THRESHOLD}%`\\n"
        f"Макс. выбросов: `{MAX_OUTLIER_PCT}%`\\n"
        f"Neg avg порог: `{NEG_AVG_THRESHOLD}%`\\n"
        f"Gemini AI: `{'включён' if GEMINI_API_KEY else 'не настроен'}`\\n"
        f"Вечерний отчёт 20:00 Киев: `{'включён' if REPORT_CHAT_ID else 'не настроен'}`\\n\\n"
        "Категории:\\n"
        "✅ *ПОДХОДИТ* — стабильность ок\\n"
        "⚡ *ЧАСТИЧНО* — neg\\_avg сильный, нестабильно\\n"
        "❌ *НЕ ПОДХОДИТ* — не прошла\\n\\n"
        "*Биржи* (нажми чтобы вкл/выкл):\\n"
        f"Активных: {len(active)} из {len(EXCHANGES_ENABLED)}"
    )


async def cmd_settings_new(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        settings_text(),
        reply_markup=make_settings_keyboard(),
        parse_mode="Markdown"
    )


async def settings_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    if q.data == "set_close":
        await q.edit_message_text("⚙️ Настройки закрыты.")
        return

    if q.data == "set_ex_all_on":
        for ex in EXCHANGES_ENABLED:
            EXCHANGES_ENABLED[ex] = True
    elif q.data == "set_ex_all_off":
        for ex in EXCHANGES_ENABLED:
            EXCHANGES_ENABLED[ex] = False
    elif q.data.startswith("set_ex_"):
        ex = q.data.replace("set_ex_", "")
        if ex in EXCHANGES_ENABLED:
            EXCHANGES_ENABLED[ex] = not EXCHANGES_ENABLED[ex]

    # Обновляем сообщение с новыми кнопками
    try:
        await q.edit_message_text(
            settings_text(),
            reply_markup=make_settings_keyboard(),
            parse_mode="Markdown"
        )
    except Exception:
        pass  # Если текст не изменился — игнорируем


# ─────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────

async def delta_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.args:
        coins, days, exchange = parse_tokens(" ".join(context.args))
        if coins:
            await do_delta(update, coins, days)
            return ConversationHandler.END
    await update.message.reply_text(
        "\u0412\u0432\u0435\u0434\u0438 \u043c\u043e\u043d\u0435\u0442\u044b \u0434\u043b\u044f \u0434\u0435\u043b\u044c\u0442\u0430-\u0430\u043d\u0430\u043b\u0438\u0437\u0430:\
\
"
        "`ENJ`\
"
        "`ENJ JTO RON`\
"
        "`ENJ /days 14`\
\
"
        "\u041e\u0442\u043c\u0435\u043d\u0430: /cancel",
        parse_mode="Markdown"
    )
    return WAIT_DELTA_COIN



async def delta_got_coins(update: Update, context: ContextTypes.DEFAULT_TYPE):
    coins, days, _ = parse_tokens(update.message.text.strip())
    if not coins:
        await update.message.reply_text("\u041d\u0435 \u0440\u0430\u0441\u043f\u043e\u0437\u043d\u0430\u043b \u043c\u043e\u043d\u0435\u0442\u044b. \u041f\u043e\u043f\u0440\u043e\u0431\u0443\u0439: `ENJ JTO`", parse_mode="Markdown")
        return WAIT_DELTA_COIN
    await do_delta(update, coins, days)
    return ConversationHandler.END



async def do_delta(update, coins, days, amount_usd=None):
    active = [e for e, on in EXCHANGES_ENABLED.items() if on]
    if len(active) < 2:
        await update.message.reply_text(
            "\u274c \u041d\u0443\u0436\u043d\u043e \u043c\u0438\u043d\u0438\u043c\u0443\u043c 2 \u0430\u043a\u0442\u0438\u0432\u043d\u044b\u0435 \u0431\u0438\u0440\u0436\u0438 \u0434\u043b\u044f \u0434\u0435\u043b\u044c\u0442\u0430-\u0430\u043d\u0430\u043b\u0438\u0437\u0430.\
"
            "\u0412\u043a\u043b\u044e\u0447\u0438 \u0431\u0438\u0440\u0436\u0438 \u0447\u0435\u0440\u0435\u0437 `/settings`",
            parse_mode="Markdown"
        )
        return

    ex_str = " + ".join(EXCHANGE_LABELS.get(e, e) for e in active)
    suffix = f" | \u0441\u0443\u043c\u043c\u0430 ${amount_usd:,.0f}" if amount_usd else ""
    await update.message.reply_text(
        f"\u2696\ufe0f \u0414\u0435\u043b\u044c\u0442\u0430-\u0430\u043d\u0430\u043b\u0438\u0437 {len(coins)} \u043c\u043e\u043d\u0435\u0442 \u043d\u0430 {ex_str} \u0437\u0430 {days} \u0434\u043d\u0435\u0439{suffix}...",
    )

    for coin in coins:
        pairs, _ = analyze_delta(coin, days, all_exchanges=active)
        reply = fmt_delta_result(coin, pairs, days, amount_usd)

        # \u041d\u0430\u0440\u0435\u0437\u0430\u0435\u043c \u0435\u0441\u043b\u0438 \u0434\u043b\u0438\u043d\u043d\u043e\u0435
        if len(reply) > 4000:
            for chunk in [reply[i:i+4000] for i in range(0, len(reply), 4000)]:
                await update.message.reply_text(chunk, parse_mode="Markdown")
        else:
            await update.message.reply_text(reply, parse_mode="Markdown")



# ─────────────────────────────────────────────
# /ai — фундаментальный анализ монеты через Gemini
# ─────────────────────────────────────────────

async def do_ai_multiple(update, coins):
    msg = update.message if hasattr(update, "message") and update.message else update
    if not GEMINI_API_KEY:
        await msg.reply_text("❌ GEMINI_API_KEY не задан. AI-анализ недоступен.")
        return
    clean_coins = [c.upper() for c in coins if c.strip()]
    if not clean_coins:
        await msg.reply_text("Введи монету, например `SOL` или `SOL ENJ`.", parse_mode="Markdown")
        return

    await msg.reply_text(f"🤖 Начинаю AI-анализ {len(clean_coins)} монет...")
    for idx, coin in enumerate(clean_coins):
        await msg.reply_text(f"🔍 Анализирую *{coin}*...", parse_mode="Markdown")
        answer = gemini_analyze_single(coin)
        if not answer:
            await msg.reply_text(f"❌ Gemini не ответил по {coin}.")
            continue
        text = f"🤖 AI-анализ {coin}\n\n{answer}"
        for chunk in [text[i:i+4000] for i in range(0, len(text), 4000)]:
            await msg.reply_text(chunk)
        if idx < len(clean_coins) - 1:
            time.sleep(3)


async def ai_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.args:
        await do_ai_multiple(update, context.args)
        return ConversationHandler.END
    await update.message.reply_text(
        "Введи монеты для AI-анализа, например:\n\n"
        "`SOL`\n"
        "`SOL ENJ RON`\n\n"
        "AI оценивает фундаментал, ликвидность, волатильность и риски. "
        "Ставки фандинга он не анализирует.",
        parse_mode="Markdown",
    )
    return WAIT_AI_COIN


async def ai_got_coin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await do_ai_multiple(update, update.message.text.split())
    return ConversationHandler.END



def main():
    if not BOT_TOKEN:
        raise ValueError("BOT_TOKEN не задан!")

    app = Application.builder().token(BOT_TOKEN).build()

    # /analyze-coin-match-filter
    acf_conv = ConversationHandler(
        entry_points=[CommandHandler("filter", acf_start)],
        states={
            ACF_COIN:     [MessageHandler(filters.TEXT & ~filters.COMMAND, acf_got_coin)],
            ACF_DAYS:     [CallbackQueryHandler(acf_days_btn, pattern="^acf_")],
            ACF_DAYS_NUM: [MessageHandler(filters.TEXT & ~filters.COMMAND, acf_days_num)],
            ACF_EXCH:     [CallbackQueryHandler(acf_exchange_btn, pattern="^acf_")],
        },
        fallbacks=[CommandHandler("cancel", cmd_cancel)]
    )
    # /funding-rates
    fr_conv = ConversationHandler(
        entry_points=[CommandHandler("funding", fr_start)],
        states={
            FR_COIN:     [MessageHandler(filters.TEXT & ~filters.COMMAND, fr_got_coin)],
            FR_DAYS:     [CallbackQueryHandler(fr_days_btn, pattern="^fr_")],
            FR_DAYS_NUM: [MessageHandler(filters.TEXT & ~filters.COMMAND, fr_days_num)],
            FR_EXCH:     [CallbackQueryHandler(fr_exchange_btn, pattern="^fr_")],
        },
        fallbacks=[CommandHandler("cancel", cmd_cancel)]
    )
    # /profit-calculator
    pc_conv = ConversationHandler(
        entry_points=[CommandHandler("calculator", pc_start)],
        states={
            PC_COIN:     [MessageHandler(filters.TEXT & ~filters.COMMAND, pc_got_coin)],
            PC_AMT:      [CallbackQueryHandler(pc_amt_btn, pattern="^pc_")],
            PC_AMT_NUM:  [MessageHandler(filters.TEXT & ~filters.COMMAND, pc_amt_num)],
            PC_DAYS:     [CallbackQueryHandler(pc_days_btn, pattern="^pc_")],
            PC_DAYS_NUM: [MessageHandler(filters.TEXT & ~filters.COMMAND, pc_days_num)],
            PC_EXCH:     [CallbackQueryHandler(pc_exchange_btn, pattern="^pc_")],
        },
        fallbacks=[CommandHandler("cancel", cmd_cancel)]
    )

    app.add_handler(CommandHandler("start",     cmd_start))
    app.add_handler(CommandHandler("help",      cmd_help))
    app.add_handler(CommandHandler("settings",  cmd_settings_new))
    app.add_handler(CallbackQueryHandler(settings_callback, pattern="^set_"))
    # /analyze ConversationHandler
    analyze_conv = ConversationHandler(
        entry_points=[CommandHandler("analyze", cmd_analyze_start)],
        states={
            AN_METHOD:     [CallbackQueryHandler(an_exchange_btn,  pattern="^an_ex_"),
                            CallbackQueryHandler(an_method_btn,    pattern="^an_method_"),
                            CallbackQueryHandler(an_cancel,        pattern="^an_cancel$")],
            AN_AMT:        [CallbackQueryHandler(an_amt_btn,       pattern="^an_amt_"),
                            CallbackQueryHandler(an_cancel,        pattern="^an_cancel$")],
            AN_AMT_NUM:    [MessageHandler(filters.TEXT & ~filters.COMMAND, an_amt_num)],
            AN_THRESH:     [CallbackQueryHandler(an_thresh_btn,    pattern="^an_thr_"),
                            CallbackQueryHandler(an_cancel,        pattern="^an_cancel$")],
            AN_THRESH_NUM: [MessageHandler(filters.TEXT & ~filters.COMMAND, an_thresh_num)],
            AN_DAYS:       [CallbackQueryHandler(an_days_btn,      pattern="^an_days_"),
                            CallbackQueryHandler(an_cancel,        pattern="^an_cancel$")],
            AN_DAYS_NUM:   [MessageHandler(filters.TEXT & ~filters.COMMAND, an_days_num)],
        },
        fallbacks=[CommandHandler("cancel", cmd_cancel)],
    )
    app.add_handler(analyze_conv)
    app.add_handler(CommandHandler("cancel",    cmd_cancel))

    delta_conv = ConversationHandler(
        entry_points=[CommandHandler("findpair", delta_start)],
        states={WAIT_DELTA_COIN: [MessageHandler(filters.TEXT & ~filters.COMMAND, delta_got_coins)]},
        fallbacks=[CommandHandler("cancel", cmd_cancel)],
    )

    ai_conv = ConversationHandler(
        entry_points=[CommandHandler("ai", ai_start)],
        states={WAIT_AI_COIN: [MessageHandler(filters.TEXT & ~filters.COMMAND, ai_got_coin)]},
        fallbacks=[CommandHandler("cancel", cmd_cancel)],
    )

    # ConversationHandlers — должны быть зарегистрированы до MessageHandler(unknown)
    app.add_handler(acf_conv)
    app.add_handler(fr_conv)
    app.add_handler(pc_conv)
    app.add_handler(delta_conv)
    app.add_handler(ai_conv)

    if REPORT_CHAT_ID and app.job_queue:
        app.job_queue.run_daily(auto_scan_job, time=dt_time(hour=17, minute=0, second=0, tzinfo=timezone.utc))

    app.add_handler(MessageHandler(filters.COMMAND, unknown))

    print("Бот запущен...")
    if REPORT_CHAT_ID:
        print("Вечерний отчёт включён: 20:00 Europe/Kyiv / 17:00 UTC")
    app.run_polling()


if __name__ == "__main__":
    main()

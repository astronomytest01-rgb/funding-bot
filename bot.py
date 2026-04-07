"""
Phemex Funding Rate Telegram Bot
"""

import os
import time
import requests
from datetime import datetime, timezone
from telegram import Update
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    filters, ContextTypes, ConversationHandler
)

# ─────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")

DEFAULT_DAYS        = 7
STABILITY_THRESHOLD = -0.04
MAX_OUTLIER_PCT     = 25
NEG_AVG_THRESHOLD   = -0.08

BASE_URL = "https://api.phemex.com"

# Состояния диалога
WAIT_ANALYZE_COINS = 1
WAIT_ANALYZE_DAYS  = 2
WAIT_SHOW_COIN     = 3
WAIT_SHOW_DAYS     = 4
WAIT_CALC_COIN     = 5
WAIT_CALC_AMOUNT   = 6
WAIT_CALC_DAYS     = 7

# ─────────────────────────────────────────────
# API
# ─────────────────────────────────────────────

def get_funding_history(symbol, start_ms, end_ms):
    url = f"{BASE_URL}/api-data/public/data/funding-rate-history"
    params = {"symbol": symbol, "end": end_ms, "limit": 1000}
    resp = requests.get(url, params=params, timeout=10)
    resp.raise_for_status()
    data = resp.json()
    if data.get("code") != 0:
        raise ValueError(f"API error: {data.get('msg')}")
    rows = data.get("data", {}).get("rows", [])
    return [r for r in rows if r["fundingTime"] >= start_ms]


def funding_symbols(coin):
    coin = coin.upper().strip()
    if coin.endswith("USDT") or coin.endswith("USD"):
        return [f".{coin}FR8H"]
    return [f".{coin}USDTFR8H", f".{coin}USDFR8H"]


def fetch_rows(coin, start_ms, end_ms):
    last_error = None
    for sym in funding_symbols(coin):
        try:
            rows = get_funding_history(sym, start_ms, end_ms)
            if rows:
                return rows, sym
        except Exception as e:
            last_error = str(e)
        time.sleep(0.15)
    return [], last_error


def analyze_coin(coin, start_ms, end_ms):
    rows, sym_or_err = fetch_rows(coin, start_ms, end_ms)
    if not rows:
        return {"coin": coin, "error": sym_or_err or "Нет данных", "sym": None}

    rates = [float(r["fundingRate"]) * 100 for r in rows]
    neg_rates = [r for r in rates if r < 0]
    total = len(rates)

    below = sum(1 for r in rates if r <= STABILITY_THRESHOLD)
    outlier_pct = (total - below) / total * 100
    pass_stability = outlier_pct <= MAX_OUTLIER_PCT

    neg_avg = sum(neg_rates) / len(neg_rates) if neg_rates else 0.0
    pass_neg_avg = bool(neg_rates) and neg_avg <= NEG_AVG_THRESHOLD

    if pass_stability:
        category = "full"
    elif pass_neg_avg:
        category = "partial"
    else:
        category = "fail"

    return {
        "coin": coin, "sym": sym_or_err, "rates": rates,
        "total": total, "avg": sum(rates) / total,
        "neg_avg": neg_avg, "neg_count": len(neg_rates),
        "min": min(rates), "max": max(rates),
        "outlier_pct": outlier_pct,
        "pass_stability": pass_stability, "pass_neg_avg": pass_neg_avg,
        "category": category, "error": None,
    }


def parse_tokens(text):
    """Парсит строку вида 'BTC ETH SOL --days 14' -> (coins, days)"""
    parts = text.strip().split()
    days = DEFAULT_DAYS
    coins = []
    i = 0
    while i < len(parts):
        if parts[i].lower() == "--days" and i + 1 < len(parts):
            try:
                days = int(parts[i + 1])
                i += 2
                continue
            except ValueError:
                pass
        coins.append(parts[i].upper())
        i += 1
    return coins, days


async def do_analyze(update, coins, days):
    await update.message.reply_text(f"🔍 Анализирую {len(coins)} монет за {days} дней...")

    now_ms = int(time.time() * 1000)
    start_ms = now_ms - days * 24 * 60 * 60 * 1000

    results = [analyze_coin(c, start_ms, now_ms) for c in coins]

    full    = [r for r in results if r.get("category") == "full"]
    partial = [r for r in results if r.get("category") == "partial"]
    fail    = [r for r in results if r.get("category") == "fail"]
    errored = [r for r in results if r.get("error")]

    lines = [f"📊 *Результат анализа* — {days} дней\n"]

    if full:
        lines.append(f"✅ *ПОДХОДЯТ* ({len(full)}):")
        for r in sorted(full, key=lambda x: x["neg_avg"]):
            s1 = "✓" if r["pass_stability"] else "✗"
            s2 = "✓" if r["pass_neg_avg"] else "✗"
            lines.append(
                f"  `{r['coin']:<8}` avg `{r['avg']:+.4f}%`  neg\\_avg `{r['neg_avg']:+.4f}%`  "
                f"выбр `{r['outlier_pct']:.0f}%`  [стаб:{s1} neg:{s2}]"
            )
    if partial:
        lines.append(f"\n⚡ *ЧАСТИЧНО* ({len(partial)}):")
        for r in sorted(partial, key=lambda x: x["neg_avg"]):
            lines.append(
                f"  `{r['coin']:<8}` avg `{r['avg']:+.4f}%`  neg\\_avg `{r['neg_avg']:+.4f}%`  "
                f"выбр `{r['outlier_pct']:.0f}%`"
            )
    if fail:
        lines.append(f"\n❌ *НЕ ПОДХОДЯТ* ({len(fail)}):")
        for r in sorted(fail, key=lambda x: x["neg_avg"]):
            lines.append(
                f"  `{r['coin']:<8}` avg `{r['avg']:+.4f}%`  neg\\_avg `{r['neg_avg']:+.4f}%`  "
                f"выбр `{r['outlier_pct']:.0f}%`"
            )
    if errored:
        lines.append(f"\n⚠️ *Ошибки* ({len(errored)}):")
        for r in errored:
            lines.append(f"  `{r['coin']}` — {r['error']}")

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def do_show(update, coin, days):
    await update.message.reply_text(f"🔍 Загружаю ставки {coin} за {days} дней...")

    now_ms = int(time.time() * 1000)
    start_ms = now_ms - days * 24 * 60 * 60 * 1000

    rows, sym_or_err = fetch_rows(coin, start_ms, now_ms)
    if not rows:
        await update.message.reply_text(f"❌ Ошибка: {sym_or_err}")
        return

    rates = [float(r["fundingRate"]) * 100 for r in rows]
    neg_rates = [r for r in rates if r < 0]
    avg = sum(rates) / len(rates)
    neg_avg = sum(neg_rates) / len(neg_rates) if neg_rates else 0.0

    header = (
        f"📈 *{coin}* — `{sym_or_err}` — {days} дней\n"
        f"Ставок: `{len(rows)}`  |  Avg: `{avg:+.4f}%`  |  Neg avg: `{neg_avg:+.4f}%`\n"
        f"Min: `{min(rates):+.4f}%`  |  Max: `{max(rates):+.4f}%`\n\n"
        f"`{'Время (UTC)':<17} {'Ставка':>10}`\n"
        f"`{'─'*29}`\n"
    )

    rate_lines = []
    for r in reversed(rows):
        ts = datetime.fromtimestamp(r["fundingTime"] / 1000, tz=timezone.utc)
        rate = float(r["fundingRate"]) * 100
        marker = " ◀" if rate <= STABILITY_THRESHOLD else ""
        rate_lines.append(f"`{ts.strftime('%m-%d %H:%M'):<12} {rate:>+10.4f}%{marker}`")

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


# ─────────────────────────────────────────────
# CONVERSATION: /analyze
# ─────────────────────────────────────────────

async def analyze_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.args:
        coins, days = parse_tokens(" ".join(context.args))
        if coins:
            await do_analyze(update, coins, days)
            return ConversationHandler.END

    await update.message.reply_text(
        "Введи названия монет через пробел:\n\n"
        "Например: `BTC ETH SOL ENJ RON`\n\n"
        "Или с периодом: `BTC ETH --days 14`\n\n"
        "Отмена: /cancel",
        parse_mode="Markdown"
    )
    return WAIT_ANALYZE_COINS


async def analyze_got_coins(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if not text:
        await update.message.reply_text("Пожалуйста, введи названия монет.")
        return WAIT_ANALYZE_COINS

    coins, days = parse_tokens(text)
    if not coins:
        await update.message.reply_text("Не распознал монеты. Попробуй ещё раз, например: `BTC ETH SOL`", parse_mode="Markdown")
        return WAIT_ANALYZE_COINS

    await do_analyze(update, coins, days)
    return ConversationHandler.END


# ─────────────────────────────────────────────
# CONVERSATION: /show
# ─────────────────────────────────────────────

async def show_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.args:
        coins, days = parse_tokens(" ".join(context.args))
        if coins:
            await do_show(update, coins[0], days)
            return ConversationHandler.END

    await update.message.reply_text(
        "Введи название монеты:\n\n"
        "Например: `ENJ`\n\n"
        "Или с периодом: `ENJ --days 14`\n\n"
        "Отмена: /cancel",
        parse_mode="Markdown"
    )
    return WAIT_SHOW_COIN


async def show_got_coin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    coins, days = parse_tokens(text)
    if not coins:
        await update.message.reply_text("Не распознал монету. Попробуй ещё раз, например: `ENJ`", parse_mode="Markdown")
        return WAIT_SHOW_COIN

    await do_show(update, coins[0], days)
    return ConversationHandler.END


# ─────────────────────────────────────────────
# ПРОСТЫЕ КОМАНДЫ
# ─────────────────────────────────────────────

async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Отменено.")
    return ConversationHandler.END


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "👋 *Phemex Funding Rate Analyzer*\n\n"
        "Команды:\n"
        "/analyze — анализ монет по фильтрам\n"
        "/show — посмотреть все ставки по монете\n"
        "/calc — калькулятор дохода от фандинга\n"
        "/settings — текущие настройки\n"
        "/help — эта справка\n\n"
        "Можно писать сразу с аргументами:\n"
        "`/analyze BTC ETH SOL`\n"
        "`/show ENJ --days 14`\n"
        "`/calc ENJ 25000 --days 7`"
    )
    await update.message.reply_text(text, parse_mode="Markdown")


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await cmd_start(update, context)


async def cmd_settings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "⚙️ *Текущие настройки*\n\n"
        f"Период по умолчанию: `{DEFAULT_DAYS}` дней\n"
        f"Порог ставки: `{STABILITY_THRESHOLD}%`\n"
        f"Макс. выбросов: `{MAX_OUTLIER_PCT}%`\n"
        f"Neg avg порог: `{NEG_AVG_THRESHOLD}%`\n\n"
        "Категории:\n"
        "✅ *ПОДХОДИТ* — стабильность ок\n"
        "⚡ *ЧАСТИЧНО* — нестабильно, но neg\\_avg сильный\n"
        "❌ *НЕ ПОДХОДИТ* — не прошла ни один критерий"
    )
    await update.message.reply_text(text, parse_mode="Markdown")


async def unknown(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Не знаю такой команды. Напиши /help.")



# ─────────────────────────────────────────────
# CALC: доход от фандинга
# ─────────────────────────────────────────────

def calc_funding_income(rows, amount_usd):
    """
    Считает доход от шорта за каждую отрицательную ставку.
    При отрицательном фандинге шорт ПОЛУЧАЕТ выплату.
    выплата = amount * abs(rate/100)
    """
    by_day = {}
    total_income = 0.0

    for r in rows:
        rate = float(r["fundingRate"]) * 100
        if rate >= 0:
            continue  # платим сами — пропускаем
        payment = amount_usd * abs(rate / 100)
        total_income += payment

        ts = datetime.fromtimestamp(r["fundingTime"] / 1000, tz=timezone.utc)
        day = ts.strftime("%Y-%m-%d")
        by_day[day] = by_day.get(day, 0.0) + payment

    return total_income, by_day


async def do_calc(update, coin, amount_usd, days):
    await update.message.reply_text(f"🔍 Считаю доход по {coin} за {days} дней...")

    now_ms = int(time.time() * 1000)
    start_ms = now_ms - days * 24 * 60 * 60 * 1000

    rows, sym_or_err = fetch_rows(coin, start_ms, now_ms)
    if not rows:
        await update.message.reply_text(f"❌ Ошибка: {sym_or_err}")
        return

    total_income, by_day = calc_funding_income(rows, amount_usd)
    income_per_day = total_income / days if days > 0 else 0

    # Считаем ставки для контекста
    rates = [float(r["fundingRate"]) * 100 for r in rows]
    neg_rates = [r for r in rates if r < 0]
    neg_avg = sum(neg_rates) / len(neg_rates) if neg_rates else 0.0
    total_payments = len(neg_rates)

    lines = [
        f"💰 *Калькулятор фандинга — {coin}*\n",
        f"Сумма позиции: `${amount_usd:,.0f}`",
        f"Период: `{days}` дней",
        f"Выплат получено: `{total_payments}` из `{len(rows)}` ставок\n",
        f"📈 *Итого заработано: `${total_income:.2f}`*",
        f"📅 В среднем в день: `${income_per_day:.2f}`",
        f"⚡ Avg neg ставка: `{neg_avg:+.4f}%`\n",
        f"`{'Дата':<12} {'Доход за день':>14}`",
        f"`{'─'*28}`",
    ]

    for day in sorted(by_day.keys(), reverse=True):
        lines.append(f"`{day:<12} ${by_day[day]:>12.2f}`")

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def calc_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # /calc ENJ 25000 --days 7
    if context.args:
        parts = context.args
        coins, days = parse_tokens(" ".join(parts))
        # Ищем число как сумму
        amount = None
        coin = None
        remaining = []
        for p in coins:
            try:
                amount = float(p.replace("$", "").replace(",", ""))
            except ValueError:
                remaining.append(p)
        coin = remaining[0] if remaining else None
        if coin and amount:
            await do_calc(update, coin, amount, days)
            return ConversationHandler.END

    await update.message.reply_text(
        "Введи монету и сумму позиции через пробел:\n\n"
        "Например: `ENJ 25000`\n"
        "Или с периодом: `ENJ 25000 --days 14`\n\n"
        "Отмена: /cancel",
        parse_mode="Markdown"
    )
    return WAIT_CALC_COIN


async def calc_got_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    coins, days = parse_tokens(text)

    amount = None
    coin = None
    remaining = []
    for p in coins:
        try:
            amount = float(p.replace("$", "").replace(",", ""))
        except ValueError:
            remaining.append(p)
    coin = remaining[0] if remaining else None

    if not coin:
        await update.message.reply_text(
            "Не распознал монету. Попробуй: `ENJ 25000` или `ENJ 25000 --days 14`",
            parse_mode="Markdown"
        )
        return WAIT_CALC_COIN

    if not amount or amount <= 0:
        await update.message.reply_text(
            "Не распознал сумму. Попробуй: `ENJ 25000`",
            parse_mode="Markdown"
        )
        return WAIT_CALC_COIN

    await do_calc(update, coin, amount, days)
    return ConversationHandler.END


# ─────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────

def main():
    if not BOT_TOKEN:
        raise ValueError("BOT_TOKEN не задан!")

    app = Application.builder().token(BOT_TOKEN).build()

    analyze_conv = ConversationHandler(
        entry_points=[CommandHandler("analyze", analyze_start)],
        states={WAIT_ANALYZE_COINS: [MessageHandler(filters.TEXT & ~filters.COMMAND, analyze_got_coins)]},
        fallbacks=[CommandHandler("cancel", cmd_cancel)],
    )

    show_conv = ConversationHandler(
        entry_points=[CommandHandler("show", show_start)],
        states={WAIT_SHOW_COIN: [MessageHandler(filters.TEXT & ~filters.COMMAND, show_got_coin)]},
        fallbacks=[CommandHandler("cancel", cmd_cancel)],
    )

    calc_conv = ConversationHandler(
        entry_points=[CommandHandler("calc", calc_start)],
        states={WAIT_CALC_COIN: [MessageHandler(filters.TEXT & ~filters.COMMAND, calc_got_input)]},
        fallbacks=[CommandHandler("cancel", cmd_cancel)],
    )

    app.add_handler(CommandHandler("start",    cmd_start))
    app.add_handler(CommandHandler("help",     cmd_help))
    app.add_handler(CommandHandler("settings", cmd_settings))
    app.add_handler(CommandHandler("cancel",   cmd_cancel))
    app.add_handler(analyze_conv)
    app.add_handler(show_conv)
    app.add_handler(calc_conv)
    app.add_handler(MessageHandler(filters.COMMAND, unknown))

    print("Бот запущен...")
    app.run_polling()


if __name__ == "__main__":
    main()

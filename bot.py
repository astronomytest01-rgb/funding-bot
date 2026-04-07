"""
Phemex Funding Rate Telegram Bot
"""

import os
import time
import requests
from datetime import datetime, timezone
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

# ─────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")

# Дефолтные настройки анализа (можно менять)
DEFAULT_DAYS            = 7
STABILITY_THRESHOLD     = -0.04   # %
MAX_OUTLIER_PCT         = 25      # %
NEG_AVG_THRESHOLD       = -0.08   # %

BASE_URL = "https://api.phemex.com"

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

# ─────────────────────────────────────────────
# HANDLERS
# ─────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "👋 *Phemex Funding Rate Analyzer*\n\n"
        "Команды:\n"
        "`/analyze BTC ETH SOL` — анализ монет\n"
        "`/analyze BTC ETH --days 14` — за 14 дней\n"
        "`/show ENJ` — все ставки по монете\n"
        "`/show ENJ --days 14` — за 14 дней\n"
        "`/settings` — текущие настройки фильтров\n"
        "`/help` — эта справка\n\n"
        "Монеты указывай через пробел: `BTC ETH SOL ENJ`"
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
        "Категории результата:\n"
        "✅ *ПОДХОДИТ* — выбросов ≤ 25%\n"
        "⚡ *ЧАСТИЧНО* — нестабильно, но neg\\_avg ≤ -0.08%\n"
        "❌ *НЕ ПОДХОДИТ* — не прошла ни один критерий"
    )
    await update.message.reply_text(text, parse_mode="Markdown")


def parse_args(args):
    """Парсит список аргументов, возвращает (coins, days)."""
    days = DEFAULT_DAYS
    coins = []
    i = 0
    while i < len(args):
        if args[i] == "--days" and i + 1 < len(args):
            try:
                days = int(args[i + 1])
                i += 2
                continue
            except ValueError:
                pass
        coins.append(args[i].upper())
        i += 1
    return coins, days


async def cmd_analyze(update: Update, context: ContextTypes.DEFAULT_TYPE):
    coins, days = parse_args(context.args)

    if not coins:
        await update.message.reply_text(
            "Укажи монеты через пробел:\n`/analyze BTC ETH SOL ENJ`",
            parse_mode="Markdown"
        )
        return

    await update.message.reply_text(
        f"🔍 Анализирую {len(coins)} монет за {days} дней...",
    )

    now_ms = int(time.time() * 1000)
    start_ms = now_ms - days * 24 * 60 * 60 * 1000

    results = []
    for coin in coins:
        res = analyze_coin(coin, start_ms, now_ms)
        results.append(res)

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


async def cmd_show(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if not args:
        await update.message.reply_text(
            "Укажи монету:\n`/show ENJ`\n`/show ENJ --days 14`",
            parse_mode="Markdown"
        )
        return

    coins, days = parse_args(args)
    coin = coins[0] if coins else None
    if not coin:
        await update.message.reply_text("Укажи монету, например: `/show ENJ`", parse_mode="Markdown")
        return

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
        f"📈 *{coin}* — {sym_or_err} — {days} дней\n"
        f"Ставок: `{len(rows)}`  |  "
        f"Avg: `{avg:+.4f}%`  |  "
        f"Neg avg: `{neg_avg:+.4f}%`\n"
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

    # Telegram limit ~4096 chars — режем на части если много строк
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


async def unknown(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Не знаю такой команды. Напиши /help чтобы увидеть список команд."
    )


# ─────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────

def main():
    if not BOT_TOKEN:
        raise ValueError("BOT_TOKEN не задан! Добавь переменную окружения BOT_TOKEN.")

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start",    cmd_start))
    app.add_handler(CommandHandler("help",     cmd_help))
    app.add_handler(CommandHandler("settings", cmd_settings))
    app.add_handler(CommandHandler("analyze",  cmd_analyze))
    app.add_handler(CommandHandler("show",     cmd_show))
    app.add_handler(MessageHandler(filters.COMMAND, unknown))

    print("Бот запущен...")
    app.run_polling()


if __name__ == "__main__":
    main()

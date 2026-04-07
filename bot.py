"""
Phemex + XT Funding Rate Telegram Bot
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

# ── Биржи ─────────────────────────────────────
# Включай/выключай биржи здесь (True/False)
EXCHANGES_ENABLED = {
    "phemex": True,
    "xt":     True,
    "toobit": True,
    "binance": True,
}

# Когда анализируешь без указания биржи — используются все включённые
# Можно переопределить через /exchange phemex или /exchange xt или /exchange all

# ─────────────────────────────────────────────
# Состояния диалога
# ─────────────────────────────────────────────
WAIT_ANALYZE_COINS = 1
WAIT_SHOW_COIN     = 3
WAIT_CALC_COIN     = 5

# ─────────────────────────────────────────────
# PHEMEX API
# ─────────────────────────────────────────────

def phemex_fetch(coin, start_ms, end_ms):
    """Возвращает list of (timestamp_ms, rate_pct) или raises"""
    candidates = []
    coin = coin.upper()
    if coin.endswith("USDT") or coin.endswith("USD"):
        candidates = [f".{coin}FR8H"]
    else:
        candidates = [f".{coin}USDTFR8H", f".{coin}USDFR8H"]

    last_err = None
    for sym in candidates:
        try:
            url = "https://api.phemex.com/api-data/public/data/funding-rate-history"
            params = {"symbol": sym, "end": end_ms, "limit": 1000}
            r = requests.get(url, params=params, timeout=10)
            r.raise_for_status()
            data = r.json()
            if data.get("code") != 0:
                raise ValueError(data.get("msg"))
            rows = [x for x in data.get("data", {}).get("rows", []) if x["fundingTime"] >= start_ms]
            if rows:
                return [(x["fundingTime"], float(x["fundingRate"]) * 100) for x in rows], sym
        except Exception as e:
            last_err = str(e)
        time.sleep(0.15)
    return [], last_err


# ─────────────────────────────────────────────
# XT API
# ─────────────────────────────────────────────

def xt_fetch(coin, start_ms, end_ms):
    """Возвращает list of (timestamp_ms, rate_pct).

    Реальный формат ответа XT:
    {
      "returnCode": 0,
      "result": {
        "hasNext": false,
        "items": [
          {"id": 123, "symbol": "enj_usdt", "fundingRate": -0.001,
           "createdTime": 1234567890000, "collectionInternal": 14400}
        ]
      }
    }
    """
    coin = coin.upper()
    if coin.endswith("USDT"):
        sym = coin.lower()
    elif coin.endswith("USD"):
        sym = coin.lower() + "t"
    else:
        sym = f"{coin.lower()}_usdt"

    last_err = None
    try:
        url = "https://fapi.xt.com/future/market/v1/public/q/funding-rate-record"
        params = {"symbol": sym, "limit": 500, "direction": "NEXT"}
        r = requests.get(url, params=params, timeout=10)
        r.raise_for_status()
        data = r.json()

        if data.get("returnCode") != 0:
            err = data.get("error", {})
            msg = err.get("msg") if isinstance(err, dict) else str(err)
            raise ValueError(msg or "API error")

        result = data.get("result", {})
        # result может быть объектом {items:[...]} или списком напрямую
        if isinstance(result, list):
            items = result
        else:
            items = result.get("items", [])

        if not items:
            return [], f"Нет данных (символ: {sym})"

        filtered = []
        for x in items:
            ts = x.get("createdTime") or x.get("settleTime") or x.get("fundingTime") or 0
            rate = float(x.get("fundingRate", 0)) * 100
            if ts >= start_ms:
                filtered.append((ts, rate))

        if not filtered:
            return [], f"Нет данных за период (символ: {sym}, всего: {len(items)})"

        return filtered, sym

    except Exception as e:
        last_err = str(e)

    return [], last_err



# ─────────────────────────────────────────────
# TOOBIT API
# ─────────────────────────────────────────────

def toobit_fetch(coin, start_ms, end_ms):
    """Возвращает list of (timestamp_ms, rate_pct).

    Формат ответа Toobit:
    [
      {"id": "123", "symbol": "BTC-SWAP-USDT",
       "settleTime": "1570708800000", "settleRate": "0.00321", "period": "8H"}
    ]
    """
    coin = coin.upper()
    # Toobit формат: BTC-SWAP-USDT
    if coin.endswith("USDT"):
        base = coin[:-4]  # убираем USDT
        sym = f"{base}-SWAP-USDT"
    elif coin.endswith("USD"):
        base = coin[:-3]
        sym = f"{base}-SWAP-USDT"
    else:
        sym = f"{coin}-SWAP-USDT"

    last_err = None
    try:
        url = "https://api.toobit.com/api/v1/futures/historyFundingRate"
        params = {"symbol": sym, "limit": 1000}
        r = requests.get(url, params=params, timeout=10)
        r.raise_for_status()
        data = r.json()

        # Ответ — массив напрямую
        if not isinstance(data, list):
            raise ValueError(f"Unexpected response format: {str(data)[:100]}")

        if not data:
            return [], f"Нет данных (символ: {sym})"

        filtered = []
        for x in data:
            ts = int(x.get("settleTime", 0))
            rate = float(x.get("settleRate", 0)) * 100
            if ts >= start_ms:
                filtered.append((ts, rate))

        if not filtered:
            return [], f"Нет данных за период (символ: {sym}, всего: {len(data)})"

        return filtered, sym

    except Exception as e:
        last_err = str(e)

    return [], last_err


# ─────────────────────────────────────────────
# BINANCE API
# ─────────────────────────────────────────────

def binance_fetch(coin, start_ms, end_ms):
    """Возвращает list of (timestamp_ms, rate_pct).

    Формат ответа Binance:
    [
      {"symbol": "BTCUSDT", "fundingRate": "0.00010000",
       "fundingTime": 1570708800000, "markPrice": "..."}
    ]
    Авторизация не нужна. Лимит 1000 записей за запрос.
    """
    coin = coin.upper()
    if coin.endswith("USDT"):
        sym = coin
    elif coin.endswith("USD"):
        sym = coin + "T"
    else:
        sym = f"{coin}USDT"

    last_err = None
    try:
        url = "https://fapi.binance.com/fapi/v1/fundingRate"
        params = {"symbol": sym, "limit": 1000, "endTime": end_ms}
        r = requests.get(url, params=params, timeout=10)
        r.raise_for_status()
        data = r.json()

        if isinstance(data, dict) and data.get("code"):
            raise ValueError(data.get("msg", "API error"))

        if not isinstance(data, list):
            raise ValueError(f"Unexpected format: {str(data)[:100]}")

        if not data:
            return [], f"Нет данных (символ: {sym})"

        filtered = []
        for x in data:
            ts = int(x.get("fundingTime", 0))
            rate = float(x.get("fundingRate", 0)) * 100
            if ts >= start_ms:
                filtered.append((ts, rate))

        if not filtered:
            return [], f"Нет данных за период (символ: {sym}, всего: {len(data)})"

        return filtered, sym

    except Exception as e:
        last_err = str(e)

    return [], last_err

# ─────────────────────────────────────────────
# УНИВЕРСАЛЬНЫЙ АНАЛИЗ
# ─────────────────────────────────────────────

EXCHANGE_FETCHERS = {
    "phemex": phemex_fetch,
    "xt":     xt_fetch,
    "toobit": toobit_fetch,
    "binance": binance_fetch,
}

EXCHANGE_LABELS = {
    "phemex": "Phemex",
    "xt":     "XT",
    "toobit": "Toobit",
    "binance": "Binance",
}


def get_active_exchanges(requested=None):
    """Возвращает список активных бирж."""
    if requested and requested != "all":
        exs = [e.strip().lower() for e in requested.split(",")]
        return [e for e in exs if e in EXCHANGE_FETCHERS]
    return [e for e, enabled in EXCHANGES_ENABLED.items() if enabled]


def analyze_rates(rates_pct):
    """Считает метрики по списку ставок в %."""
    if not rates_pct:
        return None
    neg = [r for r in rates_pct if r < 0]
    total = len(rates_pct)
    below = sum(1 for r in rates_pct if r <= STABILITY_THRESHOLD)
    outlier_pct = (total - below) / total * 100
    pass_stability = outlier_pct <= MAX_OUTLIER_PCT
    neg_avg = sum(neg) / len(neg) if neg else 0.0
    pass_neg_avg = bool(neg) and neg_avg <= NEG_AVG_THRESHOLD

    if pass_stability:
        category = "full"
    elif pass_neg_avg:
        category = "partial"
    else:
        category = "fail"

    return {
        "total": total,
        "avg": sum(rates_pct) / total,
        "neg_avg": neg_avg,
        "neg_count": len(neg),
        "min": min(rates_pct),
        "max": max(rates_pct),
        "outlier_pct": outlier_pct,
        "pass_stability": pass_stability,
        "pass_neg_avg": pass_neg_avg,
        "category": category,
    }


def analyze_coin_multi(coin, start_ms, end_ms, exchanges):
    """Анализирует монету на нескольких биржах. Возвращает dict по биржам."""
    results = {}
    for ex in exchanges:
        fetcher = EXCHANGE_FETCHERS.get(ex)
        if not fetcher:
            continue
        try:
            data, sym_or_err = fetcher(coin, start_ms, end_ms)
        except Exception as e:
            results[ex] = {"error": str(e), "sym": None}
            continue

        if not data:
            results[ex] = {"error": sym_or_err or "Нет данных", "sym": None}
            continue

        rates = [r for _, r in data]
        metrics = analyze_rates(rates)
        metrics["coin"] = coin
        metrics["sym"] = sym_or_err
        metrics["exchange"] = ex
        metrics["error"] = None
        results[ex] = metrics
    return results


# ─────────────────────────────────────────────
# ПАРСИНГ АРГУМЕНТОВ
# ─────────────────────────────────────────────

def parse_tokens(text):
    """'BTC ETH /days 14 /exchange xt' -> (coins, days, exchange)
    Поддерживает как /days и /exchange так и --days и --exchange.
    """
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
        # пропускаем команды вида /analyze если вдруг попали в текст
        if p in ("/analyze", "/show", "/calc", "/start", "/help", "/settings", "/exchanges", "/cancel"):
            i += 1; continue
        coins.append(parts[i].upper())
        i += 1
    return coins, days, exchange


# ─────────────────────────────────────────────
# ФОРМАТИРОВАНИЕ РЕЗУЛЬТАТОВ
# ─────────────────────────────────────────────

def fmt_coin_line(coin, ex_results, active_exchanges):
    """Форматирует одну строку монеты для всех бирж."""
    lines = []
    # Определяем общую категорию (лучшая из бирж)
    categories = [r.get("category") for r in ex_results.values() if not r.get("error")]
    if "full" in categories:
        overall = "✅"
    elif "partial" in categories:
        overall = "⚡"
    elif categories:
        overall = "❌"
    else:
        overall = "⚠️"

    lines.append(f"{overall} *{coin}*")
    for ex in active_exchanges:
        r = ex_results.get(ex, {})
        label = EXCHANGE_LABELS.get(ex, ex.upper())
        if r.get("error"):
            lines.append(f"  `{label}`: ошибка — {r['error'][:40]}")
            continue
        cat = {"full": "✅", "partial": "⚡", "fail": "❌"}.get(r.get("category", "fail"), "❌")
        lines.append(
            f"  `{label}` {cat}  avg `{r['avg']:+.4f}%`  neg\\_avg `{r['neg_avg']:+.4f}%`  выбр `{r['outlier_pct']:.0f}%`"
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

async def do_analyze(update, coins, days, exchange_arg):
    active = get_active_exchanges(exchange_arg)
    if not active:
        await update.message.reply_text("❌ Нет активных бирж. Проверь настройки EXCHANGES_ENABLED.")
        return

    ex_str = " + ".join(EXCHANGE_LABELS.get(e, e) for e in active)
    await update.message.reply_text(f"🔍 Анализирую {len(coins)} монет на {ex_str} за {days} дней...")

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
            await update.message.reply_text(chunk, parse_mode="Markdown")
    else:
        await update.message.reply_text(reply, parse_mode="Markdown")


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
    text = (
        "👋 *Phemex + XT Funding Rate Analyzer*\n\n"
        f"Активные биржи: `{ex_str}`\n\n"
        "Команды:\n"
        "/analyze — анализ монет\n"
        "/show — ставки по монете\n"
        "/calc — калькулятор дохода\n"
        "/exchanges — статус бирж\n"
        "/settings — настройки фильтров\n"
        "/help — справка\n\n"
        "Параметры (можно добавлять к любой команде):\n"
        "`/days 14` — период\n"
        "`/exchange phemex` — только Phemex\n"
        "`/exchange xt` — только XT\n"
        "`/exchange all` — все биржи"
    )
    await update.message.reply_text(text, parse_mode="Markdown")


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await cmd_start(update, context)


async def cmd_exchanges(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lines = ["🏦 *Статус бирж*\n"]
    for ex, enabled in EXCHANGES_ENABLED.items():
        label = EXCHANGE_LABELS.get(ex, ex.upper())
        status = "✅ включена" if enabled else "❌ выключена"
        lines.append(f"`{label}` — {status}")
    lines.append("\nЧтобы включить/выключить биржу — измени `EXCHANGES_ENABLED` в `bot.py`")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


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

    app.add_handler(CommandHandler("start",     cmd_start))
    app.add_handler(CommandHandler("help",      cmd_help))
    app.add_handler(CommandHandler("settings",  cmd_settings))
    app.add_handler(CommandHandler("exchanges", cmd_exchanges))
    app.add_handler(CommandHandler("cancel",    cmd_cancel))
    app.add_handler(analyze_conv)
    app.add_handler(show_conv)
    app.add_handler(calc_conv)
    app.add_handler(MessageHandler(filters.COMMAND, unknown))

    print("Бот запущен...")
    app.run_polling()


if __name__ == "__main__":
    main()

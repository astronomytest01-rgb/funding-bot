"""
Phemex + XT Funding Rate Telegram Bot
"""

import os
import time
import json
import requests
from datetime import datetime, timezone
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    filters, ContextTypes, ConversationHandler, CallbackQueryHandler
)

# ─────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────

BOT_TOKEN       = os.environ.get("BOT_TOKEN", "")
GEMINI_API_KEY  = os.environ.get("GEMINI_API_KEY", "")
REPORT_CHAT_ID  = os.environ.get("REPORT_CHAT_ID", "")  # куда слать авто-отчёт

# CoinW — данные из Supabase
SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "")

DEFAULT_DAYS        = 7
STABILITY_THRESHOLD = -0.04
MAX_OUTLIER_PCT     = 25
NEG_AVG_THRESHOLD   = -0.08
MIN_NEG_RATIO       = 0.30   # минимум 30% ставок должны быть отрицательными для ЛОНГ
MIN_POS_RATIO       = 0.30   # минимум 30% ставок должны быть положительными для ШОРТ

# ── Биржи ─────────────────────────────────────
EXCHANGES_ENABLED = {
    "phemex": True,
    "xt":     True,
    "toobit": True,
    "okx":    True,
    "bingx":  True,
    "coinw":  True,
}

# ─────────────────────────────────────────────
# Состояния диалога
# ─────────────────────────────────────────────
WAIT_ANALYZE_COINS = 1
WAIT_SHOW_COIN     = 3
WAIT_CALC_COIN     = 5

# Состояния пошаговых диалогов
ACF_COIN     = 20
ACF_DAYS     = 21
ACF_DAYS_NUM = 22
ACF_EXCH     = 23

FR_COIN      = 30
FR_DAYS      = 31
FR_DAYS_NUM  = 32
FR_EXCH      = 33

PC_COIN      = 40
PC_AMT       = 41
PC_AMT_NUM   = 42
PC_DAYS      = 43
PC_DAYS_NUM  = 44
PC_EXCH      = 45

# /analyze scan states
AN_METHOD    = 50
AN_AMT       = 51
AN_AMT_NUM   = 52
AN_THRESH    = 53
AN_THRESH_NUM= 54
AN_DAYS      = 55
AN_DAYS_NUM  = 56

# ─────────────────────────────────────────────
# ФЕТЧЕРЫ АПИ БИРЖ
# ─────────────────────────────────────────────

def phemex_fetch(coin, start_ms, end_ms):
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
            params = {"symbol": sym, "start": start_ms, "end": end_ms, "limit": 1000}
            r = requests.get(url, params=params, timeout=10)
            r.raise_for_status()
            data = r.json()
            if data.get("code") != 0:
                raise ValueError(data.get("msg"))
            rows = [
                x for x in data.get("data", {}).get("rows", [])
                if x["fundingTime"] >= start_ms
                and abs(float(x["fundingRate"])) < 0.01 
            ]
            if rows:
                return [(x["fundingTime"], float(x["fundingRate"]) * 100) for x in rows], sym
        except Exception as e:
            last_err = str(e)
        time.sleep(0.15)
    return [], last_err


def xt_fetch(coin, start_ms, end_ms):
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
        return filtered, sym
    except Exception as e:
        last_err = str(e)
    return [], last_err


def toobit_fetch(coin, start_ms, end_ms):
    coin = coin.upper()
    if coin.endswith("USDT"):
        sym = f"{coin[:-4]}-SWAP-USDT"
    elif coin.endswith("USD"):
        sym = f"{coin[:-3]}-SWAP-USDT"
    else:
        sym = f"{coin}-SWAP-USDT"

    last_err = None
    try:
        url = "https://api.toobit.com/api/v1/futures/historyFundingRate"
        params = {"symbol": sym, "limit": 1000}
        r = requests.get(url, params=params, timeout=10)
        r.raise_for_status()
        data = r.json()

        if not isinstance(data, list) or not data:
            return [], f"Нет данных (символ: {sym})"

        filtered = []
        for x in data:
            ts = int(x.get("settleTime", 0))
            rate = float(x.get("settleRate", 0)) * 100
            if ts >= start_ms:
                filtered.append((ts, rate))
        return filtered, sym
    except Exception as e:
        last_err = str(e)
    return [], last_err


def okx_fetch(coin, start_ms, end_ms):
    coin = coin.upper()
    if coin.endswith("USDT"):
        sym = f"{coin[:-4]}-USDT-SWAP"
    elif coin.endswith("USD"):
        sym = f"{coin[:-3]}-USDT-SWAP"
    else:
        sym = f"{coin}-USDT-SWAP"

    last_err = None
    try:
        url = "https://www.okx.com/api/v5/public/funding-rate-history"
        params = {"instId": sym, "limit": 100}
        r = requests.get(url, params=params, timeout=10)
        if r.status_code in (451, 403):
            return [], f"OKX недоступен (ошибка {r.status_code})"
        r.raise_for_status()
        data = r.json()

        if data.get("code") != "0":
            raise ValueError(data.get("msg", "API error"))

        items = data.get("data", [])
        if not items:
            return [], f"Нет данных (символ: {sym})"

        filtered = []
        for x in items:
            ts = int(x.get("fundingTime", 0))
            rate = float(x.get("fundingRate", 0)) * 100
            if ts >= start_ms:
                filtered.append((ts, rate))
        return filtered, sym
    except Exception as e:
        last_err = str(e)
    return [], last_err


def bingx_fetch(coin, start_ms, end_ms):
    coin = coin.upper()
    if coin.endswith("USDT"):
        sym = coin[:-4] + "-USDT"
    elif coin.endswith("USD"):
        sym = coin[:-3] + "-USDT"
    else:
        sym = f"{coin}-USDT"

    last_err = None
    try:
        url = "https://open-api.bingx.com/openApi/swap/v2/quote/fundingRate"
        params = {"symbol": sym, "limit": 1000}
        r = requests.get(url, params=params, timeout=10)
        if r.status_code in (451, 403):
            return [], f"BingX недоступен (ошибка {r.status_code})"
        r.raise_for_status()
        data = r.json()

        if data.get("code") != 0:
            raise ValueError(data.get("msg", "API error"))

        items = data.get("data", [])
        if not items:
            return [], f"Нет данных (символ: {sym})"

        filtered = []
        for x in items:
            ts = int(x.get("fundingTime", 0))
            rate = float(x.get("fundingRate", 0)) * 100
            if ts >= start_ms:
                filtered.append((ts, rate))
        return filtered, sym
    except Exception as e:
        last_err = str(e)
    return [], last_err


def coinw_fetch(coin, start_ms, end_ms):
    if not SUPABASE_URL or not SUPABASE_KEY:
        return [], "SUPABASE_URL/KEY не заданы"

    symbol = coin.upper()
    start_iso = datetime.fromtimestamp(start_ms / 1000, tz=timezone.utc).isoformat()

    try:
        url = f"{SUPABASE_URL}/rest/v1/funding_rates"
        headers = {
            "apikey":        SUPABASE_KEY,
            "Authorization": f"Bearer {SUPABASE_KEY}",
        }
        params = {
            "symbol":       f"eq.{symbol}",
            "collected_at": f"gte.{start_iso}",
            "order":        "funding_time.asc",
            "limit":        "1000",
            "select":       "rate_pct,collected_at,funding_time",
        }
        r = requests.get(url, headers=headers, params=params, timeout=10)
        r.raise_for_status()
        rows = r.json()

        if not rows:
            return [], f"Нет данных для {symbol} в БД"

        seen_funding_times = set()
        result = []
        for row in rows:
            ft = row.get("funding_time")
            if ft:
                if ft in seen_funding_times:
                    continue
                seen_funding_times.add(ft)
                dt = datetime.fromisoformat(ft.replace("Z", "+00:00"))
            else:
                dt = datetime.fromisoformat(row["collected_at"].replace("Z", "+00:00"))
            ts_ms = int(dt.timestamp() * 1000)
            result.append((ts_ms, float(row["rate_pct"])))
        return result, f"coinw_{symbol}"
    except Exception as e:
        return [], str(e)


EXCHANGE_FETCHERS = {
    "phemex": phemex_fetch,
    "xt":     xt_fetch,
    "toobit": toobit_fetch,
    "okx":    okx_fetch,
    "bingx":  bingx_fetch,
    "coinw":  coinw_fetch,
}

EXCHANGE_LABELS = {
    "phemex": "Phemex",
    "xt":     "XT",
    "toobit": "Toobit",
    "okx":    "OKX",
    "bingx":  "BingX",
    "coinw":  "CoinW",
}

# ─────────────────────────────────────────────
# GEMINI АНАЛИЗ
# ─────────────────────────────────────────────

GEMINI_PROMPT_TEMPLATE = """Действуй как риск-менеджер криптофонда. Проанализируй монету {coin}. 
Направление дельта-нейтральной позиции: {direction} (для контекста риска).

Дай аналитический ответ без использования Markdown-разметки (без звездочек, решеток и подчеркиваний). Начинай каждый абзац с соответствующего эмодзи.

✅ Вердикт: Напиши СТРОГО от 1 до 4 слов. Только твое решение (например: РЕКОМЕНДУЕТСЯ, НЕ РЕКОМЕНДУЕТСЯ, КАТЕГОРИЧЕСКИ НЕТ). Никаких предложений, обоснований и точек в этом абзаце быть не должно.

📊 Волатильность и Риски: Оцени типичную волатильность актива за последние 3 дня, 7 дней и месяц. Насколько высока вероятность резких сквизов (например, внезапный улет на +30% за день)? Насколько этот актив опасен для позиции?

💰 Фундаментал: Что стоит за монетой {coin}? Это старая надежная инфраструктура (уровня SOL/ETH), крепкий DeFi/L1, свежий хайп-проект или обычный мемкоин? Какая примерно у него капитализация (крупная, средняя, микро) и ликвидность?

⚖️ Обоснование и Риск-менеджмент: Развернуто обоснуй свой вердикт. Исходя из волатильности и капитализации, какое максимальное плечо безопасно использовать (х1, х2, х3) и почему. Нужен ли жесткий стоп-лосс.

Пиши подробно, аргументированно и обязательно заверши последнюю мысль до конца."""

def gemini_analyze_coin(coin, direction, avg_rate, exchange, days):
    if not GEMINI_API_KEY:
        return None

    prompt = GEMINI_PROMPT_TEMPLATE.format(
        coin=coin,
        direction="ЛОНГ (ставки отрицательные, шортисты платят лонгистам)" if direction == "LONG"
                  else "ШОРТ (ставки положительные, лонгисты платят шортистам)",
        avg_rate=avg_rate,
        exchange=exchange,
        days=days,
    )

    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={GEMINI_API_KEY}"
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.4, "maxOutputTokens": 2500},
    }

    for attempt in range(3):
        try:
            r = requests.post(url, json=payload, timeout=30)
            if r.status_code == 429:
                time.sleep(15 * (attempt + 1))
                continue
            r.raise_for_status()
            data = r.json()
            return data["candidates"][0]["content"]["parts"][0]["text"].strip()
        except Exception:
            time.sleep(5)
            continue
    return None

# ─────────────────────────────────────────────
# ЛОГИКА АНАЛИЗА И ФИЛЬТРЫ
# ─────────────────────────────────────────────

def check_recent_trend(fetcher, coin, direction, n=6):
    try:
        now_ms   = int(time.time() * 1000)
        start_ms = now_ms - 2 * 24 * 60 * 60 * 1000
        rows, _ = fetcher(coin, start_ms, now_ms)
        if not rows:
            return True
        rows_sorted = sorted(rows, key=lambda x: x[0], reverse=True)
        recent = [r for _, r in rows_sorted[:n]]
        if not recent:
            return True
        avg_recent = sum(recent) / len(recent)
        if direction == "LONG":
            return avg_recent < -0.005
        else:
            return avg_recent > 0.005
    except Exception:
        return True


def analyze_rates(rates_pct):
    if not rates_pct:
        return None

    neg   = [r for r in rates_pct if r < 0]
    pos   = [r for r in rates_pct if r > 0]
    total = len(rates_pct)
    avg   = sum(rates_pct) / total

    below_neg    = sum(1 for r in rates_pct if r <= STABILITY_THRESHOLD)
    outlier_long = (total - below_neg) / total * 100
    neg_avg      = sum(neg) / len(neg) if neg else 0.0
    neg_ratio    = len(neg) / total
    pass_stability_long = outlier_long <= MAX_OUTLIER_PCT
    pass_neg_avg        = bool(neg) and neg_avg <= NEG_AVG_THRESHOLD and neg_ratio >= MIN_NEG_RATIO

    above_pos     = sum(1 for r in rates_pct if r >= -STABILITY_THRESHOLD)
    outlier_short = (total - above_pos) / total * 100
    pos_avg       = sum(pos) / len(pos) if pos else 0.0
    pos_ratio     = len(pos) / total
    pass_stability_short = outlier_short <= MAX_OUTLIER_PCT
    pass_pos_avg         = bool(pos) and pos_avg >= -NEG_AVG_THRESHOLD and pos_ratio >= MIN_POS_RATIO

    if pass_stability_long:
        category, direction, outlier_pct = "full", "LONG", outlier_long
    elif pass_stability_short:
        category, direction, outlier_pct = "full", "SHORT", outlier_short
    elif pass_neg_avg:
        category, direction, outlier_pct = "partial", "LONG", outlier_long
    elif pass_pos_avg:
        category, direction, outlier_pct = "partial", "SHORT", outlier_short
    else:
        category, direction, outlier_pct = "fail", ("LONG" if avg <= 0 else "SHORT"), outlier_long

    return {
        "total": total, "avg": avg, "neg_avg": neg_avg, "pos_avg": pos_avg,
        "outlier_pct": outlier_pct, "category": category, "direction": direction
    }

def get_active_exchanges(requested=None):
    if requested and requested != "all":
        exs = [e.strip().lower() for e in requested.split(",")]
        return [e for e in exs if e in EXCHANGE_FETCHERS]
    return [e for e, enabled in EXCHANGES_ENABLED.items() if enabled]

def parse_tokens(text):
    parts = text.strip().split()
    days = DEFAULT_DAYS
    exchange = None
    coins = []
    i = 0
    KNOWN_EXCHANGES = {"phemex", "xt", "toobit", "okx", "bingx", "coinw"}
    while i < len(parts):
        p = parts[i].lower()
        if p in ("/days", "--days") and i + 1 < len(parts):
            try:
                days = int(parts[i + 1]); i += 2; continue
            except ValueError: pass
        if p in ("/exchange", "--exchange") and i + 1 < len(parts):
            exchange = parts[i + 1].lower(); i += 2; continue
        if p in KNOWN_EXCHANGES:
            exchange = p; i += 1; continue
        if p.startswith("/"):
            i += 1; continue
        try:
            days = int(parts[i]); i += 1; continue
        except ValueError: pass
        coins.append(parts[i].upper())
        i += 1
    return coins, days, exchange

# ─────────────────────────────────────────────
# ВЕРСИЯ ДЛЯ АНАЛИЗА МОНЕТ ПО ФИЛЬТРАМ
# ─────────────────────────────────────────────

def phemex_get_all_symbols():
    url = "https://api.phemex.com/exchange/public/cfg/v2/products"
    r = requests.get(url, timeout=15)
    r.raise_for_status()
    products = r.json().get("data", {}).get("products", [])
    seen = set()
    coins = []
    for p in products:
        if p.get("type") == "PerpetualV2" and p.get("quoteCurrency") == "USDT" and p.get("status") == "Listed":
            sym = p.get("symbol", "")
            if sym.endswith("USDT"):
                c = sym[:-4]
                if c and c not in seen:
                    coins.append(c)
                    seen.add(c)
    return coins

_scan_running = {}
SCAN_BATCH = 20

async def an_run_scan(trigger, context: ContextTypes.DEFAULT_TYPE):
    msg = trigger.message if hasattr(trigger, 'message') else trigger
    exchange  = context.user_data.get("an_exchange", "phemex")
    method    = context.user_data.get("an_method", "rate")
    days      = context.user_data.get("an_days", DEFAULT_DAYS)
    amount    = context.user_data.get("an_amount", 0)
    threshold = context.user_data.get("an_threshold", 0)
    use_ai    = context.user_data.get("use_ai", False)
    chat_id   = msg.chat_id

    if _scan_running.get(chat_id):
        await msg.reply_text("⏳ Скан уже запущен.")
        return

    label = EXCHANGE_LABELS.get(exchange, exchange)
    method_label = "Средняя ставка" if method == "rate" else f"Средний доход (${amount:,.0f}, ≥${threshold:.0f}/день)"

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
        f"Период: *{days} дней*\n"
        f"Режим AI: *{'Включен' if use_ai else 'Выключен'}*\n",
        parse_mode="Markdown"
    )

    _scan_running[chat_id] = True
    now_ms   = int(time.time() * 1000)
    start_ms = now_ms - days * 24 * 60 * 60 * 1000
    fetcher = phemex_fetch if exchange == "phemex" else EXCHANGE_FETCHERS.get(exchange)
    
    passed = []
    batches = [all_coins[i:i+SCAN_BATCH] for i in range(0, total, SCAN_BATCH)]

    for batch_idx, batch in enumerate(batches):
        if not _scan_running.get(chat_id):
            await msg.reply_text(f"⛔ Скан остановлен на порции {batch_idx+1}/{len(batches)}")
            return

        for coin in batch:
            if not _scan_running.get(chat_id): break
            try:
                rows, _ = fetcher(coin, start_ms, now_ms)
            except Exception:
                rows = []
            if not rows:
                time.sleep(0.15)
                continue

            rates = [r for _, r in rows]
            if method == "rate":
                r = analyze_rates(rates)
                if not r or r["category"] == "fail":
                    time.sleep(0.15)
                    continue
                d = r["direction"]
                k = r["neg_avg"] if d == "LONG" else r["pos_avg"]
                if check_recent_trend(fetcher, coin, d):
                    passed.append((coin, k, r["outlier_pct"], d, r["category"], None))
            else:
                clean = [x for x in rates if abs(x) <= 0.8]
                if not clean: continue
                n_r, p_r = len([x for x in clean if x < 0])/len(clean), len([x for x in clean if x > 0])/len(clean)
                a = sum(clean)/len(clean)
                if n_r >= MIN_NEG_RATIO and a < 0:
                    inc = amount * abs(a)/100 * (len(clean)/days)
                    if inc >= threshold and check_recent_trend(fetcher, coin, "LONG"):
                        outlier = (len(clean) - sum(1 for x in clean if x <= STABILITY_THRESHOLD))/len(clean)*100
                        passed.append((coin, a, outlier, "LONG", "income", inc))
                elif p_r >= MIN_POS_RATIO and a > 0:
                    inc = amount * abs(a)/100 * (len(clean)/days)
                    if inc >= threshold and check_recent_trend(fetcher, coin, "SHORT"):
                        outlier = (len(clean) - sum(1 for x in clean if x >= -STABILITY_THRESHOLD))/len(clean)*100
                        passed.append((coin, a, outlier, "SHORT", "income", inc))
            time.sleep(0.15)
        
        scanned = min((batch_idx + 1) * SCAN_BATCH, total)
        quarter = max(1, len(batches) // 4)
        if batch_idx % quarter == quarter - 1 or batch_idx == len(batches) - 1:
            await msg.reply_text(f"⏳ {scanned}/{total} | найдено: {len(passed)} | осталось {total-scanned}")

    _scan_running[chat_id] = False

    if not passed:
        await msg.reply_text(f"Скан {label} завершён: {total} монет\n\nНичего не найдено за {days} дней.")
        return

    lines = [f"✅ *Скан {label} завершён* — {total} монет за {days} дней\n"]

    if method == "income":
        inc_longs  = [(c,a,o,i) for c,a,o,d,cat,i in passed if d == "LONG"]
        inc_shorts = [(c,a,o,i) for c,a,o,d,cat,i in passed if d == "SHORT"]
        lines.append(f"💰 *Средний доход* ≥${threshold:.0f}/день:")
        if inc_longs:
            lines.append(f"\n🟢 *ЛОНГ* ({len(inc_longs)}):")
            for c,a,o,i in sorted(inc_longs, key=lambda x: -x[3]):
                lines.append(f"  `{c}` avg `{a:+.4f}%` ~${i:.1f}/день выбр `{o:.0f}%`")
        if inc_shorts:
            lines.append(f"\n🔴 *ШОРТ* ({len(inc_shorts)}):")
            for c,a,o,i in sorted(inc_shorts, key=lambda x: -x[3]):
                lines.append(f"  `{c}` avg `{a:+.4f}%` ~${i:.1f}/день выбр `{o:.0f}%`")
    else:
        for t_dir, t_name in [("LONG", "ЛОНГ"), ("SHORT", "ШОРТ")]:
            for t_cat, t_c_name, t_ico in [("full", "ПОДХОДЯТ", "✅"), ("partial", "ЧАСТИЧНО", "⚡")]:
                subset = [(c,a,o) for c,a,o,d,cat,_ in passed if d==t_dir and cat==t_cat]
                if subset:
                    lines.append(f"\n{t_ico} {'🟢' if t_dir=='LONG' else '🔴'} *{t_name} — {t_c_name}* ({len(subset)}):")
                    for c,a,o in sorted(subset, key=lambda x: x[1] if t_dir=="LONG" else -x[1]):
                        lines.append(f"  `{c}` avg `{a:+.4f}%` выбр `{o:.0f}%`")

    reply = "\n".join(lines)
    if len(reply) > 4000:
        for chunk in [reply[i:i+4000] for i in range(0, len(reply), 4000)]:
            await msg.reply_text(chunk, parse_mode="Markdown")
    else:
        await msg.reply_text(reply, parse_mode="Markdown")

    if not GEMINI_API_KEY or not use_ai:
        return

    if method == "income":
        target_coins = [(c, d, a) for c, a, o, d, cat, i in passed]
    else:
        target_coins = [(c, d, a) for c, a, o, d, cat, _ in passed if cat in ("full", "partial")]

    if not target_coins:
        return

    await msg.reply_text(
        f"🤖 *Gemini анализирует {len(target_coins)} монет...* Это займёт время.",
        parse_mode="Markdown"
    )

    for coin, direction, avg_rate in target_coins:
        analysis = gemini_analyze_coin(coin, direction, avg_rate, exchange, days)
        if analysis:
            ans_up = analysis[:150].upper()
            if "НЕ РЕКОМЕНДУЕТСЯ" in ans_up or "КАТЕГОРИЧЕСКИ НЕТ" in ans_up:
                continue
            
            dir_icon = "🟢 ЛОНГ" if direction == "LONG" else "🔴 ШОРТ"
            await msg.reply_text(f"🤖 *{coin}* | {dir_icon}", parse_mode="Markdown")
            
            if len(analysis) > 4000:
                for chunk in [analysis[i:i+4000] for i in range(0, len(analysis), 4000)]:
                    await msg.reply_text(chunk)
            else:
                await msg.reply_text(analysis)
        time.sleep(10)

# ─────────────────────────────────────────────
# АВТО-СКАН (ЕЖЕДНЕВНО)
# ─────────────────────────────────────────────
AUTO_SCAN_AMOUNT    = 20000
AUTO_SCAN_THRESHOLD = 29
AUTO_SCAN_DAYS      = 3
AUTO_SCAN_EXCHANGES = ["phemex", "xt", "toobit", "coinw", "okx", "bingx"]

async def auto_scan_job(context):
    if not REPORT_CHAT_ID: return
    chat_id = int(REPORT_CHAT_ID)
    now_str = datetime.now(timezone.utc).strftime("%d.%m.%Y %H:%M UTC")

    await context.bot.send_message(
        chat_id=chat_id,
        text=f"🕗 *Авто-скан* {now_str}\nМетод: доходность ${AUTO_SCAN_AMOUNT:,} ≥${AUTO_SCAN_THRESHOLD}/день за {AUTO_SCAN_DAYS} дней",
        parse_mode="Markdown"
    )

    all_passed = []
    for exchange in AUTO_SCAN_EXCHANGES:
        if not EXCHANGES_ENABLED.get(exchange): continue
        label = EXCHANGE_LABELS.get(exchange, exchange)
        
        try:
            if exchange == "coinw":
                r = requests.get("https://api.coinw.com/v1/perpum/instruments", timeout=15)
                coins = [x["base"].upper() for x in r.json().get("data", [])]
            else:
                coins = phemex_get_all_symbols()
        except Exception as e:
            await context.bot.send_message(chat_id=chat_id, text=f"❌ {label}: ошибка получения монет: {e}")
            continue

        fetcher = phemex_fetch if exchange == "phemex" else EXCHANGE_FETCHERS.get(exchange)
        if not fetcher: continue

        now_ms = int(time.time() * 1000)
        start_ms = now_ms - AUTO_SCAN_DAYS * 24 * 60 * 60 * 1000
        passed_exchange = []

        for coin in coins:
            try: rows, _ = fetcher(coin, start_ms, now_ms)
            except Exception: rows = []
            if not rows: continue
            clean = [r for _, r in rows if abs(r) <= 0.8]
            if not clean: continue

            a = sum(clean)/len(clean)
            if (len([r for r in clean if r < 0])/len(clean)) >= MIN_NEG_RATIO and a < 0:
                if (AUTO_SCAN_AMOUNT * abs(a)/100 * (len(clean)/AUTO_SCAN_DAYS)) >= AUTO_SCAN_THRESHOLD and check_recent_trend(fetcher, coin, "LONG"):
                    outlier = (len(clean) - sum(1 for r in clean if r <= STABILITY_THRESHOLD))/len(clean)*100
                    passed_exchange.append((coin, a, outlier, "LONG", label))
                    all_passed.append((coin, a, outlier, "LONG", label))
            elif (len([r for r in clean if r > 0])/len(clean)) >= MIN_POS_RATIO and a > 0:
                if (AUTO_SCAN_AMOUNT * abs(a)/100 * (len(clean)/AUTO_SCAN_DAYS)) >= AUTO_SCAN_THRESHOLD and check_recent_trend(fetcher, coin, "SHORT"):
                    outlier = (len(clean) - sum(1 for r in clean if r >= -STABILITY_THRESHOLD))/len(clean)*100
                    passed_exchange.append((coin, a, outlier, "SHORT", label))
                    all_passed.append((coin, a, outlier, "SHORT", label))
            time.sleep(0.1)

        if passed_exchange:
            lines = [f"📊 *{label}* — найдено {len(passed_exchange)}:"]
            for d_name, d_val, d_ico in [("ЛОНГ", "LONG", "🟢"), ("ШОРТ", "SHORT", "🔴")]:
                subset = [(c,a,o) for c,a,o,d,_ in passed_exchange if d == d_val]
                if subset:
                    lines.append(f"\n{d_ico} *{d_name}* ({len(subset)}):")
                    for c,a,o in sorted(subset, key=lambda x: x[1] if d_val=="LONG" else -x[1]):
                        lines.append(f"  `{c}` avg `{a:+.4f}%` выбр `{o:.0f}%`")
            await context.bot.send_message(chat_id=chat_id, text="\n".join(lines), parse_mode="Markdown")

    await context.bot.send_message(
        chat_id=chat_id,
        text=f"✅ *Авто-скан завершён* — всего найдено: {len(all_passed)} монет",
        parse_mode="Markdown"
    )

    if not GEMINI_API_KEY or not all_passed: return

    await context.bot.send_message(
        chat_id=chat_id,
        text=f"🤖 *Gemini анализирует {len(all_passed)} монет...*",
        parse_mode="Markdown"
    )

    seen_coins = set()
    for coin, avg_rate, outlier, direction, exch_label in all_passed:
        if coin in seen_coins: continue
        seen_coins.add(coin)
        analysis = gemini_analyze_coin(coin, direction, avg_rate, exch_label, AUTO_SCAN_DAYS)
        if analysis:
            ans_up = analysis[:150].upper()
            if "НЕ РЕКОМЕНДУЕТСЯ" in ans_up or "КАТЕГОРИЧЕСКИ НЕТ" in ans_up:
                continue

            dir_icon = "🟢 ЛОНГ" if direction == "LONG" else "🔴 ШОРТ"
            await context.bot.send_message(
                chat_id=chat_id, text=f"🤖 *{coin}* | {dir_icon}", parse_mode="Markdown"
            )
            if len(analysis) > 4000:
                for chunk in [analysis[i:i+4000] for i in range(0, len(analysis), 4000)]:
                    await context.bot.send_message(chat_id=chat_id, text=chunk)
            else:
                await context.bot.send_message(chat_id=chat_id, text=analysis)
        time.sleep(10)

# ─────────────────────────────────────────────
# ОСНОВНЫЕ КОМАНДЫ (START/HELP/SETTINGS)
# ─────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ex_str = ", ".join([EXCHANGE_LABELS.get(e, e) for e, on in EXCHANGES_ENABLED.items() if on])
    text = (
        "👋 *Phemex + XT Funding Rate Analyzer*\n\n"
        f"Активные биржи: `{ex_str}`\n\n"
        "Команды:\n"
        "/analyze — быстрый скан монет (без AI)\n"
        "/analyze_ai — скан с детальным анализом Gemini AI\n"
        "/filter — анализ монет по фильтрам фандинга\n"
        "/funding — ставки фандинга по монете\n"
        "/calculator — калькулятор дохода от фандинга\n"
        "/settings — настройки и управление биржами\n"
        "/help — справка"
    )
    await update.message.reply_text(text, parse_mode="Markdown")

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await cmd_start(update, context)

def make_settings_keyboard():
    buttons = []
    row = []
    for ex, enabled in EXCHANGES_ENABLED.items():
        row.append(InlineKeyboardButton(f"{'✅' if enabled else '❌'} {EXCHANGE_LABELS.get(ex, ex.upper())}", callback_data=f"set_ex_{ex}"))
        if len(row) == 3: buttons.append(row); row = []
    if row: buttons.append(row)
    buttons.append([InlineKeyboardButton("✅ Все ВКЛ", callback_data="set_ex_all_on"), InlineKeyboardButton("❌ Все ВЫКЛ", callback_data="set_ex_all_off")])
    buttons.append([InlineKeyboardButton("✖️ Закрыть", callback_data="set_close")])
    return InlineKeyboardMarkup(buttons)

async def cmd_settings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "⚙️ *Настройки*\n\nВключай и выключай нужные биржи для сканирования:",
        reply_markup=make_settings_keyboard(), parse_mode="Markdown"
    )

async def settings_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if q.data == "set_close": await q.edit_message_text("⚙️ Настройки закрыты."); return
    if q.data == "set_ex_all_on":
        for ex in EXCHANGES_ENABLED: EXCHANGES_ENABLED[ex] = True
    elif q.data == "set_ex_all_off":
        for ex in EXCHANGES_ENABLED: EXCHANGES_ENABLED[ex] = False
    elif q.data.startswith("set_ex_"):
        ex = q.data.replace("set_ex_", "")
        EXCHANGES_ENABLED[ex] = not EXCHANGES_ENABLED.get(ex, False)
    try: await q.edit_message_reply_markup(reply_markup=make_settings_keyboard())
    except: pass

async def unknown(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Не знаю такой команды. Напиши /help.")

async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.edit_message_text("Отменено.")
    else:
        await update.message.reply_text("Отменено.")
    return ConversationHandler.END

# ─────────────────────────────────────────────
# КОЛЛБЕКИ ДЛЯ /analyze И /analyze_ai
# ─────────────────────────────────────────────

async def cmd_analyze_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    context.user_data["use_ai"] = False
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("Phemex", callback_data="an_ex_phemex"), InlineKeyboardButton("Toobit", callback_data="an_ex_toobit")],
        [InlineKeyboardButton("XT", callback_data="an_ex_xt"), InlineKeyboardButton("CoinW", callback_data="an_ex_coinw")],
        [InlineKeyboardButton("OKX", callback_data="an_ex_okx"), InlineKeyboardButton("BingX", callback_data="an_ex_bingx")],
        [InlineKeyboardButton("Отмена", callback_data="an_cancel")],
    ])
    await update.message.reply_text("🔍 Быстрый скан (БЕЗ AI)\n\nШаг 1/3: Выбери биржу:", reply_markup=keyboard)
    return AN_METHOD

async def cmd_analyze_start_ai(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    context.user_data["use_ai"] = True
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("Phemex", callback_data="an_ex_phemex"), InlineKeyboardButton("Toobit", callback_data="an_ex_toobit")],
        [InlineKeyboardButton("XT", callback_data="an_ex_xt"), InlineKeyboardButton("CoinW", callback_data="an_ex_coinw")],
        [InlineKeyboardButton("OKX", callback_data="an_ex_okx"), InlineKeyboardButton("BingX", callback_data="an_ex_bingx")],
        [InlineKeyboardButton("Отмена", callback_data="an_cancel")],
    ])
    await update.message.reply_text("🤖 Скан с Gemini AI\n\nШаг 1/3: Выбери биржу:", reply_markup=keyboard)
    return AN_METHOD

async def an_exchange_btn(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if q.data == "an_cancel": await q.edit_message_text("Отменено."); return ConversationHandler.END
    context.user_data["an_exchange"] = q.data.replace("an_ex_", "")
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("Средняя ставка", callback_data="an_method_rate"), InlineKeyboardButton("Средний доход", callback_data="an_method_income")],
        [InlineKeyboardButton("Отмена", callback_data="an_cancel")],
    ])
    await q.edit_message_text(f"Шаг 2/3: Выбери метод анализа:", reply_markup=keyboard)
    return AN_METHOD

async def an_method_btn(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if q.data == "an_cancel": await q.edit_message_text("Отменено."); return ConversationHandler.END
    method = q.data.replace("an_method_", "")
    context.user_data["an_method"] = method
    if method == "income":
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("$15,000", callback_data="an_amt_15000"), InlineKeyboardButton("$20,000", callback_data="an_amt_20000")],
            [InlineKeyboardButton("$25,000", callback_data="an_amt_25000"), InlineKeyboardButton("Другая", callback_data="an_amt_other")],
            [InlineKeyboardButton("Отмена", callback_data="an_cancel")],
        ])
        await q.edit_message_text("Шаг: Введи сумму позиции (USDT):", reply_markup=keyboard)
        return AN_AMT
    else:
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("3 дня", callback_data="an_days_3"), InlineKeyboardButton("7 дней", callback_data="an_days_7")],
            [InlineKeyboardButton("14 дней", callback_data="an_days_14"), InlineKeyboardButton("Другой", callback_data="an_days_other")],
            [InlineKeyboardButton("Отмена", callback_data="an_cancel")],
        ])
        await q.edit_message_text("Шаг 3/3: Выбери период анализа:", reply_markup=keyboard)
        return AN_DAYS

async def an_amt_btn(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if q.data == "an_cancel": await q.edit_message_text("Отменено."); return ConversationHandler.END
    if q.data == "an_amt_other":
        await q.edit_message_text("Введи сумму в USDT, например `30000`:", parse_mode="Markdown")
        return AN_AMT_NUM
    amount = float(q.data.replace("an_amt_", ""))
    context.user_data["an_amount"] = amount
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("$20", callback_data="an_thr_20"), InlineKeyboardButton("$25", callback_data="an_thr_25")],
        [InlineKeyboardButton("$40", callback_data="an_thr_40"), InlineKeyboardButton("$50", callback_data="an_thr_50")],
        [InlineKeyboardButton("Другое", callback_data="an_thr_other")],
    ])
    await q.edit_message_text(f"Сумма: ${amount:,.0f}\n\nМинимальный доход в день:", reply_markup=keyboard)
    return AN_THRESH

async def an_amt_num(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        amount = float(update.message.text.strip().replace("$","").replace(",",""))
        if amount <= 0: raise ValueError
    except ValueError:
        await update.message.reply_text("Введи число, например `30000`:", parse_mode="Markdown")
        return AN_AMT_NUM
    context.user_data["an_amount"] = amount
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("$20", callback_data="an_thr_20"), InlineKeyboardButton("$25", callback_data="an_thr_25")],
        [InlineKeyboardButton("$40", callback_data="an_thr_40"), InlineKeyboardButton("$50", callback_data="an_thr_50")],
        [InlineKeyboardButton("Другое", callback_data="an_thr_other")],
    ])
    await update.message.reply_text(f"Сумма: ${amount:,.0f}\n\nМинимальный доход в день:", reply_markup=keyboard)
    return AN_THRESH

async def an_thresh_btn(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if q.data == "an_cancel": await q.edit_message_text("Отменено."); return ConversationHandler.END
    if q.data == "an_thr_other":
        await q.edit_message_text("Введи минимальный доход в день ($), например `30`:", parse_mode="Markdown")
        return AN_THRESH_NUM
    context.user_data["an_threshold"] = float(q.data.replace("an_thr_", ""))
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("3 дня", callback_data="an_days_3"), InlineKeyboardButton("7 дней", callback_data="an_days_7")],
        [InlineKeyboardButton("14 дней", callback_data="an_days_14"), InlineKeyboardButton("Другой", callback_data="an_days_other")],
    ])
    await q.edit_message_text(f"Шаг 3/3: Выбери период анализа:", reply_markup=keyboard)
    return AN_DAYS

async def an_thresh_num(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        threshold = float(update.message.text.strip().replace("$",""))
        if threshold <= 0: raise ValueError
    except ValueError:
        await update.message.reply_text("Введи число, например `30`:", parse_mode="Markdown")
        return AN_THRESH_NUM
    context.user_data["an_threshold"] = threshold
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("3 дня", callback_data="an_days_3"), InlineKeyboardButton("7 дней", callback_data="an_days_7")],
        [InlineKeyboardButton("14 дней", callback_data="an_days_14"), InlineKeyboardButton("Другой", callback_data="an_days_other")],
    ])
    await update.message.reply_text(f"Шаг 3/3: Выбери период анализа:", reply_markup=keyboard)
    return AN_DAYS

async def an_days_btn(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if q.data == "an_cancel": await q.edit_message_text("Отменено."); return ConversationHandler.END
    if q.data == "an_days_other":
        await q.edit_message_text("Введи количество дней числом, например `30`:", parse_mode="Markdown")
        return AN_DAYS_NUM
    context.user_data["an_days"] = int(q.data.replace("an_days_", ""))
    await q.edit_message_text("Запускаю скан...")
    await an_run_scan(q, context)
    return ConversationHandler.END

async def an_days_num(update: Update, context: ContextTypes.DEFAULT_TYPE):
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

# ─────────────────────────────────────────────
# MAIN РЕГИСТРАЦИЯ КОМАНД
# ─────────────────────────────────────────────

def main():
    if not BOT_TOKEN:
        raise ValueError("BOT_TOKEN не задан!")

    app = Application.builder().token(BOT_TOKEN).build()

    analyze_conv = ConversationHandler(
        entry_points=[
            CommandHandler("analyze", cmd_analyze_start),
            CommandHandler("analyze_ai", cmd_analyze_start_ai)
        ],
        states={
            AN_METHOD:     [CallbackQueryHandler(an_exchange_btn, pattern="^an_ex_"), CallbackQueryHandler(an_method_btn, pattern="^an_method_"), CallbackQueryHandler(cmd_cancel, pattern="^an_cancel$")],
            AN_AMT:        [CallbackQueryHandler(an_amt_btn, pattern="^an_amt_"), CallbackQueryHandler(cmd_cancel, pattern="^an_cancel$")],
            AN_AMT_NUM:    [MessageHandler(filters.TEXT & ~filters.COMMAND, an_amt_num)],
            AN_THRESH:     [CallbackQueryHandler(an_thresh_btn, pattern="^an_thr_"), CallbackQueryHandler(cmd_cancel, pattern="^an_cancel$")],
            AN_THRESH_NUM: [MessageHandler(filters.TEXT & ~filters.COMMAND, an_thresh_num)],
            AN_DAYS:       [CallbackQueryHandler(an_days_btn, pattern="^an_days_"), CallbackQueryHandler(cmd_cancel, pattern="^an_cancel$")],
            AN_DAYS_NUM:   [MessageHandler(filters.TEXT & ~filters.COMMAND, an_days_num)],
        },
        fallbacks=[CommandHandler("cancel", cmd_cancel)],
    )

    app.add_handler(CommandHandler("start",    cmd_start))
    app.add_handler(CommandHandler("help",     cmd_help))
    app.add_handler(CommandHandler("settings", cmd_settings))
    app.add_handler(CallbackQueryHandler(settings_callback, pattern="^set_"))
    
    app.add_handler(analyze_conv)
    app.add_handler(CommandHandler("cancel", cmd_cancel))
    app.add_handler(MessageHandler(filters.COMMAND, unknown))

    if REPORT_CHAT_ID and app.job_queue:
        from datetime import time as dt_time
        app.job_queue.run_daily(
            auto_scan_job,
            time=dt_time(hour=17, minute=0, second=0), # 17:00 UTC = 20:00 Киев
            name="daily_auto_scan",
        )
        print("✅ Авто-скан запланирован на 17:00 UTC (20:00 Киев)")
    else:
        print("⚠️ REPORT_CHAT_ID не задан — авто-скан отключён")

    print("Бот запущен...")
    app.run_polling()

if __name__ == "__main__":
    main()
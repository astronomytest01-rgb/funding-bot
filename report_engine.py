"""
report_engine.py — Движок ежевечернего отчёта по фандингу.

Подключение к bot.py (две строки в main()):
    from report_engine import register_report_handlers
    register_report_handlers(app)

Команды которые добавляет этот модуль:
    /report — принудительный запуск отчёта
    Автозапуск каждый день в 20:00 UTC+2 (18:00 UTC)

Ничего в bot.py не меняет. Использует fetcher-функции и analyze_rates из bot.py.
"""

import asyncio
import time
import logging
import requests
from datetime import datetime, timezone, time as dtime
from typing import Optional

from telegram import Update
from telegram.ext import CommandHandler, ContextTypes

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# КОНФИГУРАЦИЯ ОТЧЁТА
# Меняй здесь — не трогая bot.py
# ─────────────────────────────────────────────────────────────────────────────

# Биржи для скана фандинга (фаза 1)
# Toobit: список монет берётся из Phemex, fetcher фандинга работает
REPORT_EXCHANGES = ["coinw", "xt", "phemex", "toobit", "bingx", "gate"]

# Период анализа фандинга
REPORT_DAYS = 7

# Минимальный объём торгов на бирже (24h, USDT)
MIN_VOLUME_USDT = 300_000

# Максимальный спред входа между двумя биржами (%)
MAX_SPREAD_PCT = -0.5

# Порог avg ставки хедж-биржи — считаем "около нуля"
HEDGE_RATE_THRESHOLD = 0.05   # abs(avg) <= 0.05% → подходит для хеджа

# Размер порции монет за один проход
BATCH_SIZE = 10

# Пауза между монетами внутри порции (сек) — не перегружаем API
COIN_SLEEP = 0.3

# Пауза между порциями (сек)
BATCH_SLEEP = 2.0

# Время автозапуска: 20:00 по Киеву (UTC+3 летом / UTC+2 зимой)
# Ставим 17:00 UTC — это 20:00 UTC+3 (летнее время)
REPORT_HOUR_UTC = 17
REPORT_MINUTE_UTC = 0

# Маржа для расчёта дохода
MARGIN_USD = 5_000
LEVERAGE_LIST = [2, 3, 4]   # плечи для расчёта

# Метод "средний доход": позиция $20,000, минимум $25/день
INCOME_POSITION = 20_000    # размер позиции для фильтра по доходу
INCOME_MIN_DAY  = 25.0      # минимальный доход в день ($)
PAYMENTS_PER_DAY = 3        # выплат в день (каждые 8ч)

# Фильтр актуальности: последние ставки должны подтверждать направление
RECENT_RATES_COUNT = 6      # последние 6 ставок = ~2 дня (при 8ч интервале)
RECENT_MIN_RATIO   = 0.60   # минимум 60% последних ставок должны совпадать с направлением

# Антифлуд: один отчёт за раз
_report_running = False

# ─────────────────────────────────────────────────────────────────────────────
# GEMINI API — анализ надёжности монеты
# ─────────────────────────────────────────────────────────────────────────────

import os
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
GROQ_API_KEY   = os.environ.get("GROQ_API_KEY", "")
GEMINI_URL = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    "gemini-2.0-flash:generateContent"
)
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
GROQ_MODEL = "llama-3.3-70b-versatile"

def gemini_analyze(coin: str, direction: str, avg_rate: float,
                   main_ex: str, hedge_ex: str,
                   vol_main: float = 0, vol_hedge: float = 0,
                   days_stable: int = 0) -> dict:
    """AI анализ монеты через Groq (primary) или Gemini (fallback)."""

    if not GROQ_API_KEY and not GEMINI_API_KEY:
        return {"approved": True, "leverage": None, "risk": "—",
                "summary": "AI ключ не задан", "reason": ""}

    dir_text    = "ЛОНГ" if direction == "LONG" else "ШОРТ"
    funding_dir = "отрицательный (шорт платит лонгу)" if direction == "LONG" else "положительный (лонг платит шорту)"
    daily_pct   = abs(avg_rate) * 3
    vol_m_str   = f"${vol_main/1_000_000:.2f}M" if vol_main < 999_999_990 else "неизвестен"
    vol_h_str   = f"${vol_hedge/1_000_000:.2f}M" if vol_hedge < 999_999_990 else "неизвестен"
    days_str    = f"{days_stable} дней" if days_stable > 0 else "неизвестно"

    SYSTEM_PROMPT = """Ты — профессиональный крипто-трейдер с глубокими знаниями рынка, специализирующийся на дельта-нейтральных стратегиях сбора фандинга (фьючерс-фьючерс). Горизонт позиций — 1-4 недели. Стиль — «поставил и забыл».

ВАЖНО: Используй свои знания о монетах. "Нет информации" — НЕ причина для отклонения. Оценивай монету по её природе, возрасту, ликвидности и репутации на рынке.

Протокол анализа (5 фильтров):
1. РИСК ЛИКВИДАЦИИ: если монета исторически даёт свечи 30%+ за час → плечо не выше х2
2. СТРЕСС-ТЕСТ: х2=50% запас, х3=33%, х4=25% — сопоставь с реальной волатильностью монеты
3. ФУНДАМЕНТАЛ:
   ОТКЛОНЯЙ только если: монета явно <3 мес И малоизвестная, ИЛИ явный риск делистинга, ИЛИ это pump&dump схема без сообщества
   ОДОБРЯЙ если: монета существует >6 мес, есть на CoinGecko/CMC, торгуется на крупных биржах
   Оценивай каждую монету независимо на основе текущих знаний — прошлая стабильность не гарантирует будущую
4. ДОХОДНОСТЬ: считай прибыль на $20k позиции в день
5. ПЛЕЧО: х1-2 для мем/новых/волатильных, х2-3 для зрелых альтов, х3-4 только для топ ликвидных (BTC, ETH, SOL, крупные DeFi)

НЕ отклоняй монету из-за "отсутствия информации" — это запрещено. Используй свои знания о крипторынке.
Отвечай СТРОГО в формате JSON без markdown."""

    USER_PROMPT = f"""Монета: {coin}
Направление: {dir_text} на {main_ex}, хедж на {hedge_ex}
Фандинг: {funding_dir}, ставка {avg_rate:+.4f}%/выплату (каждые 8ч)
Доходность ~{daily_pct:.3f}%/день от позиции
Объём {main_ex}: {vol_m_str}/24h, {hedge_ex}: {vol_h_str}/24h
Фандинг держится: {days_str}

Используй свои знания о монете {coin} для оценки рисков.
Ответ строго JSON без markdown:
{{"approved": true/false, "leverage": 2/3/4 или null, "risk": "низкий"/"средний"/"высокий", "summary": "2-3 предложения: что за проект, почему подходит или нет для удержания 1-4 недели с плечом", "reason": "если отклонено — конкретная причина (не 'нет информации'); если одобрено — пустая строка"}}"""

    import re as _re, json as _json

    # ── Groq (primary) ────────────────────────────────────────────────────────
    if GROQ_API_KEY:
        try:
            r = requests.post(
                GROQ_URL,
                headers={"Authorization": f"Bearer {GROQ_API_KEY}",
                         "Content-Type": "application/json"},
                json={"model": GROQ_MODEL,
                      "messages": [{"role": "system", "content": SYSTEM_PROMPT},
                                   {"role": "user",   "content": USER_PROMPT}],
                      "temperature": 0.2, "max_tokens": 500},
                timeout=25
            )
            if r.status_code == 200:
                text = _re.sub(r"```(?:json)?", "", r.json()["choices"][0]["message"]["content"]).strip()
                result = _json.loads(text)
                return {"approved": bool(result.get("approved", True)),
                        "leverage": result.get("leverage"),
                        "risk":     result.get("risk", "—"),
                        "summary":  result.get("summary", ""),
                        "reason":   result.get("reason", "")}
            elif r.status_code == 429:
                logger.warning(f"Groq rate limit {coin}, ждём 15с...")
                time.sleep(15)
        except Exception as e:
            logger.warning(f"Groq error {coin}: {e}")

    # ── Gemini (fallback) ─────────────────────────────────────────────────────
    if GEMINI_API_KEY:
        try:
            payload = {
                "system_instruction": {"parts": [{"text": SYSTEM_PROMPT}]},
                "contents": [{"parts": [{"text": USER_PROMPT}]}],
                "generationConfig": {"temperature": 0.2, "maxOutputTokens": 500}
            }
            for attempt in range(3):
                r = requests.post(GEMINI_URL, params={"key": GEMINI_API_KEY},
                                  json=payload, timeout=25)
                if r.status_code == 429:
                    time.sleep(15 * (attempt + 1))
                    continue
                r.raise_for_status()
                break
            else:
                raise Exception("Gemini rate limit")
            text = _re.sub(r"```(?:json)?", "", r.json()["candidates"][0]["content"]["parts"][0]["text"]).strip()
            result = _json.loads(text)
            return {"approved": bool(result.get("approved", True)),
                    "leverage": result.get("leverage"),
                    "risk":     result.get("risk", "—"),
                    "summary":  result.get("summary", ""),
                    "reason":   result.get("reason", "")}
        except Exception as e:
            logger.warning(f"Gemini error {coin}: {e}")

    return {"approved": True, "leverage": None, "risk": "—",
            "summary": "Ошибка AI анализа", "reason": ""}

def _sym_phemex(coin: str) -> str:
    return f"{coin.upper()}USDT"

def _sym_xt(coin: str) -> str:
    return f"{coin.lower()}_usdt"

def _sym_bingx(coin: str) -> str:
    return f"{coin.upper()}-USDT"

def _sym_gate(coin: str) -> str:
    return f"{coin.upper()}_USDT"

def _sym_toobit(coin: str) -> str:
    return f"{coin.upper()}-SWAP-USDT"

def _sym_coinw(coin: str) -> str:
    # CoinW использует Supabase — для volume/orderbook нет прямого API
    # Используем публичный эндпоинт
    return f"{coin.upper()}USDT"

# ─────────────────────────────────────────────────────────────────────────────
# ─────────────────────────────────────────────────────────────────────────────
# ВОЛАТИЛЬНОСТЬ: дневная и недельная из OHLCV
# Берём дневные свечи с Phemex (самый надёжный публичный OHLCV API)
# Волатильность = среднее (high-low)/open * 100 за N свечей
# ─────────────────────────────────────────────────────────────────────────────

def get_volatility(coin: str) -> dict:
    """
    Возвращает волатильность монеты в процентах:
    {
        "day_pct":  float,  # средняя дневная волатильность за 7 дней
        "week_pct": float,  # недельная волатильность (high-low за 7 дней)
    }
    Использует Phemex OHLCV (дневные свечи).
    Возвращает {"day_pct": 0, "week_pct": 0} при ошибке.
    """
    try:
        sym = f"{coin.upper()}USDT"
        r = requests.get(
            "https://api.phemex.com/md/v2/kline",
            params={
                "symbol":     sym,
                "resolution": 86400,   # дневные свечи
                "limit":      8,       # 8 свечей = ~7 торговых дней
            },
            timeout=10
        )
        data = r.json().get("result", {})
        rows = data.get("rows", [])
        # Phemex OHLCV: [timestamp, interval, last_close, open, high, low, close, volume, turnover]
        if not rows or len(rows) < 2:
            return {"day_pct": 0, "week_pct": 0}

        day_vols = []
        highs = []
        lows  = []
        for row in rows[-7:]:  # последние 7 дней
            try:
                open_p = float(row[3]) / 1e4
                high_p = float(row[4]) / 1e4
                low_p  = float(row[5]) / 1e4
                if open_p > 0:
                    day_vols.append((high_p - low_p) / open_p * 100)
                highs.append(high_p)
                lows.append(low_p)
            except Exception:
                continue

        if not day_vols:
            return {"day_pct": 0, "week_pct": 0}

        day_pct  = sum(day_vols) / len(day_vols)
        week_pct = (max(highs) - min(lows)) / min(lows) * 100 if min(lows) > 0 else 0

        return {"day_pct": round(day_pct, 1), "week_pct": round(week_pct, 1)}

    except Exception:
        return {"day_pct": 0, "week_pct": 0}


# ФАЗА 2: ОБЪЁМ ТОРГОВ 24H
# ─────────────────────────────────────────────────────────────────────────────

def get_volume_24h(coin: str, exchange: str) -> float:
    """Возвращает 24h volume в USDT. 0.0 если не удалось получить.

    Toobit и CoinW не имеют рабочего публичного API для объёма —
    возвращаем None-sentinel 999_999_999 чтобы не блокировать их монеты.
    """
    try:
        coin = coin.upper()

        if exchange == "phemex":
            # v2 ticker: поле turnoverRv уже в USDT (не сатоши)
            sym = _sym_phemex(coin)
            r = requests.get(
                "https://api.phemex.com/md/v2/ticker/24hr",
                params={"symbol": sym}, timeout=8
            )
            d = r.json().get("result", {})
            return float(d.get("turnoverRv", 0) or 0)

        elif exchange == "xt":
            # agg-ticker: поле "v" = quote volume в USDT
            sym = _sym_xt(coin)
            r = requests.get(
                "https://fapi.xt.com/future/market/v1/public/q/agg-ticker",
                params={"symbol": sym}, timeout=8
            )
            d = r.json().get("result", {})
            return float(d.get("v", 0) or 0)

        elif exchange == "bingx":
            sym = _sym_bingx(coin)
            r = requests.get(
                "https://open-api.bingx.com/openApi/swap/v2/quote/ticker",
                params={"symbol": sym}, timeout=8
            )
            d = r.json().get("data", {})
            return float(d.get("quoteVolume", 0) or 0)

        elif exchange == "gate":
            sym = _sym_gate(coin)
            r = requests.get(
                "https://api.gateio.ws/api/v4/futures/usdt/tickers",
                params={"contract": sym}, timeout=8
            )
            items = r.json()
            if items:
                return float(items[0].get("volume_24h_quote", 0) or 0)
            return 0.0

        elif exchange in ("toobit", "coinw"):
            # Toobit: /quote/v1/ticker/24hr с символом BTCUSDT, поле qv
            # CoinW: публичный API объёма недоступен
            if exchange == "toobit":
                sym = f"{coin}USDT"
                r = requests.get(
                    "https://api.toobit.com/quote/v1/ticker/24hr",
                    params={"symbol": sym}, timeout=8
                )
                items = r.json()
                if isinstance(items, list) and items:
                    return float(items[0].get("qv", 0) or 0)
                return 0.0
            else:
                # CoinW — нет публичного API, пропускаем фильтр
                return 999_999_999.0

    except Exception:
        pass
    return 0.0


# ─────────────────────────────────────────────────────────────────────────────
# ФАЗА 3: СПРЕД ВХОДА МЕЖДУ ДВУМЯ БИРЖАМИ
# Берём топ-5 заявок orderbook на каждой бирже → средняя цена
# Спред = (средняя_ask_шорт / средняя_bid_лонг * 100) - 100
# ─────────────────────────────────────────────────────────────────────────────

def get_orderbook_top5(coin: str, exchange: str) -> dict:
    """
    Возвращает {'bid': float, 'ask': float} — средние топ-5 цены.
    Toobit и CoinW не имеют рабочего публичного orderbook API —
    возвращаем пустой dict, спред для них будет None (пропускается).
    """
    try:
        coin = coin.upper()

        if exchange == "phemex":
            # md/v2/orderbook: цены в result.orderbook_p, уже строки-float
            sym = _sym_phemex(coin)
            r = requests.get(
                "https://api.phemex.com/md/v2/orderbook",
                params={"symbol": sym}, timeout=8
            )
            ob = r.json().get("result", {}).get("orderbook_p", {})
            bids = [float(x[0]) for x in (ob.get("bids") or [])[:5]]
            asks = [float(x[0]) for x in (ob.get("asks") or [])[:5]]

        elif exchange == "xt":
            sym = _sym_xt(coin)
            r = requests.get(
                "https://fapi.xt.com/future/market/v1/public/q/depth",
                params={"symbol": sym, "level": 5}, timeout=8
            )
            d = r.json().get("result", {})
            bids = [float(x[0]) for x in (d.get("b") or [])[:5]]
            asks = [float(x[0]) for x in (d.get("a") or [])[:5]]

        elif exchange == "bingx":
            sym = _sym_bingx(coin)
            r = requests.get(
                "https://open-api.bingx.com/openApi/swap/v2/quote/depth",
                params={"symbol": sym, "limit": 5}, timeout=8
            )
            d = r.json().get("data", {})
            bids = [float(x[0]) for x in (d.get("bids") or [])[:5]]
            asks = [float(x[0]) for x in (d.get("asks") or [])[:5]]

        elif exchange == "gate":
            sym = _sym_gate(coin)
            r = requests.get(
                "https://api.gateio.ws/api/v4/futures/usdt/order_book",
                params={"contract": sym, "limit": 5}, timeout=8
            )
            d = r.json()
            bids = [float(x["p"]) for x in (d.get("bids") or [])[:5]]
            asks = [float(x["p"]) for x in (d.get("asks") or [])[:5]]

        elif exchange in ("toobit", "coinw"):
            # Публичный orderbook API недоступен — спред не считаем
            return {}

        else:
            return {}

        if not bids or not asks:
            return {}

        return {
            "bid": sum(bids) / len(bids),
            "ask": sum(asks) / len(asks),
        }

    except Exception:
        return {}


def calc_spread(long_ex: str, short_ex: str, coin: str) -> Optional[float]:
    """
    Считает спред входа между двумя биржами.
    Лонг открываем на long_ex по ask.
    Шорт открываем на short_ex по bid.
    Спред = (bid_шорт / ask_лонг * 100) - 100
    Возвращает None если данных нет.
    """
    ob_long  = get_orderbook_top5(coin, long_ex)
    ob_short = get_orderbook_top5(coin, short_ex)

    if not ob_long or not ob_short:
        return None

    ask_long  = ob_long.get("ask")
    bid_short = ob_short.get("bid")

    if not ask_long or not bid_short or ask_long == 0:
        return None

    return (bid_short / ask_long * 100) - 100


# ─────────────────────────────────────────────────────────────────────────────
# ФАЗА 1: СКАН ФАНДИНГА — получить монеты биржи
# ─────────────────────────────────────────────────────────────────────────────

def get_coins_for_exchange(exchange: str) -> list[str]:
    """Возвращает список монет для биржи."""
    try:
        if exchange == "phemex":
            r = requests.get(
                "https://api.phemex.com/exchange/public/cfg/v2/products",
                timeout=15
            )
            products = r.json().get("data", {}).get("products", [])
            return [
                p["symbol"][:-4] for p in products
                if p.get("type") == "PerpetualV2"
                and p.get("quoteCurrency") == "USDT"
                and p.get("status") == "Listed"
                and p["symbol"].endswith("USDT")
            ]

        elif exchange == "xt":
            r = requests.get(
                "https://fapi.xt.com/future/market/v1/public/symbol/list",
                timeout=15
            )
            items = r.json().get("result", [])
            return [
                x["symbol"].replace("_usdt", "").upper()
                for x in items
                if x.get("symbol", "").endswith("_usdt")
            ]

        elif exchange == "bingx":
            r = requests.get(
                "https://open-api.bingx.com/openApi/swap/v2/quote/contracts",
                timeout=15
            )
            items = r.json().get("data", [])
            return [
                x["symbol"].replace("-USDT", "").upper()
                for x in items
                if x.get("symbol", "").endswith("-USDT")
            ]

        elif exchange == "gate":
            r = requests.get(
                "https://api.gateio.ws/api/v4/futures/usdt/contracts",
                timeout=15
            )
            items = r.json()
            return [
                x["name"].replace("_USDT", "").upper()
                for x in items
                if x.get("name", "").endswith("_USDT")
            ]

        elif exchange == "toobit":
            # У Toobit нет публичного endpoint для списка контрактов.
            # Берём монеты из Phemex (самый полный список) —
            # fetcher фандинга сам вернёт пустой список если монеты нет на Toobit.
            r = requests.get(
                "https://api.phemex.com/exchange/public/cfg/v2/products",
                timeout=15
            )
            products = r.json().get("data", {}).get("products", [])
            return [
                p["symbol"][:-4] for p in products
                if p.get("type") == "PerpetualV2"
                and p.get("quoteCurrency") == "USDT"
                and p.get("status") == "Listed"
                and p["symbol"].endswith("USDT")
            ]

        elif exchange == "coinw":
            r = requests.get(
                "https://api.coinw.com/v1/perpum/instruments",
                timeout=15
            )
            items = r.json().get("data", [])
            return [x["base"].upper() for x in items if x.get("base")]

    except Exception as e:
        logger.warning(f"get_coins_for_exchange({exchange}): {e}")
    return []


# ─────────────────────────────────────────────────────────────────────────────
# ОСНОВНОЙ ДВИЖОК ОТЧЁТА
# ─────────────────────────────────────────────────────────────────────────────

async def run_report(bot, chat_id: int):
    """
    Полный пайплайн отчёта. Запускается из /report или по расписанию.
    Работает порциями, не падает на большом числе монет.
    """
    global _report_running
    if _report_running:
        await bot.send_message(chat_id, "⏳ Отчёт уже генерируется, подожди.")
        return

    _report_running = True
    start_time = time.time()

    # Импортируем из bot.py — они уже загружены в память
    try:
        import bot as _bot
        fetchers      = _bot.EXCHANGE_FETCHERS
        labels        = _bot.EXCHANGE_LABELS
        analyze_rates = _bot.analyze_rates
        STAB_THRESH   = _bot.STABILITY_THRESHOLD
        NEG_AVG_THR   = _bot.NEG_AVG_THRESHOLD
        MIN_NEG       = _bot.MIN_NEG_RATIO
    except Exception as e:
        await bot.send_message(chat_id, f"❌ Ошибка импорта bot.py: {e}")
        _report_running = False
        return

    now_ms   = int(time.time() * 1000)
    start_ms = now_ms - REPORT_DAYS * 24 * 60 * 60 * 1000

    await bot.send_message(
        chat_id,
        f"📊 *Вечерний отчёт по фандингу*\n"
        f"Биржи: {', '.join(labels.get(e, e) for e in REPORT_EXCHANGES)}\n"
        f"Период: {REPORT_DAYS} дней\n\n"
        f"⏳ Фаза 1: Скан фандинга...",
        parse_mode="Markdown"
    )

    # ── ФАЗА 1: Скан фандинга по биржам ─────────────────────────────────────
    # candidates = list of {coin, exchange, direction, avg_rate, category, neg_avg, pos_avg}
    candidates = []
    total_scanned = 0

    for exchange in REPORT_EXCHANGES:
        fetcher = fetchers.get(exchange)
        if not fetcher:
            continue

        label = labels.get(exchange, exchange)
        coins = get_coins_for_exchange(exchange)
        if not coins:
            await bot.send_message(chat_id, f"⚠️ {label}: не удалось получить список монет")
            continue

        await bot.send_message(
            chat_id,
            f"🔍 {label}: {len(coins)} монет..."
        )

        exchange_found = 0
        batches = [coins[i:i+BATCH_SIZE] for i in range(0, len(coins), BATCH_SIZE)]

        for batch_idx, batch in enumerate(batches):
            for coin in batch:
                try:
                    rows, _ = fetcher(coin, start_ms, now_ms)
                except Exception:
                    rows = []

                await asyncio.sleep(COIN_SLEEP)

                if not rows:
                    continue

                rates = [r for _, r in rows]
                if not rates:
                    continue

                result = analyze_rates(rates)
                if not result or result["category"] == "fail":
                    continue

                direction = result["direction"]
                avg_rate  = result["neg_avg"] if direction == "LONG" else result["pos_avg"]

                # ── Фильтр 1: Актуальность — последние ставки подтверждают направление
                recent = rates[-RECENT_RATES_COUNT:] if len(rates) >= RECENT_RATES_COUNT else rates
                if direction == "LONG":
                    recent_ok = sum(1 for r in recent if r < 0) / len(recent)
                else:
                    recent_ok = sum(1 for r in recent if r > 0) / len(recent)
                if recent_ok < RECENT_MIN_RATIO:
                    continue  # фандинг развернулся — пропускаем

                # ── Фильтр 2: Доход — минимум $25/день на позиции $20,000
                daily_income = INCOME_POSITION * abs(avg_rate) / 100 * PAYMENTS_PER_DAY
                if daily_income < INCOME_MIN_DAY:
                    continue  # слишком маленький доход

                total_scanned += 1
                exchange_found += 1
                candidates.append({
                    "coin":        coin,
                    "exchange":    exchange,
                    "direction":   direction,
                    "avg_rate":    avg_rate,
                    "category":    result["category"],
                    "total_rates": result["total"],
                    "recent_ratio": recent_ok,
                    "daily_income": daily_income,
                })

            # Пауза между порциями
            await asyncio.sleep(BATCH_SLEEP)

            # Прогресс каждые 5 порций
            if (batch_idx + 1) % 5 == 0:
                scanned = min((batch_idx + 1) * BATCH_SIZE, len(coins))
                await bot.send_message(
                    chat_id,
                    f"  {label}: {scanned}/{len(coins)} монет, найдено {exchange_found}"
                )

        await bot.send_message(
            chat_id,
            f"  ✅ {label}: готово — найдено {exchange_found} кандидатов"
        )

    if not candidates:
        await bot.send_message(chat_id, "😔 Фаза 1: ни одного кандидата не найдено.")
        _report_running = False
        return

    await bot.send_message(
        chat_id,
        f"📋 Фаза 1 завершена: {len(candidates)} кандидатов из {len(REPORT_EXCHANGES)} бирж\n"
        f"⏳ Фаза 2: Проверка объёма торгов..."
    )

    # ── ФАЗА 2: Объём торгов на основной бирже ≥ $300k ───────────────────────
    vol_passed = []
    for c in candidates:
        vol = get_volume_24h(c["coin"], c["exchange"])
        c["volume"] = vol
        if vol >= MIN_VOLUME_USDT:
            vol_passed.append(c)
        await asyncio.sleep(0.2)

    if not vol_passed:
        await bot.send_message(chat_id, f"😔 Фаза 2: после фильтра объёма ({MIN_VOLUME_USDT/1000:.0f}k) кандидатов не осталось.")
        _report_running = False
        return

    await bot.send_message(
        chat_id,
        f"✅ Фаза 2: {len(vol_passed)} прошли фильтр объёма ≥ ${MIN_VOLUME_USDT/1000:.0f}k\n"
        f"⏳ Фаза 3: Поиск хедж-биржи и объёма на ней..."
    )

    # ── ФАЗА 3: Поиск хедж-биржи + проверка объёма на ней ───────────────────
    # Хедж-биржа — та из REPORT_EXCHANGES где avg_rate ≈ 0, отличная от основной
    hedge_passed = []

    for c in vol_passed:
        coin     = c["coin"]
        main_ex  = c["exchange"]
        direction = c["direction"]   # LONG на main_ex

        best_hedge = None
        best_rate_abs = 999

        for hedge_ex in REPORT_EXCHANGES:
            if hedge_ex == main_ex:
                continue
            fetcher = fetchers.get(hedge_ex)
            if not fetcher:
                continue
            try:
                rows, _ = fetcher(coin, start_ms, now_ms)
            except Exception:
                rows = []
            await asyncio.sleep(COIN_SLEEP)
            if not rows:
                continue
            rates = [r for _, r in rows]
            if not rates:
                continue
            avg = sum(rates) / len(rates)
            # Для хеджа ищем ставку около нуля
            if abs(avg) < best_rate_abs:
                best_rate_abs = abs(avg)
                best_hedge = {"exchange": hedge_ex, "avg_rate": avg}

        if not best_hedge:
            continue

        if best_rate_abs > HEDGE_RATE_THRESHOLD:
            # Ставка слишком большая для хеджа — показываем но помечаем
            best_hedge["warning"] = True

        # Проверяем объём на хедж-бирже
        hedge_vol = get_volume_24h(coin, best_hedge["exchange"])
        best_hedge["volume"] = hedge_vol
        await asyncio.sleep(0.2)

        if hedge_vol < MIN_VOLUME_USDT:
            continue  # объём на хедже слишком мал

        c["hedge"] = best_hedge
        hedge_passed.append(c)

    if not hedge_passed:
        await bot.send_message(chat_id, "😔 Фаза 3: после поиска хеджа кандидатов не осталось.")
        _report_running = False
        return

    await bot.send_message(
        chat_id,
        f"✅ Фаза 3: {len(hedge_passed)} монет с хедж-биржей\n"
        f"⏳ Фаза 4: Расчёт спреда входа..."
    )

    # ── ФАЗА 4: Спред входа между основной и хедж-биржей ────────────────────
    spread_passed = []

    for c in hedge_passed:
        coin     = c["coin"]
        main_ex  = c["exchange"]
        hedge_ex = c["hedge"]["exchange"]
        direction = c["direction"]

        # Лонг на main_ex → покупаем по ask на main_ex
        # Шорт на hedge_ex → продаём по bid на hedge_ex
        if direction == "LONG":
            long_ex  = main_ex
            short_ex = hedge_ex
        else:
            # SHORT на main_ex → лонг на hedge_ex
            long_ex  = hedge_ex
            short_ex = main_ex

        spread = calc_spread(long_ex, short_ex, coin)
        c["spread"] = spread
        await asyncio.sleep(0.3)

        if spread is None:
            # Не удалось получить данные — включаем с пометкой
            c["spread_warning"] = True
            spread_passed.append(c)
        elif spread >= MAX_SPREAD_PCT:
            spread_passed.append(c)
        # else: спред слишком плохой — пропускаем

    if not spread_passed:
        await bot.send_message(chat_id, f"😔 Фаза 4: после фильтра спреда ≥ {MAX_SPREAD_PCT}% кандидатов не осталось.")
        _report_running = False
        return

    await bot.send_message(
        chat_id,
        f"✅ Фаза 4: {len(spread_passed)} прошли фильтр спреда\n"
        f"⏳ Фаза 5: AI анализ надёжности монет (Gemini)..."
    )

    # ── ФАЗА 5: Gemini AI анализ ─────────────────────────────────────────────
    ai_passed = []

    for c in spread_passed:
        coin     = c["coin"]
        main_lbl = labels.get(c["exchange"], c["exchange"])
        hedge_lbl = labels.get(c["hedge"]["exchange"], c["hedge"]["exchange"])

        # Волатильность
        volat = get_volatility(coin)
        c["volatility"] = volat
        await asyncio.sleep(0.2)

        analysis = gemini_analyze(
            coin       = coin,
            direction  = c["direction"],
            avg_rate   = c["avg_rate"],
            main_ex    = main_lbl,
            hedge_ex   = hedge_lbl,
            vol_main   = c.get("volume", 0),
            vol_hedge  = c["hedge"].get("volume", 0),
            days_stable = c.get("days_stable", 0),
        )
        c["ai"] = analysis
        await asyncio.sleep(5)   # Gemini free tier: 15 req/min → 4с минимум, берём 5с

        if analysis["approved"]:
            ai_passed.append(c)
            ok_lev = f"рекомендую x{analysis['leverage']}" if analysis["leverage"] else ""
            await bot.send_message(
                chat_id,
                f"✅ {coin}: одобрено — риск {analysis['risk']} {ok_lev}"
            )
        else:
            await bot.send_message(
                chat_id,
                f"❌ {coin}: отклонено — {analysis['reason']}"
            )

    if not ai_passed:
        await bot.send_message(chat_id, "😔 Фаза 5: все монеты отклонены AI анализом.")
        _report_running = False
        return

    await bot.send_message(
        chat_id,
        f"✅ Фаза 5: {len(ai_passed)} монет прошли AI анализ\n"
        f"⏳ Формирую финальный отчёт..."
    )

    # ── ФИНАЛЬНЫЙ ОТЧЁТ ───────────────────────────────────────────────────────
    elapsed = int(time.time() - start_time)
    now_str = datetime.now(timezone.utc).strftime("%d.%m.%Y %H:%M UTC")

    header = (
        f"🏆 *ВЕЧЕРНИЙ ОТЧЁТ ПО ФАНДИНГУ*\n"
        f"📅 {now_str} | ⏱ {elapsed}с\n"
        f"Найдено монет: *{len(ai_passed)}*\n"
        f"{'─' * 30}"
    )
    await bot.send_message(chat_id, header, parse_mode="Markdown")

    for i, c in enumerate(ai_passed, 1):
        coin       = c["coin"]
        main_ex    = labels.get(c["exchange"], c["exchange"])
        hedge_ex   = labels.get(c["hedge"]["exchange"], c["hedge"]["exchange"])
        direction  = c["direction"]
        avg_rate   = c["avg_rate"]
        hedge_rate = c["hedge"]["avg_rate"]
        vol_main   = c["volume"]
        vol_hedge  = c["hedge"]["volume"]
        spread     = c.get("spread")
        category   = c["category"]
        ai         = c["ai"]

        dir_icon  = "🟢" if direction == "LONG" else "🔴"
        dir_text  = "ЛОНГ" if direction == "LONG" else "ШОРТ"
        cat_icon  = "✅" if category == "full" else "⚡"

        payments_per_day = PAYMENTS_PER_DAY
        daily_rate_pct   = abs(avg_rate) * payments_per_day

        # Доход — показываем только рекомендованное плечо и соседние
        rec_lev = ai.get("leverage") or 3
        income_lines = []
        for lev in LEVERAGE_LIST:
            position = MARGIN_USD * lev
            daily    = position * daily_rate_pct / 100
            weekly   = daily * 7
            rec_mark = " 👈" if lev == rec_lev else ""
            income_lines.append(
                f"  x{lev} (${position/1000:.0f}k): ${daily:.1f}/день · ${weekly:.0f}/нед{rec_mark}"
            )

        # Доход на $20k позиции (основной расчёт)
        daily_20k  = INCOME_POSITION * daily_rate_pct / 100
        weekly_20k = daily_20k * 7

        # Актуальность фандинга
        recent_pct = c.get("recent_ratio", 0) * 100

        # Спред
        if spread is None or c.get("spread_warning"):
            spread_str = "❓ нет данных"
        else:
            spread_ok  = "✅" if spread >= MAX_SPREAD_PCT else "⚠️"
            spread_str = f"{spread_ok} {spread:+.3f}%"

        hedge_warn = " ⚠️ ставка > 0.05%" if c["hedge"].get("warning") else ""

        # AI блок
        risk_emoji = {"низкий": "🟢", "средний": "🟡", "высокий": "🔴"}.get(ai["risk"], "⚪")
        volat     = c.get("volatility", {})
        day_pct   = volat.get("day_pct", 0)
        week_pct  = volat.get("week_pct", 0)
        day_icon  = "🟢" if day_pct < 5 else ("🟡" if day_pct < 10 else "🔴")
        week_icon = "🟢" if week_pct < 20 else ("🟡" if week_pct < 40 else "🔴")
        ai_block = (
            f"\n🤖 *AI анализ:*\n"
            f"{risk_emoji} Риск: {ai['risk']} | Плечо: x{rec_lev}\n"
            f"{day_icon} Волатильность: день=`{day_pct:.1f}%` · неделя=`{week_pct:.1f}%`\n"
            f"_{ai['summary']}_"
        )

        card = (
            f"\n{i}. {dir_icon} *{coin}* — {dir_text} на {main_ex} {cat_icon}\n"
            f"Хедж: *{hedge_ex}*{hedge_warn}\n"
            f"\n"
            f"📈 Фандинг {main_ex}: `{avg_rate:+.4f}%` / выплата\n"
            f"📉 Фандинг {hedge_ex}: `{hedge_rate:+.4f}%` / выплата\n"
            f"🕐 Актуальность (2 дня): `{recent_pct:.0f}%` ставок в нужную сторону\n"
            f"\n"
            f"💧 Объём {main_ex}: `${vol_main/1_000_000:.2f}M`/24h\n"
            f"💧 Объём {hedge_ex}: `${vol_hedge/1_000_000:.2f}M`/24h\n"
            f"📐 Спред входа: {spread_str}\n"
            f"\n"
            f"💰 Доход на $20k позиции: `${daily_20k:.1f}/день` · `${weekly_20k:.0f}/нед`\n"
            f"💰 Доход ($5k маржа):\n"
            + "\n".join(income_lines)
            + ai_block
        )

        await bot.send_message(chat_id, card, parse_mode="Markdown")
        await asyncio.sleep(0.5)

    footer = (
        f"\n{'─' * 30}\n"
        f"✅ Отчёт завершён за {elapsed}с\n"
        f"Следующий автозапуск: сегодня в 20:00 (если не запущен)"
    )
    await bot.send_message(chat_id, footer, parse_mode="Markdown")
    _report_running = False


# ─────────────────────────────────────────────────────────────────────────────
# TELEGRAM ХЕНДЛЕРЫ — подключаются через register_report_handlers(app)
# ─────────────────────────────────────────────────────────────────────────────

async def cmd_report(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик команды /report."""
    chat_id = update.effective_chat.id
    await run_report(context.bot, chat_id)


async def scheduled_report(context: ContextTypes.DEFAULT_TYPE):
    """Вызывается JobQueue по расписанию."""
    chat_id = context.job.data
    await run_report(context.bot, chat_id)


def register_report_handlers(app, report_chat_id: int = None):
    """
    Вызови эту функцию в main() бота ПОСЛЕ создания app:

        from report_engine import register_report_handlers
        register_report_handlers(app, report_chat_id=YOUR_CHAT_ID)

    report_chat_id — куда слать автоматический отчёт в 20:00.
    Если None — автозапуск не регистрируется (только /report команда).
    """
    # Команда /report
    app.add_handler(CommandHandler("report", cmd_report))

    # Автозапуск в 20:00 по Киеву (17:00 UTC летом)
    if report_chat_id:
        app.job_queue.run_daily(
            scheduled_report,
            time=dtime(hour=REPORT_HOUR_UTC, minute=REPORT_MINUTE_UTC, tzinfo=timezone.utc),
            data=report_chat_id,
            name="daily_report",
        )
        logger.info(f"Автоотчёт зарегистрирован: {REPORT_HOUR_UTC}:{REPORT_MINUTE_UTC:02d} UTC → chat {report_chat_id}")

    logger.info("report_engine: /report зарегистрирован")

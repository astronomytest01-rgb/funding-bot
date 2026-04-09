"""
Phemex + XT Funding Rate Telegram Bot
"""

import os
import time
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

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")

DEFAULT_DAYS        = 7
STABILITY_THRESHOLD = -0.04
MAX_OUTLIER_PCT     = 25
NEG_AVG_THRESHOLD   = -0.08

# ── Биржи ─────────────────────────────────────
# Включай/выключай биржи здесь (True/False)
# Состояние бирж (изменяется в рантайме через /toggle)
EXCHANGES_ENABLED = {
    "phemex": True,
    "xt":     True,
    "toobit": True,
    "okx":    True,
    "bingx":  True,
    "kucoin": True,
    "gate":   True,
    "blofin": True,
    "weex":   True,
}

# Когда анализируешь без указания биржи — используются все включённые
# Можно переопределить через /exchange phemex или /exchange xt или /exchange all

# ─────────────────────────────────────────────
# Состояния диалога
# ─────────────────────────────────────────────
WAIT_ANALYZE_COINS = 1
WAIT_SHOW_COIN     = 3
WAIT_CALC_COIN     = 5
WAIT_DELTA_COIN    = 7
WAIT_DELTACALC     = 9

# Состояния пошаговых диалогов
# analyze-coin-match-filter
ACF_COIN     = 20
ACF_DAYS     = 21
ACF_DAYS_NUM = 22
ACF_EXCH     = 23
# funding-rates
FR_COIN      = 30
FR_DAYS      = 31
FR_DAYS_NUM  = 32
FR_EXCH      = 33
# profit-calculator
PC_COIN      = 40
PC_AMT       = 41
PC_AMT_NUM   = 42
PC_DAYS      = 43
PC_DAYS_NUM  = 44
PC_EXCH      = 45

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
            params = {"symbol": sym, "start": start_ms, "end": end_ms, "limit": 1000}
            r = requests.get(url, params=params, timeout=10)
            r.raise_for_status()
            data = r.json()
            if data.get("code") != 0:
                raise ValueError(data.get("msg"))
            rows = [
                x for x in data.get("data", {}).get("rows", [])
                if x["fundingTime"] >= start_ms
                and abs(float(x["fundingRate"])) < 0.5  # фильтр аномалий > ±50%
            ]
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
# OKX API
# ─────────────────────────────────────────────

def okx_fetch(coin, start_ms, end_ms):
    """Возвращает list of (timestamp_ms, rate_pct).

    Формат ответа OKX:
    {"code":"0","data":[
      {"fundingRate":"0.0001","fundingTime":"1570708800000","instId":"BTC-USDT-SWAP",...}
    ]}
    Авторизация не нужна. Лимит 100 записей за запрос.
    """
    coin = coin.upper()
    if coin.endswith("USDT"):
        base = coin[:-4]
        sym = f"{base}-USDT-SWAP"
    elif coin.endswith("USD"):
        base = coin[:-3]
        sym = f"{base}-USDT-SWAP"
    else:
        sym = f"{coin}-USDT-SWAP"

    last_err = None
    try:
        url = "https://www.okx.com/api/v5/public/funding-rate-history"
        # OKX возвращает макс 100 записей, фильтруем по времени на нашей стороне
        params = {"instId": sym, "limit": 100}
        r = requests.get(url, params=params, timeout=10)

        # 451 = геоблокировка
        if r.status_code == 451:
            return [], "OKX заблокирован в вашем регионе (ошибка 451)"
        if r.status_code == 403:
            return [], "OKX недоступен (ошибка 403)"

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

        if not filtered:
            return [], f"Нет данных за период (символ: {sym}, всего: {len(items)})"

        return filtered, sym

    except Exception as e:
        last_err = str(e)

    return [], last_err


# ─────────────────────────────────────────────
# BINGX API
# ─────────────────────────────────────────────

def bingx_fetch(coin, start_ms, end_ms):
    """Возвращает list of (timestamp_ms, rate_pct).

    Формат ответа BingX:
    {"code": 0, "msg": "", "data": [
      {"symbol": "BTC-USDT", "fundingRate": "0.0001", "fundingTime": 1570708800000}
    ]}
    Авторизация не нужна. Символ формата BTC-USDT.
    """
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

        if r.status_code == 451:
            return [], "BingX заблокирован в вашем регионе (ошибка 451)"
        if r.status_code == 403:
            return [], "BingX недоступен (ошибка 403)"

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

        if not filtered:
            return [], f"Нет данных за период (символ: {sym}, всего: {len(items)})"

        return filtered, sym

    except Exception as e:
        last_err = str(e)

    return [], last_err


# ─────────────────────────────────────────────
# KUCOIN API
# ─────────────────────────────────────────────

def kucoin_fetch(coin, start_ms, end_ms):
    """Возвращает list of (timestamp_ms, rate_pct).

    Формат ответа KuCoin:
    {"code": "200000", "data": [
      {"symbol": "ENJUSDTM", "fundingRate": -0.001, "timepoint": 1570708800000}
    ]}
    Символ формата: ENJUSDTM (без дефисов, M на конце).
    Авторизация не нужна.
    """
    coin = coin.upper()
    if coin.endswith("USDT"):
        sym = coin + "M"
    elif coin.endswith("USD"):
        sym = coin + "TM"
    else:
        sym = f"{coin}USDTM"

    last_err = None
    try:
        url = "https://api-futures.kucoin.com/api/v1/contract/funding-rates"
        params = {"symbol": sym, "from": start_ms, "to": end_ms}
        r = requests.get(url, params=params, timeout=10)

        if r.status_code == 451:
            return [], "KuCoin заблокирован в вашем регионе (ошибка 451)"
        if r.status_code == 403:
            return [], "KuCoin недоступен (ошибка 403)"

        r.raise_for_status()
        data = r.json()

        if data.get("code") != "200000":
            raise ValueError(data.get("msg", f"API error code: {data.get('code')}"))

        items = data.get("data", [])
        if not items:
            return [], f"Нет данных (символ: {sym})"

        # KuCoin возвращает данные от новых к старым, разворачиваем
        filtered = []
        for x in items:
            ts = int(x.get("timepoint", 0))
            rate = float(x.get("fundingRate", 0)) * 100
            filtered.append((ts, rate))

        if not filtered:
            return [], f"Нет данных за период (символ: {sym})"

        return filtered, sym

    except Exception as e:
        last_err = str(e)

    return [], last_err


# ─────────────────────────────────────────────
# GATE.IO API
# ─────────────────────────────────────────────

def gate_fetch(coin, start_ms, end_ms):
    """Возвращает list of (timestamp_ms, rate_pct).

    Формат ответа Gate.io:
    [{"t": 1547706332, "r": "0.000100"}]
    t — unix timestamp в секундах, r — ставка в долях.
    Символ формата: ENJ_USDT
    Авторизация не нужна.
    """
    coin = coin.upper()
    if coin.endswith("USDT"):
        sym = coin
    elif coin.endswith("USD"):
        sym = coin + "T"
    else:
        sym = f"{coin}_USDT"

    last_err = None
    try:
        url = "https://api.gateio.ws/api/v4/futures/usdt/funding_rate"
        params = {
            "contract": sym,
            "from": start_ms // 1000,  # Gate принимает unix секунды
            "to": end_ms // 1000,
            "limit": 1000,
        }
        r = requests.get(url, params=params, timeout=10)

        if r.status_code == 451:
            return [], "Gate.io заблокирован в вашем регионе (ошибка 451)"
        if r.status_code == 403:
            return [], "Gate.io недоступен (ошибка 403)"

        r.raise_for_status()
        data = r.json()

        if not isinstance(data, list):
            raise ValueError(f"Unexpected format: {str(data)[:100]}")

        if not data:
            return [], f"Нет данных (символ: {sym})"

        filtered = []
        for x in data:
            ts = int(x.get("t", 0)) * 1000  # конвертируем в ms
            rate = float(x.get("r", 0)) * 100
            if ts >= start_ms:
                filtered.append((ts, rate))

        if not filtered:
            return [], f"Нет данных за период (символ: {sym}, всего: {len(data)})"

        return filtered, sym

    except Exception as e:
        last_err = str(e)

    return [], last_err

# ─────────────────────────────────────────────
# BLOFIN API
# ─────────────────────────────────────────────

def blofin_fetch(coin, start_ms, end_ms):
    """Формат: {"code":"0","data":[{"instId":"ENJ-USDT","fundingRate":"0.0001","fundingTime":"1703462400000"}]}
    Символ: ENJ-USDT. Авторизация не нужна.
    """
    coin = coin.upper()
    if coin.endswith("USDT"):
        sym = coin[:-4] + "-USDT"
    elif coin.endswith("USD"):
        sym = coin[:-3] + "-USDT"
    else:
        sym = f"{coin}-USDT"

    last_err = None
    try:
        url = "https://openapi.blofin.com/api/v1/market/funding-rate-history"
        params = {"instId": sym, "limit": 100}
        r = requests.get(url, params=params, timeout=10)
        if r.status_code in (451, 403):
            return [], f"BloFin недоступен (ошибка {r.status_code})"
        r.raise_for_status()
        data = r.json()
        if data.get("code") != "0":
            raise ValueError(data.get("msg", "API error"))
        items = data.get("data", [])
        if not items:
            return [], f"Нет данных (символ: {sym})"
        filtered = [(int(x["fundingTime"]), float(x["fundingRate"]) * 100)
                    for x in items if int(x["fundingTime"]) >= start_ms]
        if not filtered:
            return [], f"Нет данных за период (символ: {sym}, всего: {len(items)})"
        return filtered, sym
    except Exception as e:
        last_err = str(e)
    return [], last_err


# ─────────────────────────────────────────────
# WEEX API
# ─────────────────────────────────────────────

def weex_fetch(coin, start_ms, end_ms):
    """Формат: [{"symbol":"ENJUSDT","fundingRate":"0.0001","fundingTime":1703462400000}]
    Символ: ENJUSDT. Лимит 7 дней на запрос — делаем несколько запросов если нужно.
    Авторизация не нужна.
    """
    coin = coin.upper()
    if coin.endswith("USDT"):
        sym = coin
    elif coin.endswith("USD"):
        sym = coin + "T"
    else:
        sym = f"{coin}USDT"

    # WEEX ограничивает 7 дней на запрос, нарезаем на чанки
    SEVEN_DAYS_MS = 7 * 24 * 60 * 60 * 1000
    all_rows = []
    last_err = None
    chunk_start = start_ms

    try:
        while chunk_start < end_ms:
            chunk_end = min(chunk_start + SEVEN_DAYS_MS, end_ms)
            url = "https://api-contract.weex.com/capi/v3/market/fundingRate"
            params = {"symbol": sym, "startTime": chunk_start, "endTime": chunk_end, "limit": 1000}
            r = requests.get(url, params=params, timeout=10)
            if r.status_code in (451, 403):
                return [], f"WEEX недоступен (ошибка {r.status_code})"
            r.raise_for_status()
            data = r.json()
            # WEEX возвращает массив напрямую
            if isinstance(data, list):
                all_rows.extend(data)
            elif isinstance(data, dict):
                if data.get("code") not in (None, 0, "0", "200"):
                    raise ValueError(str(data.get("msg", data)))
                rows = data.get("data", [])
                if isinstance(rows, list):
                    all_rows.extend(rows)
            chunk_start = chunk_end + 1
            time.sleep(0.1)

        if not all_rows:
            return [], f"Нет данных (символ: {sym})"

        filtered = []
        for x in all_rows:
            ts = int(x.get("fundingTime", 0))
            rate = float(x.get("fundingRate", 0)) * 100
            if start_ms <= ts <= end_ms:
                filtered.append((ts, rate))

        # убираем дубли и сортируем
        filtered = sorted(set(filtered), key=lambda x: x[0])

        if not filtered:
            return [], f"Нет данных за период (символ: {sym})"
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
    "okx": okx_fetch,
    "bingx": bingx_fetch,
    "kucoin": kucoin_fetch,
    "gate":   gate_fetch,
    "blofin": blofin_fetch,
    "weex":   weex_fetch,
}

EXCHANGE_LABELS = {
    "phemex": "Phemex",
    "xt":     "XT",
    "toobit": "Toobit",
    "okx": "OKX",
    "bingx": "BingX",
    "kucoin": "KuCoin",
    "gate":   "Gate.io",
    "blofin": "BloFin",
    "weex":   "WEEX",
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
        if p in ("/analyze-coin-match-filter", "/funding-rates", "/profit-calculator", "/start", "/help", "/settings", "/cancel"):
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
        "/analyze-coin-match-filter — анализ монет по фильтрам\n"
        "/funding-rates — ставки фандинга по монете\n"
        "/profit-calculator — калькулятор дохода\n"
        "/scan — полный скан всех монет Phemex\n"
        "/scankucoin — скан всех монет KuCoin\n"
        "/scanxt — скан всех монет XT\n"
        "/scantoobit — скан всех монет Toobit\n"
        "/stopscan — остановить скан\n"
        "/delta — дельта-нейтраль: лонг+шорт связка\n"
        "/deltacalc — калькулятор дохода по связке\n"
        "/settings — настройки и управление биржами\n"
        "/help — справка\n\n"
        "💡 *Быстрый ввод для команд:*\n"
        "`/analyze-coin-match-filter ENJ phemex 7`\n"
        "`/funding-rates ENJ phemex 7`\n"
        "`/profit-calculator ENJ 25000 7 phemex`\n\n"
        "Порядок параметров любой: монета, биржа, дни"
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
# PHEMEX SCAN — полный скан всех контрактов
# ─────────────────────────────────────────────

# Флаг для остановки скана (per chat_id)
_scan_running = {}   # {chat_id: True/False}
SCAN_DAYS     = 7    # период скана
SCAN_BATCH    = 20   # размер порции


def phemex_get_all_symbols():
    """Получает список всех USDT-маржинальных перпетуалов с Phemex.
    Использует /exchange/public/cfg/v2/products — там 526 Listed USDT контрактов.
    Возвращает список строк-монет: ['BTC', 'ETH', 'ENJ', ...]
    """
    url = "https://api.phemex.com/exchange/public/cfg/v2/products"
    r = requests.get(url, timeout=15)
    r.raise_for_status()
    data = r.json()
    products = data.get("data", {}).get("products", [])
    coins = []
    seen = set()
    for p in products:
        if (p.get("type") == "PerpetualV2"
                and p.get("quoteCurrency") == "USDT"
                and p.get("status") == "Listed"):
            sym = p.get("symbol", "")
            # BTCUSDT -> BTC, ENJUSDT -> ENJ
            if sym.endswith("USDT"):
                coin = sym[:-4]
                if coin and coin not in seen:
                    coins.append(coin)
                    seen.add(coin)
    return coins


async def cmd_scan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Запускает полный скан всех монет Phemex под фильтры."""
    chat_id = update.effective_chat.id

    # Если скан уже идёт — сообщаем
    if _scan_running.get(chat_id):
        await update.message.reply_text(
            "⏳ Скан уже запущен. Чтобы остановить — /stopscan",
            parse_mode="Markdown"
        )
        return

    # Получаем список монет
    await update.message.reply_text("🔍 Загружаю список монет с Phemex...")
    try:
        all_coins = phemex_get_all_symbols()
    except Exception as e:
        await update.message.reply_text(f"❌ Ошибка получения списка: {e}")
        return

    total = len(all_coins)
    await update.message.reply_text(
        f"📋 Найдено *{total}* контрактов на Phemex\n"
        f"Период: *{SCAN_DAYS} дня* | Порции: *{SCAN_BATCH}* монет\n"
        f"Фильтры: neg\_avg ≤ {NEG_AVG_THRESHOLD}% | выбросов ≤ {MAX_OUTLIER_PCT}%\n\n"
        f"Остановить: /stopscan",
        parse_mode="Markdown"
    )

    _scan_running[chat_id] = True
    now_ms = int(time.time() * 1000)
    start_ms = now_ms - SCAN_DAYS * 24 * 60 * 60 * 1000

    passed_full    = []  # ✅ стабильность
    passed_partial = []  # ⚡ только neg avg

    batches = [all_coins[i:i+SCAN_BATCH] for i in range(0, total, SCAN_BATCH)]

    for batch_idx, batch in enumerate(batches):
        # Проверяем флаг остановки перед каждой порцией
        if not _scan_running.get(chat_id):
            await update.message.reply_text(
                f"⛔ Скан остановлен на порции {batch_idx + 1}/{len(batches)}\n"
                f"Проанализировано: {batch_idx * SCAN_BATCH}/{total} монет",
                parse_mode="Markdown"
            )
            return

        # Анализируем порцию
        batch_results = []
        for coin in batch:
            if not _scan_running.get(chat_id):
                break
            rows, sym = phemex_fetch(coin, start_ms, now_ms)
            if not rows:
                continue
            rates = [r for _, r in rows]
            total_rates = len(rates)
            if not total_rates:
                continue
            neg = [r for r in rates if r < 0]
            below = sum(1 for r in rates if r <= STABILITY_THRESHOLD)
            outlier_pct = (total_rates - below) / total_rates * 100
            pass_stability = outlier_pct <= MAX_OUTLIER_PCT
            neg_avg = sum(neg) / len(neg) if neg else 0.0
            pass_neg_avg = bool(neg) and neg_avg <= NEG_AVG_THRESHOLD

            if pass_stability:
                batch_results.append(("full", coin, neg_avg, outlier_pct))
                passed_full.append((coin, neg_avg, outlier_pct))
            elif pass_neg_avg:
                batch_results.append(("partial", coin, neg_avg, outlier_pct))
                passed_partial.append((coin, neg_avg, outlier_pct))

            time.sleep(0.15)

        # Прогресс после каждой порции
        scanned = min((batch_idx + 1) * SCAN_BATCH, total)
        remaining = total - scanned
        found_so_far = len(passed_full) + len(passed_partial)

        if batch_results:
            lines = [
                f"📊 Порция {batch_idx + 1}/{len(batches)} "
                f"\[{scanned}/{total} | осталось {remaining}\]\n"
            ]
            for cat, coin, na, op in batch_results:
                icon = "✅" if cat == "full" else "⚡"
                lines.append(f"{icon} `{coin}` neg\_avg `{na:+.4f}%` выбр `{op:.0f}%`")
            await update.message.reply_text("\n".join(lines), parse_mode="Markdown")
        else:
            # Тихий прогресс без результатов (каждые 3 порции или последняя)
            if batch_idx % 3 == 2 or batch_idx == len(batches) - 1:
                await update.message.reply_text(
                    f"⏳ {scanned}/{total} проанализировано, найдено: {found_so_far} | осталось {remaining}"
                )

    _scan_running[chat_id] = False

    # Итоговый отчёт
    if not passed_full and not passed_partial:
        await update.message.reply_text(
            f"✅ Скан завершён: {total} монет\n\n"
            f"За {SCAN_DAYS} дня ни одна монета не прошла фильтры.",
            parse_mode="Markdown"
        )
        return

    lines = [f"✅ *Скан завершён* — {total} монет за {SCAN_DAYS} дня\n"]

    if passed_full:
        passed_full.sort(key=lambda x: x[1])  # по neg_avg
        lines.append(f"✅ *ПОДХОДЯТ* ({len(passed_full)}):")
        for coin, na, op in passed_full:
            lines.append(f"  `{coin}` neg\_avg `{na:+.4f}%` выбр `{op:.0f}%`")

    if passed_partial:
        passed_partial.sort(key=lambda x: x[1])
        lines.append(f"\n⚡ *ЧАСТИЧНО* ({len(passed_partial)}):")
        for coin, na, op in passed_partial:
            lines.append(f"  `{coin}` neg\_avg `{na:+.4f}%` выбр `{op:.0f}%`")

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def cmd_stopscan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Останавливает текущий скан."""
    chat_id = update.effective_chat.id
    if _scan_running.get(chat_id):
        _scan_running[chat_id] = False
        await update.message.reply_text("⛔ Скан будет остановлен после текущей монеты.")
    else:
        await update.message.reply_text("Скан не запущен.")

# ─────────────────────────────────────────────
# СКАНЕРЫ ПО БИРЖАМ (только ✅ ПОДХОДЯТ)
# ─────────────────────────────────────────────

async def run_exchange_scan(update, exchange_key, chat_id):
    """
    Универсальная функция скана по одной бирже.
    Берёт список монет с Phemex (526 шт), проверяет каждую на указанной бирже.
    Показывает только ✅ ПОДХОДЯТ (pass_stability), частично — нет.
    """
    if _scan_running.get(chat_id):
        await update.message.reply_text(
            "⏳ Скан уже запущен. Чтобы остановить — /stopscan"
        )
        return

    label    = EXCHANGE_LABELS.get(exchange_key, exchange_key.upper())
    fetcher  = EXCHANGE_FETCHERS.get(exchange_key)
    if not fetcher:
        await update.message.reply_text(f"❌ Биржа `{exchange_key}` не найдена.", parse_mode="Markdown")
        return

    await update.message.reply_text(f"🔍 Загружаю список монет с Phemex...")
    try:
        all_coins = phemex_get_all_symbols()
    except Exception as e:
        await update.message.reply_text(f"❌ Ошибка получения списка: {e}")
        return

    total = len(all_coins)
    await update.message.reply_text(
        f"📋 Скан *{label}* — {total} монет\n"
        f"Период: *{SCAN_DAYS} дней* | Порции: *{SCAN_BATCH}* монет\n"
        f"Только ✅ ПОДХОДЯТ (выбросов ≤ {MAX_OUTLIER_PCT}%)\n\n"
        "Остановить: /stopscan",
        parse_mode="Markdown"
    )

    _scan_running[chat_id] = True
    now_ms   = int(time.time() * 1000)
    start_ms = now_ms - SCAN_DAYS * 24 * 60 * 60 * 1000

    passed = []
    batches = [all_coins[i:i+SCAN_BATCH] for i in range(0, total, SCAN_BATCH)]

    for batch_idx, batch in enumerate(batches):
        if not _scan_running.get(chat_id):
            await update.message.reply_text(
                f"⛔ Скан остановлен на порции {batch_idx + 1}/{len(batches)}\n"
                f"Проанализировано: {batch_idx * SCAN_BATCH}/{total} монет"
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
                continue

            rates      = [r for _, r in rows]
            total_r    = len(rates)
            below      = sum(1 for r in rates if r <= STABILITY_THRESHOLD)
            outlier    = (total_r - below) / total_r * 100
            neg        = [r for r in rates if r < 0]
            neg_avg    = sum(neg) / len(neg) if neg else 0.0

            # Только полное прохождение фильтров
            if outlier <= MAX_OUTLIER_PCT:
                batch_results.append((coin, neg_avg, outlier))
                passed.append((coin, neg_avg, outlier))

            time.sleep(0.15)

        scanned    = min((batch_idx + 1) * SCAN_BATCH, total)
        remaining  = total - scanned

        if batch_results:
            lines = [
                f"📊 Порция {batch_idx + 1}/{len(batches)} "
                f"[{scanned}/{total} | осталось {remaining}]\n"
            ]
            for coin, na, op in batch_results:
                lines.append(f"✅ `{coin}` neg\_avg `{na:+.4f}%` выбр `{op:.0f}%`")
            await update.message.reply_text("\n".join(lines), parse_mode="Markdown")
        else:
            if batch_idx % 3 == 2 or batch_idx == len(batches) - 1:
                await update.message.reply_text(
                    f"⏳ {scanned}/{total} | найдено: {len(passed)} | осталось {remaining}"
                )

    _scan_running[chat_id] = False

    if not passed:
        await update.message.reply_text(
            f"✅ Скан {label} завершён: {total} монет\n\n"
            f"За {SCAN_DAYS} дней ни одна монета не прошла фильтры.",
            parse_mode="Markdown"
        )
        return

    passed.sort(key=lambda x: x[1])
    lines = [f"✅ *Скан {label} завершён* — {total} монет за {SCAN_DAYS} дней\n"]
    lines.append(f"✅ *ПОДХОДЯТ* ({len(passed)}):")
    for coin, na, op in passed:
        lines.append(f"  `{coin}` neg\_avg `{na:+.4f}%` выбр `{op:.0f}%`")

    reply = "\n".join(lines)
    if len(reply) > 4000:
        for chunk in [reply[i:i+4000] for i in range(0, len(reply), 4000)]:
            await update.message.reply_text(chunk, parse_mode="Markdown")
    else:
        await update.message.reply_text(reply, parse_mode="Markdown")


async def cmd_scan_kucoin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await run_exchange_scan(update, "kucoin", update.effective_chat.id)


async def cmd_scan_xt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await run_exchange_scan(update, "xt", update.effective_chat.id)


async def cmd_scan_toobit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await run_exchange_scan(update, "toobit", update.effective_chat.id)





# ─────────────────────────────────────────────
# HELPERS ДЛЯ КНОПОК
# ─────────────────────────────────────────────

def make_days_keyboard(cb_prefix):
    """Кнопки выбора периода."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("1 день",  callback_data=f"{cb_prefix}_days_1"),
         InlineKeyboardButton("3 дня",   callback_data=f"{cb_prefix}_days_3")],
        [InlineKeyboardButton("7 дней",  callback_data=f"{cb_prefix}_days_7"),
         InlineKeyboardButton("14 дней", callback_data=f"{cb_prefix}_days_14")],
        [InlineKeyboardButton("✏️ Другое", callback_data=f"{cb_prefix}_days_other")],
        [InlineKeyboardButton("❌ Отмена", callback_data=f"{cb_prefix}_cancel")],
    ])


def make_exchange_keyboard(cb_prefix):
    """Кнопки выбора биржи."""
    buttons = []
    row = []
    for ex, label in EXCHANGE_LABELS.items():
        if not EXCHANGES_ENABLED.get(ex, False):
            continue
        row.append(InlineKeyboardButton(label, callback_data=f"{cb_prefix}_ex_{ex}"))
        if len(row) == 3:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    buttons.append([InlineKeyboardButton("🌐 Все биржи", callback_data=f"{cb_prefix}_ex_all")])
    buttons.append([InlineKeyboardButton("❌ Отмена",    callback_data=f"{cb_prefix}_cancel")])
    return InlineKeyboardMarkup(buttons)


def make_amount_keyboard(cb_prefix):
    """Кнопки выбора суммы."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("$15,000", callback_data=f"{cb_prefix}_amt_15000"),
         InlineKeyboardButton("$20,000", callback_data=f"{cb_prefix}_amt_20000")],
        [InlineKeyboardButton("$25,000", callback_data=f"{cb_prefix}_amt_25000"),
         InlineKeyboardButton("✏️ Другое", callback_data=f"{cb_prefix}_amt_other")],
        [InlineKeyboardButton("❌ Отмена", callback_data=f"{cb_prefix}_cancel")],
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
        "🔍 *Анализ монет по фильтрам*\\n\\n"
        "Шаг 1/3: Введи название монеты или несколько через пробел:\\n\\n"
        "`ENJ`\\n"
        "`ENJ RON JTO`\\n\\n"
        "💡 Быстрый ввод: `/analyze-coin-match-filter ENJ phemex 7`\\n\\n"
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
        f"Монеты: *{' '.join(coins)}*\\n\\nШаг 2/3: Выбери период анализа:",
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
        f"Период: *{days} дн.*\\n\\nШаг 3/3: Выбери биржу:",
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
        f"Период: *{days} дн.*\\n\\nШаг 3/3: Выбери биржу:",
        reply_markup=make_exchange_keyboard("acf"),
        parse_mode="Markdown"
    )
    return ACF_EXCH


async def acf_exchange_btn(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Шаг 3: нажата кнопка биржи, запускаем анализ."""
    q = update.callback_query
    await q.answer()
    if q.data == "acf_cancel":
        await q.edit_message_text("❌ Отменено.")
        return ConversationHandler.END
    exchange = q.data.replace("acf_ex_", "")
    coins    = context.user_data.get("acf_coins", [])
    days     = context.user_data.get("acf_days", DEFAULT_DAYS)
    ex_label = "все биржи" if exchange == "all" else EXCHANGE_LABELS.get(exchange, exchange)
    await q.edit_message_text(
        f"🔍 Анализирую *{' '.join(coins)}* за *{days}д* на *{ex_label}*...",
        parse_mode="Markdown"
    )
    # Создаём фейковый update с message для do_analyze
    await do_analyze(q, coins, days, None if exchange == "all" else exchange)
    return ConversationHandler.END


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
        "📈 *Ставки фандинга по монете*\\n\\n"
        "Шаг 1/3: Введи название монеты:\\n\\n"
        "`ENJ`\\n\\n"
        "💡 Быстрый ввод: `/funding-rates ENJ phemex 7`\\n\\n"
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
        f"Монета: *{coins[0]}*\\n\\nШаг 2/3: Выбери период:",
        reply_markup=make_days_keyboard("fr"),
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
        f"Период: *{days} дн.*\\n\\nШаг 3/3: Выбери биржу:",
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
        f"Период: *{days} дн.*\\n\\nШаг 3/3: Выбери биржу:",
        reply_markup=make_exchange_keyboard("fr"),
        parse_mode="Markdown"
    )
    return FR_EXCH


async def fr_exchange_btn(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if q.data == "fr_cancel":
        await q.edit_message_text("❌ Отменено.")
        return ConversationHandler.END
    exchange = q.data.replace("fr_ex_", "")
    coin     = context.user_data.get("fr_coin", "")
    days     = context.user_data.get("fr_days", DEFAULT_DAYS)
    ex_label = "все биржи" if exchange == "all" else EXCHANGE_LABELS.get(exchange, exchange)
    await q.edit_message_text(
        f"🔍 Загружаю ставки *{coin}* за *{days}д* на *{ex_label}*...",
        parse_mode="Markdown"
    )
    await do_show(q, coin, days, None if exchange == "all" else exchange)
    return ConversationHandler.END


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
        "💰 *Калькулятор дохода от фандинга*\\n\\n"
        "Шаг 1/4: Введи название монеты:\\n\\n"
        "`ENJ`\\n\\n"
        "💡 Быстрый ввод: `/profit-calculator ENJ 25000 7 phemex`\\n\\n"
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
        f"Монета: *{coins[0]}*\\n\\nШаг 2/4: Выбери сумму позиции (USDT):",
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
        await q.edit_message_text("❌ Отменено.")
        return ConversationHandler.END
    exchange = q.data.replace("pc_ex_", "")
    coin     = context.user_data.get("pc_coin", "")
    amount   = context.user_data.get("pc_amount", 0)
    days     = context.user_data.get("pc_days", DEFAULT_DAYS)
    ex_label = "все биржи" if exchange == "all" else EXCHANGE_LABELS.get(exchange, exchange)
    await q.edit_message_text(
        f"🔍 Считаю доход *{coin}* ${amount:,.0f} за *{days}д* на *{ex_label}*...",
        parse_mode="Markdown"
    )
    await do_calc(q, coin, amount, days, None if exchange == "all" else exchange)
    return ConversationHandler.END


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
        f"Neg avg порог: `{NEG_AVG_THRESHOLD}%`\\n\\n"
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
# DELTA-NEUTRAL: поиск лучшей связки лонг/шорт
# ─────────────────────────────────────────────

def calc_std(rates):
    """Стандартное отклонение списка ставок."""
    if not rates:
        return 0.0
    n = len(rates)
    mean = sum(rates) / n
    return (sum((r - mean) ** 2 for r in rates) / n) ** 0.5


def analyze_delta(coin, days, long_exchanges=None, all_exchanges=None):
    """
    Ищет лучшую дельта-нейтральную связку для монеты.

    Логика:
    1. Проверяем каждую биржу как кандидата для ЛОНГА:
       монета должна проходить фильтры (стабильно отриц. фандинг).
    2. Для каждого лонга перебираем остальные биржи как кандидаты для ШОРТА:
       - Считаем средний фандинг шорта и его стабильность (std)
       - Чистый доход = avg_long_rate + avg_short_rate (оба в %)
         (long_rate отрицательный → мы получаем; short_rate положит → получаем, отриц → платим)
       - Стабильность связки = std_long + std_short (меньше = лучше)
    3. Сортируем: сначала по чистому доходу (больше = лучше),
       при равенстве — по стабильности (меньше std = лучше).
    """
    now_ms = int(time.time() * 1000)
    start_ms = now_ms - days * 24 * 60 * 60 * 1000

    if all_exchanges is None:
        all_exchanges = [e for e, on in EXCHANGES_ENABLED.items() if on]
    if long_exchanges is None:
        long_exchanges = all_exchanges

    # Собираем данные по всем биржам
    exchange_data = {}
    for ex in all_exchanges:
        fetcher = EXCHANGE_FETCHERS.get(ex)
        if not fetcher:
            continue
        try:
            data, sym = fetcher(coin, start_ms, now_ms)
            if data:
                rates = [r for _, r in data]
                exchange_data[ex] = {"rates": rates, "sym": sym}
        except Exception:
            pass
        time.sleep(0.1)

    # Ищем лонг-кандидатов (проходят наши фильтры)
    long_candidates = []
    for ex in long_exchanges:
        if ex not in exchange_data:
            continue
        rates = exchange_data[ex]["rates"]
        neg = [r for r in rates if r < 0]
        total = len(rates)
        below = sum(1 for r in rates if r <= STABILITY_THRESHOLD)
        outlier_pct = (total - below) / total * 100 if total else 100
        neg_avg = sum(neg) / len(neg) if neg else 0.0
        pass_stability = outlier_pct <= MAX_OUTLIER_PCT
        pass_neg_avg = bool(neg) and neg_avg <= NEG_AVG_THRESHOLD

        if pass_stability or pass_neg_avg:
            long_candidates.append({
                "exchange": ex,
                "rates": rates,
                "avg": sum(rates) / total if total else 0,
                "neg_avg": neg_avg,
                "std": calc_std(rates),
                "outlier_pct": outlier_pct,
                "pass_stability": pass_stability,
                "pass_neg_avg": pass_neg_avg,
            })

    if not long_candidates:
        return None, exchange_data

    # Для каждого лонга ищем лучший шорт
    best_pairs = []

    for long_info in long_candidates:
        long_ex = long_info["exchange"]
        long_avg = long_info["avg"]  # отрицательный → мы ПОЛУЧАЕМ abs(avg)

        short_candidates = []
        for ex in all_exchanges:
            if ex == long_ex:
                continue
            if ex not in exchange_data:
                continue
            rates = exchange_data[ex]["rates"]
            total = len(rates)
            if not total:
                continue
            avg = sum(rates) / total
            std = calc_std(rates)

            # Чистый доход на шорте:
            # если avg > 0 (положительный фандинг) → шорт ПОЛУЧАЕТ avg
            # если avg < 0 (отрицательный фандинг) → шорт ПЛАТИТ abs(avg)
            # Итого за период: long получает abs(long_avg), шорт получает avg_short
            # net_income_pct = abs(long_avg) + avg_short
            net_income_pct = abs(long_avg) + avg  # avg шорта: + = хорошо, - = плохо

            short_candidates.append({
                "exchange": ex,
                "avg": avg,
                "std": std,
                "net_income_pct": net_income_pct,
                "rates": rates,
            })

        # Сортируем шорт-кандидатов: больший net_income, затем меньший std
        short_candidates.sort(key=lambda x: (-x["net_income_pct"], x["std"]))

        for short_info in short_candidates[:3]:  # топ-3 варианта шорта
            best_pairs.append({
                "long_ex": long_ex,
                "short_ex": short_info["exchange"],
                "long_avg": long_avg,
                "long_std": long_info["std"],
                "long_neg_avg": long_info["neg_avg"],
                "long_outlier_pct": long_info["outlier_pct"],
                "short_avg": short_info["avg"],
                "short_std": short_info["std"],
                "net_income_pct": short_info["net_income_pct"],
                "pass_stability": long_info["pass_stability"],
                "pass_neg_avg": long_info["pass_neg_avg"],
            })

    # Финальная сортировка всех пар
    best_pairs.sort(key=lambda x: (-x["net_income_pct"], x["long_std"] + x["short_std"]))
    return best_pairs, exchange_data


def fmt_delta_result(coin, pairs, days, amount_usd=None):
    """Форматирует результат дельта-анализа."""
    if not pairs:
        return f"⚠️ *{coin}* — не найдено подходящих бирж для лонга за {days} дней"

    lines = [f"⚖️ *Дельта-нейтраль: {coin}* — {days} дней\n"]

    # Показываем топ-3 пары
    for i, p in enumerate(pairs[:3], 1):
        long_label = EXCHANGE_LABELS.get(p["long_ex"], p["long_ex"].upper())
        short_label = EXCHANGE_LABELS.get(p["short_ex"], p["short_ex"].upper())

        # Иконка стабильности лонга
        long_cat = "✅" if p["pass_stability"] else "⚡"

        # Знак для шорта: + если зарабатываем на шорте, - если платим
        short_sign = "+" if p["short_avg"] >= 0 else ""

        lines.append(f"*#{i}* 🟢 Лонг `{long_label}` + 🔴 Шорт `{short_label}`")
        lines.append(
            f"  Лонг {long_cat}: avg `{p['long_avg']:+.4f}%`  std `{p['long_std']:.4f}`"
        )
        lines.append(
            f"  Шорт: avg `{p['short_avg']:+.4f}%`  std `{p['short_std']:.4f}`"
        )
        lines.append(
            f"  📈 Чистый доход/ставку: `{p['net_income_pct']:+.4f}%`"
        )

        if amount_usd:
            # Считаем реальный доход за период
            # Количество ставок примерно = days * 3 (обычно 3 ставки/день при 8ч интервале)
            approx_payments = days * 3
            income_long = amount_usd * abs(p["long_avg"] / 100) * approx_payments
            income_short = amount_usd * (p["short_avg"] / 100) * approx_payments
            net = income_long + income_short
            net_per_day = net / days if days > 0 else 0
            income_long_day = income_long / days if days > 0 else 0
            income_short_day = income_short / days if days > 0 else 0
            short_str = (f"+${income_short:.2f}" if income_short >= 0 else f"-${abs(income_short):.2f}")
            short_day_str = (f"+${income_short_day:.2f}" if income_short_day >= 0 else f"-${abs(income_short_day):.2f}")
            lines.append(
                f"  💰 *За {days} дней:*"
            )
            lines.append(
                f"  Лонг: `+${income_long:.2f}`  Шорт: `{short_str}`"
            )
            lines.append(
                f"  Итого: *${net:.2f}*"
            )
            lines.append(
                f"  📅 *В день:* лонг `+${income_long_day:.2f}` шорт `{short_day_str}` итого `${net_per_day:.2f}`"
            )
        lines.append("")

    return "\n".join(lines)


# ─────────────────────────────────────────────
# DELTA CONVERSATION HANDLERS
# ─────────────────────────────────────────────

async def delta_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.args:
        coins, days, exchange = parse_tokens(" ".join(context.args))
        if coins:
            await do_delta(update, coins, days)
            return ConversationHandler.END
    await update.message.reply_text(
        "Введи монеты для дельта-анализа:\n\n"
        "`ENJ` — за 7 дней (по умолчанию)\n"
        "`ENJ JTO RON` — несколько монет\n"
        "`ENJ /days 14` — за 14 дней\n\n"
        "Отмена: /cancel",
        parse_mode="Markdown"
    )
    return WAIT_DELTA_COIN


async def delta_got_coins(update: Update, context: ContextTypes.DEFAULT_TYPE):
    coins, days, _ = parse_tokens(update.message.text.strip())
    if not coins:
        await update.message.reply_text("Не распознал монеты. Попробуй: `ENJ JTO`", parse_mode="Markdown")
        return WAIT_DELTA_COIN
    await do_delta(update, coins, days)
    return ConversationHandler.END


async def deltacalc_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.args:
        coins, days, _ = parse_tokens(" ".join(context.args))
        amount = None
        remaining = []
        for p in coins:
            try:
                amount = float(p.replace("$", "").replace(",", ""))
            except ValueError:
                remaining.append(p)
        if remaining and amount:
            await do_delta(update, remaining, days, amount_usd=amount)
            return ConversationHandler.END
    await update.message.reply_text(
        "Введи монету, сумму и (опционально) период:\n\n"
        "`ENJ 25000` — за 7 дней (по умолчанию)\n"
        "`ENJ 25000 /days 14` — за 14 дней\n"
        "`ENJ JTO 25000 /days 30` — несколько монет\n\n"
        "Отмена: /cancel",
        parse_mode="Markdown"
    )
    return WAIT_DELTACALC


async def deltacalc_got_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    coins, days, _ = parse_tokens(update.message.text.strip())
    amount = None
    remaining = []
    for p in coins:
        try:
            amount = float(p.replace("$", "").replace(",", ""))
        except ValueError:
            remaining.append(p)
    if not remaining:
        await update.message.reply_text("Не распознал монету. Попробуй: `ENJ 25000`", parse_mode="Markdown")
        return WAIT_DELTACALC
    if not amount or amount <= 0:
        await update.message.reply_text("Не распознал сумму. Попробуй: `ENJ 25000`", parse_mode="Markdown")
        return WAIT_DELTACALC
    await do_delta(update, remaining, days, amount_usd=amount)
    return ConversationHandler.END


async def do_delta(update, coins, days, amount_usd=None):
    active = [e for e, on in EXCHANGES_ENABLED.items() if on]
    if len(active) < 2:
        await update.message.reply_text(
            "❌ Нужно минимум 2 активные биржи для дельта-анализа.\n"
            "Включи биржи: `/toggle all`",
            parse_mode="Markdown"
        )
        return

    ex_str = " + ".join(EXCHANGE_LABELS.get(e, e) for e in active)
    suffix = f" | сумма ${amount_usd:,.0f}" if amount_usd else ""
    await update.message.reply_text(
        f"⚖️ Дельта-анализ {len(coins)} монет на {ex_str} за {days} дней{suffix}...",
    )

    for coin in coins:
        pairs, _ = analyze_delta(coin, days, all_exchanges=active)
        reply = fmt_delta_result(coin, pairs, days, amount_usd)

        # Нарезаем если длинное
        if len(reply) > 4000:
            for chunk in [reply[i:i+4000] for i in range(0, len(reply), 4000)]:
                await update.message.reply_text(chunk, parse_mode="Markdown")
        else:
            await update.message.reply_text(reply, parse_mode="Markdown")


# ─────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────

def main():
    if not BOT_TOKEN:
        raise ValueError("BOT_TOKEN не задан!")

    app = Application.builder().token(BOT_TOKEN).build()

    # /analyze-coin-match-filter
    acf_conv = ConversationHandler(
        entry_points=[CommandHandler("analyze_coin_match_filter", acf_start)],
        states={
            ACF_COIN:     [MessageHandler(filters.TEXT & ~filters.COMMAND, acf_got_coin)],
            ACF_DAYS:     [CallbackQueryHandler(acf_days_btn, pattern="^acf_")],
            ACF_DAYS_NUM: [MessageHandler(filters.TEXT & ~filters.COMMAND, acf_days_num)],
            ACF_EXCH:     [CallbackQueryHandler(acf_exchange_btn, pattern="^acf_")],
        },
        fallbacks=[CommandHandler("cancel", cmd_cancel)],
    )
    # /funding-rates
    fr_conv = ConversationHandler(
        entry_points=[CommandHandler("funding_rates", fr_start)],
        states={
            FR_COIN:     [MessageHandler(filters.TEXT & ~filters.COMMAND, fr_got_coin)],
            FR_DAYS:     [CallbackQueryHandler(fr_days_btn, pattern="^fr_")],
            FR_DAYS_NUM: [MessageHandler(filters.TEXT & ~filters.COMMAND, fr_days_num)],
            FR_EXCH:     [CallbackQueryHandler(fr_exchange_btn, pattern="^fr_")],
        },
        fallbacks=[CommandHandler("cancel", cmd_cancel)],
    )
    # /profit-calculator
    pc_conv = ConversationHandler(
        entry_points=[CommandHandler("profit_calculator", pc_start)],
        states={
            PC_COIN:     [MessageHandler(filters.TEXT & ~filters.COMMAND, pc_got_coin)],
            PC_AMT:      [CallbackQueryHandler(pc_amt_btn, pattern="^pc_")],
            PC_AMT_NUM:  [MessageHandler(filters.TEXT & ~filters.COMMAND, pc_amt_num)],
            PC_DAYS:     [CallbackQueryHandler(pc_days_btn, pattern="^pc_")],
            PC_DAYS_NUM: [MessageHandler(filters.TEXT & ~filters.COMMAND, pc_days_num)],
            PC_EXCH:     [CallbackQueryHandler(pc_exchange_btn, pattern="^pc_")],
        },
        fallbacks=[CommandHandler("cancel", cmd_cancel)],
    )

    app.add_handler(CommandHandler("start",     cmd_start))
    app.add_handler(CommandHandler("help",      cmd_help))
    app.add_handler(CommandHandler("settings",  cmd_settings_new))
    app.add_handler(CallbackQueryHandler(settings_callback, pattern="^set_"))
    app.add_handler(CommandHandler("scan",      cmd_scan))
    app.add_handler(CommandHandler("stopscan",   cmd_stopscan))
    app.add_handler(CommandHandler("scankucoin", cmd_scan_kucoin))
    app.add_handler(CommandHandler("scanxt",     cmd_scan_xt))
    app.add_handler(CommandHandler("scantoobit", cmd_scan_toobit))
    app.add_handler(CommandHandler("cancel",    cmd_cancel))

    delta_conv = ConversationHandler(
        entry_points=[CommandHandler("delta", delta_start)],
        states={WAIT_DELTA_COIN: [MessageHandler(filters.TEXT & ~filters.COMMAND, delta_got_coins)]},
        fallbacks=[CommandHandler("cancel", cmd_cancel)],
    )
    deltacalc_conv = ConversationHandler(
        entry_points=[CommandHandler("deltacalc", deltacalc_start)],
        states={WAIT_DELTACALC: [MessageHandler(filters.TEXT & ~filters.COMMAND, deltacalc_got_input)]},
        fallbacks=[CommandHandler("cancel", cmd_cancel)],
    )

    # ConversationHandlers — должны быть зарегистрированы до MessageHandler(unknown)
    app.add_handler(acf_conv)
    app.add_handler(fr_conv)
    app.add_handler(pc_conv)
    app.add_handler(delta_conv)
    app.add_handler(deltacalc_conv)

    app.add_handler(MessageHandler(filters.COMMAND, unknown))

    print("Бот запущен...")
    app.run_polling()


if __name__ == "__main__":
    main()

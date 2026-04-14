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

# CoinW — данные из Supabase (коллектор собирает каждые 8 часов)
SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "")

DEFAULT_DAYS        = 7
STABILITY_THRESHOLD = -0.04
MAX_OUTLIER_PCT     = 25
NEG_AVG_THRESHOLD   = -0.08
MIN_NEG_RATIO       = 0.30   # минимум 30% ставок должны быть отрицательными для ЛОНГ
MIN_POS_RATIO       = 0.30   # минимум 30% ставок должны быть положительными для ШОРТ

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
    "coinw":  True,
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

# /analyze scan states
AN_METHOD    = 50   # выбор метода (Средняя ставка / Средний доход)
AN_AMT       = 51   # сумма позиции (только Средний доход)
AN_AMT_NUM   = 52   # ввод суммы вручную
AN_THRESH    = 53   # минимальный доход/день
AN_THRESH_NUM= 54   # ввод порога вручную
AN_DAYS      = 55   # выбор периода
AN_DAYS_NUM  = 56   # ввод периода вручную

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
                and abs(float(x["fundingRate"])) < 0.01  # фильтр аномалий > ±1%
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


def coinw_fetch(coin, start_ms, end_ms):
    """
    Возвращает историю ставок фандинга CoinW из Supabase.
    Данные собираются коллектором каждые 4 часа.
    Дедупликация по funding_time — исключает дубли для монет с 8ч интервалом.
    Формат: list of (timestamp_ms, rate_pct)
    """
    if not SUPABASE_URL or not SUPABASE_KEY:
        return [], "SUPABASE_URL/KEY не заданы"

    symbol = coin.upper()
    from datetime import datetime, timezone
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

        # Дедупликация по funding_time — берём уникальные выплаты
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
                # Старые записи без funding_time — используем collected_at
                dt = datetime.fromisoformat(
                    row["collected_at"].replace("Z", "+00:00")
                )
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
    "kucoin": kucoin_fetch,
    "gate":   gate_fetch,
    "blofin": blofin_fetch,
    "weex":   weex_fetch,
    "coinw":  coinw_fetch,
}

EXCHANGE_LABELS = {
    "phemex": "Phemex",
    "xt":     "XT",
    "toobit": "Toobit",
    "okx":    "OKX",
    "bingx":  "BingX",
    "kucoin": "KuCoin",
    "gate":   "Gate.io",
    "blofin": "BloFin",
    "weex":   "WEEX",
    "coinw":  "CoinW",
}


def get_active_exchanges(requested=None):
    """Возвращает список активных бирж."""
    if requested and requested != "all":
        exs = [e.strip().lower() for e in requested.split(",")]
        return [e for e in exs if e in EXCHANGE_FETCHERS]
    return [e for e, enabled in EXCHANGES_ENABLED.items() if enabled]


def analyze_rates(rates_pct):
    """Считает метрики по списку ставок в %.
    Определяет направление: LONG (отрицательные ставки) или SHORT (положительные).
    """
    if not rates_pct:
        return None

    neg   = [r for r in rates_pct if r < 0]
    pos   = [r for r in rates_pct if r > 0]
    total = len(rates_pct)
    avg   = sum(rates_pct) / total

    # ── LONG: стабильно отрицательные ────────────────────────────────────
    # outlier = % ставок которые НЕ прошли порог (т.е. не достаточно отрицательные)
    below_neg    = sum(1 for r in rates_pct if r <= STABILITY_THRESHOLD)  # <= -0.04%
    outlier_long = (total - below_neg) / total * 100
    neg_avg      = sum(neg) / len(neg) if neg else 0.0
    neg_ratio    = len(neg) / total
    pass_stability_long = outlier_long <= MAX_OUTLIER_PCT
    pass_neg_avg        = bool(neg) and neg_avg <= NEG_AVG_THRESHOLD and neg_ratio >= MIN_NEG_RATIO

    # ── SHORT: стабильно положительные ───────────────────────────────────
    # outlier = % ставок которые НЕ прошли порог (т.е. не достаточно положительные)
    above_pos     = sum(1 for r in rates_pct if r >= -STABILITY_THRESHOLD)  # >= +0.04%
    outlier_short = (total - above_pos) / total * 100
    pos_avg       = sum(pos) / len(pos) if pos else 0.0
    pos_ratio     = len(pos) / total
    pass_stability_short = outlier_short <= MAX_OUTLIER_PCT
    pass_pos_avg         = bool(pos) and pos_avg >= -NEG_AVG_THRESHOLD and pos_ratio >= MIN_POS_RATIO

    # Определяем категорию и направление
    if pass_stability_long:
        category  = "full"
        direction = "LONG"
        outlier_pct = outlier_long
    elif pass_stability_short:
        category  = "full"
        direction = "SHORT"
        outlier_pct = outlier_short
    elif pass_neg_avg:
        category  = "partial"
        direction = "LONG"
        outlier_pct = outlier_long
    elif pass_pos_avg:
        category  = "partial"
        direction = "SHORT"
        outlier_pct = outlier_short
    else:
        category  = "fail"
        direction = "LONG" if avg <= 0 else "SHORT"
        outlier_pct = outlier_long

    return {
        "total":            total,
        "avg":              avg,
        "neg_avg":          neg_avg,
        "pos_avg":          pos_avg,
        "neg_count":        len(neg),
        "pos_count":        len(pos),
        "min":              min(rates_pct),
        "max":              max(rates_pct),
        "outlier_pct":      outlier_pct,
        "pass_stability":   pass_stability_long or pass_stability_short,
        "pass_neg_avg":     pass_neg_avg or pass_pos_avg,
        "category":         category,
        "direction":        direction,
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
    Также поддерживает быстрый ввод без префиксов:
    'BTC coinw 7' -> (['BTC'], 7, 'coinw')
    'BTC ETH phemex 14' -> (['BTC', 'ETH'], 14, 'phemex')
    """
    KNOWN_EXCHANGES = set(EXCHANGE_FETCHERS.keys()) if 'EXCHANGE_FETCHERS' in dir() else {
        "phemex", "xt", "toobit", "okx", "bingx", "kucoin", "gate", "blofin", "weex", "coinw"
    }

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
        # Распознаём название биржи без префикса
        if p in KNOWN_EXCHANGES:
            exchange = p; i += 1; continue
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
        lines.append(
            f"  `{label}` {cat}  avg `{r['avg']:+.4f}%`  key\\_avg `{key_avg:+.4f}%`  выбр `{r['outlier_pct']:.0f}%`"
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
    text = (
        "👋 *Phemex + XT Funding Rate Analyzer*\n\n"
        f"Активные биржи: `{ex_str}`\n\n"
        "Команды:\n"
        "/filter — анализ монет по фильтрам фандинга\n"
        "/funding — ставки фандинга по монете\n"
        "/calculator — калькулятор дохода от фандинга\n"
        "/analyze — скан всех монет на выбранной бирже\n"
        "/findpair — дельта-нейтраль: найти пару лонг+шорт\n"
        "/settings — настройки и управление биржами\n"
        "/help — справка\n\n"
        "💡 *Быстрый ввод:*\n"
        "`/filter ENJ phemex 7`\n"
        "`/funding ENJ phemex 7`\n"
        "`/calculator ENJ 25000 7 phemex`"
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



# ─────────────────────────────────────────────
# /analyze — скан монет с выбором биржи
# ─────────────────────────────────────────────

SCAN_EXCHANGE_FETCHERS = {
    "phemex": None,       # особая обработка — список монет с Phemex
    "kucoin": "kucoin",
    "toobit": "toobit",
    "xt":     "xt",
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
         InlineKeyboardButton("KuCoin",  callback_data="an_ex_kucoin")],
        [InlineKeyboardButton("Toobit",  callback_data="an_ex_toobit"),
         InlineKeyboardButton("XT",      callback_data="an_ex_xt")],
        [InlineKeyboardButton("CoinW",   callback_data="an_ex_coinw")],
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
    labels = {"phemex": "Phemex", "kucoin": "KuCoin", "toobit": "Toobit",
              "xt": "XT", "coinw": "CoinW"}
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

    labels = {"phemex": "Phemex", "kucoin": "KuCoin", "toobit": "Toobit",
              "xt": "XT", "coinw": "CoinW"}
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
                neg_ratio = len(neg) / len(clean)

                # Стабильность: минимум 30% отрицательных
                if neg_ratio < MIN_NEG_RATIO:
                    time.sleep(0.15)
                    continue

                # Среднее по всем выплатам
                avg_rate = sum(clean) / len(clean)
                if avg_rate >= 0:
                    time.sleep(0.15)
                    continue

                # payments_per_day = кол-во выплат за период / кол-во дней
                payments_per_day = len(clean) / days
                daily_income     = amount * abs(avg_rate) / 100 * payments_per_day

                if daily_income < threshold:
                    time.sleep(0.15)
                    continue

                # outlier для отображения
                below   = sum(1 for r in clean if r <= STABILITY_THRESHOLD)
                outlier = (len(clean) - below) / len(clean) * 100
                batch_results.append((coin, avg_rate, outlier, "LONG", "income", daily_income))
                passed.append((coin, avg_rate, outlier, "LONG", "income", daily_income))

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
            if batch_idx % 3 == 2 or batch_idx == len(batches) - 1:
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

    # ConversationHandlers — должны быть зарегистрированы до MessageHandler(unknown)
    app.add_handler(acf_conv)
    app.add_handler(fr_conv)
    app.add_handler(pc_conv)
    app.add_handler(delta_conv)
    

    app.add_handler(MessageHandler(filters.COMMAND, unknown))

    print("Бот запущен...")
    app.run_polling()


if __name__ == "__main__":
    main()

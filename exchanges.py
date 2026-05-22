import time
import requests
from datetime import datetime, timezone

from config import SUPABASE_KEY, SUPABASE_URL

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
            r = requests.get(url, params=params, timeout=6)
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
        r = requests.get(url, params=params, timeout=6)
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
        r = requests.get(url, params=params, timeout=6)
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
        r = requests.get(url, params=params, timeout=6)

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
        r = requests.get(url, params=params, timeout=6)

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


def kucoin_symbol_for_coin(coin):
    coin = coin.upper()
    if coin.endswith("USDT"):
        coin = coin[:-4]
    elif coin.endswith("USD"):
        coin = coin[:-3]
    base = "XBT" if coin == "BTC" else coin
    return f"{base}USDTM"


def kucoin_fetch(coin, start_ms, end_ms):
    """KuCoin Futures funding history.

    Endpoint returns fractional rates, e.g. 0.0001 = 0.01%.
    """
    sym = kucoin_symbol_for_coin(coin)
    try:
        url = "https://api-futures.kucoin.com/api/v1/contract/funding-rates"
        params = {"symbol": sym, "from": start_ms, "to": end_ms}
        r = requests.get(url, params=params, timeout=6)
        r.raise_for_status()
        data = r.json()
        if data.get("code") != "200000":
            return [], data.get("msg", "API error")
        rows = []
        for x in data.get("data", []):
            ts = int(x.get("timepoint") or x.get("fundingTime") or x.get("time") or 0)
            if start_ms <= ts <= end_ms:
                rows.append((ts, float(x.get("fundingRate", 0)) * 100))
        return sorted(rows, key=lambda x: x[0]), sym
    except Exception as e:
        return [], str(e)


def kucoin_get_all_symbols():
    """Returns USDT-margined open KuCoin Futures symbols as base coins."""
    url = "https://api-futures.kucoin.com/api/v1/contracts/active"
    r = requests.get(url, timeout=10)
    r.raise_for_status()
    data = r.json()
    if data.get("code") != "200000":
        raise ValueError(data.get("msg", "API error"))
    coins = []
    seen = set()
    for item in data.get("data", []):
        if item.get("quoteCurrency") != "USDT" or item.get("status") != "Open":
            continue
        coin = item.get("displayBaseCurrency") or item.get("baseCurrency")
        if not coin:
            continue
        coin = "BTC" if coin.upper() == "XBT" else coin.upper()
        if coin not in seen:
            coins.append(coin)
            seen.add(coin)
    return coins


# ─────────────────────────────────────────────
# BITUNIX API
# ─────────────────────────────────────────────


def bitunix_symbol_for_coin(coin):
    coin = coin.upper()
    if coin.endswith("USDT"):
        return coin
    if coin.endswith("USD"):
        return f"{coin[:-3]}USDT"
    return f"{coin}USDT"


def bitunix_fetch(coin, start_ms, end_ms):
    """Bitunix Futures funding history.

    Endpoint returns fractional rates, e.g. 0.0001 = 0.01%.
    """
    sym = bitunix_symbol_for_coin(coin)
    try:
        url = "https://fapi.bitunix.com/api/v1/futures/market/get_funding_rate_history"
        params = {"symbol": sym, "limit": 100}
        r = requests.get(url, params=params, timeout=6)
        r.raise_for_status()
        data = r.json()
        if data.get("code") != 0:
            return [], data.get("msg", "API error")
        rows = []
        for x in data.get("data", []):
            ts = int(x.get("fundingTime", 0))
            if start_ms <= ts <= end_ms:
                rows.append((ts, float(x.get("fundingRate", 0)) * 100))
        return sorted(rows, key=lambda x: x[0]), sym
    except Exception as e:
        return [], str(e)


def bitunix_get_all_symbols():
    """Returns open USDT perpetual symbols from Bitunix as base coins."""
    url = "https://fapi.bitunix.com/api/v1/futures/market/trading_pairs"
    r = requests.get(url, timeout=10)
    r.raise_for_status()
    data = r.json()
    if data.get("code") != 0:
        raise ValueError(data.get("msg", "API error"))
    coins = []
    seen = set()
    for item in data.get("data", []):
        if item.get("quote") != "USDT" or item.get("symbolStatus") != "OPEN":
            continue
        coin = str(item.get("base") or "").upper()
        if coin and coin not in seen:
            coins.append(coin)
            seen.add(coin)
    return coins


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
    from datetime import datetime, time as dt_time, timezone
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
        r = requests.get(url, headers=headers, params=params, timeout=4)
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


def phemex_get_all_symbols():
    """Получает список всех USDT-маржинальных перпетуалов с Phemex.
    Использует /exchange/public/cfg/v2/products — там 526 Listed USDT контрактов.
    Возвращает список строк-монет: ['BTC', 'ETH', 'ENJ', ...]
    """
    url = "https://api.phemex.com/exchange/public/cfg/v2/products"
    r = requests.get(url, timeout=10)
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


EXCHANGE_FETCHERS = {
    "phemex": phemex_fetch,
    "xt": xt_fetch,
    "toobit": toobit_fetch,
    "okx": okx_fetch,
    "bingx": bingx_fetch,
    "coinw": coinw_fetch,
    "kucoin": kucoin_fetch,
    "bitunix": bitunix_fetch,
}

EXCHANGE_SYMBOL_FETCHERS = {
    "phemex": phemex_get_all_symbols,
    "coinw": None,
    "kucoin": kucoin_get_all_symbols,
    "bitunix": bitunix_get_all_symbols,
}

EXCHANGE_LABELS = {
    "phemex": "Phemex",
    "xt": "XT",
    "toobit": "Toobit",
    "okx": "OKX",
    "bingx": "BingX",
    "coinw": "CoinW",
    "kucoin": "KuCoin",
    "bitunix": "Bitunix",
}

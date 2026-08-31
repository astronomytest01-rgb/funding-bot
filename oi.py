import time
import requests

from config import BINANCE_PROXY_URL, BYBIT_PROXY_URL
from exchanges import _proxies

OI_HIDE_BELOW_USD = 500_000
OI_OK_USD = 1_000_000
VOLUME_HIDE_BELOW_USD = 400_000
VOLUME_OK_USD = 2_000_000
EXCHANGE_OI_HIDE_BELOW_USD = {"xt": 150_000}
EXCHANGE_VOLUME_HIDE_BELOW_USD = {"xt": 150_000}
EXCHANGE_OI_OK_USD = {"xt": 150_000}
EXCHANGE_VOLUME_OK_USD = {"xt": 150_000}
LIQUIDITY_FILTER_DISABLED_EXCHANGES = {"aster"}

COINGECKO_DERIVATIVE_IDS = {
    "binance": "binance_futures",
    "bybit": "bybit",
    "phemex": "phemex_futures",
    "xt": "xt_derivatives",
    "toobit": "toobit_derivatives",
    "okx": "okex_swap",
    "gate": "gate_futures",
    "bingx": "bingx_futures",
    "coinw": "coinw_futures",
    "bitunix": "bitunix_futures",
    "kucoin": "kumex",
    "bitget": "bitget_futures",
    "aster": "aster",
}

_oi_cache = {}
_oi_cache_ts = {}
_volume_cache = {}
_volume_cache_ts = {}
_OI_CACHE_TTL = 10 * 60
_VOLUME_CACHE_TTL = 5 * 60


def _normalize_coin(coin):
    coin = (coin or "").upper().replace("-", "").replace("_", "")
    if coin.endswith("USDT"):
        coin = coin[:-4]
    elif coin.endswith("USD"):
        coin = coin[:-3]
    return coin


def _ticker_matches_coin(ticker, coin):
    base = _normalize_coin(coin)
    symbol = str(ticker.get("symbol") or "").upper().replace("-", "").replace("_", "")
    base_field = str(ticker.get("base") or "").upper()
    target = str(ticker.get("target") or ticker.get("quote") or "").upper()
    if base_field == base and target in ("USDT", "USD", ""):
        return True
    return symbol in (f"{base}USDT", f"{base}USD") or symbol.startswith(f"{base}USDT")


def _extract_oi_usd(ticker):
    for key in ("open_interest_usd", "open_interest", "converted_open_interest_usd"):
        value = ticker.get(key)
        if isinstance(value, dict):
            value = value.get("usd")
        if value in (None, ""):
            continue
        try:
            return float(value)
        except (TypeError, ValueError):
            continue
    return None


def _extract_volume_usd(ticker):
    for key in (
        "volume_24h",
        "converted_volume_usd",
        "converted_volume",
        "trade_volume_24h_usd",
        "volume_usd",
    ):
        value = ticker.get(key)
        if isinstance(value, dict):
            value = value.get("usd")
        if value in (None, ""):
            continue
        try:
            return float(value)
        except (TypeError, ValueError):
            continue
    return None


def _put_volume(result, coin, value):
    if value is None:
        return
    coin = _normalize_coin(coin)
    if not coin:
        return
    result[coin] = max(result.get(coin, 0), float(value))


def _fetch_native_kucoin_volume_map():
    r = requests.get("https://api-futures.kucoin.com/api/v1/contracts/active", timeout=10)
    r.raise_for_status()
    items = r.json().get("data", [])
    result = {}
    for item in items:
        symbol = str(item.get("symbol") or "").upper()
        if not symbol.endswith("USDTM"):
            continue
        try:
            volume = float(item.get("turnoverOf24h") or 0)
        except (TypeError, ValueError):
            continue
        base = str(item.get("displayBaseCurrency") or item.get("baseCurrency") or "").upper()
        if base == "XBT":
            _put_volume(result, "BTC", volume)
        _put_volume(result, base, volume)
    return result


def _fetch_native_toobit_volume_map():
    r = requests.get("https://api.toobit.com/quote/v1/contract/ticker/24hr", timeout=10)
    r.raise_for_status()
    items = r.json()
    result = {}
    for item in items if isinstance(items, list) else []:
        symbol = str(item.get("s") or "").upper()
        if not symbol.endswith("-SWAP-USDT"):
            continue
        try:
            volume = float(item.get("qv") or 0)
        except (TypeError, ValueError):
            continue
        _put_volume(result, symbol[:-len("-SWAP-USDT")], volume)
    return result


def _fetch_native_xt_volume_map():
    r = requests.get("https://fapi.xt.com/future/market/v1/public/q/tickers", timeout=10)
    r.raise_for_status()
    items = r.json().get("result", [])
    result = {}
    for item in items:
        symbol = str(item.get("s") or "").upper()
        if not symbol.endswith("_USDT"):
            continue
        try:
            volume = float(item.get("v") or 0)
        except (TypeError, ValueError):
            continue
        _put_volume(result, symbol[:-len("_USDT")], volume)
    return result


def _fetch_native_phemex_volume_map():
    r = requests.get("https://api.phemex.com/md/v2/ticker/24hr/all", timeout=10)
    r.raise_for_status()
    items = r.json().get("result", [])
    result = {}
    for item in items:
        symbol = str(item.get("symbol") or "").upper()
        if not symbol.endswith("USDT"):
            continue
        try:
            volume = float(item.get("turnoverRv") or 0)
        except (TypeError, ValueError):
            continue
        _put_volume(result, symbol[:-len("USDT")], volume)
    return result


def _fetch_native_binance_volume_map():
    r = requests.get("https://fapi.binance.com/fapi/v1/ticker/24hr", timeout=10, proxies=_proxies(BINANCE_PROXY_URL))
    r.raise_for_status()
    result = {}
    for item in r.json():
        symbol = str(item.get("symbol") or "").upper()
        if not symbol.endswith("USDT"):
            continue
        _put_volume(result, symbol[:-len("USDT")], item.get("quoteVolume"))
    return result


def _fetch_native_bybit_volume_map():
    r = requests.get("https://api.bybit.com/v5/market/tickers", params={"category": "linear"}, timeout=10, proxies=_proxies(BYBIT_PROXY_URL))
    r.raise_for_status()
    data = r.json()
    result = {}
    for item in data.get("result", {}).get("list", []):
        symbol = str(item.get("symbol") or "").upper()
        if not symbol.endswith("USDT"):
            continue
        _put_volume(result, symbol[:-len("USDT")], item.get("turnover24h"))
    return result


def _fetch_native_gate_volume_map():
    r = requests.get("https://api.gateio.ws/api/v4/futures/usdt/tickers", timeout=10)
    r.raise_for_status()
    result = {}
    for item in r.json():
        symbol = str(item.get("contract") or "").upper()
        if not symbol.endswith("_USDT"):
            continue
        _put_volume(result, symbol[:-len("_USDT")], item.get("volume_24h_quote") or item.get("volume_24h_settle"))
    return result


def _fetch_native_bitget_volume_map():
    r = requests.get(
        "https://api.bitget.com/api/v2/mix/market/tickers",
        params={"productType": "USDT-FUTURES"},
        timeout=10,
    )
    r.raise_for_status()
    data = r.json()
    if data.get("code") != "00000":
        return {}
    result = {}
    for item in data.get("data", []):
        symbol = str(item.get("symbol") or "").upper()
        if not symbol.endswith("USDT"):
            continue
        try:
            volume = float(item.get("quoteVolume") or item.get("usdtVolume") or 0)
        except (TypeError, ValueError):
            continue
        _put_volume(result, symbol[:-len("USDT")], volume)
    return result


def _fetch_native_aster_volume_map():
    r = requests.get("https://fapi.asterdex.com/fapi/v1/ticker/24hr", timeout=10)
    r.raise_for_status()
    items = r.json()
    result = {}
    for item in items if isinstance(items, list) else []:
        symbol = str(item.get("symbol") or "").upper()
        if not symbol.endswith("USDT"):
            continue
        try:
            volume = float(item.get("quoteVolume") or 0)
        except (TypeError, ValueError):
            continue
        _put_volume(result, symbol[:-len("USDT")], volume)
    return result


NATIVE_VOLUME_FETCHERS = {
    "binance": _fetch_native_binance_volume_map,
    "bybit": _fetch_native_bybit_volume_map,
    "kucoin": _fetch_native_kucoin_volume_map,
    "toobit": _fetch_native_toobit_volume_map,
    "xt": _fetch_native_xt_volume_map,
    "phemex": _fetch_native_phemex_volume_map,
    "gate": _fetch_native_gate_volume_map,
    "bitget": _fetch_native_bitget_volume_map,
    "aster": _fetch_native_aster_volume_map,
}


def _fetch_native_bitget_oi_map():
    r = requests.get(
        "https://api.bitget.com/api/v2/mix/market/tickers",
        params={"productType": "USDT-FUTURES"},
        timeout=10,
    )
    r.raise_for_status()
    data = r.json()
    if data.get("code") != "00000":
        return {}
    result = {}
    for item in data.get("data", []):
        symbol = str(item.get("symbol") or "").upper()
        if not symbol.endswith("USDT"):
            continue
        try:
            oi_usd = float(item.get("holdingAmount") or 0) * float(item.get("markPrice") or item.get("lastPr") or 0)
        except (TypeError, ValueError):
            continue
        _put_volume(result, symbol[:-len("USDT")], oi_usd)
    return result


NATIVE_OI_FETCHERS = {
    "bitget": _fetch_native_bitget_oi_map,
}


def get_oi_hide_threshold(exchange):
    return EXCHANGE_OI_HIDE_BELOW_USD.get(exchange.lower(), OI_HIDE_BELOW_USD)


def get_volume_hide_threshold(exchange):
    return EXCHANGE_VOLUME_HIDE_BELOW_USD.get(exchange.lower(), VOLUME_HIDE_BELOW_USD)


def get_oi_ok_threshold(exchange):
    return EXCHANGE_OI_OK_USD.get(exchange.lower(), OI_OK_USD)


def get_volume_ok_threshold(exchange):
    return EXCHANGE_VOLUME_OK_USD.get(exchange.lower(), VOLUME_OK_USD)


def is_liquidity_filter_disabled(exchange):
    return exchange.lower() in LIQUIDITY_FILTER_DISABLED_EXCHANGES


def get_exchange_oi_map(exchange):
    exchange = exchange.lower()
    now = time.time()
    if exchange in _oi_cache and now - _oi_cache_ts.get(exchange, 0) < _OI_CACHE_TTL:
        return _oi_cache[exchange]
    native_fetcher = NATIVE_OI_FETCHERS.get(exchange)
    if native_fetcher:
        try:
            result = native_fetcher()
        except Exception:
            result = {}
        if result:
            _oi_cache[exchange] = result
            _oi_cache_ts[exchange] = now
            return result
    cg_id = COINGECKO_DERIVATIVE_IDS.get(exchange)
    if not cg_id:
        return {}
    url = f"https://api.coingecko.com/api/v3/derivatives/exchanges/{cg_id}"
    try:
        r = requests.get(url, params={"include_tickers": "all"}, timeout=8)
        if r.status_code == 429:
            # Do not poison the cache with an empty map on rate limits.
            return _oi_cache.get(exchange, {})
        r.raise_for_status()
        data = r.json()
    except Exception:
        # Keep the last good OI snapshot if CoinGecko has a transient failure.
        return _oi_cache.get(exchange, {})
    tickers = data.get("tickers") or []
    result = {}
    for ticker in tickers:
        symbol = str(ticker.get("symbol") or "").upper()
        base = str(ticker.get("base") or "").upper()
        oi_usd = _extract_oi_usd(ticker)
        if oi_usd is None:
            continue
        if base:
            result[base] = max(result.get(base, 0), oi_usd)
        clean_symbol = symbol.replace("-", "").replace("_", "")
        for suffix in ("USDT", "USD"):
            if clean_symbol.endswith(suffix):
                coin = clean_symbol[:-len(suffix)]
                result[coin] = max(result.get(coin, 0), oi_usd)
    _oi_cache[exchange] = result
    _oi_cache_ts[exchange] = now
    return result


def get_exchange_volume_map(exchange):
    exchange = exchange.lower()
    now = time.time()
    if exchange in _volume_cache and now - _volume_cache_ts.get(exchange, 0) < _VOLUME_CACHE_TTL:
        return _volume_cache[exchange]

    result = {}
    native_fetcher = NATIVE_VOLUME_FETCHERS.get(exchange)
    if native_fetcher:
        try:
            result = native_fetcher()
        except Exception:
            result = {}

    if not result:
        cg_id = COINGECKO_DERIVATIVE_IDS.get(exchange)
        if cg_id:
            url = f"https://api.coingecko.com/api/v3/derivatives/exchanges/{cg_id}"
            try:
                r = requests.get(url, params={"include_tickers": "all"}, timeout=8)
                if r.status_code == 429:
                    return _volume_cache.get(exchange, {})
                r.raise_for_status()
                tickers = r.json().get("tickers") or []
            except Exception:
                return _volume_cache.get(exchange, {})
            for ticker in tickers:
                volume = _extract_volume_usd(ticker)
                if volume is None:
                    continue
                base = str(ticker.get("base") or "").upper()
                if base:
                    _put_volume(result, base, volume)
                symbol = str(ticker.get("symbol") or "").upper()
                clean_symbol = symbol.replace("-", "").replace("_", "")
                for suffix in ("USDT", "USD"):
                    if clean_symbol.endswith(suffix):
                        _put_volume(result, clean_symbol[:-len(suffix)], volume)

    if result:
        _volume_cache[exchange] = result
        _volume_cache_ts[exchange] = now
    return result or _volume_cache.get(exchange, {})


def get_open_interest_usd(exchange, coin):
    oi_map = get_exchange_oi_map(exchange)
    return oi_map.get(_normalize_coin(coin))


def get_24h_volume_usd(exchange, coin):
    volume_map = get_exchange_volume_map(exchange)
    return volume_map.get(_normalize_coin(coin))


def is_oi_allowed(exchange, coin):
    """Hard filter: hide only confirmed OI below the exchange threshold.

    Missing CoinGecko data is kept visible as a warning because the exchange API
    data can still be valid while the OI provider is incomplete or rate-limited.
    """
    if is_liquidity_filter_disabled(exchange):
        return True
    oi_usd = get_open_interest_usd(exchange, coin)
    return oi_usd is None or oi_usd >= get_oi_hide_threshold(exchange)


def is_volume_allowed(exchange, coin):
    """Hard filter: hide only confirmed 24h turnover below the exchange threshold."""
    if is_liquidity_filter_disabled(exchange):
        return True
    volume_usd = get_24h_volume_usd(exchange, coin)
    return volume_usd is None or volume_usd >= get_volume_hide_threshold(exchange)


def format_oi_status(exchange, coin, order_usd=15_000):
    oi_usd = get_open_interest_usd(exchange, coin)
    hide_threshold = get_oi_hide_threshold(exchange)
    ok_threshold = get_oi_ok_threshold(exchange)
    if oi_usd is None:
        return "⚠️ OI: нет данных"
    order_share = order_usd / oi_usd if oi_usd > 0 else 1
    oi_text = f"${oi_usd:,.0f}"
    share_text = f"{order_share * 100:.2f}%"
    if oi_usd >= ok_threshold:
        return f"✅ OI {oi_text} | $15k = {share_text}"
    if oi_usd < hide_threshold:
        return f"🚫 OI {oi_text} | $15k = {share_text}"
    return f"⚠️ OI {oi_text} | $15k = {share_text}"


def format_volume_status(exchange, coin):
    volume_usd = get_24h_volume_usd(exchange, coin)
    hide_threshold = get_volume_hide_threshold(exchange)
    ok_threshold = get_volume_ok_threshold(exchange)
    if volume_usd is None:
        return "⚠️ Vol24h: нет данных"
    volume_text = f"${volume_usd:,.0f}"
    if volume_usd < hide_threshold:
        return f"🚫 Vol24h {volume_text}"
    if volume_usd < ok_threshold:
        return f"⚠️ Vol24h {volume_text}"
    return f"✅ Vol24h {volume_text}"

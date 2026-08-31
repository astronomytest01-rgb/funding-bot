import time
import requests

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
_cg_tickers_cache = {}
_cg_tickers_cache_ts = {}
_OI_CACHE_TTL = 10 * 60
_VOLUME_CACHE_TTL = 5 * 60
_CG_TICKERS_CACHE_TTL = 5 * 60

# Contract: liquidity data for this scanner must come from CoinGecko
# Derivatives only. Do not use native exchange APIs for OI/24h volume here;
# funding history and symbol lists can still use exchange APIs.
LIQUIDITY_DATA_SOURCE = "coingecko_derivatives"


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


def _put_liquidity_value(result, coin, value):
    if value is None:
        return
    coin = _normalize_coin(coin)
    if not coin:
        return
    result[coin] = max(result.get(coin, 0), float(value))


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


def get_coingecko_tickers(exchange):
    exchange = exchange.lower()
    now = time.time()
    if exchange in _cg_tickers_cache and now - _cg_tickers_cache_ts.get(exchange, 0) < _CG_TICKERS_CACHE_TTL:
        return _cg_tickers_cache[exchange]
    cg_id = COINGECKO_DERIVATIVE_IDS.get(exchange)
    if not cg_id:
        return []
    url = f"https://api.coingecko.com/api/v3/derivatives/exchanges/{cg_id}"
    try:
        r = requests.get(url, params={"include_tickers": "all"}, timeout=8)
        if r.status_code == 429:
            return _cg_tickers_cache.get(exchange, [])
        r.raise_for_status()
        tickers = r.json().get("tickers") or []
    except Exception:
        return _cg_tickers_cache.get(exchange, [])
    _cg_tickers_cache[exchange] = tickers
    _cg_tickers_cache_ts[exchange] = now
    return tickers


def get_exchange_oi_map(exchange):
    exchange = exchange.lower()
    now = time.time()
    if exchange in _oi_cache and now - _oi_cache_ts.get(exchange, 0) < _OI_CACHE_TTL:
        return _oi_cache[exchange]
    tickers = get_coingecko_tickers(exchange)
    if not tickers:
        return _oi_cache.get(exchange, {})
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

    tickers = get_coingecko_tickers(exchange)
    if not tickers:
        return _volume_cache.get(exchange, {})
    result = {}
    for ticker in tickers:
        volume = _extract_volume_usd(ticker)
        if volume is None:
            continue
        base = str(ticker.get("base") or "").upper()
        if base:
            _put_liquidity_value(result, base, volume)
        symbol = str(ticker.get("symbol") or "").upper()
        clean_symbol = symbol.replace("-", "").replace("_", "")
        for suffix in ("USDT", "USD"):
            if clean_symbol.endswith(suffix):
                _put_liquidity_value(result, clean_symbol[:-len(suffix)], volume)

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

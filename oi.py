import time
import requests

OI_HIDE_BELOW_USD = 500_000
OI_OK_USD = 1_000_000

COINGECKO_DERIVATIVE_IDS = {
    "phemex": "phemex_futures",
    "xt": "xt_derivatives",
    "toobit": "toobit_derivatives",
    "okx": "okex_swap",
    "bingx": "bingx_futures",
    "coinw": "coinw_futures",
    "bitunix": "bitunix_futures",
    "kucoin": "kumex",
}

_oi_cache = {}
_oi_cache_ts = {}
_OI_CACHE_TTL = 10 * 60


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


def get_exchange_oi_map(exchange):
    exchange = exchange.lower()
    cg_id = COINGECKO_DERIVATIVE_IDS.get(exchange)
    if not cg_id:
        return {}
    now = time.time()
    if exchange in _oi_cache and now - _oi_cache_ts.get(exchange, 0) < _OI_CACHE_TTL:
        return _oi_cache[exchange]
    url = f"https://api.coingecko.com/api/v3/derivatives/exchanges/{cg_id}"
    try:
        r = requests.get(url, params={"include_tickers": "all"}, timeout=3)
        if r.status_code == 429:
            _oi_cache[exchange] = {}
            _oi_cache_ts[exchange] = now
            return {}
        r.raise_for_status()
        data = r.json()
    except Exception:
        _oi_cache[exchange] = {}
        _oi_cache_ts[exchange] = now
        return {}
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


def get_open_interest_usd(exchange, coin):
    oi_map = get_exchange_oi_map(exchange)
    return oi_map.get(_normalize_coin(coin))


def is_oi_allowed(exchange, coin):
    """Hard filter: hide only confirmed OI below $500k.

    Missing CoinGecko data is kept visible as a warning because the exchange API
    data can still be valid while the OI provider is incomplete or rate-limited.
    """
    oi_usd = get_open_interest_usd(exchange, coin)
    return oi_usd is None or oi_usd >= OI_HIDE_BELOW_USD


def format_oi_status(exchange, coin, order_usd=15_000):
    oi_usd = get_open_interest_usd(exchange, coin)
    if oi_usd is None:
        return "⚠️ OI: нет данных"
    order_share = order_usd / oi_usd if oi_usd > 0 else 1
    oi_text = f"${oi_usd:,.0f}"
    share_text = f"{order_share * 100:.2f}%"
    if oi_usd >= OI_OK_USD:
        return f"✅ OI {oi_text} | $15k = {share_text}"
    return f"⚠️ OI {oi_text} | $15k = {share_text}"

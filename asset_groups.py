"""Deterministic asset grouping for Telegram funding reports.

Gemini is intentionally not involved: the mathematical report and its sections
must remain available when the optional AI service is slow or unavailable.
"""

GROUP_ORDER = ("tradfi", "tier1", "other")
GROUP_LABELS = {
    "tradfi": "📈 Фондовый рынок / TradFi",
    "tier1": "💎 Tier-1",
    "other": "🧪 Остальные / щиткоины",
}

# Curated liquid/infrastructure crypto assets. Product-specific inclusions
# requested by the operator (DASH, GRAM) stay explicit here.
TIER1_CRYPTO = frozenset({
    "AAVE", "ADA", "ALGO", "APT", "ARB", "ATOM", "AVAX", "BCH", "BNB",
    "BTC", "CRO", "DASH", "DOT", "EGLD", "EOS", "ETC", "ETH", "FIL",
    "GRAM", "HBAR", "ICP", "IMX", "INJ", "KAS", "LDO", "LINK", "LTC",
    "MKR", "NEAR", "OKB", "OP", "POL", "QNT", "RENDER", "RUNE", "SEI",
    "SOL", "STX", "SUI", "TAO", "TIA", "TON", "TRX", "UNI", "VET",
    "XLM", "XMR", "XRP", "ZEC",
})

STOCK_SYMBOLS = frozenset({
    "AAPL", "AAOI", "ADBE", "AMAT", "AMD", "AMZN", "ARM", "ASML", "AVGO",
    "AXTI", "BA", "BABA", "BRKB", "COIN", "CRM", "CRWV", "DELL", "DIS",
    "GOOGL", "GS", "HOOD", "IBM", "INTC", "JPM", "MA", "META", "MRVL",
    "MSFT", "MSTR", "NFLX", "NKE", "NVDA", "ORCL", "PLTR", "PYPL", "QCOM",
    "RDDT", "SHOP", "SNAP", "TSLA", "TSM", "UBER", "V", "WMT",
})

INDEX_SYMBOLS = frozenset({
    "DAX", "DJIA", "GER40", "HK50", "NASDAQ", "NAS100", "NDX", "NIKKEI",
    "QQQ", "RUSSELL", "SP500", "SPX", "UK100", "US30", "US100", "US2000",
})

COMMODITY_AND_FX_SYMBOLS = frozenset({
    "AUD", "BRENT", "CAD", "CHF", "CL", "COPPER", "EUR", "GBP", "GOLD",
    "JPY", "NG", "NZD", "OIL", "SILVER", "WTI", "XAG", "XAU",
})

_EXPLICIT_ALIASES = {
    "GOOG": "GOOGL",
    "GOOGLX": "GOOGL",
    "NCSKGOOGL2": "GOOGL",
    "SPX": "SP500",
    "US500": "SP500",
    "NCSISP5002": "SP500",
    "NAS100": "NASDAQ",
    "NDX": "NASDAQ",
    "US100": "NASDAQ",
    "QQQ": "NASDAQ",
    "DJIA": "US30",
    "DOW": "US30",
    "GOLD": "XAU",
    "SILVER": "XAG",
}

# Some venues suffix tokenized equities with X. Only known stock names are
# expanded, so crypto tickers that naturally end in X are never altered.
for _stock in STOCK_SYMBOLS:
    _EXPLICIT_ALIASES.setdefault(f"{_stock}X", _stock)


def clean_symbol(symbol):
    value = str(symbol or "").upper().replace("-", "").replace("_", "").replace("/", "")
    value = value.replace("PERP", "")
    # A few provider aliases embed an underlying USD suffix before the actual
    # USDT quote (for example NCSKGOOGL2USD-USDT), so unwrap at most twice.
    for _ in range(2):
        for suffix in ("USDTM", "USDT", "USD"):
            if value.endswith(suffix):
                value = value[:-len(suffix)]
                break
        else:
            break
    return value


def canonical_asset_symbol(symbol):
    value = clean_symbol(symbol)
    return _EXPLICIT_ALIASES.get(value, value)


def asset_group(symbol):
    canonical = canonical_asset_symbol(symbol)
    if (
        canonical in STOCK_SYMBOLS
        or canonical in INDEX_SYMBOLS
        or canonical in COMMODITY_AND_FX_SYMBOLS
    ):
        return "tradfi"
    if canonical in TIER1_CRYPTO:
        return "tier1"
    return "other"


def group_items(items, symbol_getter):
    grouped = {key: [] for key in GROUP_ORDER}
    for item in items:
        grouped[asset_group(symbol_getter(item))].append(item)
    return grouped

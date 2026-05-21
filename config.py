import os

# ── Токены и ключи ─────────────────────────────
BOT_TOKEN       = os.environ.get("BOT_TOKEN", "")
GEMINI_API_KEY  = os.environ.get("GEMINI_API_KEY", "")
REPORT_CHAT_ID  = os.environ.get("REPORT_CHAT_ID", "")
SUPABASE_URL    = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY    = os.environ.get("SUPABASE_KEY", "")

# ── Настройки фильтров ─────────────────────────
DEFAULT_DAYS         = 7
STABILITY_THRESHOLD  = -0.04
MAX_OUTLIER_PCT      = 25
NEG_AVG_THRESHOLD    = -0.08
MIN_NEG_RATIO        = 0.30   # минимум 30% ставок должны быть отрицательными для ЛОНГ
MIN_POS_RATIO        = 0.30   # минимум 30% ставок должны быть положительными для ШОРТ
AN_ANOMALY_THRESHOLD = 0.8    # отсечение аномалий свыше ±0.8%

# ── Настройки вечернего авто-скана ─────────────
AUTO_SCAN_AMOUNT    = 20000
AUTO_SCAN_THRESHOLD = 29
AUTO_SCAN_DAYS      = 3
AUTO_SCAN_EXCHANGES = ["phemex", "xt", "toobit", "coinw", "okx", "bingx"]

# ── Биржи (Вкл/Выкл) ───────────────────────────
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
    "zoomex": True,
}

EXCHANGE_LABELS = {
    "phemex": "Phemex", "xt": "XT", "toobit": "Toobit",
    "okx": "OKX", "bingx": "BingX", "kucoin": "KuCoin",
    "gate": "Gate.io", "blofin": "BloFin", "weex": "WEEX",
    "coinw": "CoinW", "zoomex": "Zoomex",
}
import os

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
REPORT_CHAT_ID = os.environ.get("REPORT_CHAT_ID", "")

# CoinW — данные из Supabase (коллектор собирает каждые 8 часов)
SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "")

DEFAULT_DAYS = 7
STABILITY_THRESHOLD = -0.04
MAX_OUTLIER_PCT = 25
NEG_AVG_THRESHOLD = -0.08
MIN_NEG_RATIO = 0.30
MIN_POS_RATIO = 0.30

# Последние 4 ставки отсеивают монеты, где funding уже развернулся.
RECENT_TREND_RATES = 4
RECENT_TREND_MIN_GOOD_RATIO = 0.50

# Вечерний авто-отчёт: 20:00 Europe/Kyiv = 17:00 UTC.
AUTO_SCAN_AMOUNT = 20000
AUTO_SCAN_THRESHOLD = 29
AUTO_SCAN_DAYS = 3

EXCHANGES_ENABLED = {
    "phemex": True,
    "xt": True,
    "toobit": True,
    "okx": True,
    "bingx": True,
    # CoinW/Bitunix API-код оставлен в проекте, но биржи временно скрыты
    # из команд, кнопок, /analyze и вечернего отчёта.
    "coinw": False,
    "kucoin": True,
    "bitunix": False,
}

TEMPORARILY_DISABLED_EXCHANGES = {"coinw", "bitunix"}

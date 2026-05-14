"""
Funding Rate Report Bot
Webhook-based (no polling conflicts)
"""

import os
import time
import logging
import requests
from datetime import datetime, timezone
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

from report_engine import register_report_handlers

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────────────────────────────────────

BOT_TOKEN    = os.environ.get("BOT_TOKEN", "")
SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "")
REPORT_CHAT_ID = int(os.environ.get("REPORT_CHAT_ID", "141770005"))

# Webhook (для Railway)
WEBHOOK_URL  = os.environ.get("WEBHOOK_URL", "")   # например https://xxx.railway.app
WEBHOOK_PATH = "/webhook"
PORT         = int(os.environ.get("PORT", "8080"))

# ─────────────────────────────────────────────────────────────────────────────
# ПАРАМЕТРЫ АНАЛИЗА (используются в report_engine через импорт bot)
# ─────────────────────────────────────────────────────────────────────────────

DEFAULT_DAYS        = 3
STABILITY_THRESHOLD = -0.04
MAX_OUTLIER_PCT     = 25
NEG_AVG_THRESHOLD   = -0.08
MIN_NEG_RATIO       = 0.30
MIN_POS_RATIO       = 0.30

# ─────────────────────────────────────────────────────────────────────────────
# FETCHERS — импортируем из общего модуля
# ─────────────────────────────────────────────────────────────────────────────

def phemex_fetch(coin, start_ms, end_ms):
    candidates = []
    coin = coin.upper()
    if coin.endswith("USDT") or coin.endswith("USD"):
        candidates = [f".{coin}FR8H"]
    else:
        candidates = [f".{coin}USDTFR8H", f".{coin}USDFR8H"]
    for sym in candidates:
        try:
            url = "https://api.phemex.com/api-data/public/data/funding-rate-history"
            params = {"symbol": sym, "start": start_ms, "end": end_ms, "limit": 1000}
            r = requests.get(url, params=params, timeout=10)
            data = r.json()
            if data.get("code") != 0:
                continue
            rows = [x for x in data.get("data", {}).get("rows", [])
                    if x["fundingTime"] >= start_ms and abs(float(x["fundingRate"])) < 0.01]
            if rows:
                return [(x["fundingTime"], float(x["fundingRate"]) * 100) for x in rows], sym
        except Exception:
            pass
        time.sleep(0.15)
    return [], None


def xt_fetch(coin, start_ms, end_ms):
    coin = coin.upper()
    sym = f"{coin.lower()}_usdt" if not coin.endswith("USDT") else coin.lower()
    try:
        r = requests.get(
            "https://fapi.xt.com/future/market/v1/public/q/funding-rate-record",
            params={"symbol": sym, "limit": 500, "direction": "NEXT"}, timeout=10
        )
        data = r.json()
        if data.get("returnCode") != 0:
            return [], sym
        result = data.get("result", {})
        items = result if isinstance(result, list) else result.get("items", [])
        filtered = [(x.get("createdTime", 0), float(x.get("fundingRate", 0)) * 100)
                    for x in items if x.get("createdTime", 0) >= start_ms]
        return filtered or ([], sym)
    except Exception as e:
        return [], str(e)


def toobit_fetch(coin, start_ms, end_ms):
    coin = coin.upper()
    sym = f"{coin}-SWAP-USDT" if not coin.endswith("USDT") else f"{coin[:-4]}-SWAP-USDT"
    try:
        r = requests.get(
            "https://api.toobit.com/api/v1/futures/historyFundingRate",
            params={"symbol": sym, "limit": 1000}, timeout=10
        )
        data = r.json()
        if not isinstance(data, list):
            return [], sym
        filtered = [(int(x.get("settleTime", 0)), float(x.get("settleRate", 0)) * 100)
                    for x in data if int(x.get("settleTime", 0)) >= start_ms]
        return filtered, sym
    except Exception as e:
        return [], str(e)


def bingx_fetch(coin, start_ms, end_ms):
    coin = coin.upper()
    sym = f"{coin}-USDT" if not coin.endswith("USDT") else f"{coin[:-4]}-USDT"
    try:
        r = requests.get(
            "https://open-api.bingx.com/openApi/swap/v2/quote/fundingRate",
            params={"symbol": sym, "limit": 1000}, timeout=10
        )
        data = r.json()
        if data.get("code") != 0:
            return [], sym
        items = data.get("data", [])
        filtered = [(int(x.get("fundingTime", 0)), float(x.get("fundingRate", 0)) * 100)
                    for x in items if int(x.get("fundingTime", 0)) >= start_ms]
        return filtered, sym
    except Exception as e:
        return [], str(e)


def gate_fetch(coin, start_ms, end_ms):
    coin = coin.upper()
    sym = coin if coin.endswith("USDT") else f"{coin}_USDT"
    try:
        r = requests.get(
            "https://api.gateio.ws/api/v4/futures/usdt/funding_rate",
            params={"contract": sym, "from": start_ms // 1000, "to": end_ms // 1000, "limit": 1000},
            timeout=10
        )
        data = r.json()
        if not isinstance(data, list):
            return [], sym
        filtered = [(int(x.get("t", 0)) * 1000, float(x.get("r", 0)) * 100)
                    for x in data if int(x.get("t", 0)) * 1000 >= start_ms]
        return filtered, sym
    except Exception as e:
        return [], str(e)


def kucoin_fetch(coin, start_ms, end_ms):
    coin = coin.upper()
    sym = f"{coin}USDTM"
    try:
        r = requests.get(
            "https://api-futures.kucoin.com/api/v1/contract/funding-rates",
            params={"symbol": sym, "from": start_ms, "to": end_ms}, timeout=10
        )
        data = r.json()
        if data.get("code") != "200000":
            return [], sym
        items = data.get("data", [])
        filtered = [(int(x.get("timepoint", 0)), float(x.get("fundingRate", 0)) * 100)
                    for x in items]
        return filtered, sym
    except Exception as e:
        return [], str(e)


def zoomex_fetch(coin, start_ms, end_ms):
    sym = f"{coin.upper()}USDT"
    url = "https://openapi.zoomex.com/cloud/trade/v3/market/funding/history"
    all_rows = []
    cursor = None
    try:
        while True:
            params = {"category": "linear", "symbol": sym, "limit": 200}
            if cursor:
                params["cursor"] = cursor
            r = requests.get(url, params=params, timeout=10)
            if r.status_code != 200:
                break
            data = r.json()
            if data.get("retCode") != 0:
                break
            rows = data["result"]["list"]
            if not rows:
                break
            for row in rows:
                ts   = int(row["fundingRateTimestamp"])
                rate = float(row["fundingRate"]) * 100
                if ts < start_ms:
                    return sorted(all_rows, key=lambda x: x[0]), sym
                if ts <= end_ms:
                    all_rows.append((ts, rate))
            cursor = data["result"].get("nextPageCursor")
            if not cursor:
                break
            time.sleep(0.1)
        return sorted(all_rows, key=lambda x: x[0]), sym
    except Exception as e:
        return [], str(e)


def coinw_fetch(coin, start_ms, end_ms):
    if not SUPABASE_URL or not SUPABASE_KEY:
        return [], "SUPABASE не настроен"
    symbol = coin.upper()
    start_iso = datetime.fromtimestamp(start_ms / 1000, tz=timezone.utc).isoformat()
    try:
        r = requests.get(
            f"{SUPABASE_URL}/rest/v1/funding_rates",
            headers={"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"},
            params={"symbol": f"eq.{symbol}", "collected_at": f"gte.{start_iso}",
                    "order": "funding_time.asc", "limit": "1000",
                    "select": "rate_pct,collected_at,funding_time"},
            timeout=10
        )
        rows = r.json()
        if not rows:
            return [], f"Нет данных для {symbol}"
        seen = set()
        result = []
        for row in rows:
            ft = row.get("funding_time")
            if ft and ft not in seen:
                seen.add(ft)
                dt = datetime.fromisoformat(ft.replace("Z", "+00:00"))
                result.append((int(dt.timestamp() * 1000), float(row["rate_pct"])))
        return result, f"coinw_{symbol}"
    except Exception as e:
        return [], str(e)


EXCHANGE_FETCHERS = {
    "phemex": phemex_fetch,
    "xt":     xt_fetch,
    "toobit": toobit_fetch,
    "bingx":  bingx_fetch,
    "gate":   gate_fetch,
    "kucoin": kucoin_fetch,
    "zoomex": zoomex_fetch,
    "coinw":  coinw_fetch,
}

EXCHANGE_LABELS = {
    "phemex": "Phemex",
    "xt":     "XT",
    "toobit": "Toobit",
    "bingx":  "BingX",
    "gate":   "Gate.io",
    "kucoin": "KuCoin",
    "zoomex": "Zoomex",
    "coinw":  "CoinW",
}


def analyze_rates(rates_pct):
    if not rates_pct:
        return None
    neg   = [r for r in rates_pct if r < 0]
    pos   = [r for r in rates_pct if r > 0]
    total = len(rates_pct)
    avg   = sum(rates_pct) / total

    below_neg    = sum(1 for r in rates_pct if r <= STABILITY_THRESHOLD)
    outlier_long = (total - below_neg) / total * 100
    neg_avg      = sum(neg) / len(neg) if neg else 0.0
    neg_ratio    = len(neg) / total
    pass_stability_long = outlier_long <= MAX_OUTLIER_PCT
    pass_neg_avg        = bool(neg) and neg_avg <= NEG_AVG_THRESHOLD and neg_ratio >= MIN_NEG_RATIO

    above_pos     = sum(1 for r in rates_pct if r >= -STABILITY_THRESHOLD)
    outlier_short = (total - above_pos) / total * 100
    pos_avg       = sum(pos) / len(pos) if pos else 0.0
    pos_ratio     = len(pos) / total
    pass_stability_short = outlier_short <= MAX_OUTLIER_PCT
    pass_pos_avg         = bool(pos) and pos_avg >= -NEG_AVG_THRESHOLD and pos_ratio >= MIN_POS_RATIO

    if pass_stability_long:
        category, direction, outlier_pct = "full", "LONG", outlier_long
    elif pass_stability_short:
        category, direction, outlier_pct = "full", "SHORT", outlier_short
    elif pass_neg_avg:
        category, direction, outlier_pct = "partial", "LONG", outlier_long
    elif pass_pos_avg:
        category, direction, outlier_pct = "partial", "SHORT", outlier_short
    else:
        category  = "fail"
        direction = "LONG" if avg <= 0 else "SHORT"
        outlier_pct = outlier_long

    return {
        "total": total, "avg": avg,
        "neg_avg": neg_avg, "pos_avg": pos_avg,
        "neg_count": len(neg), "pos_count": len(pos),
        "min": min(rates_pct), "max": max(rates_pct),
        "outlier_pct": outlier_pct,
        "pass_stability": pass_stability_long or pass_stability_short,
        "pass_neg_avg": pass_neg_avg or pass_pos_avg,
        "category": category, "direction": direction,
    }


# ─────────────────────────────────────────────────────────────────────────────
# КОМАНДЫ БОТА
# ─────────────────────────────────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 *Funding Rate Report Bot*\n\n"
        "Команды:\n"
        "/report — запустить анализ прямо сейчас\n"
        "/help — справка\n\n"
        "Автоотчёт каждый день в 20:00 🕗",
        parse_mode="Markdown"
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await cmd_start(update, context)


async def unknown(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Не знаю такой команды. Напиши /help.")


# ─────────────────────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────────────────────

def main():
    if not BOT_TOKEN:
        raise ValueError("BOT_TOKEN не задан!")

    app = Application.builder().token(BOT_TOKEN).build()

    # Обычные команды
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help",  cmd_help))

    # Report engine — должен быть ДО unknown handler
    register_report_handlers(app, report_chat_id=REPORT_CHAT_ID)

    # unknown — последним, исключаем известные команды
    app.add_handler(MessageHandler(
        filters.COMMAND & ~filters.Regex(r'^/report'),
        unknown
    ))

    print("Бот запущен...")

    if WEBHOOK_URL:
        # Webhook режим (Railway)
        app.run_webhook(
            listen="0.0.0.0",
            port=PORT,
            webhook_url=f"{WEBHOOK_URL}{WEBHOOK_PATH}",
            url_path=WEBHOOK_PATH,
            drop_pending_updates=True,
        )
    else:
        # Polling режим (локально)
        app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()

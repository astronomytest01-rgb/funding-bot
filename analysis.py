import time
import requests
from config import (
    STABILITY_THRESHOLD, MAX_OUTLIER_PCT, NEG_AVG_THRESHOLD,
    MIN_NEG_RATIO, MIN_POS_RATIO, GEMINI_API_KEY
)
from exchanges import EXCHANGE_FETCHERS

def check_recent_trend(fetcher, coin, direction, n=6):
    try:
        now_ms   = int(time.time() * 1000)
        start_ms = now_ms - 3 * 24 * 60 * 60 * 1000 
        rows, _ = fetcher(coin, start_ms, now_ms)
        if not rows: return True
        rows_sorted = sorted(rows, key=lambda x: x[0], reverse=True)
        recent = [r for _, r in rows_sorted[:n]]
        if not recent: return True
        avg_recent = sum(recent) / len(recent)
        
        if direction == "LONG":
            good_count = sum(1 for r in recent if r <= 0)
            return good_count >= (len(recent) / 2.0) and avg_recent < 0.01
        else:
            good_count = sum(1 for r in recent if r >= 0)
            return good_count >= (len(recent) / 2.0) and avg_recent > -0.01
    except Exception:
        return True

def analyze_rates(rates_pct):
    if not rates_pct: return None
    neg = [r for r in rates_pct if r < 0]
    pos = [r for r in rates_pct if r > 0]
    total = len(rates_pct)
    avg = sum(rates_pct) / total

    below_neg = sum(1 for r in rates_pct if r <= STABILITY_THRESHOLD)
    outlier_long = (total - below_neg) / total * 100
    neg_avg = sum(neg) / len(neg) if neg else 0.0
    pass_stability_long = outlier_long <= MAX_OUTLIER_PCT
    pass_neg_avg = bool(neg) and neg_avg <= NEG_AVG_THRESHOLD and (len(neg) / total) >= MIN_NEG_RATIO

    above_pos = sum(1 for r in rates_pct if r >= -STABILITY_THRESHOLD)
    outlier_short = (total - above_pos) / total * 100
    pos_avg = sum(pos) / len(pos) if pos else 0.0
    pass_stability_short = outlier_short <= MAX_OUTLIER_PCT
    pass_pos_avg = bool(pos) and pos_avg >= -NEG_AVG_THRESHOLD and (len(pos) / total) >= MIN_POS_RATIO

    if pass_stability_long: cat, dir, outl = "full", "LONG", outlier_long
    elif pass_stability_short: cat, dir, outl = "full", "SHORT", outlier_short
    elif pass_neg_avg: cat, dir, outl = "partial", "LONG", outlier_long
    elif pass_pos_avg: cat, dir, outl = "partial", "SHORT", outlier_short
    else: cat, dir, outl = "fail", ("LONG" if avg <= 0 else "SHORT"), outlier_long

    return {"total": total, "avg": avg, "neg_avg": neg_avg, "pos_avg": pos_avg, "neg_count": len(neg),
            "pos_count": len(pos), "min": min(rates_pct), "max": max(rates_pct), "outlier_pct": outl,
            "pass_stability": pass_stability_long or pass_stability_short,
            "pass_neg_avg": pass_neg_avg or pass_pos_avg, "category": cat, "direction": dir}

def analyze_coin_multi(coin, start_ms, end_ms, exchanges):
    results = {}
    for ex in exchanges:
        fetcher = EXCHANGE_FETCHERS.get(ex)
        if not fetcher: continue
        try:
            data, sym_or_err = fetcher(coin, start_ms, end_ms)
            if not data: results[ex] = {"error": sym_or_err or "Нет данных", "sym": None}
            else:
                metrics = analyze_rates([r for _, r in data])
                metrics.update({"coin": coin, "sym": sym_or_err, "exchange": ex, "error": None})
                results[ex] = metrics
        except Exception as e: results[ex] = {"error": str(e), "sym": None}
    return results

def find_best_hedge(coin, main_exchange, main_direction, start_ms, end_ms, active_exchanges):
    best_ex, best_avg, best_net = None, 0, -float('inf')
    for ex in active_exchanges:
        if ex == main_exchange: continue
        fetcher = EXCHANGE_FETCHERS.get(ex)
        if not fetcher: continue
        try:
            data, _ = fetcher(coin, start_ms, end_ms)
            if not data: continue
            clean = [r for _, r in data if abs(r) <= 0.8]
            if not clean: continue
            avg_rate = sum(clean) / len(clean)
            hedge_income = avg_rate if main_direction == "LONG" else -avg_rate
            if hedge_income > best_net:
                best_net, best_avg, best_ex = hedge_income, avg_rate, ex
        except Exception: pass
    return best_ex, best_avg

# --- GEMINI AI ---
GEMINI_BULK_PROMPT = """Действуй как риск-менеджер криптофонда. Ниже список монет, которые прошли фильтр фандинга за {days} дней.
Твоя задача — быстро отсеять опасные активы (мемкоины, сверхволатильные щиткоины) и оставить надежные (DeFi, L1/L2).
Список монет:
{coins_list}
Выведи ТОЛЬКО список монет, которые ТЫ РЕКОМЕНДУЕШЬ (формат: [ЭМОДЖИ] [МОНЕТА] — [причина 3 слова]).
Используй 🟢 для ЛОНГ и 🔴 для ШОРТ. Если монета не подходит — пропусти ее. Если ни одна не подходит — напиши "Подходящих фундаментальных монет нет"."""

GEMINI_SINGLE_PROMPT = """Действуй как риск-менеджер. Проанализируй {coin} для стратегии заработка на ставках финансирования.
Напиши без Markdown-разметки (без звездочек). 4 абзаца с эмодзи:
✅ Вердикт: от 1 до 4 слов.
📊 Волатильность и Риски: риск сквизов.
💰 Фундаментал: что за проект.
⚖️ Риск-менеджмент: плечо и стопы."""

def gemini_analyze_bulk(coins_list_text, days):
    if not GEMINI_API_KEY: return None
    payload = {"contents": [{"parts": [{"text": GEMINI_BULK_PROMPT.format(coins_list=coins_list_text, days=days)}]}], "generationConfig": {"temperature": 0.2}}
    for attempt in range(3):
        try:
            r = requests.post(f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={GEMINI_API_KEY}", json=payload, timeout=30)
            if r.status_code == 429: time.sleep(15 * (attempt + 1)); continue
            return r.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
        except Exception: time.sleep(5)
    return None

def gemini_analyze_single(coin):
    if not GEMINI_API_KEY: return None
    payload = {"contents": [{"parts": [{"text": GEMINI_SINGLE_PROMPT.format(coin=coin)}]}], "generationConfig": {"temperature": 0.4}}
    for attempt in range(3):
        try:
            r = requests.post(f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={GEMINI_API_KEY}", json=payload, timeout=30)
            if r.status_code == 429: time.sleep(15 * (attempt + 1)); continue
            return r.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
        except Exception: time.sleep(5)
    return None
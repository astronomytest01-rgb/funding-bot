import time

from config import (
    EXCHANGES_ENABLED,
    MAX_OUTLIER_PCT,
    MIN_NEG_RATIO,
    MIN_POS_RATIO,
    NEG_AVG_THRESHOLD,
    RECENT_TREND_MIN_GOOD_RATIO,
    RECENT_TREND_RATES,
    STABILITY_THRESHOLD,
)
from exchanges import EXCHANGE_FETCHERS, EXCHANGE_LABELS

def get_active_exchanges(requested=None):
    """Возвращает список активных бирж из финального набора."""
    if requested and requested != "all":
        exs = [e.strip().lower() for e in requested.split(",")]
        return [e for e in exs if e in EXCHANGES_ENABLED and e in EXCHANGE_FETCHERS]
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

        rates = [r for _, r in sorted(data, key=lambda x: x[0])]
        metrics = analyze_rates(rates)
        if metrics and metrics["category"] != "fail":
            metrics["trend_ok"] = recent_trend_ok(rates, metrics["direction"])
            metrics["trend_label"] = recent_trend_label(rates, metrics["direction"])
            if not metrics["trend_ok"]:
                metrics["category"] = "fail"
                metrics["pass_stability"] = False
                metrics["pass_neg_avg"] = False
        metrics["coin"] = coin
        metrics["sym"] = sym_or_err
        metrics["exchange"] = ex
        metrics["error"] = None
        results[ex] = metrics
    return results



def recent_trend_ok(rates_pct, direction, n=RECENT_TREND_RATES):
    """Return True when at least half of the latest n rates still support the trade.

    LONG is still active when at least half of recent rates are negative.
    SHORT is still active when at least half of recent rates are positive.
    If fewer than n rates exist, use whatever is available instead of failing closed.
    """
    if not rates_pct:
        return False
    recent = list(rates_pct)[-n:]
    if not recent:
        return False
    if direction == "LONG":
        good = sum(1 for rate in recent if rate < 0)
    else:
        good = sum(1 for rate in recent if rate > 0)
    return good >= max(1, len(recent) * RECENT_TREND_MIN_GOOD_RATIO)


def recent_trend_label(rates_pct, direction, n=RECENT_TREND_RATES):
    if not rates_pct:
        return "нет последних ставок"
    recent = list(rates_pct)[-n:]
    if direction == "LONG":
        good = sum(1 for rate in recent if rate < 0)
    else:
        good = sum(1 for rate in recent if rate > 0)
    return f"{good}/{len(recent)} актуальных"


def calc_std(rates):
    """\u0421\u0442\u0430\u043d\u0434\u0430\u0440\u0442\u043d\u043e\u0435 \u043e\u0442\u043a\u043b\u043e\u043d\u0435\u043d\u0438\u0435 \u0441\u043f\u0438\u0441\u043a\u0430 \u0441\u0442\u0430\u0432\u043e\u043a."""
    if not rates:
        return 0.0
    n = len(rates)
    mean = sum(rates) / n
    return (sum((r - mean) ** 2 for r in rates) / n) ** 0.5



def analyze_delta(coin, days, long_exchanges=None, all_exchanges=None):
    """
    \u0418\u0449\u0435\u0442 \u043b\u0443\u0447\u0448\u0443\u044e \u0434\u0435\u043b\u044c\u0442\u0430-\u043d\u0435\u0439\u0442\u0440\u0430\u043b\u044c\u043d\u0443\u044e \u0441\u0432\u044f\u0437\u043a\u0443 \u0434\u043b\u044f \u043c\u043e\u043d\u0435\u0442\u044b.

    \u041b\u043e\u0433\u0438\u043a\u0430:
    1. \u041f\u0440\u043e\u0432\u0435\u0440\u044f\u0435\u043c \u043a\u0430\u0436\u0434\u0443\u044e \u0431\u0438\u0440\u0436\u0443 \u043a\u0430\u043a \u043a\u0430\u043d\u0434\u0438\u0434\u0430\u0442\u0430 \u0434\u043b\u044f \u041b\u041e\u041d\u0413\u0410:
       \u043c\u043e\u043d\u0435\u0442\u0430 \u0434\u043e\u043b\u0436\u043d\u0430 \u043f\u0440\u043e\u0445\u043e\u0434\u0438\u0442\u044c \u0444\u0438\u043b\u044c\u0442\u0440\u044b (\u0441\u0442\u0430\u0431\u0438\u043b\u044c\u043d\u043e \u043e\u0442\u0440\u0438\u0446. \u0444\u0430\u043d\u0434\u0438\u043d\u0433).
    2. \u0414\u043b\u044f \u043a\u0430\u0436\u0434\u043e\u0433\u043e \u043b\u043e\u043d\u0433\u0430 \u043f\u0435\u0440\u0435\u0431\u0438\u0440\u0430\u0435\u043c \u043e\u0441\u0442\u0430\u043b\u044c\u043d\u044b\u0435 \u0431\u0438\u0440\u0436\u0438 \u043a\u0430\u043a \u043a\u0430\u043d\u0434\u0438\u0434\u0430\u0442\u044b \u0434\u043b\u044f \u0428\u041e\u0420\u0422\u0410:
       - \u0421\u0447\u0438\u0442\u0430\u0435\u043c \u0441\u0440\u0435\u0434\u043d\u0438\u0439 \u0444\u0430\u043d\u0434\u0438\u043d\u0433 \u0448\u043e\u0440\u0442\u0430 \u0438 \u0435\u0433\u043e \u0441\u0442\u0430\u0431\u0438\u043b\u044c\u043d\u043e\u0441\u0442\u044c (std)
       - \u0427\u0438\u0441\u0442\u044b\u0439 \u0434\u043e\u0445\u043e\u0434 = avg_long_rate + avg_short_rate (\u043e\u0431\u0430 \u0432 %)
         (long_rate \u043e\u0442\u0440\u0438\u0446\u0430\u0442\u0435\u043b\u044c\u043d\u044b\u0439 \u2192 \u043c\u044b \u043f\u043e\u043b\u0443\u0447\u0430\u0435\u043c; short_rate \u043f\u043e\u043b\u043e\u0436\u0438\u0442 \u2192 \u043f\u043e\u043b\u0443\u0447\u0430\u0435\u043c, \u043e\u0442\u0440\u0438\u0446 \u2192 \u043f\u043b\u0430\u0442\u0438\u043c)
       - \u0421\u0442\u0430\u0431\u0438\u043b\u044c\u043d\u043e\u0441\u0442\u044c \u0441\u0432\u044f\u0437\u043a\u0438 = std_long + std_short (\u043c\u0435\u043d\u044c\u0448\u0435 = \u043b\u0443\u0447\u0448\u0435)
    3. \u0421\u043e\u0440\u0442\u0438\u0440\u0443\u0435\u043c: \u0441\u043d\u0430\u0447\u0430\u043b\u0430 \u043f\u043e \u0447\u0438\u0441\u0442\u043e\u043c\u0443 \u0434\u043e\u0445\u043e\u0434\u0443 (\u0431\u043e\u043b\u044c\u0448\u0435 = \u043b\u0443\u0447\u0448\u0435),
       \u043f\u0440\u0438 \u0440\u0430\u0432\u0435\u043d\u0441\u0442\u0432\u0435 \u2014 \u043f\u043e \u0441\u0442\u0430\u0431\u0438\u043b\u044c\u043d\u043e\u0441\u0442\u0438 (\u043c\u0435\u043d\u044c\u0448\u0435 std = \u043b\u0443\u0447\u0448\u0435).
    """
    now_ms = int(time.time() * 1000)
    start_ms = now_ms - days * 24 * 60 * 60 * 1000

    if all_exchanges is None:
        all_exchanges = [e for e, on in EXCHANGES_ENABLED.items() if on]
    if long_exchanges is None:
        long_exchanges = all_exchanges

    # \u0421\u043e\u0431\u0438\u0440\u0430\u0435\u043c \u0434\u0430\u043d\u043d\u044b\u0435 \u043f\u043e \u0432\u0441\u0435\u043c \u0431\u0438\u0440\u0436\u0430\u043c
    exchange_data = {}
    for ex in all_exchanges:
        fetcher = EXCHANGE_FETCHERS.get(ex)
        if not fetcher:
            continue
        try:
            data, sym = fetcher(coin, start_ms, now_ms)
            if data:
                rates = [r for _, r in data]
                exchange_data[ex] = {"rates": rates, "sym": sym}
        except Exception:
            pass
        time.sleep(0.1)

    # \u0418\u0449\u0435\u043c \u043b\u043e\u043d\u0433-\u043a\u0430\u043d\u0434\u0438\u0434\u0430\u0442\u043e\u0432 (\u043f\u0440\u043e\u0445\u043e\u0434\u044f\u0442 \u043d\u0430\u0448\u0438 \u0444\u0438\u043b\u044c\u0442\u0440\u044b)
    long_candidates = []
    for ex in long_exchanges:
        if ex not in exchange_data:
            continue
        rates = exchange_data[ex]["rates"]
        neg = [r for r in rates if r < 0]
        total = len(rates)
        below = sum(1 for r in rates if r <= STABILITY_THRESHOLD)
        outlier_pct = (total - below) / total * 100 if total else 100
        neg_avg = sum(neg) / len(neg) if neg else 0.0
        pass_stability = outlier_pct <= MAX_OUTLIER_PCT
        pass_neg_avg = bool(neg) and neg_avg <= NEG_AVG_THRESHOLD

        if pass_stability or pass_neg_avg:
            long_candidates.append({
                "exchange": ex,
                "rates": rates,
                "avg": sum(rates) / total if total else 0,
                "neg_avg": neg_avg,
                "std": calc_std(rates),
                "outlier_pct": outlier_pct,
                "pass_stability": pass_stability,
                "pass_neg_avg": pass_neg_avg,
            })

    if not long_candidates:
        return None, exchange_data

    # \u0414\u043b\u044f \u043a\u0430\u0436\u0434\u043e\u0433\u043e \u043b\u043e\u043d\u0433\u0430 \u0438\u0449\u0435\u043c \u043b\u0443\u0447\u0448\u0438\u0439 \u0448\u043e\u0440\u0442
    best_pairs = []

    for long_info in long_candidates:
        long_ex = long_info["exchange"]
        long_avg = long_info["avg"]  # \u043e\u0442\u0440\u0438\u0446\u0430\u0442\u0435\u043b\u044c\u043d\u044b\u0439 \u2192 \u043c\u044b \u041f\u041e\u041b\u0423\u0427\u0410\u0415\u041c abs(avg)

        short_candidates = []
        for ex in all_exchanges:
            if ex == long_ex:
                continue
            if ex not in exchange_data:
                continue
            rates = exchange_data[ex]["rates"]
            total = len(rates)
            if not total:
                continue
            avg = sum(rates) / total
            std = calc_std(rates)

            # \u0427\u0438\u0441\u0442\u044b\u0439 \u0434\u043e\u0445\u043e\u0434 \u043d\u0430 \u0448\u043e\u0440\u0442\u0435:
            # \u0435\u0441\u043b\u0438 avg > 0 (\u043f\u043e\u043b\u043e\u0436\u0438\u0442\u0435\u043b\u044c\u043d\u044b\u0439 \u0444\u0430\u043d\u0434\u0438\u043d\u0433) \u2192 \u0448\u043e\u0440\u0442 \u041f\u041e\u041b\u0423\u0427\u0410\u0415\u0422 avg
            # \u0435\u0441\u043b\u0438 avg < 0 (\u043e\u0442\u0440\u0438\u0446\u0430\u0442\u0435\u043b\u044c\u043d\u044b\u0439 \u0444\u0430\u043d\u0434\u0438\u043d\u0433) \u2192 \u0448\u043e\u0440\u0442 \u041f\u041b\u0410\u0422\u0418\u0422 abs(avg)
            # \u0418\u0442\u043e\u0433\u043e \u0437\u0430 \u043f\u0435\u0440\u0438\u043e\u0434: long \u043f\u043e\u043b\u0443\u0447\u0430\u0435\u0442 abs(long_avg), \u0448\u043e\u0440\u0442 \u043f\u043e\u043b\u0443\u0447\u0430\u0435\u0442 avg_short
            # net_income_pct = abs(long_avg) + avg_short
            net_income_pct = abs(long_avg) + avg  # avg \u0448\u043e\u0440\u0442\u0430: + = \u0445\u043e\u0440\u043e\u0448\u043e, - = \u043f\u043b\u043e\u0445\u043e

            short_candidates.append({
                "exchange": ex,
                "avg": avg,
                "std": std,
                "net_income_pct": net_income_pct,
                "rates": rates,
            })

        # \u0421\u043e\u0440\u0442\u0438\u0440\u0443\u0435\u043c \u0448\u043e\u0440\u0442-\u043a\u0430\u043d\u0434\u0438\u0434\u0430\u0442\u043e\u0432: \u0431\u043e\u043b\u044c\u0448\u0438\u0439 net_income, \u0437\u0430\u0442\u0435\u043c \u043c\u0435\u043d\u044c\u0448\u0438\u0439 std
        short_candidates.sort(key=lambda x: (-x["net_income_pct"], x["std"]))

        for short_info in short_candidates[:3]:  # \u0442\u043e\u043f-3 \u0432\u0430\u0440\u0438\u0430\u043d\u0442\u0430 \u0448\u043e\u0440\u0442\u0430
            best_pairs.append({
                "long_ex": long_ex,
                "short_ex": short_info["exchange"],
                "long_avg": long_avg,
                "long_std": long_info["std"],
                "long_neg_avg": long_info["neg_avg"],
                "long_outlier_pct": long_info["outlier_pct"],
                "short_avg": short_info["avg"],
                "short_std": short_info["std"],
                "net_income_pct": short_info["net_income_pct"],
                "pass_stability": long_info["pass_stability"],
                "pass_neg_avg": long_info["pass_neg_avg"],
            })

    # \u0424\u0438\u043d\u0430\u043b\u044c\u043d\u0430\u044f \u0441\u043e\u0440\u0442\u0438\u0440\u043e\u0432\u043a\u0430 \u0432\u0441\u0435\u0445 \u043f\u0430\u0440
    best_pairs.sort(key=lambda x: (-x["net_income_pct"], x["long_std"] + x["short_std"]))
    return best_pairs, exchange_data



def fmt_delta_result(coin, pairs, days, amount_usd=None):
    """\u0424\u043e\u0440\u043c\u0430\u0442\u0438\u0440\u0443\u0435\u0442 \u0440\u0435\u0437\u0443\u043b\u044c\u0442\u0430\u0442 \u0434\u0435\u043b\u044c\u0442\u0430-\u0430\u043d\u0430\u043b\u0438\u0437\u0430."""
    if not pairs:
        return f"\u26a0\ufe0f *{coin}* \u2014 \u043d\u0435 \u043d\u0430\u0439\u0434\u0435\u043d\u043e \u043f\u043e\u0434\u0445\u043e\u0434\u044f\u0449\u0438\u0445 \u0431\u0438\u0440\u0436 \u0434\u043b\u044f \u043b\u043e\u043d\u0433\u0430 \u0437\u0430 {days} \u0434\u043d\u0435\u0439"

    lines = [f"\u2696\ufe0f *\u0414\u0435\u043b\u044c\u0442\u0430-\u043d\u0435\u0439\u0442\u0440\u0430\u043b\u044c: {coin}* \u2014 {days} \u0434\u043d\u0435\u0439\
"]

    # \u041f\u043e\u043a\u0430\u0437\u044b\u0432\u0430\u0435\u043c \u0442\u043e\u043f-3 \u043f\u0430\u0440\u044b
    for i, p in enumerate(pairs[:3], 1):
        long_label = EXCHANGE_LABELS.get(p["long_ex"], p["long_ex"].upper())
        short_label = EXCHANGE_LABELS.get(p["short_ex"], p["short_ex"].upper())

        # \u0418\u043a\u043e\u043d\u043a\u0430 \u0441\u0442\u0430\u0431\u0438\u043b\u044c\u043d\u043e\u0441\u0442\u0438 \u043b\u043e\u043d\u0433\u0430
        long_cat = "\u2705" if p["pass_stability"] else "\u26a1"

        # \u0417\u043d\u0430\u043a \u0434\u043b\u044f \u0448\u043e\u0440\u0442\u0430: + \u0435\u0441\u043b\u0438 \u0437\u0430\u0440\u0430\u0431\u0430\u0442\u044b\u0432\u0430\u0435\u043c \u043d\u0430 \u0448\u043e\u0440\u0442\u0435, - \u0435\u0441\u043b\u0438 \u043f\u043b\u0430\u0442\u0438\u043c
        short_sign = "+" if p["short_avg"] >= 0 else ""

        lines.append(f"*#{i}* \ud83d\udfe2 \u041b\u043e\u043d\u0433 `{long_label}` + \ud83d\udd34 \u0428\u043e\u0440\u0442 `{short_label}`")
        lines.append(
            f"  \u041b\u043e\u043d\u0433 {long_cat}: avg `{p['long_avg']:+.4f}%`  std `{p['long_std']:.4f}`"
        )
        lines.append(
            f"  \u0428\u043e\u0440\u0442: avg `{p['short_avg']:+.4f}%`  std `{p['short_std']:.4f}`"
        )
        lines.append(
            f"  \ud83d\udcc8 \u0427\u0438\u0441\u0442\u044b\u0439 \u0434\u043e\u0445\u043e\u0434/\u0441\u0442\u0430\u0432\u043a\u0443: `{p['net_income_pct']:+.4f}%`"
        )

        if amount_usd:
            # \u0421\u0447\u0438\u0442\u0430\u0435\u043c \u0440\u0435\u0430\u043b\u044c\u043d\u044b\u0439 \u0434\u043e\u0445\u043e\u0434 \u0437\u0430 \u043f\u0435\u0440\u0438\u043e\u0434
            # \u041a\u043e\u043b\u0438\u0447\u0435\u0441\u0442\u0432\u043e \u0441\u0442\u0430\u0432\u043e\u043a \u043f\u0440\u0438\u043c\u0435\u0440\u043d\u043e = days * 24 / interval_h (\u043e\u0431\u044b\u0447\u043d\u043e 8\u0447 = 3 \u0432 \u0434\u0435\u043d\u044c)
            approx_payments = days * 3
            income_long = amount_usd * abs(p["long_avg"] / 100) * approx_payments
            income_short = amount_usd * (p["short_avg"] / 100) * approx_payments
            net = income_long + income_short
            lines.append(
                f"  \ud83d\udcb0 ~${income_long:.2f} (\u043b\u043e\u043d\u0433) {'+' if income_short >= 0 else ''}"
                f"${income_short:.2f} (\u0448\u043e\u0440\u0442) = *${net:.2f}* \u0437\u0430 {days}\u0434"
            )
        lines.append("")

    return "\
".join(lines)


# \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500
# DELTA CONVERSATION HANDLERS
# \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500

"""
test_analysis.py — локальный прогон полного анализа без Telegram.

Запуск:
    # Без CoinW (нет Supabase локально):
    python3 test_analysis.py

    # С CoinW (если есть .env или знаешь ключи):
    SUPABASE_URL=https://xxx.supabase.co SUPABASE_KEY=eyJ... python3 test_analysis.py

Что делает:
    Прогоняет фазы 1-4 отчёта по нескольким монетам и выводит результат в терминал.
    Не требует запуска бота и Telegram.
"""

import os, sys, time

# Supabase из окружения (опционально — для CoinW)
SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "")

# Подставляем в bot.py до импорта
if SUPABASE_URL:
    os.environ["SUPABASE_URL"] = SUPABASE_URL
if SUPABASE_KEY:
    os.environ["SUPABASE_KEY"] = SUPABASE_KEY

GREEN  = "\033[92m"; RED = "\033[91m"; YELLOW = "\033[93m"
CYAN   = "\033[96m"; BOLD = "\033[1m"; RESET = "\033[0m"

def ok(msg):    print(f"  {GREEN}✅ {msg}{RESET}")
def err(msg):   print(f"  {RED}❌ {msg}{RESET}")
def warn(msg):  print(f"  {YELLOW}⚠️  {msg}{RESET}")
def info(msg):  print(f"  {CYAN}ℹ️  {msg}{RESET}")
def section(t): print(f"\n{BOLD}{'─'*52}\n{t}\n{'─'*52}{RESET}")

# ── Импорт ───────────────────────────────────────────────────────────────────
section("ИМПОРТ")
try:
    import bot as _bot
    import report_engine as re
    fetchers      = _bot.EXCHANGE_FETCHERS
    labels        = _bot.EXCHANGE_LABELS
    analyze_rates = _bot.analyze_rates
    ok("bot.py и report_engine импортированы")
except Exception as e:
    err(f"Импорт: {e}"); sys.exit(1)

# ── Параметры ─────────────────────────────────────────────────────────────────
DAYS     = 7
now_ms   = int(time.time() * 1000)
start_ms = now_ms - DAYS * 24 * 60 * 60 * 1000

# Тестируем на небольшом наборе монет которые часто дают хороший фандинг
TEST_COINS = ["ENJ", "DOGS", "RON", "PENGU", "VIRTUAL", "PEPE", "WIF", "BOME", "POPCAT"]

EXCHANGES = re.REPORT_EXCHANGES
print(f"\nБиржи: {', '.join(labels.get(e,e) for e in EXCHANGES)}")
print(f"Монеты: {', '.join(TEST_COINS)}")
print(f"Период: {DAYS} дней\n")

# ─────────────────────────────────────────────────────────────────────────────
section("ФАЗА 1: Скан фандинга")
# ─────────────────────────────────────────────────────────────────────────────

candidates = []

for exchange in EXCHANGES:
    fetcher = fetchers.get(exchange)
    if not fetcher:
        warn(f"{labels.get(exchange, exchange)}: нет fetcher"); continue

    label = labels.get(exchange, exchange)
    print(f"\n  {label}:")

    for coin in TEST_COINS:
        try:
            rows, sym = fetcher(coin, start_ms, now_ms)
        except Exception as e:
            print(f"    {coin}: ошибка — {e}"); continue

        time.sleep(0.2)

        if not rows:
            print(f"    {coin}: нет данных"); continue

        rates  = [r for _, r in rows]
        result = analyze_rates(rates)

        if not result or result["category"] == "fail":
            print(f"    {coin}: [{result['category'] if result else 'None'}] avg={sum(rates)/len(rates):+.4f}%")
            continue

        direction = result["direction"]
        avg_rate  = result["neg_avg"] if direction == "LONG" else result["pos_avg"]
        category  = result["category"]

        # Фильтр 1: Актуальность — последние 6 ставок (~2 дня)
        RECENT_N     = 6
        RECENT_RATIO = 0.60
        recent = rates[-RECENT_N:] if len(rates) >= RECENT_N else rates
        if direction == "LONG":
            recent_ok = sum(1 for r in recent if r < 0) / len(recent)
        else:
            recent_ok = sum(1 for r in recent if r > 0) / len(recent)
        if recent_ok < RECENT_RATIO:
            print(f"    {coin}: ⚠️ фандинг развернулся — recent={recent_ok:.0%} (нужно ≥{RECENT_RATIO:.0%})")
            continue

        # Фильтр 2: Доход — минимум $25/день на $20,000 позиции
        INCOME_POS = 20_000
        INCOME_MIN = 25.0
        daily_income = INCOME_POS * abs(avg_rate) / 100 * 3
        if daily_income < INCOME_MIN:
            print(f"    {coin}: ⚠️ доход мал — ${daily_income:.1f}/день (нужно ≥${INCOME_MIN})")
            continue

        icon     = "🟢" if direction == "LONG" else "🔴"
        cat_icon = "✅" if category == "full" else "⚡"
        print(f"    {icon} {cat_icon} {coin}: {direction} avg={avg_rate:+.4f}% "
              f"recent={recent_ok:.0%} доход=${daily_income:.1f}/д [{category}] ({len(rows)} ставок)")

        candidates.append({
            "coin":         coin,
            "exchange":     exchange,
            "direction":    direction,
            "avg_rate":     avg_rate,
            "category":     category,
            "total_rates":  len(rows),
            "recent_ratio": recent_ok,
            "daily_income": daily_income,
        })

print(f"\n  → Найдено кандидатов: {len(candidates)}")

if not candidates:
    print(f"\n{YELLOW}Кандидатов нет. Попробуй другие монеты.{RESET}")
    sys.exit(0)

# ─────────────────────────────────────────────────────────────────────────────
section("ФАЗА 2: Объём торгов")
# ─────────────────────────────────────────────────────────────────────────────

vol_passed = []
for c in candidates:
    vol = re.get_volume_24h(c["coin"], c["exchange"])
    c["volume"] = vol
    label = labels.get(c["exchange"], c["exchange"])
    vol_str = "bypass" if vol >= 999_999_990 else f"${vol/1_000_000:.2f}M"
    status = "✅" if vol >= re.MIN_VOLUME_USDT else "❌"
    print(f"  {status} {c['coin']} @ {label}: {vol_str}")
    if vol >= re.MIN_VOLUME_USDT:
        vol_passed.append(c)
    time.sleep(0.15)

print(f"\n  → После фильтра объёма: {len(vol_passed)}")

# ─────────────────────────────────────────────────────────────────────────────
section("ФАЗА 3: Поиск хедж-биржи")
# ─────────────────────────────────────────────────────────────────────────────

hedge_passed = []

for c in vol_passed:
    coin     = c["coin"]
    main_ex  = c["exchange"]
    main_lbl = labels.get(main_ex, main_ex)
    print(f"\n  {coin} @ {main_lbl} ({c['direction']}):")

    best_hedge    = None
    best_rate_abs = 999

    for hedge_ex in EXCHANGES:
        if hedge_ex == main_ex: continue
        fetcher = fetchers.get(hedge_ex)
        if not fetcher: continue
        try:
            rows, _ = fetcher(coin, start_ms, now_ms)
        except Exception:
            rows = []
        time.sleep(0.2)
        if not rows: continue
        rates = [r for _, r in rows]
        avg   = sum(rates) / len(rates)
        hedge_lbl = labels.get(hedge_ex, hedge_ex)
        print(f"    {hedge_lbl}: avg={avg:+.4f}%")
        if abs(avg) < best_rate_abs:
            best_rate_abs = abs(avg)
            best_hedge = {"exchange": hedge_ex, "avg_rate": avg}

    if not best_hedge:
        warn(f"    нет хедж-биржи"); continue

    warning = best_rate_abs > re.HEDGE_RATE_THRESHOLD
    best_hedge["warning"] = warning
    hedge_lbl = labels.get(best_hedge["exchange"], best_hedge["exchange"])
    flag = "⚠️ ставка высокая" if warning else "✅"
    print(f"    → Лучший хедж: {hedge_lbl} avg={best_hedge['avg_rate']:+.4f}% {flag}")

    # Объём на хедже
    hedge_vol = re.get_volume_24h(coin, best_hedge["exchange"])
    best_hedge["volume"] = hedge_vol
    vol_str = "bypass" if hedge_vol >= 999_999_990 else f"${hedge_vol/1_000_000:.2f}M"
    if hedge_vol < re.MIN_VOLUME_USDT:
        warn(f"    объём на хедже мал: {vol_str}"); continue
    print(f"    объём хедж: {vol_str} ✅")

    c["hedge"] = best_hedge
    hedge_passed.append(c)

print(f"\n  → После поиска хеджа: {len(hedge_passed)}")

# ─────────────────────────────────────────────────────────────────────────────
section("ФАЗА 4: Спред входа")
# ─────────────────────────────────────────────────────────────────────────────

spread_passed = []

for c in hedge_passed:
    coin      = c["coin"]
    main_ex   = c["exchange"]
    hedge_ex  = c["hedge"]["exchange"]
    direction = c["direction"]
    main_lbl  = labels.get(main_ex, main_ex)
    hedge_lbl = labels.get(hedge_ex, hedge_ex)

    long_ex  = main_ex  if direction == "LONG" else hedge_ex
    short_ex = hedge_ex if direction == "LONG" else main_ex

    spread = re.calc_spread(long_ex, short_ex, coin)
    c["spread"] = spread
    time.sleep(0.3)

    if spread is None:
        warn(f"  {coin}: спред не получен (нет orderbook) — включаем с пометкой")
        c["spread_warning"] = True
        spread_passed.append(c)
    elif spread >= re.MAX_SPREAD_PCT:
        ok(f"  {coin} {main_lbl}↔{hedge_lbl}: спред={spread:+.4f}% ✅")
        spread_passed.append(c)
    else:
        err(f"  {coin}: спред={spread:+.4f}% — слишком плохой (порог {re.MAX_SPREAD_PCT}%)")

print(f"\n  → После фильтра спреда: {len(spread_passed)}")

# ─────────────────────────────────────────────────────────────────────────────
section("ФИНАЛЬНЫЙ ОТЧЁТ")
# ─────────────────────────────────────────────────────────────────────────────

if not spread_passed:
    print(f"\n{YELLOW}Итоговых кандидатов нет.{RESET}")
    sys.exit(0)

print()
for i, c in enumerate(spread_passed, 1):
    coin      = c["coin"]
    main_lbl  = labels.get(c["exchange"], c["exchange"])
    hedge_lbl = labels.get(c["hedge"]["exchange"], c["hedge"]["exchange"])
    direction = c["direction"]
    avg_rate  = c["avg_rate"]
    hedge_rate = c["hedge"]["avg_rate"]
    vol_main  = c["volume"]
    vol_hedge = c["hedge"]["volume"]
    spread    = c.get("spread")
    category  = c["category"]

    dir_icon  = "🟢" if direction == "LONG" else "🔴"
    cat_icon  = "✅" if category == "full" else "⚡"
    dir_text  = "ЛОНГ" if direction == "LONG" else "ШОРТ"

    payments_per_day = 3
    daily_rate_pct   = abs(avg_rate) * payments_per_day

    spread_str = f"{spread:+.4f}%" if spread is not None else "нет данных"
    vol_m = "bypass" if vol_main  >= 999_999_990 else f"${vol_main/1_000_000:.2f}M"
    vol_h = "bypass" if vol_hedge >= 999_999_990 else f"${vol_hedge/1_000_000:.2f}M"

    print(f"{'─'*50}")
    print(f"{i}. {dir_icon} {BOLD}{coin}{RESET} — {dir_text} на {main_lbl} {cat_icon}")
    print(f"   Хедж: {hedge_lbl}")
    print(f"   Фандинг {main_lbl}: {avg_rate:+.4f}%/выплата")
    print(f"   Фандинг {hedge_lbl}: {hedge_rate:+.4f}%/выплата")
    print(f"   Объём: {main_lbl}={vol_m}  {hedge_lbl}={vol_h}")
    print(f"   Спред: {spread_str}")
    print(f"   {'─'*30}")
    print(f"   💰 Доход ($5k маржа):")
    for lev in [3, 4]:
        pos     = 5_000 * lev
        daily   = pos * daily_rate_pct / 100
        weekly  = daily * 7
        print(f"      x{lev} (${pos:,}): ${daily:.1f}/день  ${weekly:.0f}/нед")
    print()

print(f"{BOLD}Итого монет в отчёте: {len(spread_passed)}{RESET}")

# ─────────────────────────────────────────────────────────────────────────────
section("ФАЗА 5: Gemini AI анализ")
# ─────────────────────────────────────────────────────────────────────────────

import time as _time

GEMINI_KEY = os.environ.get("GEMINI_API_KEY", "")
if not GEMINI_KEY:
    warn("GEMINI_API_KEY не задан — пропускаем AI анализ")
    warn("Запусти: export GEMINI_API_KEY=ваш_ключ && python3 test_analysis.py")
else:
    import re as _re_engine
    ai_passed = []
    for c in spread_passed:
        coin      = c["coin"]
        main_lbl  = labels.get(c["exchange"], c["exchange"])
        hedge_lbl = labels.get(c["hedge"]["exchange"], c["hedge"]["exchange"])

        print(f"\n  Анализирую {coin}...")
        analysis = re.gemini_analyze(
            coin       = coin,
            direction  = c["direction"],
            avg_rate   = c["avg_rate"],
            main_ex    = main_lbl,
            hedge_ex   = hedge_lbl,
            vol_main   = c.get("volume", 0),
            vol_hedge  = c["hedge"].get("volume", 0),
            days_stable = c.get("days_stable", 0),
        )
        c["ai"] = analysis
        _time.sleep(5)   # Gemini free tier: 15 req/min

        if analysis["approved"]:
            rec = f"x{analysis['leverage']}" if analysis["leverage"] else "—"
            print(f"  {GREEN}✅ {coin}: ОДОБРЕНО | риск={analysis['risk']} | плечо={rec}{RESET}")
            print(f"     {analysis['summary']}")
            ai_passed.append(c)
        else:
            print(f"  {RED}❌ {coin}: ОТКЛОНЕНО — {analysis['reason']}{RESET}")

    print(f"\n  → После AI фильтра: {len(ai_passed)} из {len(spread_passed)}")

    if ai_passed:
        section("ФИНАЛЬНЫЙ ОТЧЁТ (с AI)")
        for i, c in enumerate(ai_passed, 1):
            coin      = c["coin"]
            main_lbl  = labels.get(c["exchange"], c["exchange"])
            hedge_lbl = labels.get(c["hedge"]["exchange"], c["hedge"]["exchange"])
            direction = c["direction"]
            avg_rate  = c["avg_rate"]
            ai        = c["ai"]
            rec_lev   = ai.get("leverage") or 3
            payments_per_day = 3
            daily_rate_pct   = abs(avg_rate) * payments_per_day
            dir_icon  = "🟢" if direction == "LONG" else "🔴"
            dir_text  = "ЛОНГ" if direction == "LONG" else "ШОРТ"
            risk_emoji = {"низкий": "🟢", "средний": "🟡", "высокий": "🔴"}.get(ai["risk"], "⚪")

            print(f"\n{'─'*50}")
            print(f"{i}. {dir_icon} {BOLD}{coin}{RESET} — {dir_text} на {main_lbl}")
            print(f"   Хедж: {hedge_lbl}")
            print(f"   {risk_emoji} Риск: {ai['risk']} | Рекомендуемое плечо: x{rec_lev}")
            print(f"   📝 {ai['summary']}")
            print(f"   💰 Доход ($5k маржа):")
            for lev in [2, 3, 4]:
                pos    = 5_000 * lev
                daily  = pos * daily_rate_pct / 100
                weekly = daily * 7
                mark   = " 👈 рекомендовано" if lev == rec_lev else ""
                print(f"      x{lev} (${pos:,}): ${daily:.1f}/день  ${weekly:.0f}/нед{mark}")


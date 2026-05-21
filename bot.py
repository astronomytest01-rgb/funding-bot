import os
import time
import requests
from datetime import datetime, timezone
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    filters, ContextTypes, ConversationHandler, CallbackQueryHandler
)

from config import (
    BOT_TOKEN, REPORT_CHAT_ID, EXCHANGES_ENABLED, EXCHANGE_LABELS,
    DEFAULT_DAYS, STABILITY_THRESHOLD, MAX_OUTLIER_PCT, NEG_AVG_THRESHOLD,
    MIN_NEG_RATIO, MIN_POS_RATIO, AN_ANOMALY_THRESHOLD,
    AUTO_SCAN_AMOUNT, AUTO_SCAN_THRESHOLD, AUTO_SCAN_DAYS, AUTO_SCAN_EXCHANGES, GEMINI_API_KEY
)
from exchanges import EXCHANGE_FETCHERS, phemex_get_all_symbols
from analysis import (
    analyze_rates, analyze_coin_multi, check_recent_trend, 
    find_best_hedge, gemini_analyze_single, gemini_analyze_bulk
)

# ── Состояния ──────────────────────────────────────────────────────────
WAIT_AI_COIN   = 10
ACF_COIN, ACF_DAYS, ACF_DAYS_NUM, ACF_EXCH = 20, 21, 22, 23
FR_COIN, FR_DAYS, FR_DAYS_NUM, FR_EXCH = 30, 31, 32, 33
PC_COIN, PC_AMT, PC_AMT_NUM, PC_DAYS, PC_DAYS_NUM, PC_EXCH = 40, 41, 42, 43, 44, 45
AN_METHOD, AN_AMT, AN_AMT_NUM, AN_THRESH, AN_THRESH_NUM, AN_DAYS, AN_DAYS_NUM = 50, 51, 52, 53, 54, 55, 56

_scan_running = {}
SCAN_BATCH = 20

# ── Хелперы ────────────────────────────────────────────────────────────
def get_active_exchanges(requested=None):
    if requested and requested != "all":
        exs = [e.strip().lower() for e in requested.split(",")]
        return [e for e in exs if e in EXCHANGE_FETCHERS]
    return [e for e, enabled in EXCHANGES_ENABLED.items() if enabled]

def parse_tokens(text):
    parts = text.strip().split()
    days, exchange, coins, i = DEFAULT_DAYS, None, [], 0
    KNOWN = set(EXCHANGE_FETCHERS.keys())
    while i < len(parts):
        p = parts[i].lower()
        if p in ("/days", "--days") and i + 1 < len(parts):
            try: days = int(parts[i + 1]); i += 2; continue
            except: pass
        if p in ("/exchange", "--exchange") and i + 1 < len(parts):
            exchange = parts[i + 1].lower(); i += 2; continue
        if p.startswith("/"): i += 1; continue
        if p in KNOWN: exchange = p; i += 1; continue
        try: days = int(parts[i]); i += 1; continue
        except: pass
        coins.append(parts[i].upper())
        i += 1
    return coins, days, exchange

def fmt_coin_line(coin, ex_results, active_exchanges):
    cats_dirs = [(r.get("category"), r.get("direction", "LONG")) for r in ex_results.values() if not r.get("error")]
    overall = "✅" if any(c == "full" for c, _ in cats_dirs) else ("⚡" if any(c == "partial" for c, _ in cats_dirs) else ("❌" if cats_dirs else "⚠️"))
    dirs = [d for _, d in cats_dirs if d]
    direction = max(set(dirs), key=dirs.count) if dirs else "LONG"
    lines = [f"{overall} *{coin}* {'🟢 ЛОНГ' if direction == 'LONG' else '🔴 ШОРТ'}"]
    for ex in active_exchanges:
        r = ex_results.get(ex, {})
        label = EXCHANGE_LABELS.get(ex, ex.upper())
        if r.get("error"): lines.append(f"  `{label}`: ошибка — {r['error'][:40]}"); continue
        cat = {"full": "✅", "partial": "⚡"}.get(r.get("category", "fail"), "❌")
        key_avg = r["neg_avg"] if r.get("direction", "LONG") == "LONG" else r.get("pos_avg", 0.0)
        lines.append(f"  `{label}` {cat}  avg `{r['avg']:+.4f}%`  key\\_avg `{key_avg:+.4f}%`  выбр `{r['outlier_pct']:.0f}%`")
    return "\n".join(lines)

def build_analyze_reply(all_results, days, active_exchanges):
    lines = [f"📊 *Анализ* — {days} дней — {' + '.join(EXCHANGE_LABELS.get(e, e.upper()) for e in active_exchanges)}\n"]
    def best_cat(r):
        c = [v.get("category") for v in r.values() if not v.get("error")]
        return "full" if "full" in c else ("partial" if "partial" in c else ("fail" if c else "error"))
    for t_cat, t_name, t_ico in [("full", "ПОДХОДЯТ", "✅"), ("partial", "ЧАСТИЧНО", "⚡"), ("fail", "НЕ ПОДХОДЯТ", "❌")]:
        subset = [(c, r) for c, r in all_results.items() if best_cat(r) == t_cat]
        if subset:
            lines.append(f"{t_ico} *{t_name}* ({len(subset)}):")
            for coin, r in subset: lines.extend([fmt_coin_line(coin, r, active_exchanges), ""])
    errors = [(c, r) for c, r in all_results.items() if best_cat(r) == "error"]
    if errors:
        lines.append(f"⚠️ *Ошибки* ({len(errors)}):")
        for coin, _ in errors: lines.append(f"  `{coin}` — нет данных ни на одной бирже")
    return "\n".join(lines)

def make_days_keyboard(cb_prefix, extra_short=False):
    buttons = [[InlineKeyboardButton("1 день", callback_data=f"{cb_prefix}_days_1"), InlineKeyboardButton("3 дня", callback_data=f"{cb_prefix}_days_3")]] if extra_short else []
    buttons.append([InlineKeyboardButton("7 дней", callback_data=f"{cb_prefix}_days_7"), InlineKeyboardButton("14 дней", callback_data=f"{cb_prefix}_days_14")])
    buttons.append([InlineKeyboardButton("Другой", callback_data=f"{cb_prefix}_days_other"), InlineKeyboardButton("Отмена", callback_data=f"{cb_prefix}_cancel")])
    return InlineKeyboardMarkup(buttons)

def make_exchange_keyboard(cb_prefix, selected=None):
    if selected is None: selected = set()
    buttons, row = [], []
    for ex, enabled in EXCHANGES_ENABLED.items():
        if enabled:
            row.append(InlineKeyboardButton(f"{'✅ ' if ex in selected else ''}{EXCHANGE_LABELS.get(ex, ex.upper())}", callback_data=f"{cb_prefix}_ex_{ex}"))
            if len(row) == 3: buttons.append(row); row = []
    if row: buttons.append(row)
    buttons.extend([[InlineKeyboardButton("✅ Все", callback_data=f"{cb_prefix}_ex_all"), InlineKeyboardButton("▶️ Подтвердить", callback_data=f"{cb_prefix}_ex_confirm")], [InlineKeyboardButton("Отмена", callback_data=f"{cb_prefix}_cancel")]])
    return InlineKeyboardMarkup(buttons)

def make_settings_keyboard():
    buttons, row = [], []
    for ex, enabled in EXCHANGES_ENABLED.items():
        row.append(InlineKeyboardButton(f"{'✅' if enabled else '❌'} {EXCHANGE_LABELS.get(ex, ex.upper())}", callback_data=f"set_ex_{ex}"))
        if len(row) == 3: buttons.append(row); row = []
    if row: buttons.append(row)
    buttons.extend([[InlineKeyboardButton("✅ Все ВКЛ", callback_data="set_ex_all_on"), InlineKeyboardButton("❌ Все ВЫКЛ", callback_data="set_ex_all_off")], [InlineKeyboardButton("✖️ Закрыть", callback_data="set_close")]])
    return InlineKeyboardMarkup(buttons)

# ── DO-Функции ─────────────────────────────────────────────────────────
async def do_analyze(update, coins, days, exchange_arg, selected_exchanges=None):
    reply_fn = update.message.reply_text if hasattr(update, 'message') and update.message else update.reply_text
    active = [e for e in selected_exchanges if e in EXCHANGE_FETCHERS] if selected_exchanges else get_active_exchanges(exchange_arg)
    if not active: await reply_fn("❌ Нет активных бирж."); return
    await reply_fn(f"🔍 Анализирую {len(coins)} монет на {' + '.join(EXCHANGE_LABELS.get(e, e) for e in active)} за {days} дней...")
    start_ms, now_ms = int(time.time() * 1000) - days * 24 * 60 * 60 * 1000, int(time.time() * 1000)
    all_results = {coin: analyze_coin_multi(coin, start_ms, now_ms, active) for coin in coins}
    reply = build_analyze_reply(all_results, days, active)
    for chunk in [reply[i:i+4000] for i in range(0, len(reply), 4000)]: await reply_fn(chunk, parse_mode="Markdown")

async def do_show(update, coin, days, exchange_arg, selected_exchanges=None):
    reply_fn = update.message.reply_text if hasattr(update, 'message') and update.message else update.reply_text
    active = [e for e in selected_exchanges if e in EXCHANGE_FETCHERS] if selected_exchanges else get_active_exchanges(exchange_arg)
    if not active: await reply_fn("❌ Нет активных бирж."); return
    start_ms, now_ms = int(time.time() * 1000) - days * 24 * 60 * 60 * 1000, int(time.time() * 1000)
    for ex in active:
        label = EXCHANGE_LABELS.get(ex, ex.upper())
        try: data, sym_or_err = EXCHANGE_FETCHERS[ex](coin, start_ms, now_ms)
        except Exception as e: await reply_fn(f"❌ {label}: {e}"); continue
        if not data: await reply_fn(f"❌ {label}: {sym_or_err}"); continue
        rates = [r for _, r in sorted(data, key=lambda x: x[0], reverse=True)]
        header = f"📈 *{coin}* — {label} — `{sym_or_err}` — {days} дней\nСтавок: `{len(rates)}` | Avg: `{sum(rates)/len(rates):+.4f}%`\n`{'Время (UTC)':<17} {'Ставка':>10}`\n`{'─'*29}`\n"
        chunk, messages = header, []
        for ts, rate in sorted(data, key=lambda x: x[0], reverse=True):
            line = f"`{datetime.fromtimestamp(ts/1000, tz=timezone.utc).strftime('%m-%d %H:%M'):<12} {rate:>+10.4f}%{'  ◀' if rate <= STABILITY_THRESHOLD else ''}`\n"
            if len(chunk) + len(line) > 4000: messages.append(chunk); chunk = ""
            chunk += line
        if chunk: messages.append(chunk)
        for msg in messages: await reply_fn(msg, parse_mode="Markdown")

async def do_calc(update, coin, amount_usd, days, exchange_arg, selected_exchanges=None):
    reply_fn = update.message.reply_text if hasattr(update, 'message') and update.message else update.reply_text
    active = [e for e in selected_exchanges if e in EXCHANGE_FETCHERS] if selected_exchanges else get_active_exchanges(exchange_arg)
    if not active: await reply_fn("❌ Нет активных бирж."); return
    start_ms, now_ms = int(time.time() * 1000) - days * 24 * 60 * 60 * 1000, int(time.time() * 1000)
    for ex in active:
        label = EXCHANGE_LABELS.get(ex, ex.upper())
        try: data, sym = EXCHANGE_FETCHERS[ex](coin, start_ms, now_ms)
        except Exception as e: await reply_fn(f"❌ {label}: {e}"); continue
        if not data: continue
        by_day, total_income = {}, 0.0
        for ts, rate in data:
            if rate >= 0: continue
            payment = amount_usd * abs(rate / 100); total_income += payment
            day = datetime.fromtimestamp(ts/1000, tz=timezone.utc).strftime("%Y-%m-%d")
            by_day[day] = by_day.get(day, 0.0) + payment
        lines = [f"💰 *Калькулятор — {coin} — {label}*\nИтого: *${total_income:.2f}* (~${total_income/days if days>0 else 0:.2f}/день)\n"]
        for day in sorted(by_day.keys(), reverse=True): lines.append(f"`{day:<12} ${by_day[day]:>10.2f}`")
        await reply_fn("\n".join(lines), parse_mode="Markdown")

async def do_ai_multiple(update, coins):
    msg = update.message if hasattr(update, 'message') else update
    await msg.reply_text(f"🤖 Начинаю детальный AI-анализ {len(coins)} монет...")
    for i, coin in enumerate(coins):
        await msg.reply_text(f"🔍 Анализирую *{coin}*...", parse_mode="Markdown")
        ans = gemini_analyze_single(coin)
        if not ans: await msg.reply_text(f"❌ Ошибка Gemini по {coin}."); continue
        text = f"🤖 *Анализ {coin}*\n\n{ans}"
        for chunk in [text[j:j+4000] for j in range(0, len(text), 4000)]: await msg.reply_text(chunk)
        if i < len(coins) - 1: time.sleep(3)

# ── Команды: Скан (/analyze) ───────────────────────────────────────────
async def cmd_analyze_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("🔍 Скан монет\nШаг 1/3: Выбери биржу:", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Phemex", callback_data="an_ex_phemex"), InlineKeyboardButton("Toobit", callback_data="an_ex_toobit")], [InlineKeyboardButton("XT", callback_data="an_ex_xt"), InlineKeyboardButton("Zoomex", callback_data="an_ex_zoomex")], [InlineKeyboardButton("Отмена", callback_data="an_cancel")]]))
    return AN_METHOD

async def an_exchange_btn(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    if q.data == "an_cancel": await q.edit_message_text("Отменено."); return ConversationHandler.END
    context.user_data["an_exchange"] = q.data.replace("an_ex_", "")
    await q.edit_message_text("Шаг 2/3: Выбери метод:", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Средняя ставка", callback_data="an_method_rate"), InlineKeyboardButton("Средний доход", callback_data="an_method_income")], [InlineKeyboardButton("Отмена", callback_data="an_cancel")]]))
    return AN_METHOD

async def an_method_btn(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    if q.data == "an_cancel": await q.edit_message_text("Отменено."); return ConversationHandler.END
    method = q.data.replace("an_method_", "")
    context.user_data["an_method"] = method
    if method == "income":
        await q.edit_message_text("Шаг: Введи сумму позиции ($):", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("$15,000", callback_data="an_amt_15000"), InlineKeyboardButton("Другая", callback_data="an_amt_other")]]))
        return AN_AMT
    else:
        await q.edit_message_text("Шаг 3/3: Выбери период:", reply_markup=make_days_keyboard("an"))
        return AN_DAYS

async def an_amt_btn(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    if q.data == "an_amt_other": await q.edit_message_text("Введи число:"); return AN_AMT_NUM
    context.user_data["an_amount"] = float(q.data.replace("an_amt_", ""))
    await q.edit_message_text("Минимальный доход в день ($):", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("$25", callback_data="an_thr_25"), InlineKeyboardButton("Другой", callback_data="an_thr_other")]]))
    return AN_THRESH

async def an_amt_num(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try: context.user_data["an_amount"] = float(update.message.text.strip())
    except: return AN_AMT_NUM
    await update.message.reply_text("Минимальный доход в день ($):", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("$25", callback_data="an_thr_25"), InlineKeyboardButton("Другой", callback_data="an_thr_other")]]))
    return AN_THRESH

async def an_thresh_btn(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    if q.data == "an_thr_other": await q.edit_message_text("Введи порог:"); return AN_THRESH_NUM
    context.user_data["an_threshold"] = float(q.data.replace("an_thr_", ""))
    await q.edit_message_text("Шаг 3/3: Выбери период:", reply_markup=make_days_keyboard("an"))
    return AN_DAYS

async def an_thresh_num(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try: context.user_data["an_threshold"] = float(update.message.text.strip())
    except: return AN_THRESH_NUM
    await update.message.reply_text("Шаг 3/3: Выбери период:", reply_markup=make_days_keyboard("an"))
    return AN_DAYS

async def an_days_btn(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    if q.data == "an_days_other": await q.edit_message_text("Введи дни:"); return AN_DAYS_NUM
    context.user_data["an_days"] = int(q.data.replace("an_days_", ""))
    await q.edit_message_text("Запускаю скан...")
    await an_run_scan(q, context)
    return ConversationHandler.END

async def an_days_num(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try: context.user_data["an_days"] = int(update.message.text.strip())
    except: return AN_DAYS_NUM
    await update.message.reply_text("Запускаю скан...")
    await an_run_scan(update, context)
    return ConversationHandler.END

async def an_run_scan(trigger, context):
    msg = trigger.message if hasattr(trigger, 'message') else trigger
    ex, method, days, amount, threshold = context.user_data.get("an_exchange"), context.user_data.get("an_method"), context.user_data.get("an_days", 7), context.user_data.get("an_amount", 0), context.user_data.get("an_threshold", 0)
    chat_id = msg.chat_id
    _scan_running[chat_id] = True
    try: all_coins = phemex_get_all_symbols()
    except Exception as e: await msg.reply_text(f"Ошибка получения монет: {e}"); return
    
    await msg.reply_text(f"📋 Скан {EXCHANGE_LABELS.get(ex, ex)} — {len(all_coins)} монет\nМетод: {method}\nПериод: {days} дней")
    start_ms, now_ms = int(time.time() * 1000) - days * 24 * 60 * 60 * 1000, int(time.time() * 1000)
    fetcher = EXCHANGE_FETCHERS.get(ex)
    passed, batches = [], [all_coins[i:i+SCAN_BATCH] for i in range(0, len(all_coins), SCAN_BATCH)]
    
    for batch_idx, batch in enumerate(batches):
        if not _scan_running.get(chat_id): await msg.reply_text("⛔ Остановлено."); return
        for coin in batch:
            try: data, _ = fetcher(coin, start_ms, now_ms)
            except: data = []
            if not data: time.sleep(0.15); continue
            rates = [r for _, r in data]
            
            if method == "rate":
                r = analyze_rates(rates)
                if r and r["category"] != "fail":
                    d = r["direction"]
                    k = r["neg_avg"] if d == "LONG" else r["pos_avg"]
                    if check_recent_trend(fetcher, coin, d): passed.append((coin, k, r["outlier_pct"], d, r["category"], 0))
            else:
                clean = [x for x in rates if abs(x) <= AN_ANOMALY_THRESHOLD]
                if clean:
                    avg = sum(clean) / len(clean)
                    if (len([x for x in clean if x < 0]) / len(clean)) >= MIN_NEG_RATIO and avg < 0:
                        inc = amount * abs(avg)/100 * (len(clean)/days)
                        if inc >= threshold and check_recent_trend(fetcher, coin, "LONG"):
                            passed.append((coin, avg, 0, "LONG", "income", inc))
            time.sleep(0.15)
        if batch_idx % 4 == 3: await msg.reply_text(f"⏳ Проверено {min((batch_idx+1)*SCAN_BATCH, len(all_coins))}/{len(all_coins)} | найдено: {len(passed)}")
    _scan_running[chat_id] = False

    if not passed: await msg.reply_text("Ничего не найдено."); return
    
    lines = [f"✅ *Скан завершён* ({len(passed)} монет)\n"]
    for c, a, o, d, cat, i in passed:
        lines.append(f"{'🟢' if d=='LONG' else '🔴'} `{c}` | Avg: `{a:+.4f}%` {'| $'+str(int(i))+'/день' if cat=='income' else ''}")
    reply = "\n".join(lines)
    for chunk in [reply[i:i+4000] for i in range(0, len(reply), 4000)]: await msg.reply_text(chunk, parse_mode="Markdown")

    # ИИ АНАЛИЗ В КОНЦЕ
    target_coins = [(c, d, a) for c, a, o, d, cat, i in passed]
    if target_coins and GEMINI_API_KEY:
        await msg.reply_text(f"🤖 *Gemini фильтрует список (порциями по 15)...*", parse_mode="Markdown")
        bulk_results = []
        for i in range(0, len(target_coins), 15):
            chunk = target_coins[i:i+15]
            ans = gemini_analyze_bulk("\n".join([f"- {c} (Направление: {'ЛОНГ' if d == 'LONG' else 'ШОРТ'})" for c, d, a in chunk]), days)
            if ans and "Подходящих фундаментальных монет нет" not in ans: bulk_results.append(ans)
            time.sleep(3)
        if bulk_results: await msg.reply_text(f"🤖 *GEMINI AI ОДОБРЯЕТ:*\n\n" + "\n".join(bulk_results))
        else: await msg.reply_text("🤖 Gemini не дал рекомендаций.")

# ── Авто-скан (Job в 20:00) ────────────────────────────────────────────
async def auto_scan_job(context):
    chat_id = int(REPORT_CHAT_ID)
    await context.bot.send_message(chat_id, f"🕗 *Авто-скан запущен*\nИщем монеты и составляем пары...", parse_mode="Markdown")
    all_passed = []
    
    for ex in AUTO_SCAN_EXCHANGES:
        if not EXCHANGES_ENABLED.get(ex): continue
        fetcher = EXCHANGE_FETCHERS.get(ex)
        try: coins = phemex_get_all_symbols() if ex != "coinw" else [x["base"].upper() for x in requests.get("https://api.coinw.com/v1/perpum/instruments", timeout=15).json().get("data", [])]
        except: continue
        now_ms, start_ms = int(time.time() * 1000), int(time.time() * 1000) - AUTO_SCAN_DAYS * 24 * 60 * 60 * 1000
        for coin in coins:
            try: data, _ = fetcher(coin, start_ms, now_ms)
            except: data = []
            clean = [r for _, r in data if abs(r) <= AN_ANOMALY_THRESHOLD]
            if clean:
                avg = sum(clean) / len(clean)
                if (len([x for x in clean if x < 0])/len(clean)) >= MIN_NEG_RATIO and avg < 0:
                    if (AUTO_SCAN_AMOUNT * abs(avg)/100 * (len(clean)/AUTO_SCAN_DAYS)) >= AUTO_SCAN_THRESHOLD and check_recent_trend(fetcher, coin, "LONG"):
                        all_passed.append((coin, avg, 0, "LONG", ex))
                elif (len([x for x in clean if x > 0])/len(clean)) >= MIN_POS_RATIO and avg > 0:
                    if (AUTO_SCAN_AMOUNT * abs(avg)/100 * (len(clean)/AUTO_SCAN_DAYS)) >= AUTO_SCAN_THRESHOLD and check_recent_trend(fetcher, coin, "SHORT"):
                        all_passed.append((coin, avg, 0, "SHORT", ex))
            time.sleep(0.1)

    if not all_passed: await context.bot.send_message(chat_id, "✅ Авто-скан завершён. Подходящих монет нет."); return
    unique_coins = {p[0]: p[3] for p in all_passed}
    
    # GEMINI
    await context.bot.send_message(chat_id, f"🤖 *Gemini фильтрует {len(unique_coins)} монет(ы)...*", parse_mode="Markdown")
    uc_list, bulk_results = list(unique_coins.items()), []
    for i in range(0, len(uc_list), 15):
        chunk = uc_list[i:i+15]
        ans = gemini_analyze_bulk("\n".join([f"- {c} (Направление: {'ЛОНГ' if d == 'LONG' else 'ШОРТ'})" for c, d in chunk]), AUTO_SCAN_DAYS)
        if ans and "Подходящих фундаментальных монет нет" not in ans: bulk_results.append(ans)
        time.sleep(3)
    
    bulk_analysis = "\n".join(bulk_results)
    approved_coins = [c for c in unique_coins.keys() if c in bulk_analysis]
    if not approved_coins: await context.bot.send_message(chat_id, "🤖 Gemini забраковал все монеты."); return
    
    active_ex = get_active_exchanges()
    final_pairs = []
    for coin in approved_coins:
        coin_passes = sorted([p for p in all_passed if p[0] == coin], key=lambda x: x[1], reverse=(unique_coins[coin] != "LONG"))
        main_avg, main_dir, main_ex = coin_passes[0][1], coin_passes[0][3], coin_passes[0][4]
        hedge_ex, hedge_avg = find_best_hedge(coin, main_ex, main_dir, int(time.time()*1000)-AUTO_SCAN_DAYS*86400000, int(time.time()*1000), active_ex)
        if hedge_ex:
            net_rate = abs(main_avg) + hedge_avg if main_dir == "LONG" else main_avg - hedge_avg
            final_pairs.append({"coin": coin, "main_dir": main_dir, "long_str": f"{EXCHANGE_LABELS.get(main_ex if main_dir=='LONG' else hedge_ex)} ({main_avg if main_dir=='LONG' else hedge_avg:+.4f}%)", "short_str": f"{EXCHANGE_LABELS.get(hedge_ex if main_dir=='LONG' else main_ex)} ({hedge_avg if main_dir=='LONG' else main_avg:+.4f}%)", "net_rate": net_rate})

    if final_pairs:
        lines = ["🤖 *ВЕЧЕРНИЙ ОТЧЕТ*\n"]
        for p in sorted(final_pairs, key=lambda x: x["net_rate"], reverse=True):
            lines.append(f"{'🟢' if p['main_dir']=='LONG' else '🔴'} *{p['coin']}*\n   Лонг: `{p['long_str']}`\n   Шорт: `{p['short_str']}`\n   📈 Чистый фандинг: `{p['net_rate']:+.4f}%` / ставку\n")
        await context.bot.send_message(chat_id, "\n".join(lines), parse_mode="Markdown")

# ── Команды: Фильтр (/filter) ──────────────────────────────────────────
async def acf_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Введи монеты через пробел:")
    return ACF_COIN
async def acf_got_coin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["acf_coins"], _, _ = parse_tokens(update.message.text)
    await update.message.reply_text("Период анализа:", reply_markup=make_days_keyboard("acf"))
    return ACF_DAYS
async def acf_days_btn(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    if q.data == "acf_days_other": await q.edit_message_text("Введи дни числом:"); return ACF_DAYS_NUM
    context.user_data["acf_days"] = int(q.data.split("_")[-1])
    await q.edit_message_text("Биржа:", reply_markup=make_exchange_keyboard("acf"))
    return ACF_EXCH
async def acf_days_num(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["acf_days"] = int(update.message.text.strip())
    await update.message.reply_text("Биржа:", reply_markup=make_exchange_keyboard("acf"))
    return ACF_EXCH
async def acf_exchange_btn(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    selected = context.user_data.get("acf_selected_ex", set())
    if q.data == "acf_ex_all": 
        context.user_data["acf_selected_ex"] = set(e for e in EXCHANGES_ENABLED if EXCHANGES_ENABLED[e])
        await q.edit_message_reply_markup(reply_markup=make_exchange_keyboard("acf", context.user_data["acf_selected_ex"]))
        return ACF_EXCH
    if q.data == "acf_ex_confirm":
        await q.edit_message_text("Считаю...")
        await do_analyze(update, context.user_data["acf_coins"], context.user_data["acf_days"], None, selected or set(e for e in EXCHANGES_ENABLED if EXCHANGES_ENABLED[e]))
        return ConversationHandler.END
    ex = q.data.replace("acf_ex_", "")
    if ex in selected: selected.remove(ex)
    else: selected.add(ex)
    context.user_data["acf_selected_ex"] = selected
    await q.edit_message_reply_markup(reply_markup=make_exchange_keyboard("acf", selected))
    return ACF_EXCH

# ── Команды: Фандинг (/funding) ────────────────────────────────────────
async def fr_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Введи монету для проверки фандинга:")
    return FR_COIN
async def fr_got_coin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    coins, _, _ = parse_tokens(update.message.text)
    if not coins: return FR_COIN
    context.user_data["fr_coin"] = coins[0]
    await update.message.reply_text("Период (дней):", reply_markup=make_days_keyboard("fr"))
    return FR_DAYS
async def fr_days_btn(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    if q.data == "fr_days_other": await q.edit_message_text("Введи дни числом:"); return FR_DAYS_NUM
    context.user_data["fr_days"] = int(q.data.split("_")[-1])
    await q.edit_message_text("Биржа:", reply_markup=make_exchange_keyboard("fr"))
    return FR_EXCH
async def fr_days_num(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["fr_days"] = int(update.message.text.strip())
    await update.message.reply_text("Биржа:", reply_markup=make_exchange_keyboard("fr"))
    return FR_EXCH
async def fr_exchange_btn(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    selected = context.user_data.get("fr_selected_ex", set())
    if q.data == "fr_ex_all":
        context.user_data["fr_selected_ex"] = set(e for e in EXCHANGES_ENABLED if EXCHANGES_ENABLED[e])
        await q.edit_message_reply_markup(reply_markup=make_exchange_keyboard("fr", context.user_data["fr_selected_ex"]))
        return FR_EXCH
    if q.data == "fr_ex_confirm":
        await q.edit_message_text("Загружаю историю...")
        await do_show(update, context.user_data["fr_coin"], context.user_data["fr_days"], None, selected or set(e for e in EXCHANGES_ENABLED if EXCHANGES_ENABLED[e]))
        return ConversationHandler.END
    ex = q.data.replace("fr_ex_", "")
    if ex in selected: selected.remove(ex)
    else: selected.add(ex)
    context.user_data["fr_selected_ex"] = selected
    await q.edit_message_reply_markup(reply_markup=make_exchange_keyboard("fr", selected))
    return FR_EXCH

# ── Команды: Калькулятор (/calculator) ─────────────────────────────────
async def pc_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Введи монету для расчета профита:")
    return PC_COIN
async def pc_got_coin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    coins, _, _ = parse_tokens(update.message.text)
    if not coins: return PC_COIN
    context.user_data["pc_coin"] = coins[0]
    await update.message.reply_text("Введи сумму позиции ($):", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("$15,000", callback_data="pc_amt_15000"), InlineKeyboardButton("$20,000", callback_data="pc_amt_20000")], [InlineKeyboardButton("$25,000", callback_data="pc_amt_25000"), InlineKeyboardButton("Другая", callback_data="pc_amt_other")]]))
    return PC_AMT
async def pc_amt_btn(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    if q.data == "pc_amt_other": await q.edit_message_text("Введи сумму числом:"); return PC_AMT_NUM
    context.user_data["pc_amount"] = float(q.data.replace("pc_amt_", ""))
    await q.edit_message_text("Выбери период:", reply_markup=make_days_keyboard("pc"))
    return PC_DAYS
async def pc_amt_num(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try: context.user_data["pc_amount"] = float(update.message.text.strip())
    except: return PC_AMT_NUM
    await update.message.reply_text("Выбери период:", reply_markup=make_days_keyboard("pc"))
    return PC_DAYS
async def pc_days_btn(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    if q.data == "pc_days_other": await q.edit_message_text("Введи дни числом:"); return PC_DAYS_NUM
    context.user_data["pc_days"] = int(q.data.split("_")[-1])
    await q.edit_message_text("Биржа:", reply_markup=make_exchange_keyboard("pc"))
    return PC_EXCH
async def pc_days_num(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["pc_days"] = int(update.message.text.strip())
    await update.message.reply_text("Биржа:", reply_markup=make_exchange_keyboard("pc"))
    return PC_EXCH
async def pc_exchange_btn(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    selected = context.user_data.get("pc_selected_ex", set())
    if q.data == "pc_ex_all":
        context.user_data["pc_selected_ex"] = set(e for e in EXCHANGES_ENABLED if EXCHANGES_ENABLED[e])
        await q.edit_message_reply_markup(reply_markup=make_exchange_keyboard("pc", context.user_data["pc_selected_ex"]))
        return PC_EXCH
    if q.data == "pc_ex_confirm":
        await q.edit_message_text("Считаю доход...")
        await do_calc(update, context.user_data["pc_coin"], context.user_data["pc_amount"], context.user_data["pc_days"], None, selected or set(e for e in EXCHANGES_ENABLED if EXCHANGES_ENABLED[e]))
        return ConversationHandler.END
    ex = q.data.replace("pc_ex_", "")
    if ex in selected: selected.remove(ex)
    else: selected.add(ex)
    context.user_data["pc_selected_ex"] = selected
    await q.edit_message_reply_markup(reply_markup=make_exchange_keyboard("pc", selected))
    return PC_EXCH

# ── Gemini и Прочие команды ────────────────────────────────────────────
async def cmd_ai_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Введи монеты для AI-анализа (например SOL ENJ):")
    return WAIT_AI_COIN
async def ai_got_coin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await do_ai_multiple(update, update.message.text.split())
    return ConversationHandler.END

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 *Phemex + XT Funding Analyzer*\n\n"
        "/analyze — скан всего рынка + Gemini AI\n"
        "/ai — детальный разбор монеты через ИИ\n"
        "/filter — ручной анализ списка монет\n"
        "/funding — сырая история ставок\n"
        "/calculator — симуляция PnL позиции\n"
        "/settings — настройки бирж", parse_mode="Markdown"
    )

async def cmd_settings_new(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("⚙️ *Настройки*", reply_markup=make_settings_keyboard(), parse_mode="Markdown")

async def settings_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    if q.data == "set_close": await q.edit_message_text("Закрыто."); return
    if q.data == "set_ex_all_on":
        for e in EXCHANGES_ENABLED: EXCHANGES_ENABLED[e] = True
    elif q.data == "set_ex_all_off":
        for e in EXCHANGES_ENABLED: EXCHANGES_ENABLED[e] = False
    elif q.data.startswith("set_ex_"):
        e = q.data.replace("set_ex_", "")
        EXCHANGES_ENABLED[e] = not EXCHANGES_ENABLED.get(e, False)
    await q.edit_message_reply_markup(reply_markup=make_settings_keyboard())

async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await (update.callback_query.edit_message_text("Отменено.") if update.callback_query else update.message.reply_text("Отменено."))
    return ConversationHandler.END

# ── Запуск бота ────────────────────────────────────────────────────────
def main():
    app = Application.builder().token(BOT_TOKEN).build()

    # Регистрация меню
    app.add_handler(ConversationHandler(entry_points=[CommandHandler("filter", acf_start)], states={ACF_COIN: [MessageHandler(filters.TEXT & ~filters.COMMAND, acf_got_coin)], ACF_DAYS: [CallbackQueryHandler(acf_days_btn, pattern="^acf_")], ACF_DAYS_NUM: [MessageHandler(filters.TEXT & ~filters.COMMAND, acf_days_num)], ACF_EXCH: [CallbackQueryHandler(acf_exchange_btn, pattern="^acf_")]}, fallbacks=[CommandHandler("cancel", cmd_cancel)]))
    app.add_handler(ConversationHandler(entry_points=[CommandHandler("funding", fr_start)], states={FR_COIN: [MessageHandler(filters.TEXT & ~filters.COMMAND, fr_got_coin)], FR_DAYS: [CallbackQueryHandler(fr_days_btn, pattern="^fr_")], FR_DAYS_NUM: [MessageHandler(filters.TEXT & ~filters.COMMAND, fr_days_num)], FR_EXCH: [CallbackQueryHandler(fr_exchange_btn, pattern="^fr_")]}, fallbacks=[CommandHandler("cancel", cmd_cancel)]))
    app.add_handler(ConversationHandler(entry_points=[CommandHandler("calculator", pc_start)], states={PC_COIN: [MessageHandler(filters.TEXT & ~filters.COMMAND, pc_got_coin)], PC_AMT: [CallbackQueryHandler(pc_amt_btn, pattern="^pc_amt_")], PC_AMT_NUM: [MessageHandler(filters.TEXT & ~filters.COMMAND, pc_amt_num)], PC_DAYS: [CallbackQueryHandler(pc_days_btn, pattern="^pc_days_")], PC_DAYS_NUM: [MessageHandler(filters.TEXT & ~filters.COMMAND, pc_days_num)], PC_EXCH: [CallbackQueryHandler(pc_exchange_btn, pattern="^pc_")]}, fallbacks=[CommandHandler("cancel", cmd_cancel)]))
    app.add_handler(ConversationHandler(entry_points=[CommandHandler("analyze", cmd_analyze_start)], states={AN_METHOD: [CallbackQueryHandler(an_exchange_btn, pattern="^an_ex_"), CallbackQueryHandler(an_method_btn, pattern="^an_method_")], AN_AMT: [CallbackQueryHandler(an_amt_btn, pattern="^an_amt_")], AN_AMT_NUM: [MessageHandler(filters.TEXT & ~filters.COMMAND, an_amt_num)], AN_THRESH: [CallbackQueryHandler(an_thresh_btn, pattern="^an_thr_")], AN_THRESH_NUM: [MessageHandler(filters.TEXT & ~filters.COMMAND, an_thresh_num)], AN_DAYS: [CallbackQueryHandler(an_days_btn, pattern="^an_days_")], AN_DAYS_NUM: [MessageHandler(filters.TEXT & ~filters.COMMAND, an_days_num)]}, fallbacks=[CommandHandler("cancel", cmd_cancel)]))
    app.add_handler(ConversationHandler(entry_points=[CommandHandler("ai", cmd_ai_start)], states={WAIT_AI_COIN: [MessageHandler(filters.TEXT & ~filters.COMMAND, ai_got_coin)]}, fallbacks=[CommandHandler("cancel", cmd_cancel)]))

    # Базовые команды
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_start))
    app.add_handler(CommandHandler("settings", cmd_settings_new))
    app.add_handler(CallbackQueryHandler(settings_callback, pattern="^set_"))
    
    # Авто-скан
    if REPORT_CHAT_ID and app.job_queue:
        from datetime import time as dt_time
        app.job_queue.run_daily(auto_scan_job, time=dt_time(hour=17, minute=0, second=0))

    print("Бот запущен...")
    app.run_polling()

if __name__ == "__main__":
    main()
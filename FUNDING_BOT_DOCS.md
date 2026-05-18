# Funding Rate Analyzer Bot — Документация

Telegram-бот для анализа исторических ставок фандинга на крипто-биржах. Основная задача: находить монеты со стабильно отрицательным/положительным фандингом, отсеивать скам через Gemini AI и формировать готовые дельта-нейтральные позиции (лонг + шорт на разных биржах).

## Оглавление

1. [Технический стек](https://www.google.com/search?q=%23%D1%82%D0%B5%D1%85%D0%BD%D0%B8%D1%87%D0%B5%D1%81%D0%BA%D0%B8%D0%B9-%D1%81%D1%82%D0%B5%D0%BA)
2. [Деплой](https://www.google.com/search?q=%23%D0%B4%D0%B5%D0%BF%D0%BB%D0%BE%D0%B9)
3. [Конфигурация](https://www.google.com/search?q=%23%D0%BA%D0%BE%D0%BD%D1%84%D0%B8%D0%B3%D1%83%D1%80%D0%B0%D1%86%D0%B8%D1%8F)
4. [Фильтры анализа](https://www.google.com/search?q=%23%D1%84%D0%B8%D0%BB%D1%8C%D1%82%D1%80%D1%8B-%D0%B0%D0%BD%D0%B0%D0%BB%D0%B8%D0%B7%D0%B0)
5. [Биржи и API endpoints](https://www.google.com/search?q=%23%D0%B1%D0%B8%D1%80%D0%B6%D0%B8-%D0%B8-api-endpoints)
6. [Команды бота](https://www.google.com/search?q=%23%D0%BA%D0%BE%D0%BC%D0%B0%D0%BD%D0%B4%D1%8B-%D0%B1%D0%BE%D1%82%D0%B0)
7. [Архитектура кода](https://www.google.com/search?q=%23%D0%B0%D1%80%D1%85%D0%B8%D1%82%D0%B5%D0%BA%D1%82%D1%83%D1%80%D0%B0-%D0%BA%D0%BE%D0%B4%D0%B0)
8. [Логика Дельта-нейтрали и Авто-скана](https://www.google.com/search?q=%23%D0%BB%D0%BE%D0%B3%D0%B8%D0%BA%D0%B0-%D0%B4%D0%B5%D0%BB%D1%8C%D1%82%D0%B0-%D0%BD%D0%B5%D0%B9%D1%82%D1%80%D0%B0%D0%BB%D0%B8-%D0%B8-%D0%B0%D0%B2%D1%82%D0%BE-%D1%81%D0%BA%D0%B0%D0%BD%D0%B0)
9. [Известные ограничения](https://www.google.com/search?q=%23%D0%B8%D0%B7%D0%B2%D0%B5%D1%81%D1%82%D0%BD%D1%8B%D0%B5-%D0%BE%D0%B3%D1%80%D0%B0%D0%BD%D0%B8%D1%87%D0%B5%D0%BD%D0%B8%D1%8F)
10. [Быстрое воссоздание](https://www.google.com/search?q=%23%D0%B1%D1%8B%D1%81%D1%82%D1%80%D0%BE%D0%B5-%D0%B2%D0%BE%D1%81%D1%81%D0%BE%D0%B7%D0%B4%D0%B0%D0%BD%D0%B8%D0%B5)

---

## Технический стек

* **Python 3.9+**
* **python-telegram-bot** (v20.0+) — Telegram Bot API
* **requests** — HTTP запросы к биржам и Gemini
* Никаких баз данных — состояние только в памяти (кроме Supabase для CoinW)

**Зависимости (requirements.txt):**

```text
python-telegram-bot[job-queue]==21.3
requests==2.31.0

```

---

## Деплой

**Локально**

```bash
export BOT_TOKEN="<токен>"
export GEMINI_API_KEY="<gemini_key>"
export SUPABASE_URL="<url>"
export SUPABASE_KEY="<key>"
python3 bot.py

```

**Render.com / Railway (Web Service)**

* Репозиторий: `https://github.com/astronomytest01-rgb/funding-bot`
* Build Command: `pip install -r requirements.txt`
* Start Command: `python bot.py`
* Environment Variables: `BOT_TOKEN`, `GEMINI_API_KEY`, `REPORT_CHAT_ID`, `SUPABASE_URL`, `SUPABASE_KEY`

⚠️ Нельзя запускать бота одновременно локально и на облаке — конфликт polling.

---

## Конфигурация

В начале `bot.py` заданы жесткие лимиты:

```python
DEFAULT_DAYS        = 7      # период анализа по умолчанию
STABILITY_THRESHOLD = -0.04  # порог ставки (%)
MAX_OUTLIER_PCT     = 25     # макс % выбросов выше порога
NEG_AVG_THRESHOLD   = -0.08  # порог среднего только по отриц. ставкам
MIN_NEG_RATIO       = 0.30   # минимум 30% ставок должны быть отрицательными

```

Состояние бирж (изменяется в рантайме через меню `/settings`):

```python
EXCHANGES_ENABLED = {
    "phemex": True, "xt": True, "toobit": True,
    "okx": True, "bingx": True, "coinw": True,
}

```

---

## Фильтры анализа

Монета анализируется в двух направлениях: **ЛОНГ** (фандинг < 0) и **ШОРТ** (фандинг > 0). Проходит в одну из трёх категорий:

| Категория | Условие | Иконка |
| --- | --- | --- |
| **ПОДХОДИТ** | `outlier_pct` ≤ 25% (стабильность) | ✅ |
| **ЧАСТИЧНО** | `neg_avg` ≤ -0.08% (сильный перекос, но нестабильно) | ⚡ |
| **НЕ ПОДХОДИТ** | не прошла ни одно условие | ❌ |

**Расчёт outlier_pct:**

```python
below_threshold = count(rate <= -0.04%)  # ставок ниже порога
outlier_pct = (total - below_threshold) / total * 100
# Если outlier_pct <= 25% → монета стабильна

```

**Расчёт neg_avg (для лонга):**

```python
neg_rates = [r for r in rates if r < 0]
neg_avg = mean(neg_rates)  # среднее только по отрицательным ставкам

```

---

## Биржи и API endpoints

| Биржа | Формат Символа | Особенности и Эндпоинты |
| --- | --- | --- |
| **Phemex** | `.ENJUSDTFR8H` | `GET https://api.phemex.com/api-data/public/data/funding-rate-history?symbol=.ENJUSDTFR8H`. Отдает `rate` в виде строки. Делим на 1 для %. |
| **XT** | `enj_usdt` | `GET https://fapi.xt.com/future/market/v1/public/q/funding-rate-record`. Строго lowercase. |
| **Toobit** | `ENJ-SWAP-USDT` | `GET https://api.toobit.com/api/v1/futures/historyFundingRate`. Отдает массив объектов с `settleRate`. |
| **OKX** | `ENJ-USDT-SWAP` | `GET https://www.okx.com/api/v5/public/funding-rate-history`. Может отдавать 451/403 блок по IP в РФ. |
| **BingX** | `ENJ-USDT` | `GET https://open-api.bingx.com/openApi/swap/v2/quote/fundingRate`. |
| **CoinW** | `eq.ENJ` | `GET {SUPABASE_URL}/rest/v1/funding_rates`. Тянет данные из внешней PostgreSQL, т.к. у CoinW нет публичного API истории. |

Список всех контрактов (для сканирования) берется с Phemex:
`GET https://api.phemex.com/exchange/public/cfg/v2/products` (Фильтр: `type=PerpetualV2`, `quoteCurrency=USDT`).

---

## Команды бота

**Интерактивные команды (ConversationHandlers):**

* `/analyze` — Быстрый скан всего рынка с фильтром Gemini AI в конце. Выбор параметров идет через кнопки.
* `/ai [монеты]` — Детальный разбор (4 абзаца) от Gemini. Можно ввести сразу несколько: `/ai BTC SOL ENJ`.

**Команды с поддержкой аргументов (parse_tokens):**
Парсер понимает строку вида `ENJ RON /days 14 /exchange xt`.

* `/filter [монеты] [/days N] [/exchange биржа]` — Анализ списка монет по всем биржам.
* `/funding [монета] [/days N] [/exchange биржа]` — Выводит сырую историю ставок с таймстампами.
* `/calculator [монета] [сумма] [/days N]` — Считает исторический PnL (сколько принесла бы позиция в $).

**Системные:**

* `/settings` — Inline-меню для включения/выключения бирж.
* `/start` / `/help` — Справка.

---

## Архитектура кода

**bot.py**

```text
├── CONFIG (Лимиты, ключи, EXCHANGES_ENABLED)
│
├── API FETCHERS (одна функция на биржу)
│   ├── phemex_fetch(coin, start_ms, end_ms) → list[(ts_ms, rate_pct)]
│   ├── xt_fetch(...)
│   ├── toobit_fetch(...)
│   ├── okx_fetch(...)
│   ├── bingx_fetch(...)
│   └── coinw_fetch(...)
│
├── GEMINI AI АНАЛИЗ
│   ├── gemini_analyze_bulk(coins_list_text, days) → str (Короткий список 🟢/🔴)
│   └── gemini_analyze_single(coin) → str (Детальный разбор, 4 абзаца)
│
├── МАТЕМАТИКА И ФИЛЬТРЫ
│   ├── check_recent_trend(fetcher, coin, direction) → bool
│   ├── analyze_rates(rates_pct) → dict с avg, neg_avg, outlier_pct, category
│   ├── find_best_hedge(coin, main_exchange, main_direction...) → (exchange, avg_rate)
│   └── parse_tokens(text) → (coins, days, exchange)
│
├── CONVERSATION HANDLERS
│   ├── ai_conv: cmd_ai_start → ai_got_coin
│   ├── analyze_conv: cmd_analyze_start → an_exchange_btn → an_method_btn ...
│   ├── acf_conv (/filter): acf_start → acf_got_coin ...
│   ├── fr_conv (/funding): fr_start → fr_got_coin ...
│   └── pc_conv (/calculator): pc_start → pc_got_coin ...
│
├── АВТО-СКАН
│   ├── auto_scan_job(context) # Вызывается JobQueue
│   └── Ищет монеты -> фильтрует через Gemini -> считает хедж -> шлет отчет
│
└── MAIN
    └── Регистрация хендлеров + app.run_polling()

```

---

## Логика Дельта-нейтрали и Авто-скана

Каждый день в 17:00 UTC (20:00 Киев) запускается `auto_scan_job`.

1. **Скан:** Бот перебирает все 500+ монет с Phemex и ищет те, доходность которых превышает порог `AUTO_SCAN_THRESHOLD` (≥$29/день при марже $20k).
2. **Gemini Фильтр:** Все найденные монеты отправляются в `gemini_analyze_bulk`. Нейросеть отсеивает мемкоины и возвращает список надежных активов.
3. **Поиск хеджа (`find_best_hedge`):** Для каждой одобренной монеты бот берет основное направление (например, ЛОНГ на Phemex) и прочесывает все остальные активные биржи, чтобы найти лучший ШОРТ.
* `net_rate = abs(long_avg) + short_avg` (если шорт тоже платит)
* `net_rate = abs(long_avg) - short_avg` (если за шорт приходится платить, но лонг перекрывает убыток).


4. **Отчет:** Формируется готовый сигнал с чистым расчетным фандингом и отправляется в `REPORT_CHAT_ID`.

---

## Известные ограничения

1. **Сброс настроек:** Состояние бирж (`/settings`) не пишется на диск и сбрасывается при рестарте контейнера.
2. **Rate Limits:** Бесплатный Gemini API имеет квоты. В код вшиты ретраи: `if r.status_code == 429: time.sleep(15)`.
3. **Пагинация API:** OKX и Toobit могут не отдавать историю глубже определенного лимита (100-1000 записей) без сложной пагинации.
4. Telegram обрезает сообщения >4096 символов — бот нарезает их на чанки (особенно актуально для длинных ответов ИИ).

---

## Быстрое воссоздание

Если нужно передать проект другому AI-ассистенту:

> "Создай Telegram-бота на Python (v20+ PTB) для анализа ставок фандинга. Бот должен тянуть исторические ставки с 6 бирж (Phemex, XT, Toobit, OKX, BingX, CoinW), использовать математические фильтры (outlier_pct ≤ 25%) и симуляцию PnL для отбора лучших монет. В бота должна быть встроена ИИ-валидация через Gemini API (gemini-2.5-flash) для отсева мемкоинов. Ежедневно в 20:00 бот должен делать автоскан: найти монеты, пропустить их через ИИ, автоматически найти лучшую биржу для хеджа (дельта-нейтраль) и прислать готовый PnL отчет. Все эндпоинты, формулы и структура описаны в файле FUNDING_BOT_DOCS.md."

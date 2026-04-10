# Документация: два Telegram-бота для крипто-фандинга

> Актуальная версия документации. Последнее обновление: апрель 2026.

---

## Содержание

1. [Бот 1: Funding Rate Analyzer](#бот-1-funding-rate-analyzer)
2. [Бот 2: Phemex Position Tracker](#бот-2-phemex-position-tracker)

---

# Бот 1: Funding Rate Analyzer

## Назначение

Анализирует исторические ставки фандинга на 9 биржах. Находит монеты со стабильно отрицательным (для лонга) или положительным (для шорта) фандингом. Поддерживает поиск дельта-нейтральных пар лонг+шорт.

## Деплой

- **Платформа:** Render.com (Web Service, Free tier)
- **Репозиторий:** `https://github.com/astronomytest01-rgb/funding-bot`
- **Build Command:** `pip install -r requirements.txt`
- **Start Command:** `python bot.py`
- **Переменные окружения:** `BOT_TOKEN=<токен>`

**requirements.txt:**
```
python-telegram-bot>=20.0
requests
```

> ⚠️ Нельзя запускать бота одновременно локально и на Render — конфликт polling.

## BotFather — команды (`/setcommands`)

```
filter - Анализ монет по фильтрам фандинга
funding - Ставки фандинга по монете
calculator - Калькулятор дохода от фандинга
analyze - Скан всех монет на выбранной бирже
findpair - Дельта-нейтраль: найти пару лонг+шорт
settings - Настройки и управление биржами
help - Справка
```

## Конфигурация

```python
DEFAULT_DAYS        = 7      # период анализа по умолчанию
STABILITY_THRESHOLD = -0.04  # порог ставки (%)
MAX_OUTLIER_PCT     = 25     # макс % выбросов выше порога
NEG_AVG_THRESHOLD   = -0.08  # порог среднего только по отриц. ставкам
MIN_NEG_RATIO       = 0.30   # минимум 30% ставок должны быть отрицательными для ЛОНГ
MIN_POS_RATIO       = 0.30   # минимум 30% ставок должны быть положительными для ШОРТ
SCAN_DAYS           = 7      # период для /analyze (скан)
SCAN_BATCH          = 20     # размер порции для скана
```

**Биржи** (`EXCHANGES_ENABLED`) — управляются через `/settings` кнопками, сбрасываются при перезапуске:
```python
EXCHANGES_ENABLED = {
    "phemex": True, "xt": True, "toobit": True, "okx": True,
    "bingx": True, "kucoin": True, "gate": True, "blofin": True, "weex": True,
}
```

## Логика фильтров

Монета анализируется в двух направлениях:

### ЛОНГ (отрицательные ставки — шорт платит лонгу)

| Шаг | Проверка |
|-----|----------|
| 1 | `neg_ratio = len(neg) / total >= 0.30` — минимум 30% ставок отрицательные |
| 2 | `below = count(rate <= -0.04%)` |
| 3 | `outlier_pct = (total - below) / total * 100` |
| ✅ full | `outlier_pct <= 25%` — стабильно |
| ⚡ partial | `neg_avg <= -0.08%` AND `neg_ratio >= 30%` — сильный но нестабильный |

### ШОРТ (положительные ставки — лонг платит шорту)

Симметричная логика: ищем ставки `>= +0.04%`, `pos_avg >= +0.08%`, `pos_ratio >= 30%`.

### Фильтр аномалий Phemex

```python
abs(float(x["fundingRate"])) < 0.01  # исключаем ставки > ±1%
```
Phemex иногда пишет технические значения `-2%` которые не являются реальными выплатами.

## Команды бота

### `/filter` — Анализ монет по фильтрам

**Пошаговый диалог:**
1. Введи монету(ы) через пробел
2. Выбери период (кнопки: `7 дней` / `14 дней` / `Другой период`)
3. Выбери биржи (мультивыбор, кнопка `Подтвердить`)

**Быстрый ввод:**
```
/filter ENJ phemex 7
/filter ENJ RON JTO phemex 14
```

**Вывод:** для каждой монеты и биржи показывает `avg`, `key_avg`, `outlier_pct`, категорию и направление (🟢 ЛОНГ / 🔴 ШОРТ).

### `/funding` — Ставки фандинга по монете

**Пошаговый диалог:**
1. Введи монету
2. Выбери период (кнопки: `1 день` / `3 дня` / `7 дней` / `14 дней` / `Другой период`)
3. Выбери биржи (мультивыбор)

**Быстрый ввод:**
```
/funding ENJ phemex 7
```

### `/calculator` — Калькулятор дохода

**Пошаговый диалог:**
1. Введи монету
2. Выбери сумму позиции (кнопки: `15000` / `20000` / `25000` / `Другое`)
3. Выбери период (кнопки: `7 дней` / `14 дней` / `Другой период`)
4. Выбери биржи (мультивыбор)

**Быстрый ввод:**
```
/calculator ENJ 25000 phemex 7
```

### `/analyze` — Скан всех монет на бирже

**Диалог:**
1. Выбери биржу кнопкой: `Phemex` / `KuCoin` / `Toobit` / `XT`
2. Запускается скан всех монет (список берётся с Phemex — 526 монет)

**Результат разбит на 4 секции:**
```
✅ 🟢 ЛОНГ — ПОДХОДЯТ   (стабильно отрицательные ставки)
✅ 🔴 ШОРТ — ПОДХОДЯТ   (стабильно положительные ставки)
⚡ 🟢 ЛОНГ — ЧАСТИЧНО   (neg_avg хороший, нестабильно)
⚡ 🔴 ШОРТ — ЧАСТИЧНО   (pos_avg хороший, нестабильно)
```

Период: `SCAN_DAYS = 7` дней. Порциями по `SCAN_BATCH = 20` монет.

### `/findpair` — Дельта-нейтральная пара

Находит лучшую связку лонг+шорт для монеты.

```
/findpair ENJ
/findpair ENJ JTO RON
/findpair ENJ 14     (с указанием дней)
```

**Диалог (если без аргументов):** введи монеты текстом.

**Логика:**
1. Загружает ставки по всем активным биржам
2. Ищет биржи где монета проходит фильтры — кандидаты для **лонга**
3. Для каждого лонга перебирает остальные биржи как кандидаты для **шорта**:
   ```
   net_income_pct = abs(long_avg) + short_avg
   ```
4. Сортировка: максимальный `net_income_pct`, при равенстве — минимальное std
5. Показывает топ-3 пары

### `/settings` — Настройки и биржи

Интерактивные кнопки:
- Нажал на биржу → ✅ включена / ❌ выключена (тогглится)
- `Все ВКЛ` / `Все ВЫКЛ`
- Показывает текущие значения фильтров

> Состояние бирж сбрасывается при перезапуске бота — не сохраняется в файл.

## Биржи и форматы символов

| Ключ | Название | Формат символа | Особенности |
|------|----------|----------------|-------------|
| `phemex` | Phemex | `.ENJUSDTFR8H` | Фильтр аномалий `abs(rate) < 0.01` |
| `xt` | XT | `enj_usdt` | lowercase |
| `toobit` | Toobit | `ENJ-SWAP-USDT` | |
| `okx` | OKX | `ENJ-USDT-SWAP` | |
| `bingx` | BingX | `ENJ-USDT` | |
| `kucoin` | KuCoin | `ENJUSDTM` | суффикс `M` |
| `gate` | Gate.io | `ENJ_USDT` | timestamp в секундах (не ms!) |
| `blofin` | BloFin | `ENJ-USDT` | только последние 100 записей |
| `weex` | WEEX | `ENJUSDT` | лимит 7 дней на запрос, несколько чанков |

## API endpoints бирж

```
Phemex (история фандинга):
GET https://api.phemex.com/api-data/public/data/funding-rate-history
  ?symbol=.ENJUSDTFR8H&start=<ms>&end=<ms>&limit=1000
Ответ: {"code":0,"data":{"rows":[{"fundingRate":"0.00005","fundingTime":1775606400000}]}}
fundingRate — строка в долях → умножить на 100 для %

Phemex (список контрактов для скана):
GET https://api.phemex.com/exchange/public/cfg/v2/products
Фильтр: type=PerpetualV2, quoteCurrency=USDT, status=Listed → 526 монет

XT:
GET https://fapi.xt.com/future/market/v1/public/q/funding-rate-record
  ?symbol=enj_usdt&page=1&size=100

Toobit:
GET https://api.toobit.com/api/v1/futures/historyFundingRate
  ?symbol=ENJ-SWAP-USDT&startTime=...&endTime=...&limit=100

OKX:
GET https://www.okx.com/api/v5/public/funding-rate-history
  ?instId=ENJ-USDT-SWAP&limit=100&before=...&after=...
Пагинация через before/after (ms timestamp)

BingX:
GET https://open-api.bingx.com/openApi/swap/v2/quote/fundingRate
  ?symbol=ENJ-USDT&limit=1000

KuCoin:
GET https://api-futures.kucoin.com/api/v1/contract/funding-rates
  ?symbol=ENJUSDTM&from=<ms>&to=<ms>
code — строка "200000", не число

Gate.io:
GET https://api.gateio.ws/api/v4/futures/usdt/funding_rate
  ?contract=ENJ_USDT&from=<sec>&to=<sec>&limit=1000
t — unix секунды (не ms!), r — в долях

BloFin:
GET https://openapi.blofin.com/api/v1/market/funding-rate-history
  ?instId=ENJ-USDT&limit=100

WEEX:
GET https://api-contract.weex.com/capi/v3/market/fundingRate
  ?symbol=ENJUSDT&startTime=<ms>&endTime=<ms>&limit=1000
Лимит 7 дней на запрос → weex_fetch разбивает на чанки
```

## Архитектура кода (bot.py)

```
bot.py
├── CONFIG
│   ├── BOT_TOKEN, DEFAULT_DAYS, пороги фильтров
│   ├── MIN_NEG_RATIO, MIN_POS_RATIO (новые)
│   ├── SCAN_DAYS=7, SCAN_BATCH=20
│   └── EXCHANGES_ENABLED, EXCHANGE_FETCHERS, EXCHANGE_LABELS
│
├── API FETCHERS (одна функция на биржу)
│   └── phemex_fetch, xt_fetch, toobit_fetch, okx_fetch,
│       bingx_fetch, kucoin_fetch, gate_fetch, blofin_fetch, weex_fetch
│       Все возвращают: list[(ts_ms, rate_pct)]
│
├── СОСТОЯНИЯ ДИАЛОГОВ
│   ├── ACF_COIN/DAYS/DAYS_NUM/EXCH = 20-23  (/filter)
│   ├── FR_COIN/DAYS/DAYS_NUM/EXCH = 30-33   (/funding)
│   ├── PC_COIN/AMT/AMT_NUM/DAYS/DAYS_NUM/EXCH = 40-45  (/calculator)
│   └── WAIT_DELTA_COIN = 7                  (/findpair)
│
├── УНИВЕРСАЛЬНЫЙ АНАЛИЗ
│   ├── get_active_exchanges(requested) → list[str]
│   ├── analyze_rates(rates_pct) → dict
│   │   # Возвращает: avg, neg_avg, pos_avg, outlier_pct,
│   │   #             category (full/partial/fail), direction (LONG/SHORT)
│   └── analyze_coin_multi(coin, start_ms, end_ms, exchanges) → dict
│
├── ПАРСИНГ
│   └── parse_tokens(text) → (coins, days, exchange)
│       # "ENJ RON /days 14 /exchange xt" → (["ENJ","RON"], 14, "xt")
│
├── ФОРМАТИРОВАНИЕ
│   ├── fmt_coin_line(coin, ex_results, active_exchanges)
│   │   # Показывает направление 🟢 ЛОНГ / 🔴 ШОРТ
│   └── build_analyze_reply(all_results, days, active_exchanges)
│
├── HELPERS ДЛЯ КНОПОК
│   ├── make_days_keyboard(cb_prefix, extra_short=False)
│   │   # extra_short=True → добавляет 1 день и 3 дня (для /funding)
│   ├── make_exchange_keyboard(cb_prefix, selected=None)
│   │   # Мультивыбор с чекбоксами + кнопка Подтвердить
│   └── make_amount_keyboard(cb_prefix)
│
├── DO-ФУНКЦИИ (бизнес-логика)
│   ├── do_analyze(update, coins, days, exchange_arg, selected_exchanges=None)
│   ├── do_show(update, coin, days, exchange_arg)
│   ├── do_calc(update, coin, amount_usd, days, exchange_arg)
│   └── do_delta(update, coins, days)
│
├── CONVERSATION HANDLERS
│   ├── acf_*  → /filter   (ACF_COIN → ACF_DAYS → ACF_EXCH)
│   ├── fr_*   → /funding  (FR_COIN → FR_DAYS → FR_EXCH)
│   ├── pc_*   → /calculator (PC_COIN → PC_AMT → PC_DAYS → PC_EXCH)
│   └── delta_* → /findpair (WAIT_DELTA_COIN)
│
├── /analyze СКАН
│   ├── cmd_analyze_start → кнопки бирж
│   ├── cmd_analyze_callback → запускает скан
│   └── phemex_get_all_symbols() → 526 монет
│
├── DELTA-NEUTRAL
│   ├── calc_std(rates) → float
│   ├── analyze_delta(coin, days, long_exchanges, all_exchanges)
│   │   → (best_pairs, exchange_data) или (None, exchange_data)
│   └── fmt_delta_result(coin, pairs, days)
│
├── /settings
│   ├── cmd_settings_new → сообщение с кнопками
│   ├── settings_callback → обработка нажатий (toggle бирж)
│   ├── make_settings_keyboard() → кнопки ✅/❌ + Все ВКЛ/ВЫКЛ
│   └── settings_text() → текст с текущими настройками
│
└── MAIN
    └── Регистрация ConversationHandlers + CallbackQueryHandlers
```

## Известные ограничения

1. Состояние бирж (`/settings`) сбрасывается при перезапуске — не сохраняется
2. WEEX ограничивает историю 7 днями на запрос
3. BloFin возвращает только последние 100 записей без пагинации
4. Telegram обрезает сообщения >4096 символов — бот нарезает на чанки
5. SyntaxWarning о `\_` в строках — косметическая проблема, на работу не влияет

---

# Бот 2: Phemex Position Tracker

## Назначение

Отслеживает открытые позиции на аккаунтах Phemex. Показывает PnL, считает точный фандинг через публичные ставки, уведомляет при изменении PnL.

## Деплой

- **Платформа:** Railway
- **Репозиторий:** `phemex-tracker-bot`
- **Build:** автоматически
- **Start:** `python bot.py`

**requirements.txt:**
```
python-telegram-bot[job-queue]>=20.0
requests
```

> ⚠️ `[job-queue]` обязательно — без него `/track` и `/notify` не работают.

## Переменные окружения (Railway)

```
BOT_TOKEN       = токен от @BotFather
MAIN_API_KEY    = API ключ главного аккаунта Phemex
MAIN_API_SECRET = API секрет главного аккаунта
SUB1_API_KEY    = API ключ субаккаунта 1
SUB1_API_SECRET = API секрет субаккаунта 1
SUB2_API_KEY    = API ключ субаккаунта 2
SUB2_API_SECRET = API секрет субаккаунта 2
NOTIFY_CHAT_ID  = (опционально) chat_id для авто-сводки в 11:02 GMT+3
```

## Аккаунты

Аккаунты называются по монете которая там торгуется:
```python
ACCOUNT_NAMES = ["MAIN", "SUB1", "SUB2"]
```
Имена задаются через переменные окружения: `MAIN_API_KEY`, `SUB1_API_KEY` и т.д.

## Phemex Auth

```python
# HMAC-SHA256
expiry    = str(int(time.time()) + 60)
sign_str  = path + query_str + expiry
signature = hmac.new(api_secret, sign_str, sha256).hexdigest()

headers = {
    "x-phemex-access-token":     api_key,
    "x-phemex-request-expiry":   expiry,
    "x-phemex-request-signature": signature,
}
```

## Команды бота

### `/positions` — все позиции
Показывает все открытые позиции на всех аккаунтах.

### `/position SUB2` — позиция конкретного аккаунта
Показывает: монету, направление, размер, плечо, цену входа, mark price, PnL в $ и %, расстояние до ликвидации.

### `/funding2` — точный расчёт фандинга (пошагово)

**Диалог:**
1. Выбери аккаунт (кнопки)
2. Введи количество дней числом (например `13`)

**Логика расчёта:**
```python
# Формула: выплата за каждую ставку
payment = size_coins × entry_price × abs(funding_rate)

# Знак:
# Лонг + отрицательная ставка → +payment (получаем)
# Лонг + положительная ставка → -payment (платим)
```

Данные берутся из публичного API:
```
GET https://api.phemex.com/api-data/public/data/funding-rate-history
  ?symbol=.RONUSDTFR8H&limit=1000
Ответ: {"fundingRate": "-0.0008", "fundingTime": 1775606400000}
```

> ⚠️ **Важно:** Phemex не даёт отдельный API только для фандинга USDT-M контрактов. Приватный endpoint `tradeAccountDetail` возвращает смешанные данные (фандинг + PnL + переводы). Поэтому для точного расчёта используются публичные ставки умноженные на размер позиции.

### `/funding SUB2 2026-03-28` — история фандинга из API

Показывает историю из `tradeAccountDetail`. Без даты — суммарное может быть неточным из-за смешанных операций.

**Правило:** указывай дату = **следующий день после открытия позиции**.

```
/funding SUB2 2026-03-28
```

Найти дату: Phemex → Фьючерсы → История позиций → дата первого начисления фандинга.

### `/track SUB2 1%` — PnL трекер

Запускает мониторинг каждые 30 минут. Уведомляет когда PnL меняется на порог от последнего уведомления.

```
/track SUB2 1%   — уведомлять при изменении ±1%
/track SUB2 2.5  — порог 2.5%
/tracks          — список активных трекеров
/untrack SUB2    — остановить трекер
/untrack all     — остановить все
```

**Логика PnL** (от цены входа):
```python
# Для лонга:
pnl_pct = (mark_price - entry_price) / entry_price * 100
```

База обновляется при каждом уведомлении.

### `/notify on/off/test` — ежедневная сводка

Сводка приходит каждый день в **11:02 GMT+3** (08:02 UTC).

```
/notify on    — включить
/notify off   — выключить
/notify test  — тест прямо сейчас
```

Для автозапуска при старте бота — добавь `NOTIFY_CHAT_ID` в Railway.

### `/mychatid` — узнать свой chat_id

Нужен для переменной `NOTIFY_CHAT_ID`.

### `/accounts` — список аккаунтов

### `/debug SUB2` — диагностика

Показывает сырые данные из API `tradeAccountDetail` — первые 3 записи и все типы операций. Используется для отладки.

## Phemex API endpoints (приватные)

```
Позиции (USDT-M):
GET /g-accounts/accountPositions?currency=USDT
Ответ: data.positions[] → фильтр size != 0
Поля: symbol, side, size, avgEntryPriceRp, markPriceRp,
      liquidationPriceRp, positionMarginRv, unRealisedPnlRv

История операций:
GET /api-data/futures/v2/tradeAccountDetail
  ?currency=USDT&limit=200&offset=0&withCount=False
Ответ: data[] → typeDesc=REALIZED_PNL (включает фандинг и другие операции)

История ордеров (для автодаты):
GET /api-data/futures/orders/closedList
  ?symbol=CAKEUSDT&ordStatus=Filled&limit=200&offset=0
или
GET /exchange/order/v2/orderList
  ?symbol=CAKEUSDT&ordStatus=Filled&limit=200&offset=0&currency=USDT
```

## Архитектура кода (phemex_bot.py)

```
phemex_bot.py
├── CONFIG
│   ├── BOT_TOKEN, ACCOUNT_NAMES, ACCOUNTS
│   ├── PHEMEX_BASE = "https://api.phemex.com"
│   ├── _pnl_trackers = {}   # {chat_id: {key: tracker_dict}}
│   └── TRACKER_INTERVAL = 30 * 60  # 30 минут
│
├── HELPERS
│   └── parse_timestamp(ts) → секунды
│       # Поддерживает ns (> 1e15), ms (> 1e12), s
│
├── PHEMEX AUTH
│   ├── phemex_sign(api_key, api_secret, path, query_str) → headers
│   └── phemex_get(api_key, api_secret, path, params) → json
│
├── PHEMEX API
│   ├── get_positions(api_key, api_secret) → list[position]
│   ├── get_funding_fees(api_key, api_secret, symbol, limit) → list[row]
│   ├── get_position_open_date(api_key, api_secret, symbol) → datetime|None
│   │   # Пробует два endpoint для поиска первого Buy ордера
│   ├── calc_funding_from_rates(symbol, size_coins, entry_price, entry_time_dt)
│   │   # → (total_usdt, payments_list)
│   │   # payments_list = [(dt, rate_pct, usdt), ...]
│   └── get_current_pnl_pct(api_key, api_secret) → list[{symbol, pnl_pct, ...}]
│
├── FORMATTERS
│   ├── fmt_position(pos, account_name) → str
│   └── fmt_funding_summary(rows, symbol, account_name, since_date=None) → str
│
├── PnL TRACKER JOB
│   └── check_pnl_trackers(context)
│       # Запускается каждые 30 минут через job_queue
│       # Проверяет все активные трекеры, отправляет уведомления
│
├── ЕЖЕДНЕВНАЯ СВОДКА
│   └── send_daily_summary(context)
│       # Запускается в 08:02 UTC (11:02 GMT+3)
│       # Суммирует фандинг за последние 24ч по всем аккаунтам
│
├── ДИАЛОГ /funding2
│   ├── Состояния: F2_CHOOSE_ACCOUNT=10, F2_CHOOSE_DAYS=11
│   ├── cmd_funding2_start → кнопки аккаунтов
│   ├── cmd_funding2_account → запрашивает кол-во дней
│   └── cmd_funding2_days → запускает расчёт
│
└── MAIN
    └── job_queue.run_repeating(check_pnl_trackers, interval=1800)
        job_queue.run_daily(send_daily_summary, time=08:02 UTC)
```

## Известные ограничения

1. `/api-data/futures/funding-fees` работает только для старых USD-маржинальных контрактов (BTCUSD, ETHUSD) — **не** для USDT-M (CAKEUSDT, RONUSDT)
2. `tradeAccountDetail` смешивает фандинг с другими операциями — для точного расчёта используй `/funding2`
3. Автодата (`get_position_open_date`) работает не всегда — зависит от доступности истории ордеров
4. Состояние трекеров (`_pnl_trackers`) сбрасывается при перезапуске бота

## Быстрое воссоздание

Если нужно передать проект другому AI-ассистенту:

> "У меня два Telegram-бота для крипто-фандинга. Первый анализирует ставки фандинга на 9 биржах и ищет монеты для дельта-нейтральных позиций. Второй отслеживает позиции на Phemex и считает точный фандинг. Оба бота задеплоены — первый на Render.com, второй на Railway. Вся архитектура, API endpoints, логика фильтров и текущее состояние кода описаны в BOTS_DOCS.md. Приложенные bot.py и phemex_bot.py — актуальные рабочие версии."

Затем приложи этот файл + `bot.py` + `phemex_bot.py`.

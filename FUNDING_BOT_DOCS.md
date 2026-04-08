# Funding Rate Analyzer Bot — Документация

> Telegram-бот для анализа исторических ставок фандинга на крипто-биржах.  
> Основная задача: находить монеты со стабильно отрицательным фандингом  
> для дельта-нейтральных позиций (лонг + шорт на разных биржах).

---

## Оглавление

1. [Технический стек](#технический-стек)
2. [Деплой](#деплой)
3. [Конфигурация](#конфигурация)
4. [Фильтры анализа](#фильтры-анализа)
5. [Биржи](#биржи)
6. [Команды бота](#команды-бота)
7. [Архитектура кода](#архитектура-кода)
8. [API endpoints бирж](#api-endpoints-бирж)
9. [Логика дельта-нейтрали](#логика-дельта-нейтрали)
10. [Логика /scan](#логика-scan)
11. [Известные ограничения](#известные-ограничения)
12. [Что планировалось добавить](#что-планировалось-добавить)

---

## Технический стек

- **Python 3.9+**
- **python-telegram-bot** — Telegram Bot API
- **requests** — HTTP запросы к биржам
- Никаких баз данных — состояние только в памяти

**Зависимости (requirements.txt):**
```
python-telegram-bot>=20.0
requests
```

---

## Деплой

### Локально
```bash
BOT_TOKEN=<твой_токен> python3 bot.py
```

### Render.com (Web Service, Free tier)
- Репозиторий: `https://github.com/astronomytest01-rgb/funding-bot`
- Build Command: `pip install -r requirements.txt`
- Start Command: `python bot.py`
- Environment Variables: `BOT_TOKEN=<токен>`

> ⚠️ Нельзя запускать бота одновременно локально и на Render — конфликт polling.

### BotFather — рекомендуемые команды (`/setcommands`):
```
analyze - Анализ монет по фильтрам
show - Все ставки по монете
calc - Калькулятор дохода
delta - Дельта-нейтраль: лонг+шорт связка
deltacalc - Калькулятор дохода по связке
scan - Полный скан всех монет Phemex
stopscan - Остановить скан
exchanges - Статус и управление биржами
toggle - Включить/выключить биржи
settings - Настройки фильтров
help - Справка
```

---

## Конфигурация

В начале `bot.py`:

```python
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")

DEFAULT_DAYS        = 7      # период анализа по умолчанию
STABILITY_THRESHOLD = -0.04  # порог ставки (%)
MAX_OUTLIER_PCT     = 25     # макс % выбросов выше порога
NEG_AVG_THRESHOLD   = -0.08  # порог среднего только по отриц. ставкам

SCAN_DAYS  = 3   # период для /scan (жёстко 3 дня)
SCAN_BATCH = 20  # размер порции для /scan
```

**Состояние бирж** (`EXCHANGES_ENABLED`) изменяется в рантайме через `/toggle` и сбрасывается при перезапуске:
```python
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
}
```

---

## Фильтры анализа

Монета проходит в одну из трёх категорий:

| Категория | Условие | Иконка |
|-----------|---------|--------|
| **ПОДХОДИТ** | `outlier_pct ≤ 25%` (стабильность) | ✅ |
| **ЧАСТИЧНО** | `neg_avg ≤ -0.08%` (сильный, но нестабильный) | ⚡ |
| **НЕ ПОДХОДИТ** | не прошла ни одно условие | ❌ |

**Расчёт `outlier_pct`:**
```python
below_threshold = count(rate <= -0.04%)  # ставок ниже порога
outlier_pct = (total - below_threshold) / total * 100
# Если outlier_pct <= 25% → монета стабильна
```

**Расчёт `neg_avg`:**
```python
neg_rates = [r for r in rates if r < 0]
neg_avg = mean(neg_rates)  # среднее только по отрицательным ставкам
```

---

## Биржи

| Ключ | Название | Формат символа | Особенности |
|------|----------|----------------|-------------|
| `phemex` | Phemex | `.ENJUSDTFR8H` | История через специальный FR endpoint |
| `xt` | XT | `enj_usdt` | lowercase |
| `toobit` | Toobit | `ENJ-SWAP-USDT` | |
| `okx` | OKX | `ENJ-USDT-SWAP` | |
| `bingx` | BingX | `ENJ-USDT` | |
| `kucoin` | KuCoin | `ENJUSDTM` | суффикс `M` |
| `gate` | Gate.io | `ENJ_USDT` | timestamp в секундах (не ms) |
| `blofin` | BloFin | `ENJ-USDT` | |
| `weex` | WEEX | `ENJUSDT` | лимит 7 дней на запрос, несколько чанков |

---

## Команды бота

### `/analyze [монеты] [/days N] [/exchange биржа]`
Анализирует монеты по фильтрам. Показывает avg, neg_avg, outlier_pct по каждой бирже.

```
/analyze ENJ RON
/analyze ENJ RON /days 14
/analyze ENJ /exchange xt
/analyze ENJ /exchange all
```

### `/show [монета] [/days N] [/exchange биржа]`
Показывает все ставки по монете с таймстампами.

```
/show ENJ
/show ENJ /days 14
/show ENJ /exchange kucoin
```

### `/calc [монета] [сумма] [/days N] [/exchange биржа]`
Калькулятор дохода от фандинга.

```
/calc ENJ 25000
/calc ENJ 25000 /days 14
```

### `/delta [монеты] [/days N]`
Дельта-нейтральный анализ. Находит лучшую связку лонг+шорт.
- Ищет биржи где монета проходит фильтры → кандидаты для лонга
- Для каждого лонга перебирает остальные биржи как кандидаты для шорта
- Критерий шорта: максимальный `net_income_pct = abs(long_avg) + short_avg`
- При равенстве — минимальное стандартное отклонение (стабильность)
- Показывает топ-3 пары

```
/delta ENJ
/delta ENJ JTO RON
/delta ENJ /days 14
```

### `/deltacalc [монета] [сумма] [/days N]`
Калькулятор дохода по дельта-нейтральной связке. Считает:
- Доход с лонга за период
- Доход/расход с шорта за период  
- Итого за период
- Итого в день

```
/deltacalc ENJ 25000
/deltacalc ENJ 25000 /days 14
/deltacalc ENJ JTO 25000 /days 30
```

> Знак шорта: если avg_short > 0 (позитивный фандинг) → шорт получает. Если avg_short < 0 → шорт платит.

### `/scan`
Полный скан всех 526 USDT-перпетуалов Phemex.
- Период: жёстко 3 дня (`SCAN_DAYS`)
- Порциями по 20 монет (`SCAN_BATCH`)
- Показывает только подходящие монеты
- Пишет прогресс после каждой порции
- Время: ~80-90 секунд на все 526 монет

### `/stopscan`
Останавливает текущий `/scan`. Работает per chat_id.

### `/exchanges`
Показывает статус всех бирж и подсказки по управлению.

### `/toggle [биржа(и) | all | none]`
Включает/выключает биржи в рантайме (сбрасывается при перезапуске).

```
/toggle xt            — переключить XT
/toggle phemex okx    — несколько сразу
/toggle all           — включить все
/toggle none          — выключить все
```

### `/settings`
Показывает текущие настройки фильтров.

---

## Архитектура кода

```
bot.py
├── CONFIG
│   ├── BOT_TOKEN, DEFAULT_DAYS, пороги фильтров
│   ├── SCAN_DAYS, SCAN_BATCH
│   └── EXCHANGES_ENABLED, EXCHANGE_FETCHERS, EXCHANGE_LABELS
│
├── API FETCHERS (одна функция на биржу)
│   ├── phemex_fetch(coin, start_ms, end_ms) → list[(ts_ms, rate_pct)]
│   ├── xt_fetch(...)
│   ├── toobit_fetch(...)
│   ├── okx_fetch(...)
│   ├── bingx_fetch(...)
│   ├── kucoin_fetch(...)
│   ├── gate_fetch(...)
│   ├── blofin_fetch(...)
│   └── weex_fetch(...)   # разбивает на 7-дневные чанки
│
├── УНИВЕРСАЛЬНЫЙ АНАЛИЗ
│   ├── get_active_exchanges(requested) → list[str]
│   ├── analyze_rates(rates_pct) → dict с avg, neg_avg, outlier_pct, category
│   └── analyze_coin_multi(coin, start_ms, end_ms, exchanges) → dict
│
├── ПАРСИНГ
│   └── parse_tokens(text) → (coins, days, exchange)
│       # Парсит: "ENJ RON /days 14 /exchange xt"
│       # Поддерживает как /days так и --days
│
├── ФОРМАТИРОВАНИЕ ВЫВОДА
│   ├── fmt_coin_line(coin, ex_results, active_exchanges)
│   └── build_analyze_reply(all_results, days, active_exchanges)
│
├── DO-ФУНКЦИИ (бизнес-логика без привязки к Telegram)
│   ├── do_analyze(update, coins, days, exchange_arg)
│   ├── do_show(update, coin, days, exchange_arg)
│   ├── do_calc(update, coin, amount_usd, days, exchange_arg)
│   └── do_delta(update, coins, days, amount_usd)
│
├── CONVERSATION HANDLERS
│   ├── analyze: analyze_start → analyze_got_coins
│   ├── show: show_start → show_got_coin
│   ├── calc: calc_start → calc_got_input
│   ├── delta: delta_start → delta_got_coins
│   └── deltacalc: deltacalc_start → deltacalc_got_input
│
├── PHEMEX SCAN
│   ├── phemex_get_all_symbols()  # /exchange/public/cfg/v2/products
│   │   # Фильтр: type=PerpetualV2, quoteCurrency=USDT, status=Listed → 526 монет
│   ├── _scan_running = {}        # флаг остановки per chat_id
│   ├── cmd_scan(update, context)
│   └── cmd_stopscan(update, context)
│
├── DELTA-NEUTRAL
│   ├── calc_std(rates) → float
│   ├── analyze_delta(coin, days, long_exchanges, all_exchanges) → (pairs, exchange_data)
│   └── fmt_delta_result(coin, pairs, days, amount_usd)
│
├── UTILITY COMMANDS
│   ├── cmd_start, cmd_help
│   ├── cmd_exchanges, cmd_toggle
│   ├── cmd_settings, cmd_cancel
│   └── unknown
│
└── MAIN
    └── Регистрация всех хендлеров + app.run_polling()
```

---

## API endpoints бирж

### Phemex
```
История фандинга:
GET https://api.phemex.com/api-data/public/data/funding-rate-history
  ?symbol=.ENJUSDTFR8H&limit=1000&offset=0
Ответ: {"code":0,"data":{"rows":[[timestamp_sec, rate_e8], ...]}}
rate_e8 → делим на 1e10 для получения %

Список контрактов (для /scan):
GET https://api.phemex.com/exchange/public/cfg/v2/products
Фильтр: type=PerpetualV2, quoteCurrency=USDT, status=Listed → 526 монет
Конвертация: ENJUSDT → ENJ → .ENJUSDTFR8H
```

### XT
```
GET https://fapi.xt.com/future/market/v1/public/q/funding-rate-record
  ?symbol=enj_usdt&page=1&size=100
Ответ: {"result":{"items":[{"fundingRate":"0.0001","settleTime":1703462400000}]}}
Символ: enj_usdt (lowercase)
```

### Toobit
```
GET https://api.toobit.com/api/v1/futures/historyFundingRate
  ?symbol=ENJ-SWAP-USDT&startTime=...&endTime=...&limit=100
Ответ: {"data":{"result":[{"fundingRate":"0.0001","fundingTime":1703462400000}]}}
```

### OKX
```
GET https://www.okx.com/api/v5/public/funding-rate-history
  ?instId=ENJ-USDT-SWAP&limit=100&before=...&after=...
Ответ: {"code":"0","data":[{"fundingRate":"0.0001","fundingTime":"1703462400000"}]}
Пагинация через before/after (ms timestamp)
```

### BingX
```
GET https://open-api.bingx.com/openApi/swap/v2/quote/fundingRate
  ?symbol=ENJ-USDT&limit=1000
Ответ: {"code":0,"data":[{"symbol":"ENJ-USDT","fundingRate":"0.0001","fundingTime":1703462400000}]}
```

### KuCoin
```
GET https://api-futures.kucoin.com/api/v1/contract/funding-rates
  ?symbol=ENJUSDTM&from=1703462400000&to=1703548800000
Ответ: {"code":"200000","data":[{"symbol":"ENJUSDTM","fundingRate":0.0001,"timepoint":1703462400000}]}
Символ: ENJUSDTM (суффикс M)
code — строка "200000", не число
```

### Gate.io
```
GET https://api.gateio.ws/api/v4/futures/usdt/funding_rate
  ?contract=ENJ_USDT&from=1703462400&to=1703548800&limit=1000
Ответ: [{"t":1703462400,"r":"0.000100"}]
t — unix секунды (не ms!), r — в долях
```

### BloFin
```
GET https://openapi.blofin.com/api/v1/market/funding-rate-history
  ?instId=ENJ-USDT&limit=100
Ответ: {"code":"0","data":[{"instId":"ENJ-USDT","fundingRate":"0.0001","fundingTime":"1703462400000"}]}
```

### WEEX
```
GET https://api-contract.weex.com/capi/v3/market/fundingRate
  ?symbol=ENJUSDT&startTime=1703462400000&endTime=1703548800000&limit=1000
Ответ: [{"symbol":"ENJUSDT","fundingRate":"0.0001","fundingTime":1703462400000}]
Лимит: 7 дней на запрос → weex_fetch разбивает на чанки по 7 дней
```

---

## Логика дельта-нейтрали

### `analyze_delta(coin, days, long_exchanges, all_exchanges)`

1. Загружаем данные по всем активным биржам
2. Ищем **лонг-кандидатов** — биржи где монета проходит фильтры:
   - `outlier_pct ≤ MAX_OUTLIER_PCT` (✅ стабильность), **или**
   - `neg_avg ≤ NEG_AVG_THRESHOLD` (⚡ частично)
3. Для каждого лонга перебираем **шорт-кандидатов** (все остальные биржи):
   ```python
   net_income_pct = abs(long_avg) + short_avg
   # long_avg отрицательный → abs() = то что получаем
   # short_avg положительный → получаем ещё
   # short_avg отрицательный → платим, но меньше чем зарабатываем на лонге
   ```
4. Сортировка шорт-кандидатов:
   - Первый приоритет: максимальный `net_income_pct`
   - Второй приоритет: минимальное стандартное отклонение (стабильность)
5. Возвращаем топ-3 пары

### Расчёт дохода (`/deltacalc`)
```python
approx_payments = days * 3  # ~3 ставки в день при 8ч интервале
income_long  = amount_usd * abs(long_avg / 100) * approx_payments
income_short = amount_usd * (short_avg / 100) * approx_payments
net_total    = income_long + income_short
net_per_day  = net_total / days
```

---

## Логика /scan

```python
# 1. Получаем список монет
GET https://api.phemex.com/exchange/public/cfg/v2/products
→ 526 монет (PerpetualV2, USDT, Listed)
→ ENJUSDT → ENJ → phemex_fetch("ENJ", start_ms, end_ms)

# 2. Период всегда SCAN_DAYS = 3 дня

# 3. Анализируем порциями по SCAN_BATCH = 20 монет
# После каждой порции:
#   - если есть подходящие → отправляем сообщение с ними
#   - если нет → тихий прогресс каждые 3 порции

# 4. Флаг остановки
_scan_running = {}  # {chat_id: bool}
# Проверяется перед каждой порцией и между монетами

# 5. Итог: список ✅ ПОДХОДЯТ + ⚡ ЧАСТИЧНО
# Сортировка по neg_avg (лучшие наверху)
```

---

## Парсинг команд (`parse_tokens`)

Парсит строку вида `BTC ETH /days 14 /exchange xt`:
- Токены без `/` → монеты (uppercase)
- `/days N` или `--days N` → период
- `/exchange X` → конкретная биржа
- Фильтрует `/analyze`, `/calc` и другие команды если попали в текст

```python
parse_tokens("ENJ RON /days 14 /exchange xt")
→ (["ENJ", "RON"], 14, "xt")

parse_tokens("ENJ")
→ (["ENJ"], 7, None)  # DEFAULT_DAYS = 7
```

---

## Известные ограничения

1. **Состояние бирж** (`/toggle`) сбрасывается при перезапуске бота — не сохраняется в файл
2. **WEEX** ограничивает историю 7 днями на запрос — для длинных периодов делает несколько запросов
3. **BloFin** возвращает только последние 100 записей без пагинации по времени — для коротких периодов ок
4. **Telegram** обрезает сообщения >4096 символов — бот нарезает на чанки, но разрыв может быть неудобным
5. **SyntaxWarning** о `\_` в f-строках — косметическая проблема, не влияет на работу
6. **/scan** занимает ~80-90 секунд на все 526 монет — нормально, пользователь видит прогресс

---

## Что планировалось добавить

### Готово обсуждалось, не реализовано:
1. **Интерактивные кнопки для бирж** — inline keyboard с ✅/❌ вместо текстовых команд
2. **Трекер позиции Phemex** — мониторинг открытых позиций через приватный API
3. **Уведомления** — если позиция в минусе на -30/-50%
4. **Сетка ордеров** — автоматическое выставление TP/SL через API (5 ордеров по 20% объёма)

### Технические улучшения:
- Сохранение состояния бирж в файл чтобы не сбрасывалось при рестарте
- Исправление SyntaxWarning в f-строках
- Добавить Bybit (отличный API, хорошая документация)
- Добавить Zoomex (API похож на Bybit)

---

## Быстрое воссоздание

Если нужно воссоздать бота с нуля, скажи AI-ассистенту:

> "Создай Telegram-бота для анализа ставок фандинга на крипто-биржах. 
> Бот должен анализировать исторические ставки на 9 биржах (Phemex, XT, Toobit, OKX, BingX, KuCoin, Gate.io, BloFin, WEEX), 
> находить монеты со стабильно отрицательным фандингом (neg_avg ≤ -0.08%, выбросов ≤ 25%), 
> искать дельта-нейтральные связки лонг+шорт, 
> и делать полный скан всех 526 USDT-перпетуалов Phemex порциями по 20 монет. 
> Все API endpoints, форматы символов и логика описаны в файле FUNDING_BOT_DOCS.md."

Затем приложи этот файл и `bot.py` как референс.

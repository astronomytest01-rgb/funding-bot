# Funding Rate Analyzer Bot — Документация

> Финальная сборка: Claude-фильтры и API + Gemini AI + вечерний отчёт.

---

## Оглавление

1. [Технический стек](#технический-стек)
2. [Деплой](#деплой)
3. [Конфигурация](#конфигурация)
4. [Фильтры анализа](#фильтры-анализа)
5. [Биржи](#биржи)
6. [Команды бота](#команды-бота)
7. [Архитектура кода](#архитектура-кода)
8. [Логика /analyze](#логика-analyze)
9. [Вечерний отчёт](#вечерний-отчёт)
10. [Известные ограничения](#известные-ограничения)

---

## Технический стек

- Python 3.11
- `python-telegram-bot[job-queue]==21.3`
- `requests==2.31.0`
- Supabase REST API для CoinW
- Gemini API `gemini-2.5-flash`

## GitHub / Railway

```text
Repository: https://github.com/astronomytest01-rgb/funding-bot
Branch: main
Railway: deploy from GitHub main
Runtime: Python 3.11
Worker command: python bot.py
```

После правок: обновить код, обновить `BOTS_DOCS.md` и `FUNDING_BOT_DOCS.md`, сделать commit, push в `main`, затем redeploy Railway.

## Деплой

```bash
BOT_TOKEN=<token> python3 bot.py
```

```text
Start Command: python bot.py
Procfile: worker: python bot.py
```

## Конфигурация

Env:

```text
BOT_TOKEN=
SUPABASE_URL=
SUPABASE_KEY=
GEMINI_API_KEY=
REPORT_CHAT_ID=
```

Параметры фильтра:

```python
STABILITY_THRESHOLD = -0.04
MAX_OUTLIER_PCT = 25
NEG_AVG_THRESHOLD = -0.08
MIN_NEG_RATIO = 0.30
MIN_POS_RATIO = 0.30
RECENT_TREND_RATES = 4
RECENT_TREND_MIN_GOOD_RATIO = 0.50
```

## Фильтры анализа

`analyze_rates` не меняется без отдельного решения.

LONG:

- минимум 30% ставок отрицательные;
- `full`, если outlier `<= 25%`;
- `partial`, если `neg_avg <= -0.08%`.

SHORT:

- минимум 30% ставок положительные;
- `full`, если outlier `<= 25%`;
- `partial`, если `pos_avg >= +0.08%`.

Фильтр последних 4 ставок:

- применяется после основного фильтра;
- LONG остаётся актуальным, если минимум 2 из последних 4 ставок отрицательные;
- SHORT остаётся актуальным, если минимум 2 из последних 4 ставок положительные;
- применяется в `/filter`, `/analyze` и вечернем отчёте.

## Биржи

Активные:

```text
Phemex, XT, Toobit, OKX, BingX, CoinW, KuCoin, Bitunix
```

Toobit scan uses native `/api/v1/exchangeInfo` `contracts`, so TradFi futures such as `GER40` and commodities such as `NG` are included even when they are absent from the Phemex universe.

CoinW берётся из Supabase таблицы `funding_rates`:

```text
symbol, rate_pct, collected_at, funding_time
```

## Open Interest validation

OI-проверка теперь является частью фильтра. Бот берёт open interest через CoinGecko Derivatives API по ID биржи, кэширует данные на 10 минут и показывает статус рядом с монетой.

Правило отображения:

- монета скрывается из `/analyze` и вечернего отчёта, если CoinGecko подтверждает OI ниже `$500,000`;
- ⚠️ если OI от `$500,000` до `$1,000,000`, CoinGecko не отдал данные или пары нет в источнике;
- ✅ если OI на конкретной бирже от `$1,000,000`;
- отсутствующие OI-данные не скрывают монету, чтобы не терять рабочие пары из-за неполного источника.

Применяется в `/analyze` и вечернем отчёте. В вечернем отчёте OI-фильтр проверяет сигнал и итоговую long/short пару, а OI показывается отдельно для long-биржи и short-биржи.

## Gemini AI

Gemini используется как фундаментальный риск-фильтр, а не как источник расчёта funding.

`/ai` анализирует актив без ставок фандинга: инфраструктура, ликвидность, волатильность, риск 30%+ движения за сутки, scam-risk.

`/analyze` передаёт в Gemini найденные монеты как AI-фильтр, а вечерний отчёт передаёт FULL-монеты как рекомендацию без удаления из отчёта. Формат bulk-ответа: `[ЭМОДЖИ] [МОНЕТА] — [причина 3-6 слов]`. Направление фиксируется из расчёта бота: 🟢 LONG, 🔴 SHORT. Если Gemini ставит неверный emoji, код исправляет его через `enforce_direction_emojis()`.

## Команды бота

- `/filter` — ручной анализ монет.
- `/funding` — история ставок.
- `/calculator` — расчёт дохода.
- `/analyze` — скан рынка + Gemini-фильтр.
- `/ai` — фундаментальный AI-анализ монеты.
- `/oi` — пошаговая проверка Open Interest монеты по выбранной бирже.
- `/findpair` — дельта-нейтральная пара.
- `/report` — ручной запуск вечернего отчёта.
- `/instruction` — инструкция входа и риск-проверок.
- `/settings` — inline-настройки бирж.
- `/help` — справка.

## Telegram сценарии

- `/filter`: монеты -> период -> мультивыбор бирж.
- `/funding`: монета -> период `1/3/7/14 дней` или ручной ввод -> биржи.
- `/calculator`: монета -> сумма `15000/20000/25000/Другое` -> период -> биржи.
- `/analyze`: биржа -> метод -> период -> batch progress -> итог -> Gemini.
- `/analyze` runs exchange fetches and OI checks off the Telegram event loop, so large scans do not freeze the bot.
- `/oi`: монета -> выбор биржи кнопкой -> рекомендация OI: 🚫 ниже $500k, ⚠️ $500k-$1m, ✅ $1m+.
- `/report`: ручной запуск вечернего отчёта в текущий чат.
- `/settings`: inline-переключение бирж и кнопки `Все ВКЛ/ВЫКЛ`.

Пошаговые кнопки и тексты Telegram считаются частью продукта, их нельзя упрощать без отдельного решения.

## Архитектура кода

```text
bot.py       - Telegram UI, handlers, кнопки, сообщения
config.py    - env vars and constants
exchanges.py - exchange API fetchers
analysis.py  - filters, recent trend, delta-neutral calculations
ai.py        - Gemini prompts and API calls
reports.py   - evening report job
```

## Логика /analyze

1. Пользователь выбирает биржу.
2. Выбирает метод: `Средняя ставка` или `Средний доход`.
3. Выбирает период.
4. Бот сканирует монеты пачками по 20.
5. Основной фильтр ищет `full`/`partial`.
6. Фильтр последних 4 ставок отбрасывает устаревшие сигналы.
7. Метод `Средний доход` учитывает обе стороны: LONG при отрицательном funding и SHORT при положительном funding.
8. Gemini получает найденные монеты и возвращает фундаментально приемлемые с emoji направления 🟢 LONG / 🔴 SHORT.

## Вечерний отчёт

Запускается в 20:00 Europe/Kyiv, если задан `REPORT_CHAT_ID`.

Берёт только `full`-монеты, применяет фильтр последних 4 ставок, затем ищет лучшую дельта-нейтральную пару, показывает OI-статус long/short бирж и оценку прибыли за 1 день и отдельным блоком даёт Gemini-рекомендацию без фильтрации. После отчёта отдельным сообщением отправляется инструкция входа: риск депозиту, OI ниже $500k скрывается, $500k-$1m помечается ⚠️, $1m+ помечается ✅; ордер до 1–1.5% OI, прибыльность 0.03-0.08%, спред до -0.5%, проверка объёма/стакана/arcways.io/Predicted Rate, TWAP $500-$1000, защита от пустого стакана, SL/TP сетка и верификация через Gemini.

## QA checklist

Перед деплоем проверять:

1. `PYTHONPYCACHEPREFIX=/private/tmp/pycache python3 -m py_compile bot.py config.py analysis.py ai.py exchanges.py reports.py oi.py`.
2. Нет потерянных импортов после разбиения `bot.py`.
3. CoinW работает через Supabase для исторических данных.
4. `/analyze` `Средний доход` ищет обе стороны: LONG и SHORT.
5. Длинный `/analyze` не блокирует Telegram polling: fetch/OI операции выполняются через `asyncio.to_thread`.
6. Trend-фильтр использует последние 4 ставки и правило 2 из 4.
7. Gemini prompt и emoji-направления работают корректно.
8. OI ниже $500k скрывается в `/analyze` и вечернем отчёте; $500k-$1m показывает ⚠️, $1m+ показывает ✅.
9. `/oi` вручную проверяет OI: монета -> биржа -> рекомендация заходить или пропустить.
10. Вечерний отчёт запускается в 20:00 Europe/Kyiv при наличии `REPORT_CHAT_ID`; `/report` запускает тот же отчёт вручную.
11. Документация обновлена перед push.

## Известные ограничения

1. Настройки бирж не сохраняются после перезапуска.
2. CoinW зависит от внешнего Supabase collector.
3. Gemini не является торговым сигналом.
4. Скан может идти несколько минут.
5. Один bot token нельзя запускать одновременно локально и на сервере.

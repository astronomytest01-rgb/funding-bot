# Документация: Funding Rate Analyzer

> Финальная версия. Последнее обновление: май 2026.

---

## Содержание

1. [Назначение](#назначение)
2. [Деплой](#деплой)
3. [Конфигурация](#конфигурация)
4. [Фильтры анализа](#фильтры-анализа)
5. [Gemini AI](#gemini-ai)
6. [Команды бота](#команды-бота)
7. [Вечерний отчёт](#вечерний-отчёт)
8. [Биржи](#биржи)
9. [Архитектура кода](#архитектура-кода)
10. [Ограничения](#ограничения)

---

## Назначение

Бот анализирует исторические ставки фандинга, ищет монеты для LONG/SHORT сбора фандинга, показывает историю ставок, считает доход, ищет дельта-нейтральные пары и добавляет Gemini AI как риск-фильтр.

Критично: API бирж, CoinW/Supabase и функция `analyze_rates` взяты из рабочей Claude-версии и являются эталоном. Gemini не меняет расчёт фандинга.

## Деплой

```text
Start Command: python bot.py
Procfile: worker: python bot.py
Python: 3.11
```

`requirements.txt`:

```text
python-telegram-bot[job-queue]==21.3
requests==2.31.0
```

Нельзя запускать один Telegram bot token одновременно локально и на сервере: polling будет конфликтовать.

## Конфигурация

Переменные окружения:

```text
BOT_TOKEN=
SUPABASE_URL=
SUPABASE_KEY=
GEMINI_API_KEY=
REPORT_CHAT_ID=
```

`GEMINI_API_KEY` нужен для `/ai`, AI-фильтра `/analyze` и AI-фильтра вечернего отчёта. `REPORT_CHAT_ID` включает вечерний отчёт.

Пороговые настройки в `config.py`:

```python
DEFAULT_DAYS = 7
STABILITY_THRESHOLD = -0.04
MAX_OUTLIER_PCT = 25
NEG_AVG_THRESHOLD = -0.08
MIN_NEG_RATIO = 0.30
MIN_POS_RATIO = 0.30
RECENT_TREND_RATES = 6
RECENT_TREND_MIN_GOOD_RATIO = 0.50
AUTO_SCAN_AMOUNT = 20000
AUTO_SCAN_THRESHOLD = 29
AUTO_SCAN_DAYS = 3
```

Активные биржи:

```text
phemex, xt, toobit, okx, bingx, coinw, zoomex
```

## Фильтры анализа

Основной фильтр `analyze_rates`:

- LONG: стабильные отрицательные ставки или сильный `neg_avg`.
- SHORT: стабильные положительные ставки или сильный `pos_avg`.
- `full`: проходит стабильность по outlier `<= 25%`.
- `partial`: средняя ставка сильная, но стабильность хуже.
- `fail`: не проходит условия.

Дополнительный фильтр актуальности тренда:

- после основного фильтра бот проверяет последние 6 ставок;
- для LONG минимум половина последних ставок должна быть отрицательной;
- для SHORT минимум половина последних ставок должна быть положительной;
- если тренд развернулся к нулю или в другую сторону, монета отбрасывается.

Этот фильтр применяется в `/filter`, `/analyze` и вечернем отчёте.

## Gemini AI

Gemini используется только после фандинг-фильтров:

- `/ai` анализирует фундаментал, инфраструктуру, ликвидность, волатильность, риск движения 30%+ за сутки и риск скама;
- `/analyze` после обычного отчёта отдаёт найденные монеты в Gemini и показывает только те, которые AI оставил для ручной проверки;
- вечерний отчёт пропускает через Gemini только FULL-монеты.

## Команды бота

```text
filter - Анализ монет по фильтрам фандинга
funding - Ставки фандинга по монете
calculator - Калькулятор дохода от фандинга
analyze - Скан рынка + Gemini AI фильтр
ai - Фундаментальный AI-анализ монеты
findpair - Дельта-нейтраль: найти пару лонг+шорт
settings - Настройки и управление биржами
help - Справка
```

`/filter`:

- пошагово: монеты -> период -> мультивыбор бирж;
- быстрый ввод: `/filter ENJ coinw 7`.

`/funding`:

- история ставок;
- периоды: 1, 3, 7, 14 дней или ручной ввод;
- мультивыбор бирж.

`/calculator`:

- монета -> сумма -> период -> биржи;
- пресеты суммы: 15000, 20000, 25000.

`/analyze`:

- биржа -> метод -> период;
- методы: `Средняя ставка`, `Средний доход`;
- после результата запускается Gemini-фильтр, если есть `GEMINI_API_KEY`.

`/ai`:

```text
/ai SOL
/ai SOL ENJ RON
```

`/findpair`:

```text
/findpair ENJ
/findpair ENJ RON 14
```

`/settings`:

- inline-кнопки включения/выключения активных бирж;
- состояние не сохраняется после перезапуска.

## Вечерний отчёт

Если задан `REPORT_CHAT_ID`, бот каждый день в 20:00 Europe/Kyiv запускает job queue задачу. На сервере время указано как 17:00 UTC.

Логика:

1. Сканирует активные биржи.
2. Берёт только `full`-монеты.
3. Применяет фильтр последних 6 ставок.
4. Прогоняет монеты через Gemini.
5. Подбирает дельта-нейтральную пару.
6. Отправляет отчёт в Telegram.

## Биржи

| Ключ | Название | Источник |
|------|----------|----------|
| `phemex` | Phemex | public funding-rate-history |
| `xt` | XT | public funding-rate-record |
| `toobit` | Toobit | public historyFundingRate |
| `okx` | OKX | public funding-rate-history |
| `bingx` | BingX | public fundingRate |
| `coinw` | CoinW | Supabase collector, table `funding_rates` |
| `zoomex` | Zoomex | Bybit-compatible funding history |

## Архитектура кода

```text
bot.py       - Telegram handlers, диалоги, кнопки, тексты команд
config.py    - env vars, фильтры, активные биржи, авто-отчёт
exchanges.py - API fetchers бирж и phemex_get_all_symbols
analysis.py  - analyze_rates, recent trend filter, delta-neutral logic
ai.py        - Gemini prompts и API helpers
reports.py   - evening auto_scan_job
```

## Ограничения

1. `/settings` хранит состояние только в памяти.
2. CoinW зависит от Supabase collector.
3. Gemini может ошибаться; это только риск-фильтр.
4. Вечерний скан может занимать несколько минут.
5. Telegram режет сообщения длиннее примерно 4096 символов, бот отправляет чанки.

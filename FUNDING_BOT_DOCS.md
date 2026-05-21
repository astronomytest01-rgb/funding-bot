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
Phemex, XT, Toobit, OKX, BingX, CoinW, Zoomex
```

CoinW берётся из Supabase таблицы `funding_rates`:

```text
symbol, rate_pct, collected_at, funding_time
```

## Команды бота

- `/filter` — ручной анализ монет.
- `/funding` — история ставок.
- `/calculator` — расчёт дохода.
- `/analyze` — скан рынка + Gemini-фильтр.
- `/ai` — фундаментальный AI-анализ монеты.
- `/findpair` — дельта-нейтральная пара.
- `/settings` — inline-настройки бирж.
- `/help` — справка.

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

Берёт только `full`-монеты, применяет фильтр последних 4 ставок, затем Gemini с сохранением направления 🟢 LONG / 🔴 SHORT, затем ищет лучшую дельта-нейтральную пару.

## Известные ограничения

1. Настройки бирж не сохраняются после перезапуска.
2. CoinW зависит от внешнего Supabase collector.
3. Gemini не является торговым сигналом.
4. Скан может идти несколько минут.
5. Один bot token нельзя запускать одновременно локально и на сервере.

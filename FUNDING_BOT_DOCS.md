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
Phemex, XT, Toobit, OKX, BingX, KuCoin
```

Временно отключены и скрыты из всех пользовательских команд и кнопок:

```text
CoinW, Bitunix
```

API-код CoinW и Bitunix сохранён. Для возврата нужно убрать ключи `coinw` и `bitunix` из `TEMPORARILY_DISABLED_EXCHANGES` и включить их в `EXCHANGES_ENABLED`.

Toobit scan uses native `/api/v1/exchangeInfo` `contracts`, so TradFi futures such as `GER40` and commodities such as `NG` are included even when they are absent from the Phemex universe.

XT, Toobit and KuCoin scans use native exchange symbol lists instead of the Phemex fallback universe.

CoinW берётся из Supabase таблицы `funding_rates`, но сейчас временно отключена в runtime:

```text
symbol, rate_pct, collected_at, funding_time
```

## Open Interest validation

OI-проверка теперь является частью фильтра. Бот берёт open interest через CoinGecko Derivatives API по ID биржи, кэширует данные на 10 минут и показывает статус рядом с монетой.

Правило отображения:

- монета скрывается из `/analyze` и вечернего отчёта, если CoinGecko подтверждает OI ниже `$500,000`;
- transient CoinGecko errors/429 do not overwrite the last valid OI cache with empty data;
- ⚠️ если OI от `$500,000` до `$1,000,000`, CoinGecko не отдал данные или пары нет в источнике;
- ✅ если OI на конкретной бирже от `$1,000,000`;
- отсутствующие OI-данные не скрывают монету, чтобы не терять рабочие пары из-за неполного источника.

Применяется в `/analyze` и вечернем отчёте. В вечернем отчёте OI-фильтр проверяет сигнал и итоговую long/short пару, а OI показывается отдельно для long-биржи и short-биржи.

## 24h Volume validation

24h volume/turnover является обязательным фильтром ликвидности для активных бирж.

Правило:

- монета скрывается из `/analyze` и вечернего отчёта, если подтверждённый оборот за 24 часа ниже `$400,000`;
- ⚠️ если оборот от `$400,000` до `$2,000,000` или данные временно недоступны;
- ✅ если оборот от `$2,000,000`;
- отсутствующие volume-данные не скрывают монету, чтобы не терять рабочие пары из-за rate-limit/API-сбоя, но в Telegram показывается `⚠️ Vol24h: нет данных`.

Источники:

- `kucoin`: native KuCoin Futures `turnoverOf24h`;
- `toobit`: native Toobit USDT-M `/quote/v1/contract/ticker/24hr`, поле `qv`;
- `xt`: native XT futures `/future/market/v1/public/q/tickers`, поле `v`;
- `phemex`: native Phemex `/md/v2/ticker/24hr/all`, поле `turnoverRv`;
- `okx`, `bingx` и fallback для остальных: CoinGecko Derivatives `volume_24h`.

Фильтр применяется в `/analyze`, вечернем отчёте и при подборе итоговой long/short пары. Команда `/oi` показывает OI и 24h volume вместе.

## Gemini AI

Gemini используется как фундаментальный риск-фильтр, а не как источник расчёта funding.

`/ai` анализирует актив без ставок фандинга: инфраструктура, ликвидность, волатильность, риск 30%+ движения за сутки, scam-risk. Команда зарегистрирована прямым `CommandHandler` до `unknown`; пошаговый режим `/ai` -> `SOL` хранит ожидание монеты в `user_data["awaiting_ai_coin"]`.

`/analyze` передаёт в Gemini найденные монеты как AI-фильтр, а вечерний отчёт передаёт FULL-монеты как рекомендацию без удаления из отчёта. `/longfunding` использует Gemini как longterm-риск-фильтр и оставляет только активы, которые выглядят достаточно серьёзными для долгосрока с плечом x3-x4: не мемкоины, не микрокапы, не scam-risk и не активы с высоким риском свечи 30-50% за день. Формат bulk-ответа для `/analyze`: `[ЭМОДЖИ] [МОНЕТА] — [причина 3-6 слов]`. Направление фиксируется из расчёта бота: 🟢 LONG, 🔴 SHORT. Если Gemini ставит неверный emoji, код исправляет его через `enforce_direction_emojis()`.

В вечернем отчёте Gemini не должен блокировать основной отчёт: bulk-рекомендация запускается через `asyncio.to_thread` с outer timeout 75 секунд на порцию до 15 монет и внутренним HTTP timeout 20 секунд. Если Gemini завис, ушёл в rate-limit или не вернул текст, бот отправляет основной отчёт и предупреждение, а не остаётся на сообщении `Gemini готовит рекомендацию`.

## Команды бота

- `/filter` — ручной анализ монет.
- `/funding` — история ставок. Зарегистрирована прямым `CommandHandler` до `unknown`; быстрый ввод `/funding ENJ phemex 7` и пошаговый режим `/funding` -> `ENJ` используют `user_data` fallback и `ApplicationHandlerStop`, чтобы не было двойного ответа с `unknown`.
- `/calculator` — расчёт дохода.
- `/analyze` — скан рынка + Gemini-фильтр.
- `/ai` — фундаментальный AI-анализ монеты.
- `/oi` — пошаговая проверка Open Interest и 24h volume монеты по одной или нескольким выбранным биржам.
- `/findpair` — дельта-нейтральная пара.
- `/report` — ручной запуск вечернего отчёта.
- `/longfunding` — долгосрочные стабильные funding-связки.
- `/instruction` — инструкция входа и риск-проверок.
- `/settings` — inline-настройки бирж.
- `/help` — справка.

## Handler safety contract

- `unknown` должен оставаться последним обработчиком команд.
- Критичные команды `/ai` и `/funding` зарегистрированы прямыми handlers до `unknown` и после обработки вызывают `ApplicationHandlerStop`.
- Если команда использует мультивыбор бирж, выбранный set должен доходить до бизнес-функции. Нельзя превращать множественный выбор в `exchange=None`, потому что это означает все активные биржи.
- Кнопка `Все биржи` является toggle: повторное нажатие снимает выделение со всех активных бирж.
- Подтверждение без выбранных бирж не запускает команду по всем биржам, а просит выбрать хотя бы одну.

## Telegram сценарии

- `/filter`: монеты -> период -> мультивыбор бирж.
- `/funding`: монета -> период `1/3/7/14 дней` или ручной ввод -> биржи.
- `/calculator`: монета -> сумма `15000/20000/25000/Другое` -> период -> биржи.
- `/analyze`: биржа -> метод -> период -> batch progress -> итог -> Gemini.
- `/analyze` runs exchange fetches and OI/volume checks off the Telegram event loop, so large scans do not freeze the bot.
- `/oi`: монета -> мультивыбор бирж кнопками -> подтверждение -> отдельная рекомендация OI и 24h volume по каждой выбранной бирже: OI 🚫 ниже $500k, ⚠️ $500k-$1m, ✅ $1m+; Vol24h 🚫 ниже $400k, ⚠️ $400k-$2m, ✅ $2m+.
- `/report`: ручной запуск вечернего отчёта в текущий чат.
- `/longfunding`: долгосрочный funding-скан -> Gemini longterm-фильтр -> summary -> первая порция монет -> кнопка `Показать ещё`; OI/24h volume показываются как warning, но не скрывают связки.
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
longterm.py  - long-term stable funding scan and 21:00 auto job
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

Берёт только `full`-монеты, применяет фильтр последних 4 ставок, скрывает подтверждённый OI ниже `$500k` и подтверждённый 24h volume ниже `$400k`, затем ищет лучшую дельта-нейтральную пару, показывает OI/Vol24h-статус long/short бирж и оценку прибыли за 1 день и отдельным блоком даёт Gemini-рекомендацию без фильтрации. После отчёта отдельным сообщением отправляется инструкция входа: риск депозиту, OI ниже $500k скрывается, $500k-$1m помечается ⚠️, $1m+ помечается ✅; Vol24h ниже $400k скрывается, $400k-$2m помечается ⚠️, $2m+ помечается ✅; ордер до 1–1.5% OI, прибыльность 0.03-0.08%, спред до -0.5%, проверка объёма/стакана/arcways.io/Predicted Rate, TWAP $500-$1000, защита от пустого стакана, SL/TP сетка и верификация через Gemini.

Отдельный долгосрочный funding-скан запускается в 21:00 Europe/Kyiv, если задан `REPORT_CHAT_ID`. Он использует ту же логику, что `/longfunding`: стабильность ставок, оценка на `$20,000` на каждую ногу, минимум `$300` в месяц, Gemini-фильтр качества актива, группировка одна монета = одно сообщение. OI и 24h volume не фильтруют результат, а выводятся как warning. Результаты сохраняются в памяти процесса на 30 минут и выдаются порциями по `LONGTERM_PAGE_SIZE`, по умолчанию 5 монет.

## QA checklist

Перед деплоем проверять:

1. `PYTHONPYCACHEPREFIX=/private/tmp/pycache python3 -m py_compile bot.py config.py analysis.py ai.py exchanges.py reports.py oi.py longterm.py`.
2. Нет потерянных импортов после разбиения `bot.py`.
3. CoinW API-код сохранён и работает через Supabase для исторических данных, но CoinW временно отключена вместе с Bitunix.
4. `/analyze` `Средний доход` ищет обе стороны: LONG и SHORT.
5. Длинный `/analyze` не блокирует Telegram polling: fetch/OI/volume операции выполняются через `asyncio.to_thread`.
6. Trend-фильтр использует последние 4 ставки и правило 2 из 4.
7. Gemini prompt и emoji-направления работают корректно; вечерний отчёт не зависает, если Gemini не ответил.
8. `/ai SOL` и `/ai` -> `SOL` оба запускают Gemini-анализ и не попадают в `unknown`.
9. `/funding ENJ phemex 7` и `/funding` -> `ENJ` оба запускают показ ставок и не попадают в `unknown`.
10. `/filter`, `/funding`, `/calculator`, `/oi` при выборе нескольких бирж используют только выбранные биржи.
11. `Все биржи` повторным нажатием снимает выделение, а подтверждение без выбора не запускает все биржи молча.
12. OI ниже $500k и 24h volume ниже $400k скрываются в `/analyze` и вечернем отчёте; OI $500k-$1m показывает ⚠️, OI $1m+ показывает ✅; Vol24h $400k-$2m показывает ⚠️, Vol24h $2m+ показывает ✅.
13. `/oi` вручную проверяет OI и 24h volume: монета -> мультивыбор бирж -> подтверждение -> рекомендация заходить или пропустить по каждой выбранной бирже.
14. Вечерний отчёт запускается в 20:00 Europe/Kyiv при наличии `REPORT_CHAT_ID`; `/report` запускает тот же отчёт вручную.
15. `/longfunding` группирует результат по монетам, показывает несколько маршрутов long/short и не фильтрует по OI/volume.
16. `/longfunding` с `GEMINI_API_KEY` фильтрует активы по долгосрочному качеству, исключая мемкоины, микрокапы и высокий риск резких свечей.
17. Кнопка `Показать ещё` отдаёт следующую порцию сохранённого результата без повторного скана.
18. Longterm auto job запускается в 21:00 Europe/Kyiv при наличии `REPORT_CHAT_ID`.
19. CoinW и Bitunix временно отключены: скрыты из кнопок и не работают через прямые команды, API-код оставлен для быстрого возврата.
20. Документация обновлена перед push.

## Известные ограничения

1. Настройки бирж не сохраняются после перезапуска.
2. CoinW зависит от внешнего Supabase collector и сейчас временно отключена вместе с Bitunix.
3. Gemini не является торговым сигналом.
4. Скан может идти несколько минут.
5. Один bot token нельзя запускать одновременно локально и на сервере.

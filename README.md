# Phemex Funding Rate Bot

## Деплой на Railway

### 1. Залей код на GitHub
1. Зайди на github.com → New repository → назови `funding-bot`
2. Загрузи все файлы (bot.py, requirements.txt, Procfile, railway.toml)

### 2. Задеплой на Railway
1. Зайди на railway.app → New Project → Deploy from GitHub repo
2. Выбери репозиторий `funding-bot`
3. После деплоя перейди в Settings → Variables
4. Добавь переменную: `BOT_TOKEN` = твой токен от BotFather

### 3. Убедись что тип сервиса — Worker
В Railway: Settings → убедись что команда запуска `python bot.py`

### Команды бота
- `/analyze BTC ETH SOL ENJ` — анализ монет
- `/analyze BTC ETH --days 14` — за 14 дней
- `/show ENJ` — все ставки по монете
- `/show ENJ --days 14` — за 14 дней
- `/settings` — текущие настройки
- `/help` — справка

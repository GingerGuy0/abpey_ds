# Розгортання на Koyeb (Worker Service)

Бот працює в режимі long polling: постійний процес без HTTP-порту. На Koyeb
цьому відповідає тип **Worker Service**. Безкоштовний інстанс Koyeb для Worker
не підходить (free-інстанси не можна використовувати як Worker і вони
засинають без трафіку), тому потрібен платний інстанс — найменшого вистачає з
запасом: боту треба ~150 МБ RAM.

---

## 1. База даних

Файлова система контейнера ефемерна: після кожного редеплою SQLite-файл
`bot.db` зникне разом з усіма підписками. Тому потрібен зовнішній Postgres.

Підійде будь-який керований Postgres (Neon, Supabase, Koyeb Managed Postgres).
Скопіюй connection string і приведи його до формату asyncpg:

```
# було
postgresql://user:pass@host/dbname?sslmode=require

# треба
postgresql+asyncpg://user:pass@host/dbname?ssl=require
```

Два обов'язкові виправлення:

- `postgresql://` → `postgresql+asyncpg://` (інакше SQLAlchemy візьме
  синхронний драйвер і впаде на старті);
- `?sslmode=require` → `?ssl=require` (asyncpg не розуміє `sslmode`).

Таблиці створюються автоматично при першому запуску (`init_db`). Міграції
запускати не треба — проєкт їх не використовує.

## 2. Створення сервісу

1. Koyeb → **Create Service** → **Worker** (не Web Service).
2. Джерело: цей GitHub-репозиторій, гілка `main`.
3. Builder: **Dockerfile** (він у корені репозиторію).
4. Instance: платний (eco-nano і вище). Регіон — будь-який, ближче до тебе.
5. **Scaling: min 1, max 1.**

Пункт про scaling критичний. Два інстанси одночасно почнуть тягнути оновлення
одним токеном, Telegram віддасть `TelegramConflictError`, і бот працюватиме
через раз — половина натискань іде в «інший» процес.

## 3. Змінні оточення

Секретні (Koyeb → Secrets):

```
BOT_TOKEN
DATABASE_URL
ETHERSCAN_API_KEY
TRONGRID_API_KEY
TONAPI_KEY
```

Звичайні:

```
OWNER_ID=<твій tg_id>
ADMIN_IDS=<інші адміни через кому>
CHANNEL_ID=-100...
ENABLED_NETWORKS=bsc,tron
BSC_RPC_URLS=https://bsc-rpc.publicnode.com,https://1rpc.io/bnb,...
LOG_LEVEL=INFO
```

Порту й `MODE` тут немає навмисно: бот працює тільки в режимі long polling,
webhook не реалізований.

`LOG_LEVEL=DEBUG` на проді не став: логи ростуть швидко, а корисного там мало.

## 4. Права бота в каналі

Бот має бути адміністратором каналу з правами:

- **Додавати учасників** (`can_invite_users`) — інакше не видасть посилання й
  не схвалить заявки на вступ;
- **Блокувати учасників** (`can_restrict_members`) — інакше не видалить тих,
  у кого підписка закінчилась.

Перевірка прав виконується при старті: якщо чогось бракує, бот напише про це
всім адмінам.

## 5. Після деплою

У логах Koyeb має з'явитись:

```
Адміни: ...
Бот запущено. Адмінів: N
Run polling for bot @your_bot
```

Швидка перевірка:

1. `/start` у боті — має з'явитись меню;
2. `/stats` — має показати статистику (значить, БД жива);
3. купівля підписки до екрана з QR — значить, гаманці й мережі налаштовані;
4. у логах немає рядків `автоматичне сканування вимкнено` — значить, RPC-ноди
   віддають `eth_getLogs` і фоновий пошук платежів працює.

## 6. Оновлення

Koyeb пересобирає образ на кожен push у гілку. Бот обробляє SIGTERM і
зупиняється коректно, тож редеплой безпечний: незавершені інвойси лишаються в
базі, а сканер підхопить платежі після старту.

---

## Локальний запуск

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env    # заповнити
python run.py
```

За замовчуванням використовується SQLite (`bot.db`) — для розробки цього
достатньо.

Тести:

```bash
pip install pytest pytest-asyncio
pytest tests/ --asyncio-mode=auto
```

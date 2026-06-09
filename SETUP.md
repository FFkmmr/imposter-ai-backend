# Imposter AI — Backend Setup

## Структура проекта

```
Imposter_AI_Api/
├── api/                  # Python/FastAPI приложение
│   ├── main.py
│   ├── models.py
│   ├── config.py
│   ├── database.py
│   ├── redis_client.py
│   ├── auth.py
│   ├── jwt_utils.py
│   ├── rate_limit.py
│   ├── seed.py
│   ├── requirements.txt
│   ├── Dockerfile
│   ├── alembic.ini
│   ├── alembic/
│   └── routers/
│       ├── users.py
│       ├── categories.py
│       ├── ai.py
│       ├── config.py
│       ├── webhooks.py
│       └── admin.py
├── docs/                 # Документация
│   ├── openapi.yaml
│   ├── seed_words.json
│   └── backend_spec.md
├── keys/                 # RSA ключи для JWT (не в git)
├── docker-compose.yml
├── .env.example
└── .gitignore
```

## Первый запуск

### 1. Сгенерировать RSA-ключи для JWT

```bash
openssl genpkey -algorithm RSA -out keys/private.pem -pkeyopt rsa_keygen_bits:2048
openssl rsa -in keys/private.pem -pubout -out keys/public.pem
```

### 2. Создать `.env`

```bash
cp .env.example .env
```

Заполнить в `.env`:
- `API_KEY` — любой случайный ключ (клиентский)
- `ADMIN_API_KEY` — другой случайный ключ (только для admin)
- `OPENAI_API_KEY` — ключ OpenAI

### 3. Запустить

```bash
docker compose up --build
```

При первом старте автоматически:
1. Применяются миграции Alembic
2. Загружаются seed-данные из `docs/seed_words.json`
3. Стартует API на `http://localhost:8000`

### Swagger UI

```
http://localhost:8000/docs
```

### Проверка

```bash
# Health
curl http://localhost:8000/health

# Создать пользователя
curl -X POST http://localhost:8000/v1/users \
  -H "Content-Type: application/json" \
  -d '{"device_id": "550e8400-e29b-41d4-a716-446655440099"}'

# Список категорий
curl http://localhost:8000/v1/categories \
  -H "X-Api-Key: YOUR_API_KEY" \
  -H "X-Device-Id: 550e8400-e29b-41d4-a716-446655440099" \
  -H "X-App-Version: 1.0.0"

# Генерация темы
curl -X POST http://localhost:8000/v1/ai/generate-theme \
  -H "X-Api-Key: YOUR_API_KEY" \
  -H "X-Device-Id: 550e8400-e29b-41d4-a716-446655440099" \
  -H "X-App-Version: 1.0.0" \
  -H "Content-Type: application/json" \
  -d '{"topic": "space", "locale": "en", "mode": "standard"}'
```

## Переменные окружения

| Переменная | Описание |
|---|---|
| `API_KEY` | Клиентский ключ (`X-Api-Key`) |
| `ADMIN_API_KEY` | Админский ключ (`X-Admin-Api-Key`) |
| `OPENAI_API_KEY` | OpenAI API key |
| `OPENAI_MODEL` | Модель (default: `gpt-4.1-mini`) |
| `POSTGRES_*` | Параметры PostgreSQL |
| `REDIS_HOST/PORT` | Redis |
| `JWT_PRIVATE_KEY_PATH` | Путь к RSA private key |
| `JWT_PUBLIC_KEY_PATH` | Путь к RSA public key |
| `ADAPTY_WEBHOOK_SECRET` | Пусто = webhook принимает всё (dev mode) |

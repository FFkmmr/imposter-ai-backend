# Known Tech Debt

Реестр известного технического долга. Каждая запись имеет ID `TD-NNN` и cross-ref из docs/кода.

| ID | Заголовок | Статус | Владелец |
|---|---|---|---|
| TD-001 | Seed-контент не соответствует контент-контракту MVP (6 локалей + description + premium) | Done | backend |
| TD-002 | Списание токенов за AI-генерацию не спроектировано (out of scope webhook-переработки) | Done | architect |
| TD-003 | api/Dockerfile запускает процесс от root (нет non-root USER) | Open | devops |
| TD-004 | Базовые образы запиннены по тегу, не по digest | Open | devops |

---

## TD-001 — Seed-контент не соответствует контент-контракту MVP

**Статус:** Done (закрыто 2026-06-09)
**Обнаружено:** 2026-06-09 (аудит соответствия ТЗ)

### Суть
Контент-контракт MVP (ТЗ §12, `backend_spec.md` §4 «Контент-контракт MVP») требует:
- 6 локалей (`en`, `ru`, `es`, `pt`, `fr`, `de`) для всех категорий и слов;
- локализованный `description` для каждой категории;
- минимум 3 premium-категории с полным наполнением.

Фактическое состояние кода/данных:
- `docs/seed_words.json` — только `en`/`ru`, 10 free-категорий, без `description`, без premium-категорий.
- `api/seed.py` грузит только `["en", "ru"]` (строка с `for locale in ["en", "ru"]`) и при INSERT категории
  записывает пустой `description` (`json.dumps({})`).

### Влияние
- API `GET /categories` отдаёт пустой `description` (fallback на `""`).
- Локали `es/pt/fr/de` не наполнены — `GET /categories/{id}/words` для них всегда уходит в fallback `en`.
- Нет premium-категорий — premium-флоу нечем проверить на реальных данных.

### План устранения (backend-агент)
1. Расширить `docs/seed_words.json`: 6 локалей во всех `civilian_word`/`impostor_word`/`name`,
   добавить локализованный `description` каждой категории, добавить ≥3 premium-категории (`is_premium: true`).
2. В `api/seed.py`: грузить все 6 локалей; писать `description` из данных файла вместо пустого `{}`.
3. Синхронизировать загрузку seed с тем, как файл монтируется в Docker (`docs/seed_words.json` → `/app/seed_words.json`).

### Cross-ref
- `docs/backend_spec.md` §1 (Category), §4 «Контент-контракт MVP»
- `docs/openapi.yaml` tag `Seed Words`
- Код: `api/seed.py`, `docs/seed_words.json`, `docker-compose.yml`

### Резолюция (2026-06-09, backend-агент)
- `docs/seed_words.json` приведён к контракту: 6 локалей (`en/ru/es/pt/fr/de`) во всех
  `name`/`description`/`civilian_word`/`impostor_word`, 13 категорий (10 free + 3 premium:
  `famous_brands`, `world_landmarks`, `mythology`), по 20 слов на категорию.
- `api/seed.py`: локали читаются из `meta.locales` (все 6), `description` пишется из данных
  категории вместо пустого `{}`.
- `api/seed_words.json` синхронизирован байт-в-байт с `docs/seed_words.json` (используется при
  локальном запуске; в Docker монтируется `docs/seed_words.json`).
- `api/routers/config.py`: `DEFAULT_LOCALIZATIONS` расширен до 6 локалей с сохранением ключей.

---

## TD-002 — Списание токенов за AI-генерацию не спроектировано

**Статус:** Done (закрыто 2026-06-10, ADR-003)
**Обнаружено:** 2026-06-10 (переработка Adapty webhook, токеномика вариант Б)

### Суть
Токеномика вариант Б (ADR-002) вводит **начисление** токенов через Adapty webhook (`POST /v1/billing/adapty/webhook`), но **списание** токенов за AI-генерацию (`POST /ai/generate-theme`) в этот scope не входит и не спроектировано.

Не определены:
- стоимость одной AI-генерации в токенах;
- поведение при нулевом балансе (hard-block `402`/`403` либо fallback на curated-слово);
- приоритет «баланс токенов» vs «`is_premium`-безлимит» (списываются ли токены у premium-пользователя);
- атомарность списания и идемпотентность повторных запросов.

### Влияние
- Webhook начисляет токены, но они пока ни на что не тратятся — баланс растёт без оттока.
- Экономика фичи неполна до проектирования списания.

### План устранения
- Отдельный ADR на модель списания токенов за AI-генерацию (architect), затем backend-реализация.

### Резолюция (2026-06-10, architect)
Модель списания спроектирована и зафиксирована в [ADR-003](adr/ADR-003-ai-token-spend.md). Закрыты все открытые пункты Q-BILL-1:
- **Стоимость:** `AI_THEME_TOKEN_COST` токенов за выдачу AI-слова (env, default `1`).
- **Поведение при нулевом/недостаточном балансе:** curated fallback `fallback_reason: insufficient_tokens` (HTTP 200), без hard-block; баланс не уходит в минус.
- **Приоритет токены vs premium:** AI premium-only; premium-пользователь платит токенами за каждую выдачу (включая отдачу из pool-cache); не-premium → `premium_required` fallback без списания.
- **Атомарность / идемпотентность:** атомарный conditional decrement на уровне строки `User` (`UPDATE ... WHERE tokens >= cost`, проверка `rowcount`) ПЕРЕД фиксацией выдачи в Redis-историю; единой ACID-транзакции Redis+DB нет, связность обеспечивается порядком шагов (decrement в Postgres ПЕРЕД `sadd device_history`). Гонки параллельных запросов одного device_id не уводят баланс в минус (`rowcount = 0` → `insufficient_tokens`).
- Контракт ответа расширен: `tokens_remaining`, `fallback_reason` += `premium_required`/`insufficient_tokens`.
- **Backend-задача:** добавить `AI_THEME_TOKEN_COST` в `config.py`/`.env.example` и реализовать списание по ADR-003.

### Cross-ref
- `docs/adr/ADR-003-ai-token-spend.md`
- `docs/backend_spec.md` §3 («Premium-gating и токеномика списания», «Порядок проверок»), §4 («Списание токенов за AI-генерацию», Q-BILL-1), §6, §10
- `docs/openapi.yaml` — `AIThemeResponse`, `POST /ai/generate-theme`
- `docs/adr/ADR-002-adapty-bearer-auth-token-economy.md`

---

## TD-003 — api/Dockerfile запускает процесс от root

**Статус:** Open
**Severity:** major (container security)
**Обнаружено:** 2026-06-10 (документирование прод-развёртывания)
**Владелец:** devops

### Суть
`api/Dockerfile` (`FROM python:3.12-slim`) не создаёт и не переключается на непривилегированного пользователя — `uvicorn` и все процессы контейнера выполняются от `root`. В проде контейнер `api` смонтирован в shared-сервере за общим Traefik (см. `docs/07-deployment.md`).

### Влияние
- При компрометации приложения процесс имеет права `root` внутри контейнера — расширяет последствия RCE/escape.
- Нарушает baseline container-security (принцип наименьших привилегий).

### План устранения (devops)
1. В `api/Dockerfile` создать системного пользователя (например `appuser`) и переключиться на него (`USER appuser`) перед `CMD`.
2. Убедиться, что смонтированные `:ro`-ключи (`/app/keys`) и рабочий каталог читаемы этим пользователем.
3. Проверить, что `alembic upgrade head` / `seed.py` / `uvicorn` стартуют без root.

### Cross-ref
- `docs/07-deployment.md` §5, §12
- Код: `api/Dockerfile`, `docker-compose.prod.yml`

---

## TD-004 — Базовые образы запиннены по тегу, не по digest

**Статус:** Open
**Severity:** minor
**Обнаружено:** 2026-06-10 (документирование прод-развёртывания)
**Владелец:** devops

### Суть
Базовые образы запиннены по подвижному тегу, а не по неизменяемому digest:
- `api/Dockerfile`: `python:3.12-slim`;
- `docker-compose.prod.yml`: `postgres:16-alpine`, `redis:7-alpine`.

При `--build`/`pull` тег может разрешиться в обновлённый образ → невоспроизводимость и риск незаметного дрейфа.

### Влияние
- Деплой не полностью воспроизводим: один и тот же commit может собраться на разных базовых образах.
- Потенциальный supply-chain-риск при подмене/обновлении тега.

### План устранения (devops)
1. Зафиксировать образы по digest (`image@sha256:...`) или ввести политику обновления digest'ов.
2. Согласовать с процессом обновления безопасности базовых образов.

### Cross-ref
- `docs/07-deployment.md` §5, §12
- Код: `api/Dockerfile`, `docker-compose.prod.yml`

# Imposter AI — Backend Specification

---

## 1. Сущности и их структура

### Category

Описывает тематическую категорию слов.

```
Category {
  id: UUID
  slug: string              // "animals", "food", "movies"
  name: LocalizedString     // { "en": "Animals", "ru": "Животные", ... }
  description: LocalizedString
  is_premium: bool
  is_active: bool
  cover_image_url: string | null
  sort_order: int
  created_at: timestamp
  updated_at: timestamp
}
```

**Логика:**
- Категории делятся на free и premium через флаг `is_premium`.
- Активность категории управляется через `is_active` — скрывает без удаления.
- `sort_order` определяет порядок отображения в клиенте.
- Все текстовые поля (`name`, `description`) хранятся как `LocalizedString` — JSON-объект с ключами по ISO locale.
- `description` обязателен и должен быть локализован на все 6 локалей MVP (см. §4 «Контент-контракт MVP»).
- В API-ответах `name`/`description` локализуются по `locale` запроса с fallback на `en` (см. `_localize_category` в `api/routers/categories.py`).

---

### WordPack

Одна запись слова, привязанная к категории и локали (плоская модель — одна строка БД = одно слово в одной локали).

```
WordPack {
  id: UUID
  category_id: UUID (FK → Category, ondelete CASCADE)
  locale: string            // "en", "ru", "es", "pt", "fr", "de"
  civilian_word: string     // слово для мирных
  impostor_word: string | null  // слово для Undercover в Party Mode; null = impostor не знает
  difficulty: "easy" | "medium" | "hard"   // enum difficulty_enum
  tags: string[]            // JSON-массив, default []
}
```

`WordEntry` в API-ответах — проекция полей `WordPack` (`id`, `civilian_word`, `impostor_word`, `difficulty`, `tags`).

**Логика:**
- Каждое слово хранится отдельной строкой под конкретную локаль — автоматического перевода нет. Поле `locale` индексировано.
- Для стандартного режима `impostor_word = null` — impostor просто не получает слово.
- Для Party Mode (Undercover) `impostor_word` содержит похожее, но отличное слово.
- При запросе клиент передаёт `locale`, сервер выбирает записи этой локали; если для категории нет записей в запрошенной локали — fallback на `en`.

---

### FeatureConfig

Управление feature flags и контент-гейтингом.

```
FeatureConfig {
  id: UUID
  key: string               // "ai_theme", "premium_categories", "party_mode_extra_roles"
  is_enabled: bool
  platform: "ios" | "android" | "all"
  min_app_version: string | null
  payload: JSON | null      // дополнительный конфиг (limits, thresholds и т.д.)
  updated_at: timestamp
}
```

**Логика:**
- Клиент получает весь конфиг одним запросом `GET /config` при старте.
- Флаги кешируются на клиенте с TTL 15–30 мин.
- `payload` может содержать, например, лимиты AI-запросов в день для free-tier.

---

### AITopicRequestLog

Лог запросов на AI-генерацию — для дебага, модерации, rate limiting.

```
AITopicRequestLog {
  id: UUID
  device_id: string         // anonymous identifier с клиента
  locale: string
  sanitized_prompt: string  // только после очистки; raw_prompt не хранится
  was_rejected: bool
  rejection_reason: string | null
  ai_response: JSON | null
  fallback_used: bool
  latency_ms: int
  created_at: timestamp
}
```

**Логика:**
- Хранится для аудита и анализа, не возвращается клиенту.
- `device_id` — анонимный, не привязан к аккаунту.
- `raw_prompt` не логируется — может содержать персональные данные пользователя.
- `was_rejected = true` если prompt не прошёл модерацию; тело ответа в этом случае — curated fallback.
- Retention: записи удаляются через 90 дней.

---

### ProcessedWebhookEvent

Ledger обработанных webhook-событий Adapty — обеспечивает идемпотентность начисления токенов (см. §4).

```
ProcessedWebhookEvent {
  event_id: string          // PK / unique — event_id из payload Adapty (или payload.id)
  event_type: string        // тип события на момент обработки
  customer_user_id: string  // = User.device_id (UUID как строка), для аудита
  tokens_granted: int       // сколько токенов начислено этим событием (0 для cancelled/expired)
  created_at: timestamp     // когда событие обработано сервером
}
```

**Логика:**
- `event_id` — первичный ключ (string, unique). Перед начислением сервер проверяет наличие записи; если есть → `200 duplicate` без повторной обработки.
- Запись создаётся **в той же транзакции**, что и обновление `User` (баланс + premium-флаги). Атомарность гарантирует, что событие не будет начислено дважды и не «потеряется».
- Таблица только пополняется (append-only); retention не ограничен в MVP (объём событий мал).
- Не возвращается клиенту — внутренняя сущность биллинга.

**Маппинг на БД (миграция `0002`):** таблица `processed_webhook_events`, `event_id VARCHAR PRIMARY KEY`, `event_type VARCHAR(64) NOT NULL`, `customer_user_id VARCHAR(64) NOT NULL`, `tokens_granted INTEGER NOT NULL DEFAULT 0`, `created_at TIMESTAMPTZ NOT NULL DEFAULT now()`.

---

### LocalizationContent

Серверные строки локализации — для контента, который нельзя обновить без релиза (paywall, onboarding, promo).

```
LocalizationContent {
  id: UUID
  key: string               // "onboarding.step1.title"
  locale: string
  value: string
  updated_at: timestamp
}
```

**Логика:**
- Только для строк, требующих серверного управления (промо, paywall-тексты, onboarding).
- Большинство строк приложения идут через нативный Localizable.strings.
- Endpoint включён в scope MVP: paywall и onboarding тексты должны меняться без релиза.

---

## 2. Логика Party Mode

### Роли

#### Civilian
- Знает слово. Цель — вычислить импостера и проголосовать за него.

#### Impostor
- Не знает слово. Цель — не спалиться и выжить до победного условия.
- Взаимоисключает Undercover: в матче либо Impostor либо Undercover на каждую "импостер-слот".
- При 2 импостерах возможна комбинация 1 Impostor + 1 Undercover.

#### Undercover
- Заменяет Impostor (см. выше).
- Получает слово похожее на слово civilians, но не то же самое.
- Задача civilians — вычислить что его слово отличается.
- Задача Undercover — убедить всех что он civilian.

#### Detective *(дополнительная роль, поверх состава)*
- Является civilian — знает слово, играет на стороне мирных.
- Абилка: может проверить одного игрока и получить подсказку является ли тот импостером.
- Начинает с 0 зарядами. Заряд добавляется каждые 2 хода. Первая проверка доступна после хода 2.
- Может быть исключён из матча в настройках.

#### Joker *(дополнительная роль, поверх состава)*
- Знает слово civilians.
- Выигрывает если: импостер был выгнан первым → после этого выгнали Joker.
- Проигрывает если дошло до ситуации 1 civilian + Joker без импостера → победа civilians.
- Требует минимум 2 civilians в матче (не считая себя) и минимум 2 импостера — иначе роль не назначается. Причина: при 1 импостере civilians побеждают сразу после его выгона и у Joker нет шанса реализовать своё условие.
- Может быть исключён из матча в настройках.

---

### Условия победы

| Сторона | Условие победы |
|---|---|
| Civilians | Все импостеры (Impostor/Undercover) выгнаны голосованием |
| Impostor/Undercover | Остались 1 на 1 с civilians (без учёта Joker) |
| Joker | Импостер выгнан первым → следующим голосованием выгнан Joker |

---

### Валидация состава матча

- Detective и Joker могут присутствовать одновременно если позволяет число игроков.
- Joker не назначается если civilians < 2 (не считая Joker).
- Detective и Joker назначаются случайно из пула civilians.
- Настройки матча позволяют включить/выключить каждую дополнительную роль отдельно.
- Double Impostor — не роль, а настройка матча (количество импостер-слотов = 2).

---

## 3. Логика AI-генерации слов

> **Premium-only + списание токенов (ADR-003):** `POST /ai/generate-theme` — premium-фича со
> списанием токенов за каждую фактическую выдачу AI-слова. Free-юзер получает curated fallback
> (`premium_required`), AI не вызывается. Полная модель — [ADR-003](adr/ADR-003-ai-token-spend.md),
> закрывает [TD-002](100-known-tech-debt.md#td-002--списание-токенов-за-ai-генерацию-не-спроектировано) / Q-BILL-1.

### Premium-gating и токеномика списания (`POST /ai/generate-theme`)

- **Доступ:** AI-генерация — premium-only. Premium = `User.is_premium == true` И (`premium_expires_at` is null ИЛИ `premium_expires_at > now`) — та же проверка, что в `GET /categories/{id}/words`. Не-premium (включая отсутствующего в БД пользователя) → curated fallback, `fallback_reason: premium_required`, HTTP 200, токены не трогаются. Клиент по reason показывает paywall.
- **Стоимость:** одна фактическая выдача AI-слова = `AI_THEME_TOKEN_COST` токенов (env, default `1`).
- **Когда списываем:** ТОЛЬКО при фактической выдаче AI-слова — как свежая LLM-генерация, так и отдача из Redis pool-cache premium-пользователю. На ЛЮБОЙ fallback (`premium_required`, `rate_limit_exceeded`, `moderation_rejected`, `insufficient_tokens`, `ai_unavailable`) токены НЕ списываются.
- **Недостаток баланса:** premium с `tokens < AI_THEME_TOKEN_COST` → curated fallback, `fallback_reason: insufficient_tokens`, HTTP 200. Баланс не уходит в минус.
- **Атомарность и гонки:** выдача AI-слова — Redis-операция (`sadd` pool/history), списание — Postgres `UPDATE`; единой ACID-транзакции Redis+DB нет. Корректность обеспечивается (а) атомарностью conditional decrement на уровне строки `User` и (б) порядком шагов (decrement ПЕРЕД фиксацией выдачи, см. «Порядок проверок»). Conditional decrement: `UPDATE users SET tokens = tokens - :cost WHERE id = :id AND tokens >= :cost`, далее проверка `rowcount`. Параллельные запросы одного `device_id` не списывают дважды сверх баланса и не уводят баланс в минус: при гонке только один `UPDATE` затронет строку. Если `rowcount = 0` (баланс уже недостаточен из-за параллельного запроса или `tokens < cost`) → fallback `insufficient_tokens`, AI-слово не отдаётся как платное и не фиксируется в device-историю.
- **Rate-limit как анти-абуз потолок:** существующий rate-limit для premium (50/24h/device) сохраняется поверх токеномики. Превышение → fallback `rate_limit_exceeded`, токены НЕ списываются. Free-tier лимит `ai_theme_free` (5/24h) фактически больше не достигается — не-premium отсекается раньше на premium-gating (`premium_required`); free-лимит остаётся в конфиге как defensive потолок и для совместимости заголовков.
- **Ответ:** в успешный ответ и в premium-fallback'и добавляется `tokens_remaining` (баланс после обработки; при успехе — уже за вычетом стоимости). Для не-premium / неизвестного устройства `tokens_remaining = null`.
- **Лог:** `AITopicRequestLog` пишется как и раньше. Списанные токены в модель логировать не обязательно (опционально; модель не меняется в этом scope).

### Порядок проверок (`POST /ai/generate-theme`) — нормативный

Финальный однозначный порядок (первое сработавшее условие определяет ответ):

1. **Валидация запроса:** пустой/несанитизируемый `topic` → `400`.
2. **Авторизация:** `X-Api-Key` (через `require_api_key`), `X-Device-Id` (валидный UUID, иначе `400`).
3. **Premium-gating:** lookup `User` по `device_id`; не premium → fallback `premium_required`, лог, return (токены не трогаем).
4. **Rate-limit (анти-абуз):** превышение premium-потолка → fallback `rate_limit_exceeded`, лог, return.
5. **Blacklist / модерация темы:** `_is_blocked(sanitized)` → fallback `moderation_rejected`, лог, return.
6. **Проверка баланса:** `tokens < AI_THEME_TOKEN_COST` → fallback `insufficient_tokens`, лог, return.
7. **Подбор AI-слова-кандидата:** из Redis pool-cache (если есть доступное неотданное слово) ИЛИ свежая LLM-генерация (+ post-moderation). На этом шаге слово ещё НЕ фиксируется в device-историю (`sadd device_history` НЕ вызывается). LLM недоступен/таймаут → fallback `ai_unavailable` (токены не списаны); все слова отмодерированы как unsafe → fallback `moderation_rejected` (токены не списаны).
8. **Атомарное списание** `AI_THEME_TOKEN_COST` — conditional decrement, проверка `rowcount` (см. «Атомарность и гонки»). Выполняется ДО фиксации выдачи. Если `rowcount = 0` (гонка/недостаток баланса) → fallback `insufficient_tokens`, device-история НЕ меняется, return.
9. **Фиксация выдачи ТОЛЬКО при `rowcount = 1`:** `sadd device_history` (Redis) + возврат AI-слова. Нормативный порядок «decrement → фиксация» исключает рассинхрон вида «слово помечено выданным в Redis-истории, но токен не списан»: пока списание не подтверждено, слово не пишется в историю.
10. **Лог** в `AITopicRequestLog` + возврат ответа с `tokens_remaining`.

> `POST /ai/generate-words` (admin) токеномикой НЕ затрагивается — admin-key, без списания токенов.

### Флоу генерации темы (`POST /ai/generate-theme`)

> **Единый источник истины — нормативный «Порядок проверок» выше** (шаги 1–10). Полный порядок проверок,
> premium-gating, проверка баланса, атомарное conditional decrement и правило «`sadd device_history`
> ТОЛЬКО при `rowcount = 1`» описаны там и здесь НЕ дублируются, чтобы исключить рассинхрон.
> Ниже — только специфика, не покрытая нормативным списком: формат LLM-промпта на шаге «Подбор
> AI-слова-кандидата» (шаг 7 нормативного порядка) и логика пула/истории.

**Подбор AI-слова-кандидата (шаг 7 нормативного порядка) — детали LLM-генерации:**

1. Сервер проверяет пул слов для данного `sanitized_prompt + locale`:
   - Из пула исключаются слова, уже выданные этому `device_id` в `device_history` (TTL 30 дней).
   - Если остались доступные слова → одно случайное из них становится кандидатом (это и есть отдача из pool-cache; тарифицируется по нормативному порядку).
   - Если все слова пула уже были выданы этому устройству → переходим к свежей LLM-генерации (следующий пункт).
2. Если нет доступных слов в пуле → формирует системный промпт для LLM:
   - Язык: `locale`
   - Режим: standard (один civilian_word) или party (civilian_word + impostor_word)
   - Требование: короткое, безопасное, party-friendly, без NSFW
   - Формат ответа: JSON
   - Guardrails против NSFW/hate/illegal встроены в системный промпт
3. Вызов LLM. Таймаут: 5 секунд. Таймаут/ошибка → fallback `ai_unavailable` (токены НЕ списаны).
4. Парсинг и валидация ответа LLM.
5. Post-generation проверка через OpenAI Moderation API (отдельный вызов — даёт точный сигнал для логирования и аудита). Все кандидаты unsafe → fallback `moderation_rejected` (токены НЕ списаны).
6. Новые слова добавляются в пул для данной темы; кандидатом становится одно случайное из **только что полученных** новых слов.

> На этом шаге слово-кандидат ещё НЕ фиксируется в `device_history` и НЕ тарифицируется. Далее — строго по
> нормативному «Порядку проверок»: **атомарное conditional decrement** (`UPDATE ... WHERE tokens >= :cost`,
> проверка `rowcount`) выполняется **ПЕРЕД** фиксацией выдачи; **`sadd device_history` вызывается ТОЛЬКО при
> `rowcount = 1`**. При `rowcount = 0` (гонка/недостаток баланса) Redis-история НЕ меняется, слово не отдаётся
> как платное → fallback `insufficient_tokens`. Затем лог `AITopicRequestLog` и ответ с `tokens_remaining`.

**Логика пула слов:**
- Пул хранится в Redis с ключом `topic_pool:{sanitized_prompt}:{locale}`. Без TTL — накапливается со временем и переиспользуется разными пользователями.
- История выданных слов хранится на сервере в Redis с ключом `device_history:{device_id}:{sanitized_prompt}:{locale}`. TTL: 30 дней — после этого история сбрасывается и слова снова доступны.
- При каждом запросе сервер сам исключает из пула слова уже выданные этому устройству — клиент ничего дополнительно не передаёт.

**Fallback-цепочка** (на всех — `fallback_used: true`, HTTP 200, токены НЕ списываются):
- `premium_required` → пользователь не premium; AI не вызывается, curated fallback (клиент показывает paywall).
- `rate_limit_exceeded` → превышен анти-абуз потолок (premium 50/24h); curated fallback из default категории.
- `moderation_rejected` → тема в blacklist или ответ LLM не прошёл модерацию; curated fallback из default категории.
- `insufficient_tokens` → premium, но баланс `< AI_THEME_TOKEN_COST`; curated fallback из default категории.
- `ai_unavailable` → LLM timeout (5 сек) / ошибка; случайный word из категории.
- `500` только если вообще нет контента для fallback (не должно происходить в production).

---

### Флоу генерации слов по теме (`POST /ai/generate-words`)

Аналогично generate-theme, но вместо одного слова — список из N слов для полного WordPack.
- Используется для admin-панели и контент-команды в MVP.
- В Phase 2 планируется как клиентский эндпоинт.
- Лимит: 20–30 слов на запрос.

---

## 4. Логика premium / paywall и токеномика (вариант Б)

> **Архитектурное решение:** premium-доступ и начисление токенов построены на **Adapty webhook + серверный флаг `User.is_premium` + баланс `User.tokens`**, а не на Apple App Store Server API. Базовое решение «используем Adapty» — [ADR-001](adr/ADR-001-adapty-instead-of-apple-server-api.md). Переход на **bearer-авторизацию webhook (вместо HMAC) и реальную токеномику (вариант Б)** — [ADR-002](adr/ADR-002-adapty-bearer-auth-token-economy.md). Apple App Store Server API, эндпоинт `POST /purchase/validate` и entitlement-токен (`X-Entitlement-Token`, RS256 JWT) **не используются**. HMAC-верификация (`Adapty-Signature`) **отменена** (superseded ADR-002).

### Эндпоинт

`POST /v1/billing/adapty/webhook` — единственный механизм активации/деактивации premium и начисления токенов за подписку. Приложение использует префикс `/v1` (см. `api/main.py`), поэтому **финальный путь — `/v1/billing/adapty/webhook`**. Старый `POST /v1/webhooks/adapty` удалён.

### Авторизация (bearer-token, не HMAC)

- Заголовок: `Authorization: Bearer <ADAPTY_WEBHOOK_SECRET>`. Adapty **не подписывает** payload — секрет статический, из env `ADAPTY_WEBHOOK_SECRET`.
- Сравнение секрета — **constant-time**. Неверный/отсутствующий токен → `401`.
- Если `ADAPTY_WEBHOOK_SECRET` не задан в env → `500` с понятным текстом (сервер не сконфигурирован), а не молчаливый пропуск проверки.
- Путь **исключён из любой глобальной `X-Api-Key` защиты** (Adapty не знает service key). На уровне реализации webhook-роутер не подключает `require_api_key`; при введении глобального API-key middleware этот путь обязан быть в allowlist.

### Дефенсивный парсинг payload

Поля разбросаны по версиям Adapty SDK — извлекаются с приоритетом (первое непустое):

| Поле | Источник (по приоритету) |
|---|---|
| `event_id` | `payload.event_id` → `payload.id` |
| `event_type` | `payload.event_type` (приводится к **lowercase**) |
| `customer_user_id` | `payload.customer_user_id` → `payload.profile.customer_user_id` → `payload.user_id` |
| `vendor_product_id` | `payload.event_properties.vendor_product_id` → `payload.event_properties.product_id` → `payload.vendor_product_id` → `payload.product_id` |
| `expires_at` (опц., ISO 8601) | `payload.event_properties.expires_at` → `payload.profile.expires_at` |

`customer_user_id` совпадает с идентификатором пользователя приложения — `User.device_id` (UUID). На клиенте это `X-Device-Id` (Apple `identifierForVendor`), он же передаётся в `Adapty.identify(...)`.

### Толерантность к payload (пробный пинг / кривое тело)

После успешной авторизации **любой** некорректный payload → **HTTP 200** с телом `{"status":"ignored","reason":...}` (не `400`/`5xx`):

| Случай | Ответ |
|---|---|
| Пустое тело | `{"status":"ignored","reason":"empty_body"}` |
| Невалидный JSON | `{"status":"ignored","reason":"invalid_json"}` |
| JSON не объект | `{"status":"ignored","reason":"not_an_object"}` |
| Нет `event_id` | `{"status":"ignored","reason":"missing_event_id"}` |
| Неизвестный `event_type` | `{"status":"ignored","event_type":<type>}` |
| Нет `customer_user_id` | `{"status":"ignored","reason":"missing_customer_user_id"}` |
| Валидное событие обработано | `{"status":"applied","event_type":<type>,"tokens_granted":<n>}` |
| Повтор `event_id` | `{"status":"duplicate"}` |

`5xx` возвращается **только** при реальном внутреннем сбое (например, БД недоступна). Adapty ретраит любой не-2xx ответ бесконечно — поэтому пробные пинги и кривые payload отдают `200 ignored`, а не ошибку.

### События (4) и эффект

| `event_type` | `is_premium` | Токены |
|---|---|---|
| `subscription_started` | `true` | начислить по тиру `vendor_product_id` |
| `subscription_renewed` | `true` | начислить по тиру `vendor_product_id` |
| `subscription_cancelled` | `false` | **не трогаем** |
| `subscription_expired` | `false` | **не трогаем** |

**`expires_at` — обновление `User.premium_expires_at` (Q-BILL-2):** при `subscription_started` / `subscription_renewed` поле `premium_expires_at` обновляется **только если `expires_at` реально извлечён из payload** (непустой и валидно распарсенный ISO 8601). Если `expires_at` в payload **отсутствует** (или не распарсился) → прежнее значение `premium_expires_at` **СОХРАНЯЕТСЯ** (НЕ обнуляется в `null`). Это предотвращает потерю даты окончания подписки при `renewed`-событии без `expires_at` (что иначе сделало бы premium «бессрочным до now is None» и сломало бы проверку истечения в gating). Деактивационные события (`cancelled`/`expired`) `premium_expires_at` не трогают вовсе.

### Тиры начисления токенов (env-маппинг)

SKU и гранты вынесены в env, чтобы менять без пересборки:

| `vendor_product_id` (из env) | Грант токенов (из env) |
|---|---|
| `SUBSCRIPTION_PRODUCT_WEEKLY` | `SUBSCRIPTION_TOKENS_WEEKLY` |
| `SUBSCRIPTION_PRODUCT_YEARLY` | `SUBSCRIPTION_TOKENS_YEARLY` |
| любой неизвестный `product_id` | fallback `SUBSCRIPTION_TOKENS_GRANT` |

### Идемпотентность (обязательно)

- Перед начислением сервер проверяет, обработан ли `event_id`, по ledger-таблице обработанных событий (`ProcessedWebhookEvent`, см. §1).
- Если `event_id` уже есть → `200 {"status":"duplicate"}` **без повторного начисления**.
- Обновление баланса (`User.tokens`, `is_premium`, `premium_expires_at`) **и** запись факта обработки в `ProcessedWebhookEvent` выполняются в **одной транзакции** (атомарно). Сбой записи ledger откатывает начисление.

### ENV-переменные (биллинг)

Backend обязан добавить в `api/config.py` (`Settings`) и в `.env.example`:

| ENV | Тип | Назначение |
|---|---|---|
| `ADAPTY_WEBHOOK_SECRET` | string | Bearer-секрет для авторизации webhook. Обязателен в проде; если пуст → webhook отвечает `500`. |
| `SUBSCRIPTION_PRODUCT_WEEKLY` | string | SKU недельной подписки (matchится с `vendor_product_id`). |
| `SUBSCRIPTION_PRODUCT_YEARLY` | string | SKU годовой подписки. |
| `SUBSCRIPTION_TOKENS_WEEKLY` | int | Сколько токенов начислять за недельную подписку. |
| `SUBSCRIPTION_TOKENS_YEARLY` | int | Сколько токенов начислять за годовую подписку. |
| `SUBSCRIPTION_TOKENS_GRANT` | int | Fallback-грант для неизвестного `product_id`. |

> Семантика `ADAPTY_WEBHOOK_SECRET` меняется относительно ADR-001: раньше это был HMAC shared secret, теперь — статический bearer-токен. Старое поведение «пустой секрет → пропуск проверки» **отменено**: пустой секрет теперь даёт `500`.

### Premium-gating и источник истины

- При запросе premium-контента backend идентифицирует пользователя по `X-Device-Id`, читает `User.is_premium` + `User.premium_expires_at` из БД. Токен entitlement клиент не передаёт.
- Источник истины о статусе подписки и балансе токенов на сервере — `User` (`is_premium`, `premium_expires_at`, `tokens`). Клиент читает актуальное состояние через `GET /users/me`.
- Adapty — единый proxy-слой подписок. Backend не обращается к Apple напрямую и не валидирует receipt самостоятельно.

### Списание токенов за AI-генерацию

**Списание** токенов за AI-генерацию спроектировано и зафиксировано в [ADR-003](adr/ADR-003-ai-token-spend.md) (закрывает Q-BILL-1 / [TD-002](100-known-tech-debt.md#td-002--списание-токенов-за-ai-генерацию-не-спроектировано)). Модель: `POST /ai/generate-theme` — premium-only; одна фактическая выдача AI-слова стоит `AI_THEME_TOKEN_COST` (env, default 1); атомарное списание; недостаток баланса / не-premium / любой fallback → curated word без списания. Полная логика и порядок проверок — §3 «Premium-gating и токеномика списания».

### ENV-переменная (списание токенов, ADR-003)

| ENV | Тип | Назначение |
|---|---|---|
| `AI_THEME_TOKEN_COST` | int | Стоимость одной фактической выдачи AI-слова в `POST /ai/generate-theme`. Default `1`. Списывается только при выдаче AI-слова (не на fallback). |

<a id="open-questions-billing"></a>
### Open questions (billing)

- **Q-BILL-1:** *(Закрыт 2026-06-10, ADR-003.)* Модель списания токенов за AI-генерацию определена: premium-only, стоимость `AI_THEME_TOKEN_COST` (default 1), атомарное списание только при выдаче AI-слова, любой fallback без списания. См. §3, [ADR-003](adr/ADR-003-ai-token-spend.md), `100-known-tech-debt.md#TD-002` (Done).
- **Q-BILL-2:** *(Закрыт 2026-06-10.)* При `subscription_started`/`subscription_renewed` `premium_expires_at` обновляется только при наличии `expires_at` в payload; при отсутствии — прежнее значение сохраняется (не обнуляется). См. §4 «`expires_at` — обновление `User.premium_expires_at`». Примечание в [ADR-002](adr/ADR-002-adapty-bearer-auth-token-economy.md) (Amendment).

---

## 4. Логика категорий и контента

### Контент-контракт MVP (ТЗ §12)

Требования к наполнению seed-базы (`docs/seed_words.json`) для релиза MVP. Реализация — задача backend-агента.

- **6 локалей MVP:** `en`, `ru`, `es`, `pt` (Brazil), `fr`, `de`. Все категории и все слова (`civilian_word`, `impostor_word`) обязаны иметь переводы на все 6 языков.
- **`description` категории** — локализованный объект на все 6 локалей; обязателен для каждой категории.
- **Минимум 3 premium-категории** (`is_premium: true`) с полным наполнением словами на все 6 локалей.
- Источник seed — `docs/seed_words.json`; в Docker монтируется в контейнер как `/app/seed_words.json` (`docker-compose.yml`). `api/seed_words.json` — синхронизированная копия (байт-в-байт) для локального запуска вне Docker. Загрузка — `api/seed.py` (idempotent, локали читаются из `meta.locales`).
- **Статус:** контракт выполнен — 13 категорий (10 free + 3 premium), 6 локалей во всех `name`/`description`/`civilian_word`/`impostor_word`, по 20 слов на категорию. См. `100-known-tech-debt.md#TD-001` (Done).

### `GET /categories`
- Возвращает все активные категории с признаком `is_premium`.
- Клиент сам решает, показывать ли lock-иконку на основе статуса `is_premium` пользователя (из `GET /users/me`).
- Пагинация не нужна в MVP (ожидается < 50 категорий).
- `preview_words` отсутствует — только мета-информация. Preview только в `/categories/premium`.
- TTL кеша на клиенте: 30 мин. Server-side: `Cache-Control: max-age=1800`.

### `GET /categories/premium`
- Возвращает только premium-категории с `preview_words` (до 3 слов для paywall-экрана).
- Используется на paywall-экране для preview доступного контента.

### `GET /categories/{category_id}/words`
- Premium-gating через серверный флаг `User.is_premium`: backend идентифицирует пользователя по `X-Device-Id` (lookup `User`), проверяет `is_premium == true` **и** (`premium_expires_at` is null **или** `premium_expires_at > now`).
- Для free-категорий (`is_premium == false`) проверка не выполняется — слова доступны всем с валидным `X-Api-Key`.
- Если категория premium, а у пользователя нет активной подписки (или пользователь не найден) → `403`. Токен entitlement не используется.
- Клиент загружает слова пачкой при старте раунда (не поштучно).

### Выбор WordPack при старте раунда
1. Клиент знает `category_id` и `locale`.
2. Для offline-категорий слова бандлятся в приложение — сеть не нужна.
3. Для premium online-категорий — запрос к API при наличии интернета.
4. При отсутствии сети для premium — клиент показывает ошибку, не ломает flow.

---

## 5. Логика Remote Config

### `GET /config`
Отдаёт единый конфиг приложения:
- список активных feature flags
- минимальная версия приложения (force update)
- промо-баннеры / A/B эксперименты
- лимиты AI-запросов по тарифам
- сроки seasonal контента

**Логика:**
- Запрашивается при каждом cold start.
- Кешируется локально с TTL 15–30 мин.
- При недоступности сервера → клиент использует last known good config.
- Критические флаги (force update) должны проверяться до входа в игру.

---

## 6. Rate Limiting

| Endpoint | Free | Premium |
|---|---|---|
| `POST /ai/generate-theme` | premium-only → `premium_required` fallback (free до rate-limit не доходит) | 50 / 24h / device (анти-абуз потолок поверх токеномики) |
| `POST /ai/generate-words` | только admin | только admin |
| `GET /categories/{id}/words` | 100 / 24h / device | unlimited |
| `GET /categories` | unlimited | unlimited |
| `GET /config` | unlimited | unlimited |

**Логика:**
- Rate limit по `device_id` (anonymous), не по IP.
- Окно: rolling 24 hours (не сброс в midnight UTC).
- При превышении на `POST /ai/generate-theme` → **HTTP 200** + тело с `fallback_used: true` + curated слово (`fallback_reason: rate_limit_exceeded`) — тихий fallback, эндпоинт НИКОГДА не возвращает 429 (ADR-003, единый принцип «тихий fallback»). Клиент обрабатывает как успех. Токены не списываются. Заголовки `X-RateLimit-*` присутствуют и в fallback-ответе.
- `POST /ai/generate-theme` — premium-only (ADR-003): не-premium отсекается на premium-gating (`premium_required`) ДО rate-limit, поэтому free-лимит `ai_theme_free` (5/24h) фактически не достигается; он остаётся в конфиге как defensive потолок. Для premium rate-limit (50/24h) — анти-абуз потолок поверх списания токенов.
- Хранилище: Redis.

**Rate limit заголовки в ответе** (для всех rate-limited эндпоинтов):
```
X-RateLimit-Limit: 5
X-RateLimit-Remaining: 2
X-RateLimit-Reset: 1717689600   // Unix timestamp, когда освободится первый слот (rolling window)
```

---

## 7. Контент-модерация

**Слои защиты:**
1. **Client-side**: ограничение длины prompt (80 символов), базовый UI hint.
2. **Server blacklist**: список запрещённых слов/паттернов на regex.
3. **System prompt guardrails**: инструкции LLM отказать на NSFW/hate/illegal — встроены в системный промпт каждого запроса.
4. **Post-generation check**: ответ LLM проверяется через OpenAI Moderation API (отдельный вызов после генерации).
5. **Logging**: все rejected requests логируются в `AITopicRequestLog` для аудита.

**Поведение при отказе:**
- Клиент получает валидный ответ 200 с `fallback_used: true`.
- Никогда не отдавать сообщение "запрос заблокирован" в клиент как ошибку — только тихий fallback.

---

## 8. Описание эндпоинтов

**Base URL:** `https://api.imposterai.app/v1`

**Аутентификация:**
- Клиентские запросы: API Key в заголовке `X-Api-Key` (пользователи анонимны).
- Admin запросы: `X-Admin-Api-Key`.
- Premium-доступ: серверный флаг `User.is_premium`, определяемый по `X-Device-Id`. Отдельного entitlement-токена нет.

**Общие заголовки запроса:**
```
X-Api-Key: <app_api_key>
X-Device-Id: <anonymous_uuid>       // генерируется при первом запуске, хранится локально
X-App-Version: 1.0.0
X-Locale: ru                        // ISO 639-1
```

---

### `GET /categories`

**Описание:** Возвращает список всех активных категорий (free + premium) для отображения в пикере.

**Query params:**
| Param | Type | Required | Description |
|---|---|---|---|
| `locale` | string | no | ISO locale. Default: `en` |

**Response 200:**
```json
{
  "categories": [
    {
      "id": "uuid",
      "slug": "animals",
      "name": "Animals",
      "description": "Guess the animal",
      "is_premium": false,
      "cover_image_url": "https://cdn.../animals.png",
      "sort_order": 1
    },
    {
      "id": "uuid",
      "slug": "cinema",
      "name": "Cinema",
      "description": "Movies & TV",
      "is_premium": true,
      "cover_image_url": "https://cdn.../cinema.png",
      "sort_order": 10
    }
  ]
}
```

**Errors:** `500` — внутренняя ошибка

**Кеш:** `Cache-Control: max-age=1800` (30 мин)

---

### `GET /categories/premium`

**Описание:** Возвращает только premium категории. Используется на paywall-экране.

**Query params:** те же, что у `/categories`

**Response 200:**
```json
{
  "categories": [
    {
      "id": "uuid",
      "slug": "cinema",
      "name": "Cinema",
      "description": "Movies & TV",
      "is_premium": true,
      "cover_image_url": "https://cdn.../cinema.png",
      "sort_order": 10,
      "preview_words": ["Matrix", "Avatar"]
    }
  ]
}
```

`preview_words` — 2–3 слова для preview на paywall. Не весь пак.

---

### `GET /categories/{category_id}/words`

**Описание:** Возвращает слова для конкретной категории. Для premium-категорий проверяет серверный флаг `User.is_premium` по `X-Device-Id`. Клиент загружает слова пачкой при старте раунда.

**Path params:**
| Param | Type | Description |
|---|---|---|
| `category_id` | UUID | ID категории |

**Query params:**
| Param | Type | Required | Description |
|---|---|---|---|
| `locale` | string | no | ISO locale. Fallback: `en` |
| `mode` | string | no | `standard` или `party`. Default: `standard` |
| `count` | int | no | Количество слов для раунда. Default: 1, Max: 5 |

**Headers:** `X-Api-Key` (обязателен) + `X-Device-Id` (обязателен — по нему определяется premium-статус для premium-категорий).

**Response 200:**
```json
{
  "category_id": "uuid",
  "locale": "en",
  "mode": "standard",
  "words": [
    {
      "id": "uuid",
      "civilian_word": "Elephant",
      "impostor_word": null,
      "difficulty": "easy"
    }
  ]
}
```

**Errors:**
- `403` — категория premium, а у пользователя нет активной подписки (`is_premium == false`, подписка истекла, или пользователь не найден)
- `404` — категория не найдена
- нет паков для запрошенной локали → сервер возвращает fallback `en`, не ошибку

---

### `POST /ai/generate-theme`

**Описание:** Генерирует слово / пару слов по пользовательской теме через LLM. **Premium-only** фича со списанием токенов (см. §3 «Premium-gating и токеномика списания», [ADR-003](adr/ADR-003-ai-token-spend.md)). Не-premium получает curated fallback (`premium_required`); каждая фактическая выдача AI-слова списывает `AI_THEME_TOKEN_COST` токенов.

**Request body:**
```json
{
  "topic": "космос",
  "locale": "ru",
  "mode": "standard"
}
```

| Field | Type | Required | Description |
|---|---|---|---|
| `topic` | string | yes | Пользовательская тема, max 80 символов |
| `locale` | string | yes | ISO locale |
| `mode` | string | no | `standard` (1 civilian word) / `party` (civilian + impostor word). Default: `standard` |

**Response 200 (standard, premium — токен списан):**
```json
{
  "locale": "ru",
  "topic": "космос",
  "civilian_word": "Марс",
  "impostor_word": null,
  "difficulty": "medium",
  "is_safe": true,
  "fallback_used": false,
  "fallback_reason": null,
  "tokens_remaining": 199
}
```

**Response 200 (party mode, premium — токен списан):**
```json
{
  "locale": "ru",
  "topic": "космос",
  "civilian_word": "Марс",
  "impostor_word": "Луна",
  "difficulty": "medium",
  "is_safe": true,
  "fallback_used": false,
  "fallback_reason": null,
  "tokens_remaining": 199
}
```

**Response 200 (fallback — токены НЕ списаны):**
```json
{
  "locale": "ru",
  "topic": "космос",
  "civilian_word": "Земля",
  "impostor_word": null,
  "difficulty": "easy",
  "is_safe": true,
  "fallback_used": true,
  "fallback_reason": "premium_required",
  "tokens_remaining": null
}
```

`fallback_reason` варианты: `"premium_required"` | `"insufficient_tokens"` | `"rate_limit_exceeded"` | `"moderation_rejected"` | `"ai_unavailable"`.

`tokens_remaining` — баланс токенов после обработки (при успехе уже за вычетом `AI_THEME_TOKEN_COST`; на fallback токены не списываются — для premium это текущий баланс, для не-premium/неизвестного устройства `null`).

**Errors:**
- `400` — `topic` пустой или превышает лимит
- `503` — LLM или внешний сервис временно недоступен (клиент получает fallback, не ошибку)

> **Этот эндпоинт НИКОГДА не возвращает `429`** (ADR-003). Превышение rate-limit → **HTTP 200** + `fallback_used: true`, `fallback_reason: rate_limit_exceeded` (см. «Fallback-цепочка»). Заголовки `X-RateLimit-*` присутствуют в ответе.

**Rate limit headers** включены в каждый ответ:
```
X-RateLimit-Limit: 5
X-RateLimit-Remaining: 0
X-RateLimit-Reset: 1717689600
```

---

### `POST /ai/generate-words` *(admin / content team)*

**Описание:** Генерирует полный набор слов по теме для наполнения WordPack. Только для internal use в MVP. В Phase 2 планируется как клиентский эндпоинт.

**Auth:** Bearer JWT (admin)

**Request body:**

| Field | Type | Required | Description |
|---|---|---|---|
| `topic` | string | yes | Тема для генерации |
| `locale` | string | yes | ISO locale |
| `count` | int | no | Желаемое количество слов. Min: 10, Default: 20, Max: 30. Если LLM вернул меньше 10 — сервер автоматически повторяет запрос и добирает до минимума без участия клиента |
| `mode` | string | no | `standard` или `party`. Default: `standard` |
| `difficulty` | string | no | `easy` / `medium` / `hard` |

```json
{
  "topic": "Space exploration",
  "locale": "en",
  "count": 20,
  "mode": "party",
  "difficulty": "medium"
}
```

**Response 200:**
```json
{
  "locale": "en",
  "topic": "Space exploration",
  "words": [
    {
      "civilian_word": "Mars",
      "impostor_word": "Moon",
      "difficulty": "medium",
      "is_safe": true
    },
    {
      "civilian_word": "Rocket",
      "impostor_word": "Satellite",
      "difficulty": "easy",
      "is_safe": true
    },
    ...
  ],
  "total": 19,
  "unsafe_filtered": 1
}
```

---

### `GET /config`

**Описание:** Возвращает полный remote config приложения. Запрашивается при cold start и периодически обновляется.

**Response 200:**
```json
{
  "features": {
    "ai_theme": { "enabled": true, "free_daily_limit": 5, "premium_daily_limit": 50 },
    "premium_categories": { "enabled": true },
    "party_mode_extra_roles": { "enabled": true, "is_premium": false },
    "import_custom_words": { "enabled": true }
  },
  "content": {
    "default_free_categories": ["animals", "food", "movies", ...],
    "featured_category_slug": "cinema"
  },
  "app": {
    "min_supported_version": "1.0.0",
    "force_update_version": null,
    "maintenance_mode": false
  },
  "updated_at": "2026-06-06T12:00:00Z"
}
```

**Кеш:** `Cache-Control: max-age=900` (15 мин)

---

### `GET /localizations`

**Описание:** Серверные строки локализации для контента, который требует обновления без релиза приложения (paywall тексты, промо, onboarding). Включён в scope MVP.

**Query params:**
| Param | Type | Required | Description |
|---|---|---|---|
| `locale` | string | yes | ISO locale |
| `keys` | string | no | Comma-separated список нужных ключей. Если пусто — все |

**Response 200:**
```json
{
  "locale": "ru",
  "strings": {
    "paywall.title": "Открой всё",
    "paywall.subtitle": "AI-темы, премиум-категории и многое другое",
    "onboarding.step1.title": "Раздаём роли"
  }
}
```

**Кеш:** `Cache-Control: max-age=3600` (1 час)

---

### `POST /v1/billing/adapty/webhook`

**Описание:** Принимает webhook-события подписок от Adapty и начисляет токены за подписку (токеномика вариант Б). Единственный механизм активации/деактивации premium на сервере — Apple App Store Server API и `POST /purchase/validate` не используются (см. [ADR-001](adr/ADR-001-adapty-instead-of-apple-server-api.md), [ADR-002](adr/ADR-002-adapty-bearer-auth-token-economy.md)). Полная логика — §4.

**Путь:** `POST /v1/billing/adapty/webhook` (префикс `/v1` из `api/main.py`). Старый `POST /v1/webhooks/adapty` удалён.

**Auth:** bearer-token — `Authorization: Bearer <ADAPTY_WEBHOOK_SECRET>` (статический секрет из env, constant-time сравнение). **Не HMAC** — Adapty не подписывает payload. Стандартный `X-Api-Key` не используется, путь исключён из глобальной `X-Api-Key` защиты. Если `ADAPTY_WEBHOOK_SECRET` не задан на сервере → `500`.

**Headers:** `Authorization: Bearer <ADAPTY_WEBHOOK_SECRET>` — обязателен.

**Request body (пример валидного события):**
```json
{
  "event_id": "evt_8f3a1c",
  "event_type": "subscription_started",
  "customer_user_id": "550e8400-e29b-41d4-a716-446655440099",
  "event_properties": {
    "vendor_product_id": "sub_yearly",
    "expires_at": "2027-06-10T10:00:00Z"
  }
}
```

Дефенсивный парсинг полей и приоритеты источников — см. §4. Поддерживаемые `event_type` (lowercase): `subscription_started`, `subscription_renewed`, `subscription_cancelled`, `subscription_expired`.

**Responses (всегда 200, кроме auth/внутреннего сбоя):**
```json
{ "status": "applied", "event_type": "subscription_started", "tokens_granted": 200 }
{ "status": "duplicate" }
{ "status": "ignored", "reason": "missing_event_id" }
{ "status": "ignored", "event_type": "subscription_paused" }
```

Полный перечень исходов `ignored` (`empty_body`, `invalid_json`, `not_an_object`, `missing_event_id`, `missing_customer_user_id`, неизвестный `event_type`) — в §4.

**Errors:**
- `401` — отсутствует или неверный bearer-токен
- `500` — `ADAPTY_WEBHOOK_SECRET` не сконфигурирован, либо реальный внутренний сбой (БД недоступна)

> Кривой/пробный payload **не** возвращает `4xx` — только `200 {"status":"ignored",...}`, чтобы Adapty не ретраил бесконечно.

---

## 9. Обработка ошибок — единый формат

Все ошибочные ответы возвращаются в формате:

```json
{
  "error": {
    "code": "RATE_LIMIT_EXCEEDED",
    "message": "AI request limit reached for today",
    "details": null
  }
}
```

| HTTP Status | Когда использовать |
|---|---|
| `200` | Успех, включая fallback-случаи |
| `400` | Невалидный запрос |
| `401` | Неверный/отсутствующий `X-Api-Key` / `X-Admin-Api-Key` / Adapty bearer-токен (`Authorization: Bearer <ADAPTY_WEBHOOK_SECRET>`) |
| `403` | Premium-категория запрошена пользователем без активной подписки (`is_premium == false`) |
| `404` | Ресурс не найден |
| `429` | Rate limit (с телом ответа — не hard error для клиента). НЕ применяется к `POST /ai/generate-theme` — там превышение rate-limit отдаётся как HTTP 200 + fallback (ADR-003). |
| `500` | Внутренняя ошибка |
| `503` | Внешний сервис недоступен (LLM) |

---

## 10. Нефункциональные требования

| Требование | Целевое значение |
|---|---|
| P95 latency `/ai/generate-theme` | < 3 секунды |
| P95 latency `/categories`, `/config` | < 200 мс |
| Uptime | 99.5% |
| Rate limit storage | Redis |
| AI topic word pool | Redis, без TTL (накапливается) |
| Categories cache | Redis, TTL 30 мин |
| Localizations cache | Redis, TTL 1h |
| LLM таймаут | 5 секунд |
| LLM fallback | немедленный, transparent для клиента |
| AITopicRequestLog retention | 90 дней, затем удаление |
| Premium-доступ | Серверный флаг `User.is_premium` + `premium_expires_at`, обновляется Adapty webhook (`POST /v1/billing/adapty/webhook`) |
| Adapty webhook авторизация | bearer-token `Authorization: Bearer <ADAPTY_WEBHOOK_SECRET>` (статический секрет, constant-time, не HMAC) |
| Начисление токенов | Webhook начисляет токены по тиру `vendor_product_id`; идемпотентно через ledger `ProcessedWebhookEvent` (одна транзакция) |
| Списание токенов за AI | `POST /ai/generate-theme` premium-only; `AI_THEME_TOKEN_COST` (env, default 1) за фактическую выдачу AI-слова; атомарное списание; fallback без списания (ADR-003) |
| JWT алгоритм (`/users` access_token) | RS256 |
| Rate limit window | Rolling 24h (не UTC midnight сброс) |

---

## 11. Флоу запуска игры на клиенте (iOS)

Игра полностью локальная — сервер не ведёт игровую сессию, не знает о составе игроков и ходах. Сервер отвечает только за: контент (слова), конфиг и профиль пользователя (включая premium-статус). Вся игровая логика (раздача ролей, голосование, победа) — на клиенте.

---

### Cold Start (при каждом запуске приложения)

Два параллельных запроса:

```
GET /config          — feature flags, лимиты, force update
GET /localizations   — paywall/onboarding тексты (если нужны на текущем экране)
```

Клиент кеширует оба ответа локально с TTL из `Cache-Control`. При недоступности сервера — использует последний known good.

Критическая проверка до входа в игру: `app.force_update_version` и `app.maintenance_mode` из `/config`.

---

### Экран выбора категории

```
GET /categories?locale={locale}
```

Возвращает все активные категории (free + premium) с флагом `is_premium`. Клиент сам рисует lock-иконку на premium-категориях на основе статуса `is_premium` пользователя (из `GET /users/me`).

Если пользователь нажимает на premium-категорию без активной подписки → показывает paywall. Перед paywall-экраном:

```
GET /categories/premium?locale={locale}
```

Возвращает premium-категории с `preview_words` для отображения на paywall.

---

### Настройка раунда

Всё на клиенте — сервер не вызывается. Игроки вводят имена, выбирают настройки:
- Количество импостеров (1 или 2 — Double Impostor)
- Включить Detective (да/нет)
- Включить Joker (да/нет)
- Режим: standard / party

Валидация состава матча (Joker, Detective) — на клиенте согласно правилам из секции 2.

---

### Старт раунда — получение слова

**Вариант A — категория из библиотеки:**

```
GET /categories/{category_id}/words?locale={locale}&mode={mode}&count=1
Headers: X-Api-Key + X-Device-Id  // premium-статус определяется по X-Device-Id
```

Клиент получает `WordEntry` с `civilian_word` и `impostor_word` (null для standard).

**Вариант B — AI-тема (пользователь ввёл свою тему):**

```
POST /ai/generate-theme
Body: { topic, locale, mode }
```

Клиент получает `AIThemeResponse` с теми же полями.

В обоих случаях сервер возвращает одну пару слов. Клиент сам раздаёт роли локально.

---

### Раздача ролей (только клиент, сервер не участвует)

1. Клиент определяет список ролей на основе настроек матча и числа игроков.
2. Перемешивает роли случайно.
3. Каждый игрок по очереди берёт устройство и видит свою роль + слово (или "Ты импостер — слова нет").
4. Undercover видит `impostor_word`, Civilian видит `civilian_word`, Impostor не видит ничего.

Слово с сервера пришло одно — клиент сам решает что показать каждой роли.

---

### Игровой процесс и завершение

Сервер не вызывается. Клиент ведёт:
- Список активных игроков
- Стадию игры (обсуждение → голосование → результат)
- Заряды Detective (0 зарядов, +1 каждые 2 хода)
- Счётчик выгнанных (для определения победы Joker)

Проверка победы — на клиенте согласно условиям из секции 2.

---

### Повторный раунд

Клиент запрашивает новое слово заново — тот же флоу старта раунда. Предыдущая сессия не передаётся на сервер.

---

### Диаграмма вызовов по экранам

```
App Launch
  ├── GET /config
  └── GET /localizations

Category Picker
  └── GET /categories

[is_premium = false + нажата premium-категория]
  └── GET /categories/premium   → Paywall экран
        └── [покупка через StoreKit 2 + Adapty SDK → Adapty webhook POST /v1/billing/adapty/webhook → сервер обновляет User.is_premium + начисляет токены]

Round Setup
  └── (нет запросов)

Round Start
  ├── [библиотека]  GET /categories/{id}/words
  └── [AI-тема]     POST /ai/generate-theme

In-Game
  └── (нет запросов)

Next Round
  └── повтор Round Start
```

---

### Важные детали

- `X-Device-Id` — UUID генерируется при первом запуске, хранится в Keychain, передаётся в каждом запросе. Используется для идентификации пользователя и определения premium-статуса.
- Premium-статус не хранится в виде токена на клиенте: сервер — источник истины (`User.is_premium`), клиент читает его через `GET /users/me`. Активация происходит асинхронно через Adapty webhook.
- Offline для free-категорий: слова бандлятся в приложение, сеть не нужна.
- Offline для premium: клиент показывает ошибку, игра не ломается.

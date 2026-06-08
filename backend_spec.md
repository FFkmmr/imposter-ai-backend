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
- Все текстовые поля хранятся как `LocalizedString` — JSON-объект с ключами по ISO locale.

---

### WordPack

Набор слов, привязанный к категории.

```
WordPack {
  id: UUID
  category_id: UUID (FK → Category)
  locale: string            // "en", "ru", "es", ...
  words: WordEntry[]
  is_active: bool
  created_at: timestamp
  updated_at: timestamp
}

WordEntry {
  id: UUID
  civilian_word: string     // слово для мирных
  impostor_word: string | null  // слово для Undercover в Party Mode; null = impostor не знает
  difficulty: "easy" | "medium" | "hard"
  tags: string[]
}
```

**Логика:**
- Для стандартного режима `impostor_word = null` — impostor просто не получает слово.
- Для Party Mode (Undercover) `impostor_word` содержит похожее, но отличное слово.
- Пак создаётся отдельно под каждую локаль — автоматического перевода нет.
- При запросе клиент передаёт `locale`, сервер ищет подходящий пак; если нет — fallback на `en`.

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

### Флоу генерации темы (`POST /ai/generate-theme`)

1. Клиент отправляет: `{ topic, locale, mode }`.
2. Сервер очищает prompt: обрезает до 80 символов, убирает спецсимволы.
3. Сервер проверяет тему по blacklist (список запрещённых слов/паттернов на regex).
4. Если blocked → возвращает `fallback_used: true` + случайный curated word из подходящей категории. Клиент получает валидный ответ 200, не ошибку.
5. Если прошло → проверяет пул слов для данного `sanitized_prompt + locale`:
   - Из пула исключаются слова, уже выданные этому `device_id` в текущей сессии.
   - Если остались доступные слова → выдаётся одно случайное из них.
   - Если все слова пула уже были выданы этому пользователю → переходим к шагу 6.
6. Если нет доступных слов в пуле → формирует системный промпт для LLM:
   - Язык: `locale`
   - Режим: standard (один civilian_word) или party (civilian_word + impostor_word)
   - Требование: короткое, безопасное, party-friendly, без NSFW
   - Формат ответа: JSON
   - Guardrails против NSFW/hate/illegal встроены в системный промпт
7. Вызов LLM. Таймаут: 5 секунд.
8. Парсинг и валидация ответа LLM.
9. Post-generation проверка через OpenAI Moderation API (отдельный вызов — даёт точный сигнал для логирования и аудита).
10. Новые слова добавляются в пул для данной темы. Выдаётся одно случайное из **только что полученных** новых слов.
11. Логирование в `AITopicRequestLog`.
12. Возврат клиенту.

**Логика пула слов:**
- Пул хранится в Redis с ключом `topic_pool:{sanitized_prompt}:{locale}`. Без TTL — накапливается со временем и переиспользуется разными пользователями.
- История выданных слов хранится на сервере в Redis с ключом `device_history:{device_id}:{sanitized_prompt}:{locale}`. TTL: 30 дней — после этого история сбрасывается и слова снова доступны.
- При каждом запросе сервер сам исключает из пула слова уже выданные этому устройству — клиент ничего дополнительно не передаёт.

**Fallback-цепочка:**
- LLM timeout (5 сек) → случайный word из категории с похожими тегами.
- Moderation rejected → curated fallback из default категории.
- Rate limit exceeded → curated fallback из default категории.
- `500` только если вообще нет контента для fallback (не должно происходить в production).

---

### Флоу генерации слов по теме (`POST /ai/generate-words`)

Аналогично generate-theme, но вместо одного слова — список из N слов для полного WordPack.
- Используется для admin-панели и контент-команды в MVP.
- В Phase 2 планируется как клиентский эндпоинт.
- Лимит: 20–30 слов на запрос.

---

## 4. Логика premium / paywall

### Entitlement flow

1. Клиент после успешной транзакции в StoreKit 2 отправляет receipt/transaction token на `POST /purchase/validate`.
2. Backend верифицирует через Apple App Store Server API.
3. При успехе возвращает `{ is_valid: true, product_id, expires_at, entitlement_token }`.
4. `entitlement_token` — подписанный JWT (RS256), payload: `{ device_id, product_id, expires_at }`.
5. Клиент хранит `entitlement_token` локально. TTL токена = срок подписки.
6. При запросе premium-контента клиент передаёт токен в заголовке `X-Entitlement-Token`.
7. Backend верифицирует подпись и TTL токена без обращения к Apple на каждый запрос.
8. Периодическая ре-валидация: клиент обновляет токен при каждом cold start если до истечения < 7 дней.

**Важно:**
- Рекомендуется RevenueCat как proxy-слой — упрощает логику на backend.
- Если backend недоступен → клиент работает с последним известным токеном + Grace Period 7 дней.
- Лог транзакций сохраняется (без PII).

---

## 4. Логика категорий и контента

### `GET /categories`
- Возвращает все активные категории с признаком `is_premium`.
- Клиент сам решает, показывать ли lock-иконку на основе entitlement пользователя.
- Пагинация не нужна в MVP (ожидается < 50 категорий).
- `preview_words` отсутствует — только мета-информация. Preview только в `/categories/premium`.
- TTL кеша на клиенте: 30 мин. Server-side: `Cache-Control: max-age=1800`.

### `GET /categories/premium`
- Возвращает только premium-категории с `preview_words` (2–3 слова для paywall-экрана).
- Используется на paywall-экране для preview доступного контента.

### `GET /categories/{category_id}/words`
- Для premium-категорий требует валидный `X-Entitlement-Token` в заголовке.
- Backend проверяет подпись токена и его `expires_at`. Не обращается к Apple.
- Если токен отсутствует → `401`. Если токен истёк или невалиден → `403`.
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
| `POST /ai/generate-theme` | 5 / 24h / device | 50 / 24h / device |
| `POST /ai/generate-words` | только admin | только admin |
| `GET /categories/{id}/words` | 100 / 24h / device | unlimited |
| `GET /categories` | unlimited | unlimited |
| `GET /config` | unlimited | unlimited |

**Логика:**
- Rate limit по `device_id` (anonymous), не по IP.
- Окно: rolling 24 hours (не сброс в midnight UTC).
- При превышении → ответ `429` с телом (`fallback_used: true` + curated слово) — клиент обрабатывает как успех с fallback, не как ошибку.
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
- Admin запросы: Bearer JWT.

**Общие заголовки запроса:**
```
X-Api-Key: <app_api_key>
X-Device-Id: <anonymous_uuid>       // генерируется при первом запуске, хранится локально
X-App-Version: 1.0.0
X-Locale: ru                        // ISO 639-1
X-Entitlement-Token: <jwt>          // только для premium-эндпоинтов, опционален
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

**Описание:** Возвращает слова для конкретной категории. Для premium-категорий требует валидный `X-Entitlement-Token`. Клиент загружает слова пачкой при старте раунда.

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

**Headers:** `X-Entitlement-Token` — требуется для premium категорий.

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
- `401` — отсутствует `X-Entitlement-Token` для premium категории
- `403` — токен невалиден или истёк
- `404` — категория не найдена
- нет паков для запрошенной локали → сервер возвращает fallback `en`, не ошибку

---

### `POST /ai/generate-theme`

**Описание:** Генерирует слово / пару слов по пользовательской теме через LLM. Основной AI-эндпоинт.

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

**Response 200 (standard):**
```json
{
  "locale": "ru",
  "topic": "космос",
  "civilian_word": "Марс",
  "impostor_word": null,
  "difficulty": "medium",
  "is_safe": true,
  "fallback_used": false
}
```

**Response 200 (party mode):**
```json
{
  "locale": "ru",
  "topic": "космос",
  "civilian_word": "Марс",
  "impostor_word": "Луна",
  "difficulty": "medium",
  "is_safe": true,
  "fallback_used": false
}
```

**Response 200 (fallback):**
```json
{
  "locale": "ru",
  "topic": "космос",
  "civilian_word": "Земля",
  "impostor_word": null,
  "difficulty": "easy",
  "is_safe": true,
  "fallback_used": true,
  "fallback_reason": "ai_unavailable"
}
```

`fallback_reason` варианты: `"ai_unavailable"` | `"moderation_rejected"` | `"rate_limit_exceeded"`

**Errors:**
- `400` — `topic` пустой или превышает лимит
- `429` — превышен rate limit (тело содержит `fallback_used: true` + curated word — клиент обрабатывает как успех)
- `503` — LLM или внешний сервис временно недоступен (клиент получает fallback, не ошибку)

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

### `POST /purchase/validate`

**Описание:** Верифицирует Apple IAP транзакцию через Apple App Store Server API. Возвращает `entitlement_token` — JWT для авторизации последующих premium-запросов без повторного обращения к Apple.

**Request body:**
```json
{
  "transaction_id": "...",
  "receipt_data": "...",
  "product_id": "com.imposterai.premium.monthly"
}
```

**Response 200 (valid):**
```json
{
  "is_valid": true,
  "product_id": "com.imposterai.premium.monthly",
  "expires_at": "2026-07-06T12:00:00Z",
  "environment": "production",
  "entitlement_token": "<signed_jwt>"
}
```

**Response 200 (invalid):**
```json
{
  "is_valid": false,
  "reason": "receipt_expired"
}
```

**Errors:**
- `400` — невалидные поля запроса
- `503` — Apple API временно недоступен

Если backend недоступен (`503`), клиент применяет Grace Period (7 дней) и не лишает пользователя доступа резко.

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
| `401` | Неверный/отсутствующий API Key или Entitlement Token |
| `403` | Истёкший или невалидный Entitlement Token |
| `404` | Ресурс не найден |
| `429` | Rate limit (с телом ответа — не hard error для клиента) |
| `500` | Внутренняя ошибка |
| `503` | Внешний сервис недоступен (Apple, LLM) |

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
| Entitlement token алгоритм | RS256 JWT |
| Rate limit window | Rolling 24h (не UTC midnight сброс) |

---

## 11. Флоу запуска игры на клиенте (iOS)

Игра полностью локальная — сервер не ведёт игровую сессию, не знает о составе игроков и ходах. Сервер отвечает только за: контент (слова), конфиг и entitlement. Вся игровая логика (раздача ролей, голосование, победа) — на клиенте.

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

Возвращает все активные категории (free + premium) с флагом `is_premium`. Клиент сам рисует lock-иконку на premium-категориях на основе наличия валидного `entitlement_token`.

Если пользователь нажимает на premium-категорию без entitlement → показывает paywall. Перед paywall-экраном:

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
Headers: X-Entitlement-Token: <jwt>  // только для premium
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

[если нет entitlement и нажата premium]
  └── GET /categories/premium   → Paywall экран
        └── POST /purchase/validate  → получить entitlement_token

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

- `X-Device-Id` — UUID генерируется при первом запуске, хранится в Keychain, передаётся в каждом запросе.
- `entitlement_token` — хранится в Keychain. Обновляется при cold start если до истечения < 7 дней (повторный `POST /purchase/validate`).
- Offline для free-категорий: слова бандлятся в приложение, сеть не нужна.
- Offline для premium: клиент показывает ошибку, игра не ломается.

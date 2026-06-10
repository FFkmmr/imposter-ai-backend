# ADR-002: Adapty webhook на bearer-авторизации и реальная токеномика (вариант Б)

- **Статус:** Accepted
- **Дата:** 2026-06-10
- **Контекст модуля:** premium / paywall / billing, backend
- **Отношение к другим ADR:** частично **supersedes** [ADR-001](ADR-001-adapty-instead-of-apple-server-api.md) — в части способа верификации webhook (HMAC `Adapty-Signature` → bearer-token) и пути эндпоинта. Решение «используем Adapty как единый слой подписок» из ADR-001 **остаётся в силе**.

## Context

ADR-001 зафиксировал premium через Adapty webhook с верификацией по `Adapty-Signature` (HMAC-SHA256 от raw body) и обновлением только флага `User.is_premium` / `premium_expires_at`. Эндпоинт — `POST /v1/webhooks/adapty`.

Изменились два продуктовых факта:

1. **Adapty не подписывает payload.** Фактический механизм аутентификации webhook у Adapty — статический bearer-секрет в заголовке `Authorization`, а не HMAC-подпись тела. Реализация HMAC из ADR-001 не соответствует тому, что реально присылает Adapty.
2. **Переход на реальную токеномику (вариант Б).** Подписка теперь не только включает `is_premium`, но и **начисляет токены** на баланс `User.tokens`. Токены — внутриигровая валюта; их начисление должно быть идемпотентным (Adapty ретраит доставку), а гранты и SKU — настраиваемыми без пересборки.

Дополнительно уточняется путь и поведение при пробных пингах: Adapty отправляет проверочные/кривые запросы и ретраит любой не-2xx бесконечно — webhook должен отвечать `200` на любой авторизованный, но непригодный к обработке payload.

## Decision

### 1. Авторизация — bearer-token вместо HMAC

- Заголовок `Authorization: Bearer <ADAPTY_WEBHOOK_SECRET>`. Секрет — статический, из env, **constant-time** сравнение.
- Неверный/отсутствующий токен → `401`. `ADAPTY_WEBHOOK_SECRET` не задан на сервере → `500` (не молчаливый пропуск, как было в ADR-001).
- Путь исключён из глобальной `X-Api-Key` защиты (Adapty не знает service key).
- HMAC `Adapty-Signature` и поведение «пустой секрет → пропуск проверки» — **отменены**.

### 2. Путь эндпоинта

- Новый: `POST /v1/billing/adapty/webhook`. Старый `POST /v1/webhooks/adapty` — удалён из контракта.

### 3. Токеномика (вариант Б)

- `subscription_started` / `subscription_renewed` → `is_premium = true` + начисление токенов по тиру `vendor_product_id`.
- `subscription_cancelled` / `subscription_expired` → `is_premium = false`, токены не трогаются.
- `expires_at` (если есть) → `premium_expires_at`.
- Тиры — env-маппинг: `SUBSCRIPTION_PRODUCT_WEEKLY → SUBSCRIPTION_TOKENS_WEEKLY`, `SUBSCRIPTION_PRODUCT_YEARLY → SUBSCRIPTION_TOKENS_YEARLY`, неизвестный product → fallback `SUBSCRIPTION_TOKENS_GRANT`.

### 4. Идемпотентность

- Новая сущность `ProcessedWebhookEvent` (ledger, PK `event_id`). Перед начислением — проверка наличия; повтор → `200 duplicate`.
- Обновление `User` (баланс + premium-флаги) и запись в ledger — в **одной транзакции**.
- Миграция Alembic `0002` поверх `0001` — таблица `processed_webhook_events`.

### 5. Дефенсивный парсинг и толерантность к payload

- Поля извлекаются по приоритету источников (см. `backend_spec.md` §4) — устойчивость к версиям SDK.
- После авторизации любой кривой/пробный payload → `200 {"status":"ignored","reason":...}`; `5xx` только при реальном внутреннем сбое.

## Consequences

**Положительные:**
- Соответствие реальному поведению Adapty (bearer вместо несуществующей HMAC-подписи) — webhook начнёт работать в проде.
- Идемпотентный ledger исключает двойное начисление токенов при ретраях Adapty.
- SKU/гранты вынесены в env — изменение тарифов без пересборки/редеплоя кода.
- `200 ignored` на пробные пинги останавливает бесконечные ретраи Adapty.

**Отрицательные / риски:**
- Требуется миграция БД (`0002`) и изменение `config.py` + `.env.example` — задача backend.
- Безопасность держится на статическом `ADAPTY_WEBHOOK_SECRET` — только env/secret manager; ротация при компрометации.
- `is_premium = true` без `vendor_product_id` (или с неизвестным SKU) приведёт к fallback-гранту `SUBSCRIPTION_TOKENS_GRANT` — нужно корректно сконфигурировать env, иначе пользователи получат дефолтный грант.
- **Списание** токенов за AI-генерацию не определено этим ADR — открытый вопрос Q-BILL-1 / `TD-002`.

## Amendment (2026-06-10, Q-BILL-2)

Уточнение поведения активационных событий по `expires_at`: при `subscription_started` / `subscription_renewed` поле `User.premium_expires_at` обновляется **только если `expires_at` реально извлечён из payload** (непустой, валидно распарсенный ISO 8601). Если `expires_at` отсутствует/не распарсился — прежнее значение `premium_expires_at` **сохраняется** (НЕ обнуляется в `null`). Причина: `renewed`-событие без `expires_at` иначе занулило бы дату окончания, что сделало бы подписку «бессрочной» (`premium_expires_at is None` трактуется gating'ом как «не истекает») и сломало бы проверку истечения. Деактивационные события (`cancelled`/`expired`) `premium_expires_at` не трогают. Закрывает Q-BILL-2; полная формулировка — `backend_spec.md` §4 «`expires_at` — обновление `User.premium_expires_at`».

## Alternatives

1. **Сохранить HMAC из ADR-001** — отвергнут: Adapty не подписывает payload, HMAC-проверка нереализуема против реальных запросов.
2. **JWT-токен от Adapty** — отвергнут: Adapty шлёт статический shared secret, не JWT; введение JWT добавило бы несуществующую сложность.
3. **Начислять токены без ledger (идемпотентность по `expires_at`/времени)** — отвергнут: ненадёжно при ретраях и дубликатах; явный ledger по `event_id` — корректный способ.
4. **Хранить токеномику только на флаге `is_premium` (вариант А, без баланса)** — отвергнут продуктовым решением в пользу варианта Б (реальный баланс токенов).

# ADR-001: Adapty webhook вместо Apple App Store Server API для premium-доступа

- **Статус:** Accepted (частично superseded [ADR-002](ADR-002-adapty-bearer-auth-token-economy.md))
- **Дата:** 2026-06-09
- **Контекст модуля:** premium / paywall, backend

> **Обновлено ADR-002 (2026-06-10):** способ верификации webhook (HMAC `Adapty-Signature`) и путь `POST /v1/webhooks/adapty` **отменены** — заменены на bearer-token (`Authorization: Bearer <ADAPTY_WEBHOOK_SECRET>`) и путь `POST /v1/billing/adapty/webhook`; добавлено начисление токенов (вариант Б). Решение «используем Adapty» остаётся в силе. Все упоминания HMAC и старого пути ниже читать с этой поправкой.

## Context

Приложению нужен механизм определения premium-статуса пользователя для гейтинга премиум-контента
(`GET /categories/premium`, `GET /categories/{id}/words` для premium-категорий).

Ранее в спецификации (`docs/backend_spec.md`) был описан вариант на базе **Apple App Store Server API**:
клиент после StoreKit 2 транзакции отправлял receipt на `POST /purchase/validate`, backend валидировал его
у Apple и выдавал подписанный entitlement-токен (RS256 JWT), который клиент передавал в заголовке
`X-Entitlement-Token` при каждом premium-запросе.

Фактическая реализация в коде (`api/routers/webhooks.py`, `api/routers/categories.py`) пошла по другому пути:
премиум активируется через **webhook от Adapty**, а статус хранится серверным флагом `User.is_premium`.
Документация рассинхронизировалась с кодом — это блокер.

Продуктовое решение **окончательное**: остаёмся на Adapty.

## Decision

Premium-доступ строится на **Adapty webhook + серверный флаг `User.is_premium` (+ `premium_expires_at`)**:

1. Покупка и валидация подписки выполняются на стороне Adapty (StoreKit 2 + Adapty SDK на устройстве).
2. Adapty шлёт webhook на `POST /v1/webhooks/adapty`. Верификация — `Adapty-Signature` (HMAC-SHA256 от raw body,
   формат `sha256=<hex>`, constant-time сравнение, shared secret `ADAPTY_WEBHOOK_SECRET`).
3. По `customer_user_id` (= `device_id`, UUID) backend находит `User` и обновляет флаг:
   - активация (`subscription_initial_purchase` / `subscription_renewed` / `subscription_activated`) →
     `is_premium = true`, `premium_expires_at = paid_access_level.expires_at`;
   - деактивация (`subscription_expired` / `subscription_cancelled` / `subscription_refunded`) →
     `is_premium = false`, `premium_expires_at = null`.
4. Premium-gating: backend читает `User.is_premium` по `X-Device-Id`. Активной подписка считается при
   `is_premium == true` и (`premium_expires_at` is null или `premium_expires_at > now`).

**Apple App Store Server API, эндпоинт `POST /purchase/validate` и entitlement-токен `X-Entitlement-Token`
(RS256 JWT) — отменены и из документации удалены.**

## Consequences

**Положительные:**
- Backend не интегрируется с Apple App Store Server API напрямую — меньше кода, секретов и точек отказа.
- Adapty — единый слой подписок: при добавлении Android/других платформ механизм не меняется (единый webhook).
- Источник истины о подписке — серверный флаг; клиенту не нужно хранить и обновлять entitlement-токен.
- Premium-проверка на запросе — простой lookup в БД по `device_id`, без обращения к внешнему API.

**Отрицательные / риски:**
- Активация премиума асинхронна (через webhook) — между покупкой и обновлением флага есть лаг. Клиент
  опрашивает `GET /users/me` для актуального статуса.
- Зависимость от доставки и подписи webhook Adapty. Webhook обязан быть идемпотентным и возвращать `200`
  на корректно подписанные запросы (включая неизвестные события) для retry-механизма Adapty.
- Безопасность держится на `ADAPTY_WEBHOOK_SECRET` (HMAC). Секрет — только в env/secret manager.

## Alternatives

1. **Apple App Store Server API + entitlement JWT (`X-Entitlement-Token`)** — отвергнут продуктовым решением:
   привязывает backend к Apple, не масштабируется на другие платформы, требует серверной валидации receipt
   и управления жизненным циклом JWT-токена на клиенте.
2. **RevenueCat как proxy-слой** — альтернативный вендор того же класса, что и Adapty. Не выбран: продукт уже
   остановился на Adapty.
3. **Клиентская проверка StoreKit без сервера** — отвергнут: сервер обязан гейтить премиум-контент,
   клиентская проверка недостаточна для защиты контента.

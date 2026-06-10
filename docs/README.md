# Документация imposter-ai-backend

Единственный источник истины проекта. При расхождении docs ↔ код — синхронизируется docs под реальный код.

## Карта документов

| Документ | Назначение |
|---|---|
| [backend_spec.md](backend_spec.md) | Backend-спецификация: сущности, Party Mode, AI-генерация, premium/paywall, категории, rate limiting, эндпоинты, NFR, клиентский флоу. |
| [openapi.yaml](openapi.yaml) | OpenAPI 3.0.3 контракт всех HTTP-эндпоинтов + схемы. |
| [seed_words.json](seed_words.json) | Источник истины для seed-контента (категории + слова). Монтируется в Docker как `/app/seed_words.json`. |
| [07-deployment.md](07-deployment.md) | Прод-развёртывание: топология за общим Traefik, prod-compose, сети, домен/TLS, CI/CD, генерация ключей, runbook первичного деплоя/обновления, rollback. |
| [adr/INDEX.md](adr/INDEX.md) | Реестр архитектурных решений (ADR). |
| [100-known-tech-debt.md](100-known-tech-debt.md) | Реестр известного технического долга (`TD-NNN`). |

## Ключевые решения

- **Premium-доступ + токеномика (вариант Б):** Adapty webhook (`POST /v1/billing/adapty/webhook`, bearer-token `Authorization: Bearer <ADAPTY_WEBHOOK_SECRET>`) обновляет `User.is_premium` и начисляет токены по тиру продукта (идемпотентно через ledger `ProcessedWebhookEvent`). Apple App Store Server API и `POST /purchase/validate` **не используются** — см. [ADR-001](adr/ADR-001-adapty-instead-of-apple-server-api.md); переход с HMAC на bearer и токеномика — [ADR-002](adr/ADR-002-adapty-bearer-auth-token-economy.md) (Amendment Q-BILL-2: `premium_expires_at` обновляется только при наличии `expires_at`).
- **Списание токенов за AI (ADR-003):** `POST /ai/generate-theme` — premium-only; одна фактическая выдача AI-слова стоит `AI_THEME_TOKEN_COST` (env, default 1), списывается атомарно; любой fallback (`premium_required`/`insufficient_tokens`/`rate_limit_exceeded`/`moderation_rejected`/`ai_unavailable`) — без списания. См. [ADR-003](adr/ADR-003-ai-token-spend.md), `backend_spec.md` §3.
- **Аутентификация клиента:** `X-Api-Key` + `X-Device-Id` (анонимный UUID). Admin — `X-Admin-Api-Key`.
- **Локализация контента:** 6 локалей MVP (`en`, `ru`, `es`, `pt`, `fr`, `de`) — ТЗ §12.
- **Развёртывание ([ADR-004](adr/ADR-004-traefik-shared-edge-deployment.md)):** прод на shared-сервере `87.239.135.154` за общим Traefik (`/opt/edge`), домен `nexaliohub.shop`, TLS терминирует Traefik (ACME `le`). Standalone `docker-compose.prod.yml` без host-портов; CI/CD — `push main` → SSH `git pull` + `docker compose up -d --build`. Детали — [07-deployment.md](07-deployment.md).

## Открытый tech debt

- [TD-003](100-known-tech-debt.md#td-003--api-dockerfile-запускает-процесс-от-root) (Open, major): `api/Dockerfile` запускает процесс от `root` — нужен non-root `USER`. Cross-ref: `docs/07-deployment.md`.
- [TD-004](100-known-tech-debt.md#td-004--базовые-образы-запиннены-по-тегу-не-по-digest) (Open, minor): базовые образы запиннены по тегу, не по digest.

Закрытый:

- [TD-002](100-known-tech-debt.md#td-002--списание-токенов-за-ai-генерацию-не-спроектировано) закрыт (2026-06-10, ADR-003): спроектировано списание токенов за AI-генерацию (premium-only, `AI_THEME_TOKEN_COST`, атомарно, fallback без списания). Q-BILL-1 закрыт.
- **Q-BILL-2** закрыт (2026-06-10, [ADR-002](adr/ADR-002-adapty-bearer-auth-token-economy.md) Amendment): на событиях `started`/`renewed` `premium_expires_at` обновляется только при наличии `expires_at` в payload (иначе сохраняется прежнее значение). См. `backend_spec.md` §2.
- [TD-001](100-known-tech-debt.md#td-001--seed-контент-не-соответствует-контент-контракту-mvp) закрыт (2026-06-09): seed-контент приведён к контракту — 6 локалей, локализованные `description`, 3 premium-категории.

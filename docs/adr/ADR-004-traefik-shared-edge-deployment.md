# ADR-004 — Развёртывание за общим Traefik-edge со standalone prod-compose

**Статус:** Accepted
**Дата:** 2026-06-10
**Связано:** [docs/07-deployment.md](../07-deployment.md)

## Context

`imposter-ai-backend` развёртывается на shared-сервере `87.239.135.154` (Ubuntu 22.04), где уже работает общий reverse-proxy Traefik (`/opt/edge`) с настроенным ACME (Let's Encrypt, certresolver `le`) и внешней Docker-сетью `web`. На сервере живут и другие сервисы за тем же Traefik.

Нужно решить:
- кто терминирует TLS и управляет сертификатами;
- как маршрутизировать `nexaliohub.shop` на сервис, не публикуя порты наружу;
- как изолировать внутренний трафик сервиса (`db`/`redis`) от соседей по серверу;
- как отделить прод-конфигурацию от dev (`docker-compose.yml` с bind-mount, host-портами и `--reload`).

## Decision

1. **TLS и сертификаты — на общем Traefik.** Сервис не терминирует TLS и не выпускает сертификаты. Маршрут описывается Docker-метками на контейнере `api` (router `imposterai`, `Host(nexaliohub.shop)`, `entrypoints=websecure`, `tls.certresolver=le`, `loadbalancer.server.port=8000`).
2. **Подключение к edge — через внешнюю сеть `web`** (`external: true`). Только `api` подключён к `web`. `db` и `redis` — только в приватной сети `default` стека и наружу не видны. Host-порты прод-сервис не публикует.
3. **Отдельный standalone `docker-compose.prod.yml`** (не `extends` dev-файл): без bind-mount `./api`, без host-портов, `command` без `--reload`, с healthcheck и `expose 8000`.
4. **CI/CD — pull-based по SSH:** `push` в `main` → `appleboy/ssh-action` → `git pull --ff-only` + `docker compose -f docker-compose.prod.yml up -d --build` на сервере. Concurrency-группа исключает наложение деплоев.

## Consequences

**Плюсы:**
- Один централизованный ACME/TLS на сервере — нет дублирования логики сертификатов в каждом сервисе.
- Поверхность атаки минимальна: наружу торчит только Traefik; `api` доступен лишь через сеть `web`, БД/кэш изолированы в `default`.
- Чёткое разделение dev/prod-конфигов — прод не наследует dev-удобства (`--reload`, открытые порты).
- Простой pull-based деплой без хранения секретов в CI кроме SSH-доступа.

**Минусы / риски:**
- Зависимость от внешнего владельца Traefik/`/opt/edge`: изменения edge-конфига вне репозитория сервиса могут повлиять на маршрутизацию.
- Внешняя сеть `web` должна существовать до подъёма стека (предусловие первичного деплоя).
- Деплой требует SSH-доступа агента CI к серверу (`SSH_HOST`/`SSH_USER`/`SSH_PRIVATE_KEY`).

## Alternatives

- **Собственный reverse-proxy/TLS в стеке сервиса** (nginx/Caddy + ACME). Отклонено: дублирует уже имеющийся edge, усложняет управление сертификатами и портами 80/443 на shared-сервере.
- **Публикация host-порта + проксирование снаружи.** Отклонено: расширяет поверхность атаки, конфликты портов с соседями.
- **Расширение dev `docker-compose.yml` через `extends`/override.** Отклонено: риск протечки dev-настроек (`--reload`, bind-mount, открытые порты) в прод; standalone-файл явнее и безопаснее.
- **Push-based деплой (registry + pull образа на сервере).** Отклонено для текущего масштаба: build-on-server проще, не требует реестра образов; зафиксировано как возможная будущая оптимизация.

# ADR-0014: REST API и Web UI

- Статус: Принято (реализация — S8)
- Дата: 2026-11-05

## Контекст
ТЗ §12/§13: `POST /projects`, `POST /projects/{id}/runs`, `GET /runs/{id}`, `POST /runs/{id}/approve`, `POST /runs/{id}/cancel`, `GET /runs/{id}/artifacts`, `GET /projects/{id}/findings`, `GET /audit-events`; создание идемпотентно по `Idempotency-Key`. Web UI — React, необязателен для CLI MVP. NFR-04.

## Решение
- FastAPI (`ai_coding.api`), версия API в пути `/api/v1`, OpenAPI 3.1 — контракт; Pydantic-схемы разделены на API DTO и доменные модели.
- Аутентификация: локальные пользователи (argon2) + токены API с областью проекта; OIDC — backlog. RBAC проверяется в зависимостях FastAPI на каждый endpoint и в сервисном слое (не только UI).
- `Idempotency-Key` хранится с хэшем тела запроса; повтор возвращает исходный ответ, иной body — 422.
- Rate limiting (token bucket на пользователя/IP), security headers, TLS терминируется reverse proxy (Caddy/Traefik) в docker-compose.
- Web UI: React + TypeScript + Vite, TanStack Query, клиент генерируется из OpenAPI (`openapi-typescript`), каталог `web/`. Сборка — статические файлы, раздаются API или proxy.

## Альтернативы
GraphQL — отклонено (ТЗ задаёт REST). Next.js — отклонено (SSR не нужен, больше поверхность).

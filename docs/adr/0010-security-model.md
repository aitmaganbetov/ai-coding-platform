# ADR-0010: Модель безопасности

- Статус: Принято
- Дата: 2026-11-05

## Контекст
ТЗ §12, §18, AC-07, AC-08, NFR-04, NFR-07, FR-18.

## Решение (инварианты платформы)
1. **Недоверенный контент.** README, issues, diff, зависимости, тестовые данные, вывод инструментов — данные, а не инструкции. Они передаются агентам в отдельно маркированных блоках, никогда не расширяют `allowed_tools`/`scope`. Права задаются только оркестратором из политики (AC-07).
2. **Least privilege.** Агент по умолчанию: чтение в пределах проекта, без записи, без shell, без сети (`AgentScope`). Права выдаются на задачу, краткоживущие.
3. **Изоляция исполнения.** Недоверенный код — только в Docker sandbox без privileged/Docker socket, read-only root, `--network none` по умолчанию. Git не исполняет hooks/fsmonitor из репозитория (ADR-0008).
4. **Egress.** `security.network`: `local-only` (запрещён весь egress), `allowlist`, `open`. Внешние LLM — только после проверки политики.
5. **Секреты.** Не в YAML (валидатор), не в логах/prompts/PR-комментариях/артефактах (единый `Redactor`), передаются через environment/secret store; gitleaks в CI.
6. **Human-in-the-loop.** delete/migration/merge/deploy — approval всегда; auto-merge запрещён; production без approval невозможен (FR-17).
7. **RBAC на сервере** (S8): Owner/Admin, Maintainer, Developer, Auditor, Agent; межпроектная изоляция; каждая операция — audit event.
8. **Цепочка поставок.** Actions закреплены на SHA, lockfile, pip-audit/OSV, SBOM и provenance для релизов (S9).
9. **API** (S8): аутентификация, TLS, rate limiting, Idempotency-Key, audit trail.

## Последствия
Каждый модуль, вводящий новую точку ввода/вывода, обязан пройти чек-лист `SECURITY.md` и иметь негативные тесты.

## Альтернативы
Доверять LLM-«системным инструкциям» как защите от prompt injection — отклонено: защита должна быть архитектурной (права и изоляция), а не текстовой.

# ADR-0004: Конфигурация `.ai-coding.yml` и её JSON Schema

- Статус: Принято
- Дата: 2026-11-05

## Контекст
ТЗ §11: `.ai-coding.yml` содержит `schema_version, project, mode, stacks, execution.backend, ai.provider/model, agents.enabled, security.network, quality_gates, budgets, approvals, artifacts.retention`, проходит schema validation; секреты — только через secret store / environment.

## Решение
- **Источник истины** — Pydantic-модели `ai_coding.config.models`. JSON Schema (Draft 2020-12) генерируется (`ai-coding config schema`) и коммитится в `src/ai_coding/schemas/ai-coding.schema.json` (используется IDE и внешними инструментами). Тест и CI-шаг проверяют отсутствие расхождения (drift).
- **Конвейер валидации** (`ai-coding config validate`):
  1. безопасный YAML-парсер (SafeLoader, без python-тегов, **дубли ключей — ошибка**, лимит 1 MiB, только UTF-8);
  2. поиск секретов: ключи вида `api_key/token/password/...` с литеральным значением и значения, совпадающие с форматами токенов, — ошибка; ссылки `*_env: VAR` и `${env:VAR}` разрешены;
  3. JSON Schema — структура, типы, перечисления, диапазоны (все ошибки сразу, с путями);
  4. Pydantic — межсекционные правила безопасности.
- **Безопасные значения по умолчанию**: `mode: audit`, AI выключен, `execution.backend: local`, `allow_host_commands: false`, `use_worktree: true`, `security.network: local-only`, все гейты включены и блокирующие, `fail_on: high`, `required_approvals: 1`.
- **Неотключаемые инварианты** (тип `Literal`): `security.redact_secrets: true`, `security.treat_repository_content_as_untrusted: true`, `approvals.auto_merge: false`; `approvals.required_for` обязан содержать `delete, migration, merge, deploy` (FR-17, ТЗ §12).
- **Межсекционные правила**: внешний провайдер (openai/anthropic) или внешний fallback/routing при `network: local-only` — ошибка (AC-05); `github-actions` backend при `local-only` — ошибка; `mode: audit` + `allow_host_commands: true` — ошибка (FR-06 read-only); `default_branch` обязан попадать в `protected_branches` (AC-03).
- Сообщения об ошибках **никогда не повторяют** значение поля (оно может быть секретом) и проходят через redactor.
- `extra="forbid"` на всех уровнях: опечатка не может молча отключить политику.
- Версионирование: `schema_version: "2.1"`; несовместимые изменения — новая версия схемы и миграция `ai-coding config migrate` (backlog).

## Последствия
Схема пригодна для автодополнения в IDE (`# yaml-language-server: $schema=...`). Добавление поля требует регенерации схемы — CI подсказывает команду.

## Альтернативы
Рукописная JSON Schema + отдельные Pydantic-модели — отклонено (две истины расходятся). Только Pydantic без JSON Schema — отклонено (ТЗ требует JSON Schema, нужна интеграция с IDE и не-Python инструментами).

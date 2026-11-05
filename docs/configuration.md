# Конфигурация `.ai-coding.yml`

JSON Schema: [`src/ai_coding/schemas/ai-coding.schema.json`](../src/ai_coding/schemas/ai-coding.schema.json) (Draft 2020-12). Для автодополнения в VS Code (расширение YAML) добавьте первой строкой:

```yaml
# yaml-language-server: $schema=https://raw.githubusercontent.com/aitmaganbetov/ai-coding-platform/main/src/ai_coding/schemas/ai-coding.schema.json
```

## Разделы

| Ключ | Назначение | По умолчанию |
| --- | --- | --- |
| `schema_version` | Версия схемы, обязательно `"2.1"` | — (обязательно) |
| `project.name` | Имя `[A-Za-z0-9][A-Za-z0-9._-]*` | — (обязательно) |
| `project.default_branch` | Основная ветка, должна попадать в `protected_branches` | `main` |
| `project.protected_branches` | Glob-шаблоны веток, в которые платформа не коммитит | `[main, master, release/*]` |
| `mode` | `create` \| `develop` \| `modernize` \| `audit` \| `maintain` | `audit` |
| `stacks` | `laravel, php, react, typescript, javascript, python, go, rust` | `[]` (заполняет `init`) |
| `execution.backend` | `local` \| `docker` \| `github-actions` | `local` |
| `execution.use_worktree` | Изменения агентов в отдельном git worktree | `true` |
| `execution.allow_host_commands` | Разрешить allowlist-команды на хосте | `false` |
| `execution.command_allowlist` | Исполняемые файлы без approval | `[]` |
| `execution.limits` | `cpus`, `memory_mb`, `timeout_seconds`, `max_retries`, `max_parallel_tasks` | 2 / 2048 / 1800 / 2 / 4 |
| `execution.docker` | `image`, `read_only_root`, `network` (`none`\|`bridge`), `user` | `python:3.12-slim`, `true`, `none`, `1000:1000` |
| `ai.enabled` | Включить LLM | `false` |
| `ai.provider` | `none` \| `openai` \| `anthropic` \| `ollama` \| `vllm` | `none` |
| `ai.model`, `ai.base_url` | Модель, URL для Ollama/vLLM | — |
| `ai.api_key_env` | **Имя** переменной окружения с ключом | — |
| `ai.max_output_tokens`, `ai.temperature` | Лимиты генерации | 4096, 0.0 |
| `ai.fallback` | Цепочка `{provider, model}` (до 5) | `[]` |
| `ai.routing` | `{<agent>: {provider, model}}` | `{}` |
| `agents.enabled` | Подмножество 12 агентов | все |
| `security.network` | `local-only` \| `allowlist` \| `open` | `local-only` |
| `security.egress_allowlist` | Хосты (`*.example.com`, `host:port`) | `[]` |
| `security.secret_scan` | Сканирование секретов | `true` |
| `security.redact_secrets` | Всегда `true` (неотключаемо) | `true` |
| `quality_gates.{build,tests,static,security,coverage,review}` | `enabled`, `blocking` + параметры | все включены и блокирующие |
| `quality_gates.static.max_errors` | Порог ошибок линтера/типов | `0` |
| `quality_gates.security.fail_on` | Порог severity | `high` |
| `quality_gates.coverage.min_percent` | Минимальное покрытие | `80` |
| `quality_gates.review.required_approvals` | Человеческие approvals | `1` |
| `quality_gates.review.ai_review_blocking` | AI review блокирует только по явной политике | `false` |
| `budgets` | `max_tokens_per_run`, `max_cost_usd_per_run`, `max_duration_minutes` | 500000 / 5.0 / 120 |
| `approvals.required_for` | Операции с approval; **обязательно** `delete, migration, merge, deploy` | + `critical_file_change, pr_create, external_llm, host_command` |
| `approvals.critical_paths` | Glob критичных файлов | `.github/**`, `.ai-coding.yml`, миграции, Dockerfile, lock-файлы |
| `approvals.auto_merge` | Всегда `false` | `false` |
| `artifacts` | `store` (`local`\|`minio`), `path`, `retention_days`, `formats` (`json, markdown, sarif, junit`) | `local`, `.ai-coding/artifacts`, 30, `[json, markdown]` |

## Правила безопасности, проверяемые валидатором

| Правило | Пример ошибки |
| --- | --- |
| Литеральные секреты запрещены | `[secret] ai.api_key: key 'api_key' holds a literal value` |
| Токен-подобные значения в любом поле запрещены | `value matches secret pattern 'github-token'` |
| Внешний LLM (включая fallback/routing) при `local-only` | `security.network 'local-only' forbids egress …` |
| `github-actions` при `local-only` | `execution.backend 'github-actions' requires network access` |
| `mode: audit` + `allow_host_commands: true` | `mode 'audit' is read-only` |
| Отключение обязательных approvals | `approvals.required_for must include mandatory operations: …` |
| `default_branch` не защищена | `default_branch 'dev' must be covered by protected_branches` |
| Неизвестные ключи | `Additional properties are not allowed ('typo' was unexpected)` |
| Дубликаты ключей YAML | `duplicate key 'mode'` (с номером строки) |
| `allowlist` с пустым списком / `local-only` с непустым | `security.egress_allowlist must not be empty …` |

Значения полей **не выводятся** в сообщениях об ошибках — они могут содержать секреты.

## Пример полной конфигурации

См. [`tests/fixtures/configs/full.yml`](../tests/fixtures/configs/full.yml).

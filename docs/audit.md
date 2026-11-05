# Аудит проекта (`ai-coding audit`)

Read-only аудит без LLM (FR-06, приёмка S3). Решения: [ADR-0018](adr/0018-executors-implementation.md), [ADR-0019](adr/0019-tool-adapters-and-findings.md), [ADR-0020](adr/0020-quality-gates-semantics-and-baseline.md).

## Быстрый старт

```bash
ai-coding init /path/to/project
```

```bash
ai-coding tools pull
```

```bash
ai-coding audit -C /path/to/project --backend docker --output reports/
```

`tools pull` — единственный шаг, которому нужна сеть: он скачивает образы инструментов, закреплённые по digest. Сам аудит работает в песочнице без сети (`--network none`) и никогда не скачивает образы неявно (`--pull never`).

## Что происходит

| Фаза | Шаги |
| --- | --- |
| discovery | конфигурация, состояние git, стеки, **снимок рабочего дерева** |
| execution | `scan.gitleaks` (секреты), `scan.semgrep` (SAST, офлайн-правила), `scan.ruff` (lint, только для Python) — инструменты выбираются по стекам |
| verification | Quality Gates → **проверка целостности** (если дерево изменилось, run → FAILED) → отчёт JSON + Markdown |

## Бэкенды

| `--backend` | Изоляция | Когда использовать |
| --- | --- | --- |
| `docker` (рекомендуется) | контейнер без сети, корень только для чтения, без capabilities, non-root, лимиты CPU/RAM/pids, проект смонтирован только для чтения | всегда, если доступен Docker |
| `local` | бинарники с хоста из allow-list, окружение из allow-list, таймауты, rlimits (POSIX), убийство дерева процессов | без Docker; инструменты должны быть в `PATH` |

На хосте политика разрешает только зарегистрированные инструменты анализа, которые читают проект и не выполняют его код (правило `tools.read_only_analysis`).

## Результат и коды выхода

| Код | Значение |
| --- | --- |
| 0 | PASS: ни один блокирующий гейт не провален |
| 1 | FAIL: провален блокирующий гейт, или run завершился ошибкой (например, нарушена целостность) |
| 2 | Docker недоступен / проект не найден |
| 3 | Неверные опции (`github-actions` до S4, конфигурация Semgrep из реестра, неизвестный инструмент) |

Гейты: PASS / FAIL / SKIPPED, у каждого есть причина и доказательства. Если инструмент не дал результата, гейт получает **FAIL** (fail closed). `--strict` превращает SKIPPED блокирующего гейта в FAIL.

## Baseline существующих проблем

```bash
ai-coding baseline create reports/audit-report.json --owner team-core --reason "legacy, JIRA-123" --expires 2027-03-31
```

Файл `.ai-coding-baseline.json` коммитится в репозиторий и проходит ревью. Принятые находки не влияют на гейты до даты `expires_at`, после неё снова считаются.

## Опции

| Опция | Описание |
| --- | --- |
| `-C DIR` | Каталог проекта |
| `-b, --backend local\|docker` | Переопределить `execution.backend` |
| `-t, --tool NAME` | Запустить только указанные инструменты (повторяемая) |
| `--semgrep-config FILE` | Свои правила Semgrep (только локальный файл) |
| `--strict` | SKIPPED блокирующего гейта = FAIL |
| `-o, --output DIR` | Записать `audit-report.json` и `audit-report.md` |
| `--json` | Вывести JSON-отчёт в stdout |
| `--dry-run` | Показать план и обработчики, ничего не выполнять |

## Импорт проекта

```bash
ai-coding project add https://github.com/org/repo.git --dest ./repo
```

Принимаются только `https://`, `ssh://` и `user@host:path`. `file://`, локальные пути и `ext::` отклоняются. Подмодули не загружаются, hooks отключены, учётные данные не запрашиваются. Без `--full-history` клон неглубокий (`--depth 1`).

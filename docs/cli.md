# CLI `ai-coding`

## Реализовано

| Команда | Описание | Спринт |
| --- | --- | --- |
| `ai-coding --help` / `-h` | Справка | S1 |
| `ai-coding --version` / `-V` | Версия | S1 |
| `ai-coding init [PATH]` | Создать `.ai-coding.yml` | S1 |
| `ai-coding config validate [PATH]` | Проверить конфигурацию | S1 |
| `ai-coding config schema [-o FILE]` | Вывести JSON Schema | S1 |
| `ai-coding run discover [-C DIR] [--dry-run] [--require-approval] [--idempotency-key K] [--json]` | DISCOVERY-прогон: конфигурация, git, стеки, индекс контекста → отчёт-артефакт (offline, без LLM) | S2 |
| `ai-coding run list` | Последние прогоны проекта | S2 |
| `ai-coding run status RUN` | Состояние, задачи, approvals, артефакты | S2 |
| `ai-coding run events RUN` | Журнал переходов состояния | S2 |
| `ai-coding run audit RUN [-o FILE]` | Экспорт audit trail в JSON (AC-10) | S2 |
| `ai-coding run resume RUN [--force] [--yes]` | Продолжить с последнего подтверждённого шага | S2 |
| `ai-coding run cancel RUN [--reason] [--yes] [--dry-run]` | Отменить (кооперативно, если run выполняется) | S2 |
| `ai-coding run approve RUN [--approval ID] [--note] [--resume]` | Подтвердить (только Owner/Maintainer) | S2 |
| `ai-coding run reject RUN [--approval ID] [--note]` | Отклонить; run будет отменён при возобновлении | S2 |
| `ai-coding audit [-C DIR] [-b local\|docker] [-t TOOL]… [--semgrep-config F] [--strict] [-o DIR] [--json] [--dry-run]` | Read-only аудит: секреты, SAST, lint → Quality Gates → отчёт ([audit.md](audit.md)) | S3 |
| `ai-coding baseline create REPORT --owner O --reason R --expires YYYY-MM-DD [-o FILE] [--force]` | Baseline существующих находок с владельцем и сроком | S3 |
| `ai-coding project add SOURCE [--dest DIR] [--branch B] [--depth N] [--full-history]` | Импорт локальной папки или Git URL (безопасный clone) | S3 |
| `ai-coding tools list` | Инструменты, категории, образы, доступность | S3 |
| `ai-coding tools pull [-t TOOL]…` | Явно скачать образы, закреплённые по digest (единственный шаг, требующий сети) | S3 |

`RUN` — полный UUID, короткий id (последние 12 символов, их показывает `run list`) или уникальный префикс. Префикс UUIDv7 — это метка времени, поэтому для коротких id используется случайный хвост. Состояние хранится в `.ai-coding/state.db` проекта; каталог `.ai-coding/` сам себя исключает из git. Другую БД можно указать через `AI_CODING_DATABASE_URL`.

### `init`

| Опция | Описание |
| --- | --- |
| `PATH` | Каталог проекта (по умолчанию текущий) |
| `--name, -n` | Имя проекта (по умолчанию — имя каталога, нормализованное) |
| `--mode, -m` | Режим по умолчанию (`audit`) |
| `--stack, -s` | Явный стек (повторяемая), отключает автоопределение |
| `--detect / --no-detect` | Автоопределение стеков по манифестам |
| `--dry-run` | Вывести конфигурацию без записи |
| `--force` | Перезаписать существующий файл (с подтверждением и `.bak`) |
| `--yes, -y` | Не спрашивать подтверждение |

`init` показывает состояние git (ветка, чистота рабочей копии), найденные стеки с доказательствами (путь к манифесту) и проверяет сгенерированный файл тем же валидатором до записи. Запись атомарная (через временный файл).

### `config validate`

| Опция | Описание |
| --- | --- |
| `PATH` | Файл или каталог (ищется `.ai-coding.yml`, затем `.ai-coding.yaml`) |
| `--format, -f` | `text` (по умолчанию) или `json` |

## Коды выхода

| Код | Значение |
| --- | --- |
| 0 | Успех / конфигурация валидна |
| 1 | Конфигурация невалидна / операция отклонена |
| 2 | Файл или каталог не найден |
| 3 | Неверная комбинация опций |
| 4 | Пользователь отказался от подтверждения |
| 5 | Run ожидает (approval или пауза по бюджету) — не завершён и не провален |
| 6 | Конфликт: run арендован другим worker'ом / Idempotency-Key использован для другого плана |

## Запланировано (ТЗ §14)

`create` (S5), `develop` (S6), `modernize` (S6), `maintain` (S7), `agents list` (S5), `report export` (S4); `audit` расширяется адаптерами стеков в S7. Все изменяющие команды получат `--dry-run` и `--require-approval`.

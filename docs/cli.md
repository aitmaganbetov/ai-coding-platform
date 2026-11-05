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
| `ai-coding run resume RUN [--force] [--yes]` | Продолжить с последнего подтверждённого шага (отказывает, если для оставшихся шагов нужны обработчики другой команды: `pr resume`, `create --resume`) | S2 |
| `ai-coding run cancel RUN [--reason] [--yes] [--dry-run]` | Отменить (кооперативно, если run выполняется) | S2 |
| `ai-coding run approve RUN [--approval ID] [--note] [--resume]` | Подтвердить (только Owner/Maintainer) | S2 |
| `ai-coding run reject RUN [--approval ID] [--note]` | Отклонить; run будет отменён при возобновлении | S2 |
| `ai-coding audit [-C DIR] [-b local\|docker] [-t TOOL]… [--semgrep-config F] [--strict] [-o DIR] [--json] [--dry-run]` | Read-only аудит: секреты, SAST, lint → Quality Gates → отчёт ([audit.md](audit.md)) | S3 |
| `ai-coding baseline create REPORT --owner O --reason R --expires YYYY-MM-DD [-o FILE] [--force]` | Baseline существующих находок с владельцем и сроком | S3 |
| `ai-coding project add SOURCE [--dest DIR] [--branch B] [--depth N] [--full-history]` | Импорт локальной папки или Git URL (безопасный clone) | S3 |
| `ai-coding tools list` | Инструменты, категории, образы, доступность | S3 |
| `ai-coding tools pull [-t TOOL]…` | Явно скачать образы, закреплённые по digest: анализаторы и (S6) рантаймы тестов `python-runtime`, `node-runtime` | S3 |
| `ai-coding report export [RUN] [-i FILE] [-f json\|markdown\|sarif\|junit] [-o FILE]` | Экспорт отчёта аудита (SARIF 2.1.0, JUnit XML, JSON, Markdown) | S4 |
| `ai-coding github comment --pr N --report FILE [--key K] [-R owner/repo]` | PR-комментарий с отчётом без дублей (маркер + upsert) | S4 |
| `ai-coding github check --sha SHA --report FILE [--name N] [-R owner/repo]` | Check run с вердиктом и аннотациями по файлам/строкам | S4 |
| `ai-coding github protection [-b BRANCH] [--json] [-R owner/repo]` | Проверка защиты ветки (rulesets); exit 1 при пробелах | S4 |
| `ai-coding pr open -b BRANCH -t TITLE [--body-file F] [--base B] [--draft] [--remote R] [-R owner/repo] [-C DIR] [--require-approval] [--dry-run]` | Push рабочей ветки и PR под политикой и approval `pr_create`; без merge/auto-merge ([github.md](github.md)) | S4 |
| `ai-coding pr resume RUN [-R owner/repo] [-C DIR]` | Продолжить PR-прогон после approval (обработчики PR) | S4 |
| `ai-coding create [IDEA] [--idea-file F] --dest DIR [--config F] [--name N] [--constraint C]… [--branch B] [--base B] [--pr [-R owner/repo] [--pr-title T]] [--interactive/--no-interactive] [--dry-run] [--require-approval]` | CREATE: требования → approval версии → ADR → backlog → каркас в ветке → (PR) ([create.md](create.md)) | S5 |
| `ai-coding create --resume RUN --dest DIR` / `--revise RUN --answers F --dest DIR` | Продолжить CREATE-прогон / новая версия ТЗ с ответами на вопросы | S5 |
| `ai-coding agents list [-C DIR] [--json]` | 12 агентов ТЗ §6: статус, вход → выход, нужен ли LLM, цепочка моделей | S5 |
| `ai-coding develop [TASK] [--issue N] [--task-file F] [-C DIR] [--base B] [--branch B] [--file P]… [--max-iterations N] [--strict] [--no-review] [--pr [-R owner/repo]] [--resume RUN] [--interactive/--no-interactive] [--dry-run] [--require-approval]` | DEVELOP: задача/issue → изменения с тестами → тесты в песочнице → review → коммит в рабочую ветку → проверка → (PR) ([develop.md](develop.md)) | S6 |
| `ai-coding modernize GOAL \| --continue RUN [--step M-NN] \| --rollback RUN [-C DIR] [--allow-red-baseline] [--pr] [--resume RUN] [--dry-run] [--require-approval]` | MODERNIZE: baseline → анализ влияния и план → approval → один шаг = одна ветка/PR; откат шага через `git revert` | S6 |
| `ai-coding review --base B [--head H] [-C DIR] [--pr N --comment] [--check] [-o FILE] [--json] [--skip-if-disabled]` | Рекомендательный AI review diff (комментарий, check run); ненулевой код только при блокирующей политике | S6 |

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

`maintain` (S7); `audit` расширяется адаптерами стеков в S7. Все изменяющие команды получат `--dry-run` и `--require-approval`.

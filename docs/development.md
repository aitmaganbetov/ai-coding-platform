# Разработка

## Окружение

```bash
uv sync
```

Все команды — через `uv run` (или `make <цель>` на POSIX).

| Задача | Команда |
| --- | --- |
| Тесты | `uv run pytest` |
| Тесты с покрытием (порог 80%) | `uv run pytest --cov` |
| Только unit / только интеграционные | `uv run pytest tests/unit` / `uv run pytest -m integration` |
| Линтер | `uv run ruff check .` |
| Форматирование | `uv run ruff format .` |
| Типы (strict) | `uv run mypy` |
| Архитектурные слои | `uv run lint-imports` |
| Регенерация JSON Schema | `uv run ai-coding config schema -o src/ai_coding/schemas/ai-coding.schema.json` |
| Всё, что проверяет CI | `make check` |
| Аудит зависимостей | `make security` |

## Правила

1. **Ветки.** `main` защищена; работа — в feature-ветках, изменения — через PR. Прямой push в `main` запрещён.
2. **Тесты обязательны.** Каждая задача сопровождается тестами; тест считается пройденным только после фактического запуска (локально и в CI). Покрытие core ≥ 80% (NFR-06), критические переходы состояний — 100% ветвей.
3. **Без заглушек.** Модуль появляется вместе с рабочим кодом и тестами. Тестовые двойники (fake-провайдеры) живут только в `tests/`.
4. **Слои.** Новый модуль добавляется в контракт `[tool.importlinter]` в том же PR (ADR-0003).
5. **Схема конфигурации.** После изменения `config/models.py` регенерируйте JSON Schema — иначе упадут тест и CI (drift check).
6. **Зависимости.** Добавляются вместе с кодом, который их использует; `uv add <pkg>` обновляет `uv.lock`; CI проверяет `uv lock --check`.
7. **Безопасность.** Никаких секретов в коде, тестах и фикстурах (gitleaks в CI). Тестовые «секреты» собираются динамически. Подпроцессы — только argv-список, без `shell=True`, с таймаутом. Вывод, попадающий в логи/отчёты, проходит через `ai_coding.security.redact`.
8. **ADR.** Изменение архитектурного инварианта — новый ADR в `docs/adr/`.
9. **Commit messages** — Conventional Commits (`feat:`, `fix:`, `docs:`, `test:`, `ci:`, `refactor:`).

## Структура тестов

```text
tests/
├── conftest.py        # фикстуры: изолированная git-конфигурация, временный репозиторий
├── unit/              # быстрые тесты без внешних процессов (кроме CLI-раннера)
├── integration/       # реальный git в tmp-каталогах (маркер integration)
└── fixtures/configs/  # эталонные .ai-coding.yml
```

Интеграционные git-тесты изолированы от `~/.gitconfig` разработчика (`GIT_CONFIG_GLOBAL=/dev/null`, `GIT_CONFIG_NOSYSTEM=1`).

Асинхронные тесты — `pytest-asyncio` (`asyncio_mode = "auto"`): достаточно объявить `async def test_…`. Базы данных, открытые через `tests/helpers.make_orchestrator`, закрываются автоматически. Предупреждения (включая `ResourceWarning` о незакрытых соединениях) — ошибки (`filterwarnings = error`). Тестовые двойники (`ScriptedHandler`, `FakeProvider`) живут только в `tests/`.

Docker-тесты помечены `docker` и пропускаются, если нет демона или образов (неявных скачиваний нет). Полный прогон локально:

```bash
uv run ai-coding tools pull
```

```bash
uv run pytest -m "docker or integration" -rs
```

Для тестов Local-бэкенда `gitleaks` должен быть в `PATH`. В CI это делает job `sandbox`: он ставит gitleaks с проверкой checksum, скачивает образы, запускает тесты и **падает, если хоть один из них пропущен**, а затем аудирует сам репозиторий.

Тест восстановления (`tests/integration/test_recovery.py`) запускает настоящий процесс-worker и убивает его `os._exit` посреди задачи.

## Схема БД и миграции

Модели — `src/ai_coding/persistence/models.py`, миграции — `src/ai_coding/persistence/migrations/versions/`. После изменения моделей:

```bash
uv run python -c "from alembic import command; from ai_coding.persistence.migrate import alembic_config; command.revision(alembic_config('sqlite:///tmp-gen.db'), message='describe change', autogenerate=True)"
```

Проверьте сгенерированный файл вручную (миграции не должны импортировать код приложения) и удалите `tmp-gen.db`. Тест `test_migration_matches_models` падает, если схема и модели расходятся.

## Добавление CLI-команды

1. Модуль в `src/ai_coding/cli/commands/<name>.py`, регистрация в `cli/app.py`.
2. Бизнес-логика — вне CLI (CLI только разбирает аргументы и печатает).
3. Изменяющие операции поддерживают `--dry-run`; опасные — подтверждение (`--yes` для неинтерактивного режима) и `--require-approval` (S2).
4. Коды выхода — `cli/exit_codes.py`; документировать в [cli.md](cli.md).

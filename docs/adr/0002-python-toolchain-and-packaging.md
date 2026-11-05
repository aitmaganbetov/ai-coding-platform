# ADR-0002: Python 3.12+, src-layout, hatchling, uv + lockfile

- Статус: Принято
- Дата: 2026-11-05

## Контекст
Ядро — Python 3.12+ (ТЗ §4). NFR-01: CLI на Linux/Windows/macOS. NFR-05: воспроизводимость на закреплённых версиях.

## Решение
- **src-layout** (`src/ai_coding`) — тесты всегда работают с установленным пакетом, а не с рабочей копией.
- **Сборка**: `hatchling` (PEP 517/621), версия в `ai_coding/__init__.py`.
- **Зависимости**: диапазоны совместимости в `pyproject.toml` (`>=min,<next-major`), точная фиксация — `uv.lock` (коммитится, CI проверяет `uv lock --check` и запускает с `UV_FROZEN=1`).
- **Инструменты разработки** (dependency group `dev`): pytest, pytest-cov, ruff, mypy (strict), import-linter.
- **Поддерживаемые Python**: 3.12, 3.13 (матрица CI на трёх ОС).
- **Runtime-зависимости S1**: typer, rich, pydantic v2, PyYAML, jsonschema. Последующие спринты добавляют зависимости только вместе с использующим их кодом:

| Спринт | Зависимости (план) |
| --- | --- |
| S2 | SQLAlchemy 2.x, Alembic, structlog |
| S3 | docker (Docker SDK) — опционально, extra `docker` |
| S4 | httpx (GitHub REST/GraphQL), PyJWT (GitHub App) |
| S5 | openai, anthropic SDK — extras `openai`, `anthropic`; Ollama/vLLM через httpx (OpenAI-совместимый API) |
| S8 | FastAPI, uvicorn, psycopg 3, redis (extra), argon2-cffi |

## Последствия
`pip install ai-coding-platform` остаётся лёгким; тяжёлые и сетевые зависимости — через extras. Обновления — Dependabot (`uv`, `github-actions`).

## Альтернативы
Poetry — отклонено (медленнее, нестандартные секции); setuptools — больше конфигурации; flat-layout — риск тестировать неустановленный код.

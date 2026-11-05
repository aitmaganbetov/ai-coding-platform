# ADR-0015: Интеграция с GitHub Actions

- Статус: Принято (CI самого проекта — S1; reusable workflows — S4)
- Дата: 2026-11-05

## Контекст
ТЗ §9: reusable workflows `quality, security, test-php, test-node, test-python, test-go, test-rust, ai-review, release`; caller workflows через `workflow_call` с минимальными permissions; Actions закреплены на SHA; секреты — через environments и approval; fork PR — непривилегированный read-only; запрет `pull_request_target` с write-token для недоверенного кода; self-hosted runners — только доверенные задания. GitHub Actions — не offline-оркестратор.

## Решение
- CI самого проекта: `.github/workflows/ci.yml` (lint, mypy, import-linter, schema drift, тесты на Linux/Windows/macOS × Python 3.12/3.13, сборка и smoke-тест wheel) и `repo-security.yml` (gitleaks по всей истории с проверкой SHA-256 бинарника, pip-audit по lockfile). Имена `quality.yml`, `security.yml`, `test-*.yml` зарезервированы под reusable workflows S4.
- Все сторонние actions — `uses: owner/repo@<40-символьный SHA> # vX.Y.Z`; обновление — Dependabot.
- `permissions: contents: read` на уровне workflow; job'ы расширяют права точечно (`checks: write`, `pull-requests: write`) только в доверенных событиях.
- `persist-credentials: false` для checkout.
- Reusable workflows (S4) выдают отчёты SARIF/JUnit и единый JSON-итог платформы; `ai-review` запускается только для PR из того же репозитория или после ручного approval через environment.
- Проверка workflow-файлов: actionlint и zizmor (локально и в S4 — в CI).

## Последствия
GitHub Hybrid использует те же инструменты и гейты, что и Local; GitHub остаётся опциональным.

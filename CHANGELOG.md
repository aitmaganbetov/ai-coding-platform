# Changelog

Формат — [Keep a Changelog](https://keepachangelog.com/ru/1.1.0/), версии — [SemVer](https://semver.org/lang/ru/). Номер версии совпадает с версией ТЗ, которую реализует релиз.

## [2.1.0] — 2026-11-05

Первый выпуск. Реализует ТЗ «AI Coding Platform v2.1 — Local, Self-Hosted & GitHub Hybrid». Все приёмочные сценарии AC-01…AC-10 автоматизированы и проходят в CI ([docs/acceptance.md](docs/acceptance.md)). Отчёты спринтов — [docs/sprints/](docs/sprints/).

### Режимы и сценарии

- **CREATE** (S5) — новый проект из описания: согласованная версия ТЗ, ADR, backlog, каркас в ветке, PR.
- **DEVELOP / MODERNIZE / REVIEW** (S6) — задача или issue → изменения с тестами → тесты в песочнице → AI review → ветка → PR. Модернизация малыми шагами с откатом.
- **AUDIT** (S3, S7) — read-only и офлайн:
  - секреты, SAST, зависимости (Trivy, OSV-Scanner) по офлайн-базам;
  - инструменты стеков: PHPStan, Pint, ESLint, `tsc`, mypy, `go test`, golangci-lint, `cargo test`, Clippy;
  - E2E на Playwright и нагрузка на k6 (S9);
  - Quality Gates, baseline, SARIF и JUnit.
- **MAINTAIN** (S7) — план → approval → обновление зависимостей пакетным менеджером → исправление с регрессионным тестом → PR.
- **12 агентов ТЗ §6** со схемами вывода и циклом исправления. LLM-адаптеры OpenAI, Anthropic, Ollama и vLLM с маршрутизацией, fallback, кэшем и бюджетами (S5).

### Оркестратор и исполнение

- Конечный автомат, DAG, checkpoints, retry, политики и approvals, журнал аудита. Прогон восстанавливается после падения worker'а без повторных коммитов (S2).
- Executors:
  - Local — allow-list команд; rlimits на POSIX, Job Objects на Windows (S9);
  - Docker-песочница — без сети и capabilities, только для чтения;
  - GitHub Actions (S4).
- **Egress-allowlist** (S9): шаги с сетью работают в изолированной сети, где единственный выход — прокси со списком хостов. Без DNS, без прямых маршрутов, с защитой от SSRF.
- **gVisor** (S9): `execution.docker.runtime: runsc`.

### GitHub

- PR под политикой и approval (без merge), check runs и комментарии без дублей, проверка правил ветки, reusable workflows, пилоты на двух стеках (S4).

### Self-Hosted сервер (S8, S9)

- REST API ТЗ §13 с `Idempotency-Key`.
- Пользователи и токены; RBAC на сервере; изоляция проектов — чужое неотличимо от несуществующего.
- Журнал аудита, включая попытки доступа к чужим проектам.
- Очередь в БД (PostgreSQL `SKIP LOCKED`), worker'ы на ≥ 10 прогонов, `serve --workers N`, общий rate limiter.
- Web UI на React, метрики Prometheus, хранилище артефактов S3/MinIO.
- docker-compose с Caddy (TLS), backup, ротация ключей, экспорт и retention.

### Качество и безопасность (S9)

- Приёмочная матрица AC-01…AC-10 в CI.
- Нагрузочный тест k6 (API + worker'ы на PostgreSQL).
- Проверка прав по всем маршрутам OpenAPI, fuzzing API (Schemathesis), конфигурации и redaction (Hypothesis), 26 проб выхода из песочницы.
- Цепочка поставок:
  - SBOM в CycloneDX (пакет, Web UI, образ);
  - SLSA provenance и SBOM attestations (Sigstore);
  - подпись образа cosign;
  - черновик релиза, публикуемый человеком.

### Исправлено в S9

- DoS через YAML-«billion laughs» в `.ai-coding.yml`.
- `security.network: local-only` пропускал внешний `base_url` у Ollama и vLLM.
- Токены `aic_…` не маскировались в логах.
- Ошибки фреймворка шли не в едином формате API.
- Разбор YAML и JSON Schema на каждый запрос к прогону.
- Инструмент, пишущий в копию дерева, терял установленные зависимости проекта.

### Требования к установке

- Python 3.12+; Docker для песочницы; PostgreSQL 15+ для сервера.
- Миграции: `0001_initial`, `0002_server`, `0003_rate_limits` (`ai-coding server migrate`).

[2.1.0]: https://github.com/aitmaganbetov/ai-coding-platform/releases/tag/v2.1.0

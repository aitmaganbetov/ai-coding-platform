# Отчёт Sprint S7 — AUDIT/MAINTAIN, инструменты стеков, агенты, prompt injection

Дата: 2026-11-05 · Ветка: `claude/cool-sagan-ho737s` · Приёмка ТЗ §16: «Воспроизводимые результаты сканеров» (AC-02, AC-07).

## 1. Результат приёмки

| Критерий | Чем подтверждается | Результат |
| --- | --- | --- |
| **AC-02:** подключить существующий Laravel+React проект, выполнить read-only аудит, получить подтверждённые находки с путями файлов | `tests/integration/test_pilot_audit.py` на пилоте `tests/fixtures/pilot-laravel-react`. **Настоящий Docker без сети:** 18 находок, у каждой путь — SQL-инъекция и `unserialize` (`app/Http/Controllers/ReportController.php:14/20`), XSS в React (`Comment.jsx:5`), AWS-ключ (`config/services.php:2`), CVE guzzlehttp/psr7 (`composer.lock`) и lodash (`package-lock.json`), root в `Dockerfile`. Секрет в отчёт не попадает. Целостность репозитория подтверждена | ✅ |
| **Воспроизводимость** | Два аудита дают одинаковые fingerprint и версии. В отчёте для каждого инструмента: версия, digest образа, снимок БД (`db_updated_at`) | ✅ тот же тест + `test_trivy_offline_is_reproducible` |
| **AC-07:** prompt injection из README/issue/diff не даёт агенту прав | `tests/integration/test_prompt_injection.py`: 7 сценариев, модель **выполняет** инъекцию. Запись в `.git/hooks` отклонена; CI и `.ai-coding.yml` требуют approval; `main` не меняется; тесты без сети; `.env` и токен не уходят в модель; инъекция не становится system-текстом и не закрывает `<input>`; одобрение review не заменяет тесты; triage не отклоняет секрет; обновление до версии злоумышленника отклонено | ✅ |
| B-600…B-602: адаптеры | Trivy, OSV-Scanner, PHPStan, Pint, ESLint, `tsc`, mypy, `go test`, golangci-lint, `cargo test`, Clippy. Парсеры проверены на **реальном выводе** инструментов (`tests/fixtures/tools/`). go test, golangci-lint, cargo test, Clippy и Trivy запущены в настоящем Docker | ✅ (OSV — в CI, см. §5) |
| B-604…B-608: агенты | Security, QA, Performance, DevOps, Maintenance — схемы, семантические проверки, цикл исправления. **Все 12 агентов ТЗ §6 реализованы** | ✅ `test_agents_s7.py`, `test_agents_cli.py` |
| B-609, B-612: полный `ai-coding audit` | Инструменты по стекам, рекомендательный `--triage`, `--resume`, отчёт v2 | ✅ |
| B-610, B-612: `ai-coding maintain` | Источники → план → approval → обновление зависимостей пакетным менеджером → исправление с регрессионным тестом → проверка → PR; `--continue` | ✅ `test_maintain.py` |
| B-225: ignore-файлы в отчёте | `.semgrepignore`, `.gitleaksignore`, `.trivyignore`, inline-маркеры, неявный ignore Semgrep | ✅ `test_audit_s7.py` |
| B-210 follow-up: entry points инструментов | Группа `ai_coding.tools` | ✅ |
| B-603: Playwright, k6 (P1) | **Перенесено в S9**: нужны запущенное приложение и сеть. Performance Agent уже анализирует переданные результаты k6/APM (ADR-0027) | ⏭️ |

## 2. Структура (новое в S7)

```
src/ai_coding/
  agents/{security,qa,performance,devops,maintenance}.py   # 5 агентов
  schemas/agents/*.output.schema.json                      # +5 схем (всего 13)
  tools/
    project.py      # ProjectTool + ProjectToolRunner (песочница, /work RO или копия, /deps)
    cache.py        # кэш платформы для офлайн-данных
    prepare.py      # подготовка БД и toolchain в `tools pull`
    deps.py         # обновление зависимостей пакетным менеджером
    trivy.py osv.py php.py node.py mypy.py golang.py rust.py
  quality/suppressions.py      # B-225
  workflows/maintain.py        # MAINTAIN
  workflows/audit.py           # + audit.suppressions, audit.triage, отчёт v2
  workflows/agent_runtime.py   # + build_agent_run
  cli/commands/{maintain.py, agents.py (run), audit.py (--triage, --resume), tools.py (--json, --refresh)}
tests/fixtures/pilot-laravel-react/   # пилот AC-02
tests/fixtures/tools/                 # реальный вывод инструментов
```

## 3. Архитектурные решения

- [ADR-0027](../adr/0027-project-tools-and-offline-data.md) — инструменты стеков только в песочнице; установка зависимостей — единственный сетевой шаг по политике; SKIPPED вместо подделки; офлайн-данные в кэше платформы; подавления и воспроизводимость в отчёте.
- [ADR-0028](../adr/0028-s7-agents-and-advisory-triage.md) — пять агентов и их проверки; `agents run`; triage рекомендательный и в фазе execution; модель угроз prompt injection.
- [ADR-0029](../adr/0029-maintain-and-dependency-upgrades.md) — MAINTAIN: план утверждается по версии, одна задача на прогон, lock-файлы создаёт пакетный менеджер.

## 4. Реализованные компоненты

| Компонент | Ключевое |
| --- | --- |
| Каркас инструментов | `ToolRun.skipped/coverage/metadata`, `skipped_run`. Гейты исключают SKIPPED-инструменты и объясняют SKIPPED-гейт. Гейт Coverage работает по покрытию тестовых инструментов. Entry points `ai_coding.tools` |
| Trivy | Офлайн (`--skip-db-update --offline-scan`): уязвимости и misconfiguration. БД в кэше, снимок в отчёте |
| OSV-Scanner | Офлайн-БД по экосистемам (seed-lockfile). Запуск через `sandbox_entry` |
| PHPStan, Pint | `vendor/bin`. Если `vendor/` нет — `composer install` по политике. Уровень 5 без конфигурации |
| ESLint, `tsc` | `node_modules`. Если их нет — `npm ci --ignore-scripts` по политике |
| mypy | Только если настроен. Устанавливается в `/deps` без изменения проекта |
| go test, golangci-lint | Копия дерева, покрытие, ошибки сборки как находки |
| cargo test, Clippy | Копия дерева, `--offline`. Clippy из toolchain в кэше. JSON-строки дедуплицируются |
| `tools list/pull` | `--json`, колонка офлайн-данных, путь кэша; `pull` готовит БД и toolchain, `--refresh` |
| Агенты S7 | См. ADR-0028; `ai-coding agents run` |
| AUDIT | `audit.suppressions`, `audit.triage` (advisory, approval-пауза, `--resume`), отчёт v2: triage, подавления, воспроизводимость, SKIPPED с причиной |
| MAINTAIN | `maintain.baseline/plan/approve/deps/item/verify` + PR; `ai-coding maintain` |
| Обновление зависимостей | npm, composer, go, cargo — в песочнице на копии; PyPI — закрепление в `requirements.txt` |
| Цикл реализации | `seed` — детерминированные изменения в начале; подсказки проходят исключения и redaction |
| Redaction | + заголовки `Authorization: Bearer/Basic/Token` и `bearer <token>` |

## 5. Фактически запущенные проверки (Linux, Docker, 2026-11-05)

| Проверка | Результат |
| --- | --- |
| `pytest --cov`, Python 3.13 (Docker: gitleaks, semgrep, trivy + БД, golang, golangci-lint, rust + Clippy toolchain, composer, node, python) | **800 passed**, 2 skipped по окружению (см. ниже), покрытие **94.6%** |
| `pytest`, Python 3.12 (отдельное окружение `uv sync --python 3.12`) | **801 passed**, 1 skipped по окружению |
| Новые тесты S7 | адаптеры и runner 26, агенты 11 + CLI 3, аудит 4 + triage 3 + пилот 1, инструменты в Docker 4, MAINTAIN 3 + 6, prompt injection 7, redaction 1 |
| ruff, ruff format, mypy strict (148 файлов), import-linter | без замечаний |
| Дрейф схемы конфигурации; `uv lock --check` | нет / OK (новых зависимостей нет) |
| actionlint; zizmor `--offline` | без замечаний; zizmor показывает 2 предупреждения `dependabot-cooldown`, они уже есть в `main` |
| Самоаудит репозитория (gitleaks + semgrep в Docker) после полного прогона тестов | PASS, 0 находок, репозиторий не изменён |
| Wheel | 13 схем агентов; команды `maintain`, `agents run` доступны |

Пропуски в этой среде:

1. `test_npm_upgrade_writes_a_real_lock_file` — egress здесь идёт через прокси на loopback хоста, а из контейнера он недоступен. Та же команда `npm install … --package-lock-only` проверена вручную с сетью хоста: lock-файл с `integrity sha512-…` создан. В CI тест выполняется.
2. `test_cli.py` (console script) — в venv для 3.13 не установлен entry point. В окружении 3.12 тест прошёл.

CI на GitHub: результаты добавляются после прогона PR (раздел обновляется).

## 6. Найденные и исправленные проблемы

1. **Утечка секретов через файлы-подсказки** (нашли тесты AC-07). Файлы, которые план агента указывает в MODERNIZE/MAINTAIN, добавлялись в контекст модели мимо исключений `.env` и `*.pem`. Закоммиченный `.env`, указанный в плане, попадал в prompt. Теперь подсказки проходят те же исключения и redaction; без исправления тест падает (проверено).
2. **Triage проглатывал approval.** `ApprovalRequiredError` и `BudgetExceededError` — подклассы `PermanentError`, поэтому рекомендательный шаг превращал паузу в «triage failed». Исправлено и закреплено тестом.
3. **Approval посреди прогона возможен только в фазе execution** (FSM). Triage перенесён туда, а baseline он читает сам. Добавлен `audit --resume`: общий `run resume` справедливо отказывает и даёт подсказку.
4. **Сбой модели (`AllRoutesFailedError`) валил весь аудит.** Теперь triage помечается как failed, аудит остаётся действительным.
5. **Ключ в `.pyc`.** Python сворачивает `"AKIA" + "…"` в одну константу, и gitleaks самоаудита находил ключ в `__pycache__` после тестов — CI бы упал. Тестовые ключи теперь собираются в рантайме.
6. **Redaction не закрывал заголовки `Authorization: Bearer`.** Добавлены шаблоны и тест.
7. **Maintenance Agent запрещал новые файлы:** любой путь не из репозитория считался выдумкой. Задача может создавать код; оставлена только проверка безопасности пути.
8. **`go test` запускает `go vet`**, поэтому ошибка printf — ошибка сборки, а не находка линтера. Пилотный Go-тест исправлен; поведение адаптера верное.
9. **Trivy не открывал БД на RO-монтировании** (bbolt пишет lock). Кэш монтируется изменяемым — это данные платформы, не проекта.
10. **В slim- и полном образе Rust нет Clippy.** Toolchain с Clippy ставится в кэш при `tools pull`.
11. **`cargo` пишет `Cargo.lock`, Go — артефакты сборки.** Для них используется копия дерева.
12. **OSV-образ:** `ENTRYPOINT /osv-scanner` не совпадает с местом копирования бинарника в Dockerfile v2.0.2. Блобы ghcr.io в этой среде недоступны, поэтому запуск сделан устойчивым (`sandbox_entry`): берётся первый существующий исполняемый файл, иначе понятная ошибка 127.
13. **Composer честно отказывает** в `guzzlehttp/psr7:1.8.4` при Laravel 11 (конфликт с Guzzle 7). Ошибка показывает строку конфликта, а не общий хвост вывода.
14. **Таблица `tools list` в 80 колонках обрезала имена.** Добавлен `--json`, тест опирается на него.
15. **`--dry-run` в `maintain` не читал локальный файл issues.** Исправлено: dry-run не обращается только к GitHub.
16. **Таймаут CI-job sandbox** поднят с 20 до 50 минут: 14 образов и офлайн-данные.

## 7. Ограничения и незавершённое

- **OSV-Scanner в Docker не запускался локально** (блобы ghcr.io заблокированы прокси). Парсер проверен на документированном формате. Подготовка БД и сканирование выполняются в CI (`tools pull` и пилотный тест).
- **Реальные модели не проверялись** (нет ключей и загрузки моделей). Агенты проверены против fake-сервера в формате API провайдера.
- **PHPStan, Pint и ESLint на пилоте без сети — SKIPPED** с причиной. Для их работы нужны `network: bridge` и egress к Packagist/npm, либо готовые `vendor/` и `node_modules/`.
- **Проверка «уязвимость больше не находится» после MAINTAIN** — пока отдельный `ai-coding audit` на ветке.
- **QA, Performance и DevOps** запускаются через `agents run`. Встраивание в API и UI — S8.
- **B-603 (Playwright, k6)** перенесён в S9.

## 8. План Sprint S8 — Self-Hosted API, RBAC, Web UI, observability

1. FastAPI: endpoints ТЗ §13, OpenAPI, Idempotency-Key (B-700).
2. Аутентификация, токены API, RBAC, межпроектная изоляция (B-701, AC-08).
3. Rate limiting, security headers, TLS через reverse proxy (B-702).
4. PostgreSQL, очередь в БД с `SKIP LOCKED`, Redis-адаптер (B-703); worker'ы, ≥10 параллельных заданий (B-704).
5. MinIO ArtifactStore (B-705); Web UI на React+TS: проекты, runs, approvals, findings, audit (B-706) — в том числе запуск агентов QA, Performance и DevOps.
6. Метрики Prometheus, OpenTelemetry (B-707); docker-compose, Dockerfile, backup (B-708); экспорт и retention (B-709).

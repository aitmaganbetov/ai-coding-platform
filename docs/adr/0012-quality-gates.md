# ADR-0012: Детерминированные Quality Gates

- Статус: Принято; движок и baseline — S3 (семантика SKIPPED уточнена в ADR-0020); адаптеры стеков — S7.
- Дата: 2026-11-05

## Контекст
ТЗ §10: Build, Tests, Static, Security, Coverage, Review блокируют merge. AI Review — рекомендательный; блокировка только при явной утверждённой политике и проверяемых результатах. Baseline существующих проблем с владельцем и сроком действия. FR-11.

## Решение
- Результат гейта — `GateResult`: `PASS`, `FAIL`, `SKIPPED` (+ причина и ссылки на evidence). `SKIPPED` блокирующего гейта трактуется политикой (по умолчанию: блокирует, если гейт обязателен для стека).
- Гейт вычисляется **только** из нормализованных результатов реальных инструментов (exit code, SARIF, JUnit, coverage XML); вывод LLM на статус не влияет.
- Адаптеры инструментов (`ai_coding.tools`, порт `ToolAdapter`: `detect()`, `version()`, `command()`, `parse() -> findings/metrics`):

| Категория | Инструменты |
| --- | --- |
| Security | Semgrep, Gitleaks, Trivy, OSV-Scanner |
| PHP | PHPStan, Pint, PHPUnit/Pest |
| JS/TS | ESLint, TypeScript (`tsc --noEmit`), Vitest/Jest |
| Python | Ruff, mypy, pytest |
| Go | `go test`, golangci-lint |
| Rust | `cargo test`, Clippy |
| E2E / Perf | Playwright, k6 |

- Версии инструментов закрепляются (образы Docker с digest), записываются в отчёт (NFR-05).
- Baseline: файл `.ai-coding/baseline.json` с fingerprint находки, `owner`, `expires_at`, `reason`; просроченный baseline → находка снова блокирует.

## Последствия
Гейты воспроизводимы и объяснимы (AC-06: человек видит причины и доказательства).

## Альтернативы
Оценка качества LLM-агентом как гейт — отклонено ТЗ.

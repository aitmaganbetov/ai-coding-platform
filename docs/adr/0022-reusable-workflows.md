# ADR-0022: Reusable workflows — права, форки, установка платформы, закрепление версий

- Статус: Принято
- Дата: 2026-11-05
- Спринт: S4 (B-310…B-313); развивает ADR-0015

## Контекст
ТЗ §9 требует:
- reusable workflows через `workflow_call`;
- минимальные permissions;
- закрепление actions по SHA;
- секреты только через environments с approval;
- fork PR только для чтения;
- без `pull_request_target` для недоверенного кода.

## Решение
1. **Набор.**
   - `quality.yml` и `security.yml` запускают `ai-coding audit` (Docker-песочница, офлайн после `tools pull`) и публикуют результаты.
   - `test-python|node|php|go|rust.yml` запускают тесты стека и выгружают JUnit.
   - `release.yml` собирает релиз только по тегу через environment с approval: SHA256SUMS, provenance-аттестация, draft-релиз с `--verify-tag`.
   - `ai-review.yml` перенесён в S6, потому что ему нужны LLM-адаптеры (S5). Пустую заглушку не делаем (правило «без фиктивных реализаций»).
2. **Права.**
   - На верхнем уровне `permissions: {}`.
   - Job получает только нужное: `contents: read`, `pull-requests: write`, `checks: write`, `security-events: write` (последнее — только `security.yml`).
   - Вызывающий workflow обязан выдать эти права явно.
3. **Форки.**
   - `SAME_REPO` вычисляется из события.
   - Для PR из форка комментарий, check run и загрузка SARIF пропускаются. Аудит и вердикт выполняются, отчёт остаётся артефактом.
   - `pull_request_target` не используется. Это проверяет тест `tests/unit/test_workflow_files.py`.
4. **Защита от template injection.**
   - Входы и поля события попадают в `run:` только через `env:`. Выражения `${{ inputs.* }}` и `${{ github.event.* }}` в скриптах запрещены тестом.
   - Пользовательские команды тестов передаются через env и выполняются `bash -c "$CMD"`.
5. **Установка платформы.**
   - Вход `platform-spec`: по умолчанию git-спецификация репозитория платформы; `"."` означает текущий checkout (используется пилотами).
   - Установка — `uv tool install`.
   - Образы инструментов скачиваются явно (`ai-coding tools pull`) и закреплены по digest.
6. **Закрепление версий.**
   - Все сторонние actions закреплены по 40-символьному SHA с комментарием версии; это проверяет тест.
   - Обновления приходят через Dependabot.
   - Вызывающим рекомендуется закреплять и сами reusable workflows по SHA (`@<sha>`).
7. **Проверка.** actionlint и zizmor запускаются локально и в CI (job `workflows` в `repo-security.yml`, версии закреплены). Правило zizmor `self-repository` отключено только для `pilots.yml` (`.github/zizmor.yml`): пилоты нарочно вызывают локальные версии workflow, чтобы каждый PR проверял изменённые workflow.
8. **Пилоты (AC-09).** `examples/pilot-python` (uv, pytest, ruff) и `examples/pilot-node` (`node --test`, gitleaks и semgrep) проверяются workflow `pilots.yml` на каждом PR платформы через те же reusable workflows, что получит внешний пользователь.

## Последствия
Внешний репозиторий подключает проверки несколькими строками `uses:`. GitHub при этом остаётся опциональным: те же гейты работают локально (`ai-coding audit`).

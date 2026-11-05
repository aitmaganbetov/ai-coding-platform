# ADR-0021: GitHub-интеграция — клиент, порт RepositoryHost, PR, checks, Actions executor

- Статус: Принято
- Дата: 2026-11-05
- Спринт: S4 (B-300…B-304, B-320, B-321)

## Контекст
ТЗ §4 требует Git-адаптеры и адаптер GitHub Actions. FR-17 и AC-03 требуют создавать PR только через approval, не делать push в защищённые ветки и не включать auto-merge. FR-10 требует отчёты в PR. В режиме GitHub Hybrid (§3) GitHub опционален: локальный оркестратор остаётся главным (§15).

## Решение
1. **Порт `RepositoryHost`** (`integrations/host.py`) — `Protocol` с операциями платформы: найти, получить или создать PR, upsert комментария, check run, commit status, отчёт о защите ветки. Merge, auto-merge, force-push и удаления веток в порт **намеренно не входят**. Workflows зависят от порта, а не от GitHub. GitLab или Gitea можно добавить отдельным адаптером.
2. **HTTP-клиент на httpx** (`integrations/github/client.py`):
   - только https (исключение — `http://localhost` для тестового двойника);
   - повторы при сетевых ошибках и 5xx с экспоненциальной задержкой;
   - при rate limit (`retry-after`, `x-ratelimit-*`) клиент ждёт не дольше `max_wait`, иначе бросает `RateLimitError`;
   - пагинация по `Link`;
   - ошибки проходят через redactor.

   REST покрывает все нужные операции, поэтому GraphQL не используется.
3. **Аутентификация.**
   - Fine-grained token: `AI_CODING_GITHUB_TOKEN` или `GITHUB_TOKEN`.
   - GitHub App: JWT RS256 со сроком жизни 9 минут обменивается на installation token, который кэшируется до истечения минус 2 минуты.
   - Оба токена регистрируются в redactor. Секреты читаются только из окружения или файла и в конфиге не хранятся.
4. **Идемпотентность (AC-04).**
   - `create_pull_request` сначала ищет открытый PR с той же парой head/base.
   - Комментарий помечается маркером `<!-- ai-coding:{key} -->` и обновляется (upsert), а не дублируется.
   - В workflow `pull_request` push и открытие PR проходят через журнал эффектов. Пробы (SHA удалённой ветки, существующий PR) не дают повторить эффект при resume.
5. **Push.**
   - `LocalGitRepository.push` выполняет `--no-verify`, без force, только явный refspec `refs/heads/b:refs/heads/b`.
   - Push в защищённые ветки запрещён.
   - Токен передаётся через `GIT_CONFIG_COUNT/KEY/VALUE` (`http.extraHeader`), не попадает в argv, URL или `.git/config`.
   - Egress-политика проверяется до выхода в сеть, и для remote, и для API.
6. **Approval.** Шаг `github.pr` требует `pr_create` на уровне плана (в состоянии PLANNED). Возобновлять PR-прогон нужно через `ai-coding pr resume`: он собирает реестр PR-обработчиков, а параметры берёт из сохранённых входов шагов. Универсальный `run resume` **отказывается** продолжать прогон, если для оставшихся шагов нет обработчиков. Иначе он провалил бы прогон с «no route».
7. **Проверка после создания.** Шаг `github.pr.verify` проверяет, что head SHA PR равен отправленному SHA, base совпадает, а auto-merge выключен.
8. **Checks и statuses.** Основной канал — check runs: аннотации отправляются пачками по 50 (лимит API) и проверяются на полный SHA и допустимый conclusion. Commit status — запасной канал для токенов без `checks: write`.
9. **Rulesets.** `branch_protection` читает действующие правила (`/rules/branches/{b}`) и сообщает пробелы: нет PR-review, нет required status checks, разрешён force-push. Команда `github protection` возвращает ненулевой код, если пробелы есть.
10. **GitHubActionsExecutor.**
    - Запускает только workflow из allow-list через `workflow_dispatch`; произвольные команды не запускаются.
    - Прогон находится по `correlation_id` в `run-name`, без гонок.
    - По таймауту прогон отменяется.
    - Артефакты перечисляются в итоговой сводке; при заданном `artifacts_dir` они скачиваются.
    - Подписанный blob-URL запрашивается отдельным клиентом **без токена**, только по https и с лимитом размера.
    - Архив распаковывается безопасно: имена проверяются, zip-slip, абсолютные пути и symlink отклоняются, лимит на распакованный размер и число файлов.
11. **Отчёты.** SARIF 2.1.0:
    - отдельный run на инструмент;
    - `security-severity`;
    - `partialFingerprints`;
    - `baselineState`;
    - `%SRCROOT%`;
    - `automationDetails`.

    JUnit: suites `quality-gates` и `tools`.

    Экспорт: `ai-coding report export`.

## Последствия
- **Плюс:** GitHub-слой тестируется end-to-end по настоящему HTTP против in-memory двойника (`tests/fake_github.py`), без сети.
- **Плюс:** формы ответов API в двойнике соответствуют документации REST. Реальный GitHub проверяется в CI пилотов (комментарии, check runs, загрузка SARIF).
- **Плюс:** отказ от GraphQL упрощает клиент. Если понадобится (например, merge queue), его можно добавить в тот же клиент.

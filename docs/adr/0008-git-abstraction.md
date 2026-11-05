# ADR-0008: Git-абстракция — CLI-адаптер, worktree-изоляция, защита веток

- Статус: Принято (реализовано в S1)
- Дата: 2026-11-05

## Контекст
ТЗ §4: локальный Git и GitHub API; интеграции заменяемы. ТЗ §18: повреждение репозитория → Git worktree, отдельная ветка, diff review, rollback. AC-03: PR без прямого push в main. ТЗ §12: README, diff, зависимости — недоверенный контент.

## Решение
- Порт `GitRepository` (`ai_coding.git.base`) — операции рабочей копии: status, branches, create_branch, worktrees add/list/remove, commit, remote_url, protected-branch guard.
- Адаптер `LocalGitRepository` — системный `git` через `subprocess` **с argv-списком, без shell, с таймаутом**.
- **Защита от исполнения кода из репозитория**: каждый вызов получает `-c core.fsmonitor=false -c core.hooksPath=<devnull>` и `GIT_TERMINAL_PROMPT=0`; коммиты — `--no-verify`. Иначе вредоносный `.git/config` (`core.fsmonitor=<script>`) исполнил бы код при обычном `git status` — подтверждено интеграционным тестом.
- **Валидация ref-имён** до вызова git (подмножество `git check-ref-format` + запрет ведущего `-` против option injection); `--` перед путями и ветками.
- **Защищённые ветки** (`project.protected_branches`, glob): `create_branch`, `add_worktree`, `commit` отказываются работать с ними; коммит на detached HEAD запрещён.
- Изменения агентов — только в отдельном worktree на новой ветке (`ai/<run>/<task>`), рабочая копия пользователя не трогается.
- Хостинг (GitHub) — отдельный порт `RepositoryHost` (S4): PR, checks, комментарии с защитой от дублей, на httpx; GitHub App или fine-grained token.

## Последствия
Нет нативных зависимостей (libgit2). Требуется git ≥ 2.30 (worktree porcelain `-z`).

## Альтернативы
GitPython — отклонено: тоже вызывает git CLI, но скрывает аргументы и исторически имел уязвимости с option injection. pygit2 — отклонено: нативная зависимость, проблемы сборки на Windows, нет полной поддержки worktree.

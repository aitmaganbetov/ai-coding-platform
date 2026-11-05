# ADR-0007: Execution Engine — единый интерфейс и три исполнителя

- Статус: Принято (реализация — S3 Local/Docker, S4 GitHub Actions)
- Дата: 2026-11-05

## Контекст
ТЗ §4: Local (ограниченный), Docker sandbox, GitHub Actions adapter; один интерфейс исполнения. ТЗ §5: Executor ограничивает CPU/RAM, время, сеть, файловую систему и число повторов; команды — allowlist/policy-based. ТЗ §12: без privileged, без Docker socket, read-only mounts. GitHub не обязателен для локального запуска.

## Решение
Порт `Executor` (`ai_coding.executors.base`):

```python
class Executor(Protocol):
    name: ExecutionBackend

    def capabilities(self) -> ExecutorCapabilities: ...
    async def run(self, spec: CommandSpec, ctx: ExecutionContext) -> ExecutionResult: ...
    async def cancel(self, execution_id: UUID) -> None: ...
```

`CommandSpec`: argv (без shell), рабочий каталог (worktree), env-allowlist, ResourceLimits, network policy, ожидаемые артефакты. `ExecutionResult`: exit code, длительность, усечённые и **redacted** stdout/stderr, артефакты с SHA-256, версия инструмента (FR-16, NFR-05).

| Executor | Изоляция | Когда |
| --- | --- | --- |
| LocalExecutor | Только allowlist из `execution.command_allowlist`; иначе — approval `host_command`; таймаут, лимит вывода; на POSIX `setrlimit`, на Windows Job Objects | Доверенные детерминированные инструменты (линтеры) при `allow_host_commands: true` |
| DockerSandboxExecutor | `--network none` по умолчанию, `--read-only`, `--cap-drop ALL`, `--security-opt no-new-privileges`, non-root user, `--pids-limit`, `--cpus/--memory`, worktree смонтирован отдельно, без Docker socket | По умолчанию для недоверенного кода (тесты, сборка) |
| GitHubActionsExecutor | Запуск reusable workflow через `workflow_dispatch`/`workflow_call`, ожидание check-runs, загрузка артефактов | GitHub Hybrid |

Выбор: `execution.backend` + политика (недоверенная команда никогда не идёт в LocalExecutor без approval).

## Последствия
Все исполнители проходят общий набор контрактных тестов. Docker-тесты маркируются и запускаются, если Docker доступен.

## Альтернативы
gVisor/Firecracker — рассматривается как усиление для Self-Hosted (backlog S9). Запуск через `shell=True` — запрещён.

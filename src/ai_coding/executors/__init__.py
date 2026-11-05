"""Execution Engine: one interface, several isolated backends (ТЗ §4–5; ADR-0007, ADR-0018)."""

from ai_coding.executors.base import (
    CommandSpec,
    ExecutionResult,
    Executor,
    ExecutorError,
    ExecutorUnavailableError,
    Mount,
)
from ai_coding.executors.docker import DockerSandboxExecutor, docker_available
from ai_coding.executors.local import LocalExecutor

__all__ = [
    "CommandSpec",
    "DockerSandboxExecutor",
    "ExecutionResult",
    "Executor",
    "ExecutorError",
    "ExecutorUnavailableError",
    "LocalExecutor",
    "Mount",
    "docker_available",
]

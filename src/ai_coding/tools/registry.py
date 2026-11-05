"""Registry of tool adapters; selection by project stacks."""

from __future__ import annotations

from collections.abc import Iterable, Sequence

from ai_coding.domain.enums import Stack
from ai_coding.tools.base import ToolAdapter
from ai_coding.tools.gitleaks import GitleaksAdapter
from ai_coding.tools.ruff import RuffAdapter
from ai_coding.tools.semgrep import SemgrepAdapter


class ToolRegistry:
    """Named tool adapters."""

    def __init__(self, adapters: Iterable[ToolAdapter] = ()) -> None:
        self._adapters: dict[str, ToolAdapter] = {}
        for adapter in adapters:
            self.register(adapter)

    def register(self, adapter: ToolAdapter) -> None:
        """Add an adapter (names are unique)."""
        if not isinstance(adapter, ToolAdapter):
            raise TypeError(f"{adapter!r} does not implement ToolAdapter")
        name = adapter.spec.name
        if name in self._adapters:
            raise ValueError(f"tool '{name}' is already registered")
        self._adapters[name] = adapter

    def get(self, name: str) -> ToolAdapter:
        """Adapter by name."""
        try:
            return self._adapters[name]
        except KeyError:
            raise KeyError(f"unknown tool '{name}' (known: {', '.join(self.names())})") from None

    def names(self) -> list[str]:
        """Registered tool names, sorted."""
        return sorted(self._adapters)

    def select(
        self, stacks: Sequence[Stack], *, only: Sequence[str] | None = None
    ) -> list[ToolAdapter]:
        """Tools applicable to ``stacks`` (stack-agnostic tools always apply)."""
        chosen = [self.get(n) for n in only] if only else [self._adapters[n] for n in self.names()]
        wanted = set(stacks)
        return [a for a in chosen if not a.spec.stacks or a.spec.stacks & wanted]


def default_tool_registry() -> ToolRegistry:
    """Built-in adapters (S3): Gitleaks, Semgrep, Ruff."""
    return ToolRegistry([GitleaksAdapter(), SemgrepAdapter(), RuffAdapter()])

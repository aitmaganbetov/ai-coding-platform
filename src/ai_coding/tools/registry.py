"""Registry of tool adapters; selection by project stacks (ADR-0019, ADR-0027).

Two kinds of tools share the registry:

* :class:`~ai_coding.tools.base.ToolAdapter` — self-contained analysers that read
  the project (Gitleaks, Semgrep, Ruff, Trivy, OSV-Scanner);
* :class:`~ai_coding.tools.project.ProjectTool` — tools that run the project's own
  toolchain and dependencies, sandbox only (PHPStan, Pint, ESLint, tsc, mypy,
  ``go test``, golangci-lint, ``cargo test``, Clippy).

Third-party adapters are added through the ``ai_coding.tools`` entry point group.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from importlib.metadata import entry_points

from ai_coding.domain.enums import Stack
from ai_coding.tools.base import ToolAdapter
from ai_coding.tools.gitleaks import GitleaksAdapter
from ai_coding.tools.golang import GolangciLintAdapter, GoTestAdapter
from ai_coding.tools.mypy import MypyAdapter
from ai_coding.tools.node import ESLintAdapter, TypeScriptAdapter
from ai_coding.tools.osv import OSVScannerAdapter
from ai_coding.tools.php import PHPStanAdapter, PintAdapter
from ai_coding.tools.project import ProjectTool
from ai_coding.tools.ruff import RuffAdapter
from ai_coding.tools.rust import CargoTestAdapter, ClippyAdapter
from ai_coding.tools.semgrep import SemgrepAdapter
from ai_coding.tools.trivy import TrivyAdapter

ENTRY_POINT_GROUP = "ai_coding.tools"
AnyTool = ToolAdapter | ProjectTool


class ToolRegistry:
    """Named tool adapters."""

    def __init__(self, adapters: Iterable[AnyTool] = ()) -> None:
        self._adapters: dict[str, AnyTool] = {}
        for adapter in adapters:
            self.register(adapter)

    def register(self, adapter: AnyTool) -> None:
        """Add an adapter (names are unique)."""
        if not isinstance(adapter, (ToolAdapter, ProjectTool)):
            raise TypeError(f"{adapter!r} implements neither ToolAdapter nor ProjectTool")
        name = adapter.spec.name
        if name in self._adapters:
            raise ValueError(f"tool '{name}' is already registered")
        self._adapters[name] = adapter

    def load_entry_points(self) -> list[str]:
        """Register adapters advertised by installed plugins; returns their names."""
        loaded: list[str] = []
        for ep in entry_points(group=ENTRY_POINT_GROUP):
            factory = ep.load()
            adapter = factory() if callable(factory) else factory
            self.register(adapter)
            loaded.append(adapter.spec.name)
        return loaded

    def get(self, name: str) -> AnyTool:
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
    ) -> list[AnyTool]:
        """Tools applicable to ``stacks`` (stack-agnostic tools always apply)."""
        chosen = [self.get(n) for n in only] if only else [self._adapters[n] for n in self.names()]
        wanted = set(stacks)
        return [a for a in chosen if not a.spec.stacks or a.spec.stacks & wanted]


def builtin_tools() -> list[AnyTool]:
    """Every built-in adapter (S3 + S7)."""
    return [
        GitleaksAdapter(), SemgrepAdapter(), RuffAdapter(), TrivyAdapter(), OSVScannerAdapter(),
        PHPStanAdapter(), PintAdapter(), ESLintAdapter(), TypeScriptAdapter(), MypyAdapter(),
        GoTestAdapter(), GolangciLintAdapter(), CargoTestAdapter(), ClippyAdapter(),
    ]  # fmt: skip


def default_tool_registry(*, plugins: bool = True) -> ToolRegistry:
    """Built-in adapters plus installed plugins."""
    registry = ToolRegistry(builtin_tools())
    if plugins:
        registry.load_entry_points()
    return registry

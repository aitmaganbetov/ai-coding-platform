"""Bootstrap Agent: approved plan → project skeleton (ТЗ §6, FR-03).

Deterministic by design: the skeleton (layout, CI, README, configuration, docs) is
rendered from templates, so it is reproducible and reviewable, and it always passes
its own tests. The LLM-produced content (specification, architecture, backlog)
enters only as documentation and module descriptions.

Supported stacks: Python (uv + pytest + ruff) and Node.js (``node --test``). Any other
language chosen by the Architect Agent is reported as unsupported instead of
producing a half-working skeleton.

Inputs: ``requirements``, ``architecture``, ``plan``, ``project_name`` and optionally
``requirements_version``.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from typing import Any

from ai_coding.agents import architect as architect_docs
from ai_coding.agents import planner as planner_docs
from ai_coding.agents import requirements as requirements_docs
from ai_coding.agents.base import AgentContext, AgentError, load_output_schema
from ai_coding.config.generator import render_initial_config, sanitize_project_name
from ai_coding.contracts.agent import (
    AgentResult,
    AgentResultStatus,
    AgentTask,
    validate_agent_output,
)
from ai_coding.domain.enums import AgentName, Mode, Stack

# Pinned like the platform's own workflows (ADR-0022); Dependabot updates them.
CHECKOUT = "actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1"
SETUP_UV = "astral-sh/setup-uv@68559e99cc807c1eb7e9238fd43a15bba460eec2 # v10.3.3"
SETUP_NODE = "actions/setup-node@949feb2413d6458794dcd2491c4babbbce0c15c1 # v7.1.0"

STACKS = {"python": "python", "javascript": "node", "typescript": "node"}
_IDENT = re.compile(r"[^a-z0-9_]+")


def package_name(title: str) -> str:
    """snake_case identifier usable as a Python package / npm-safe name."""
    raw = _IDENT.sub("_", title.lower()).strip("_")
    if not raw or not raw[0].isalpha():
        raw = "app_" + raw
    return raw[:40].rstrip("_") or "app"


class BootstrapAgent:
    """Renders the initial repository content for the approved plan."""

    name = AgentName.BOOTSTRAP
    title = "Bootstrap Agent"
    requires_llm = False

    @property
    def output_schema(self) -> Mapping[str, Any]:
        """Packaged schema for this agent."""
        return load_output_schema(self.name.value)

    async def run(self, task: AgentTask, ctx: AgentContext) -> AgentResult:
        """Render the files; output lists every file with its content and hash."""
        files, stack, package, commands = self.render(ctx.inputs)
        output = {
            "stack": stack,
            "package": package,
            "files": [
                {
                    "path": path,
                    "sha256": hashlib.sha256(text.encode()).hexdigest(),
                    "bytes": len(text.encode()),
                    "content": text,
                }
                for path, text in sorted(files.items())
            ],
            "commands": commands,
            "notes": [
                "Generated deterministically from the approved specification, "
                "architecture and plan."
            ],
        }
        result = AgentResult(
            task_id=task.task_id,
            status=AgentResultStatus.SUCCEEDED,
            confidence=1.0,
            output=output,
            evidence_refs=["template:bootstrap/" + stack],
        )
        verdict = validate_agent_output(task, result)
        if not verdict.accepted:  # pragma: no cover - templates are tested
            raise AgentError("bootstrap output rejected: " + "; ".join(verdict.errors))
        return result

    # ---------------------------------------------------------------- render --
    def render(self, inputs: Mapping[str, Any]) -> tuple[dict[str, str], str, str, dict[str, str]]:
        """``(files, stack, package, commands)`` for the inputs."""
        spec = inputs["requirements"]
        arch = inputs["architecture"]
        plan = inputs["plan"]
        language = arch["stack"]["language"]
        stack = STACKS.get(language)
        if stack is None:
            raise AgentError(
                f"bootstrap does not support language '{language}' yet "
                f"(supported: {', '.join(sorted(STACKS))})"
            )
        title = str(inputs.get("project_name") or spec["title"])
        package = package_name(title)
        modules = [(m["name"], m["responsibility"]) for m in arch["modules"]]
        files: dict[str, str] = {
            "docs/requirements.md": requirements_docs.render_markdown(
                spec, version=inputs.get("requirements_version")
            ),
            "docs/architecture.md": architect_docs.render_markdown(arch),
            "docs/backlog.md": planner_docs.render_markdown(plan),
            **architect_docs.adr_documents(arch),
        }
        if stack == "python":
            files.update(_python(package, spec, modules))
            commands = {
                "install": "uv sync",
                "test": "uv run pytest",
                "lint": "uv run ruff check .",
            }
        else:
            files.update(_node(package, spec, modules))
            commands = {
                "install": "npm install",
                "test": "npm test",
                "lint": "node --check src/index.js",
            }
        files["README.md"] = _readme(title, spec, arch, commands)
        files[".ai-coding.yml"] = render_initial_config(
            name=sanitize_project_name(package.replace("_", "-")),
            mode=Mode.DEVELOP,
            stacks=[Stack.PYTHON if stack == "python" else Stack.JAVASCRIPT],
        )[0]
        return files, stack, package, commands


# ------------------------------------------------------------------ python --
def _py_str(text: str) -> str:
    """A JSON string literal is also a valid Python string literal (any content)."""
    return json.dumps(text, ensure_ascii=False)


def _python(
    package: str, spec: Mapping[str, Any], modules: list[tuple[str, str]]
) -> dict[str, str]:
    files: dict[str, str] = {}
    files["pyproject.toml"] = f'''[project]
name = "{package.replace("_", "-")}"
version = "0.1.0"
description = {json.dumps(spec["summary"][:200], ensure_ascii=False)}
readme = "README.md"
requires-python = ">=3.12"
dependencies = []

[dependency-groups]
dev = ["pytest>=8.3", "ruff>=0.8"]

[build-system]
requires = ["hatchling>=1.25"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src/{package}"]

[tool.pytest.ini_options]
testpaths = ["tests"]
pythonpath = ["src"]

[tool.ruff]
line-length = 100
src = ["src"]

[tool.ruff.lint]
select = ["E", "F", "I", "B", "UP"]
'''
    names = ", ".join(f'"{n}"' for n, _ in modules)
    files[f"src/{package}/__init__.py"] = (
        f'{_py_str(spec["title"])}\n\n__version__ = "0.1.0"\n\nMODULES = ({names},)\n'
    )
    for name, responsibility in modules:
        files[f"src/{package}/{name}/__init__.py"] = (
            f"{_py_str(responsibility)}\n\nRESPONSIBILITY = {_py_str(responsibility)}\n"
        )
    files["tests/__init__.py"] = ""
    files[
        "tests/test_structure.py"
    ] = f'''"""The skeleton matches the approved architecture (generated by ai-coding)."""

import importlib

import pytest

import {package}


def test_version() -> None:
    assert {package}.__version__ == "0.1.0"


@pytest.mark.parametrize("name", {package}.MODULES)
def test_module_is_importable_and_documented(name: str) -> None:
    module = importlib.import_module(f"{package}.{{name}}")
    assert module.RESPONSIBILITY
    assert module.__doc__
'''
    files[".gitignore"] = (
        ".venv/\n__pycache__/\n*.pyc\n.pytest_cache/\n.ruff_cache/\ndist/\n.coverage\n"
    )
    files[".github/workflows/ci.yml"] = f"""name: CI

on:
  pull_request:
  push:
    branches: [main]

permissions:
  contents: read

jobs:
  test:
    runs-on: ubuntu-24.04
    timeout-minutes: 15
    steps:
      - uses: {CHECKOUT}
        with:
          persist-credentials: false
      - uses: {SETUP_UV}
        with:
          python-version: "3.12"
      - run: uv sync
      - run: uv run ruff check .
      - run: uv run pytest
"""
    return files


# -------------------------------------------------------------------- node --
def _node(package: str, spec: Mapping[str, Any], modules: list[tuple[str, str]]) -> dict[str, str]:
    files: dict[str, str] = {}
    files["package.json"] = (
        json.dumps(
            {
                "name": package.replace("_", "-"),
                "version": "0.1.0",
                "description": spec["summary"][:200],
                "type": "module",
                "main": "src/index.js",
                "private": True,
                "engines": {"node": ">=20"},
                "scripts": {"test": "node --test"},
            },
            indent=2,
            ensure_ascii=False,
        )
        + "\n"
    )
    imports = "\n".join(f'import * as {n} from "./{n}/index.js";' for n, _ in modules)
    files["src/index.js"] = (
        f"// {spec['title']}\n{imports}\n\n"
        f'export const version = "0.1.0";\n'
        f"export const modules = {{ {', '.join(n for n, _ in modules)} }};\n"
    )
    for name, responsibility in modules:
        files[f"src/{name}/index.js"] = (
            f"export const name = {json.dumps(name)};\n"
            f"export const responsibility = {json.dumps(responsibility, ensure_ascii=False)};\n"
        )
    files[
        "test/structure.test.js"
    ] = """// The skeleton matches the approved architecture (generated by ai-coding).
import { test } from "node:test";
import assert from "node:assert/strict";
import { modules, version } from "../src/index.js";

test("version", () => {
  assert.equal(version, "0.1.0");
});

for (const [key, mod] of Object.entries(modules)) {
  test(`module ${key} is documented`, () => {
    assert.equal(mod.name, key);
    assert.ok(mod.responsibility.length > 0);
  });
}
"""
    files[".gitignore"] = "node_modules/\ncoverage/\ndist/\n"
    files[".github/workflows/ci.yml"] = f"""name: CI

on:
  pull_request:
  push:
    branches: [main]

permissions:
  contents: read

jobs:
  test:
    runs-on: ubuntu-24.04
    timeout-minutes: 15
    steps:
      - uses: {CHECKOUT}
        with:
          persist-credentials: false
      - uses: {SETUP_NODE}
        with:
          node-version: "22"
      - run: node --check src/index.js
      - run: npm test
"""
    return files


# ------------------------------------------------------------------ README --
def _readme(
    title: str, spec: Mapping[str, Any], arch: Mapping[str, Any], commands: Mapping[str, str]
) -> str:
    lines = [f"# {title}", "", spec["summary"], "", "## Цели", ""]
    lines += [f"- {g}" for g in spec["goals"]]
    stack = arch["stack"]
    lines += [
        "",
        "## Стек",
        "",
        f"{stack['language']} · {stack['framework']} · {stack['database']}",
        "",
        "## Разработка",
        "",
        "```bash",
        commands["install"],
        "```",
        "",
        "```bash",
        commands["test"],
        "```",
        "",
        "## Документация",
        "",
        "- [Требования](docs/requirements.md)",
        "- [Архитектура](docs/architecture.md)",
        "- [Backlog](docs/backlog.md)",
        "",
        "Каркас создан AI Coding Platform (режим CREATE) из утверждённой версии требований.",
    ]
    return "\n".join(lines) + "\n"

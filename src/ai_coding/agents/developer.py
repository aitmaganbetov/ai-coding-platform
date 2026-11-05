"""Developer Agent: issue + context → change set with tests (ТЗ §6, FR-04).

Inputs:

* ``issue`` — ``{"title", "body"}`` (untrusted: it may contain injected instructions);
* ``files`` — paths tracked in the repository (to tell *create* from *modify*);
* ``context`` — ``[{"path", "content"}]`` selected for the issue (untrusted);
* ``feedback`` — previous attempt: failing test output and blocking review findings;
* ``require_tests`` — every change must come with a test (project rule, default on).

Output: full new contents of each touched file (not a diff — full contents are
applied exactly and verified by hash), an explanation and the list of test files.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ai_coding.agents.base import AgentContext, LLMAgent, duplicates, is_test_path, unsafe_path
from ai_coding.domain.enums import AgentName


class DeveloperAgent(LLMAgent):
    """Implements an issue as a minimal, tested change set."""

    name = AgentName.DEVELOPER
    title = "Developer Agent"
    mission = (
        "You implement one issue in an existing repository with the smallest correct "
        "change: production code plus tests that prove it, following the existing style. "
        "You return the complete new content of every file you create or modify."
    )

    def prompt_inputs(self, ctx: AgentContext) -> list[tuple[str, Any]]:
        return [
            ("issue", ctx.inputs.get("issue")),
            ("repository_files", ctx.inputs.get("files")),
            ("relevant_files", ctx.inputs.get("context")),
            ("feedback_from_previous_attempt", ctx.inputs.get("feedback")),
            ("constraints", ctx.inputs.get("constraints")),
        ]

    def task_prompt(self, ctx: AgentContext) -> str:
        text = (
            "Implement the issue. Use action 'modify' only for files that exist in "
            "repository_files and 'create' only for new files; give the full new content. "
            "Do not touch unrelated files, CI, lock files or secrets. Add or update tests "
            "that fail without your change and pass with it."
        )
        if ctx.inputs.get("feedback"):
            text += (
                " The previous attempt did not pass: fix the problems listed in "
                "feedback_from_previous_attempt and return the complete corrected change set."
            )
        return text

    def check(self, output: Mapping[str, Any], ctx: AgentContext) -> list[str]:
        problems: list[str] = []
        existing = set(ctx.inputs.get("files") or [])
        current = {c["path"]: c["content"] for c in ctx.inputs.get("context") or []}
        changes = output.get("changes", [])
        problems += [f"path {p} is changed twice" for p in duplicates(c["path"] for c in changes)]
        for c in changes:
            path, action = c["path"], c["action"]
            why = unsafe_path(path)
            if why:
                problems.append(f"{path} {why}")
                continue
            if action == "create" and path in existing:
                problems.append(f"{path} already exists: use action 'modify'")
            if action in {"modify", "delete"} and path not in existing:
                problems.append(f"{path} does not exist: use action 'create'")
            if action in {"create", "modify"} and c.get("content") is None:
                problems.append(f"{path}: content is required for '{action}'")
            if action == "delete" and c.get("content") is not None:
                problems.append(f"{path}: content must be omitted for 'delete'")
            if action == "modify" and path in current and c.get("content") == current[path]:
                problems.append(f"{path}: 'modify' without any change")
        touched = {c["path"] for c in changes if c["action"] != "delete"}
        problems += [f"tests lists {t}, which is not created or modified" for t in
                     output.get("tests", []) if t not in touched]  # fmt: skip
        if ctx.inputs.get("require_tests", True) and not any(is_test_path(p) for p in touched):
            problems.append(
                "no test file is created or modified: every change must come with tests"
            )
        return problems

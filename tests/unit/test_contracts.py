"""Tests for the uniform agent contract (ТЗ §6)."""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest
from pydantic import ValidationError

from ai_coding.contracts import (
    AgentResult,
    AgentResultStatus,
    AgentScope,
    AgentTask,
    validate_agent_output,
)
from ai_coding.domain.enums import AgentName

REVIEW_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["findings"],
    "properties": {
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["file", "line", "severity"],
                "properties": {
                    "file": {"type": "string"},
                    "line": {"type": "integer", "minimum": 1},
                    "severity": {"enum": ["critical", "high", "medium", "low", "info"]},
                },
            },
        }
    },
}


def make_task(**kw: Any) -> AgentTask:
    data: dict[str, Any] = {
        "task_id": uuid4(),
        "agent": AgentName.REVIEWER,
        "scope": AgentScope(project_id=uuid4(), run_id=uuid4()),
        "max_cost": 0.5,
        "max_duration": 300,
        "output_schema": REVIEW_SCHEMA,
    }
    data.update(kw)
    return AgentTask(**data)


def test_scope_is_least_privilege_by_default() -> None:
    scope = AgentScope(project_id=uuid4(), run_id=uuid4())
    assert scope.write_paths == []
    assert scope.network is False
    assert scope.shell is False
    assert make_task().allowed_tools == []


def test_invalid_output_schema_rejected() -> None:
    with pytest.raises(ValidationError, match="not a valid JSON Schema"):
        make_task(output_schema={"type": "no-such-type"})


def test_valid_output_accepted() -> None:
    task = make_task()
    result = AgentResult(
        task_id=task.task_id,
        status=AgentResultStatus.SUCCEEDED,
        confidence=0.8,
        output={"findings": [{"file": "a.py", "line": 3, "severity": "high"}]},
    )
    outcome = validate_agent_output(task, result)
    assert outcome.accepted
    assert outcome.errors == []


def test_invalid_output_is_not_a_completed_task() -> None:
    task = make_task()
    result = AgentResult(
        task_id=task.task_id,
        status=AgentResultStatus.SUCCEEDED,
        confidence=0.99,
        output={"findings": [{"file": "a.py", "line": 0, "severity": "urgent"}]},
    )
    outcome = validate_agent_output(task, result)
    assert not outcome.accepted
    assert len(outcome.errors) == 2
    assert all(e.startswith("output at findings/0/") for e in outcome.errors)


def test_missing_root_field_reported_at_root() -> None:
    task = make_task()
    result = AgentResult(task_id=task.task_id, status=AgentResultStatus.SUCCEEDED, confidence=1)
    outcome = validate_agent_output(task, result)
    assert outcome.errors == ["output at <root>: 'findings' is a required property"]


def test_mismatched_task_id_rejected() -> None:
    task = make_task()
    result = AgentResult(
        task_id=uuid4(), status=AgentResultStatus.SUCCEEDED, confidence=1, output={"findings": []}
    )
    assert not validate_agent_output(task, result).accepted


def test_failed_status_never_accepted() -> None:
    task = make_task()
    result = AgentResult(
        task_id=task.task_id, status=AgentResultStatus.FAILED, confidence=0.1, error="boom"
    )
    outcome = validate_agent_output(task, result)
    assert not outcome.accepted
    assert outcome.errors == []


def test_confidence_bounds() -> None:
    with pytest.raises(ValidationError):
        AgentResult(task_id=uuid4(), status=AgentResultStatus.SUCCEEDED, confidence=1.5)

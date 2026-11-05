"""Uniform contract between the orchestrator and every agent (ТЗ §6).

Contract fields: task_id, input_refs, allowed_tools, scope, max_cost,
max_duration, output_schema, evidence_refs, status, confidence.

An agent's output is accepted only if it validates against the task's
``output_schema``; otherwise the task is *not* completed, whatever the agent
claims (ТЗ §6, §18 "Нестабильность LLM").
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Self
from uuid import UUID

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError
from pydantic import BaseModel, ConfigDict, Field, model_validator

from ai_coding.domain.enums import AgentName
from ai_coding.security import redact


class AgentResultStatus(StrEnum):
    """Self-reported status; final status is decided by the orchestrator."""

    SUCCEEDED = "succeeded"
    FAILED = "failed"
    NEEDS_APPROVAL = "needs_approval"
    REJECTED = "rejected"  # output failed schema validation


class AgentScope(BaseModel):
    """What the agent may touch. Everything not granted is denied (least privilege)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    project_id: UUID
    run_id: UUID
    read_paths: list[str] = Field(default_factory=lambda: ["**"])
    write_paths: list[str] = Field(default_factory=list)
    network: bool = False
    shell: bool = False


class AgentTask(BaseModel):
    """Input contract handed to an agent."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    task_id: UUID
    agent: AgentName
    input_refs: list[str] = Field(default_factory=list, description="Artifact/context URIs.")
    allowed_tools: list[str] = Field(default_factory=list)
    scope: AgentScope
    max_cost: float = Field(ge=0, description="Maximum cost in USD.")
    max_duration: int = Field(gt=0, description="Maximum duration in seconds.")
    output_schema: dict[str, Any]

    @model_validator(mode="after")
    def _schema_is_valid(self) -> Self:
        try:
            Draft202012Validator.check_schema(self.output_schema)
        except SchemaError as exc:
            raise ValueError(f"output_schema is not a valid JSON Schema: {exc.message}") from exc
        return self


class AgentResult(BaseModel):
    """Output envelope returned by an agent."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    task_id: UUID
    status: AgentResultStatus
    confidence: float = Field(ge=0.0, le=1.0)
    output: dict[str, Any] = Field(default_factory=dict)
    evidence_refs: list[str] = Field(default_factory=list)
    error: str | None = None


@dataclass(frozen=True, slots=True)
class OutputValidation:
    """Outcome of validating an agent result against its task."""

    accepted: bool
    errors: list[str] = field(default_factory=list)


def validate_agent_output(task: AgentTask, result: AgentResult) -> OutputValidation:
    """Decide whether ``result`` satisfies ``task``'s contract."""
    errors: list[str] = []
    if result.task_id != task.task_id:
        errors.append("result.task_id does not match task.task_id")
    if result.status is AgentResultStatus.SUCCEEDED:
        validator = Draft202012Validator(task.output_schema)
        for err in sorted(validator.iter_errors(result.output), key=lambda e: list(e.path)):
            location = "/".join(str(p) for p in err.absolute_path) or "<root>"
            errors.append(f"output at {location}: {redact(err.message)}")
    accepted = not errors and result.status is AgentResultStatus.SUCCEEDED
    return OutputValidation(accepted=accepted, errors=errors)

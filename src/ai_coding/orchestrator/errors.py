"""Orchestrator error hierarchy.

Errors are classified for Retry & Recovery (ADR-0006): ``TransientError`` may be
retried, ``PermanentError`` and policy violations never are.
"""

from __future__ import annotations

from ai_coding.domain.enums import ApprovalOperation
from ai_coding.domain.errors import PermanentError as _PermanentBase
from ai_coding.domain.errors import TransientError as _TransientBase


class OrchestratorError(Exception):
    """Base class for orchestrator errors."""


class InvalidTransitionError(OrchestratorError):
    """A run state transition is not allowed by the transition table."""


class ConcurrencyConflictError(OrchestratorError):
    """Optimistic concurrency check failed (another writer changed the row)."""


class PlanValidationError(OrchestratorError):
    """The task DAG is invalid (cycle, unknown dependency, duplicate key, …)."""


class RunNotFoundError(OrchestratorError):
    """No run with the given id."""


class RunLeaseError(OrchestratorError):
    """Another worker holds the run lease."""


class IdempotencyConflictError(OrchestratorError):
    """The idempotency key was already used with a different request."""


class NoRouteError(OrchestratorError):
    """No registered handler can serve a capability under current constraints."""


class ApprovalError(OrchestratorError):
    """Approval request/decision is invalid (wrong role, expired, already decided)."""


class ApprovalDeniedError(ApprovalError):
    """The actor's role may not decide approvals (RBAC, ТЗ §3)."""


class StepError(OrchestratorError):
    """Base for errors raised by step handlers."""


class TransientError(StepError, _TransientBase):
    """Temporary failure; the step may be retried with backoff."""


class PermanentError(StepError, _PermanentBase):
    """Deterministic failure; retrying cannot help."""


class PolicyDeniedError(PermanentError):
    """The policy engine denied an action."""


class ApprovalRequiredError(PermanentError):
    """The action needs a human approval that has not been granted."""

    def __init__(self, message: str, operation: ApprovalOperation | None = None) -> None:
        super().__init__(message)
        self.operation = operation


class BudgetExceededError(PermanentError):
    """Token, cost or time budget exhausted (FR-15)."""


class SuccessCriteriaError(PermanentError):
    """The step output does not satisfy its success criteria."""


class RunCancelledError(OrchestratorError):
    """The run was cancelled while the step was executing."""

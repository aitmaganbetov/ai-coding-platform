"""What a running step records (ТЗ §12: secrets never stored): artifacts, model usage
(the source of truth for budgets) and normalised findings, all redacted."""

from __future__ import annotations

import uuid
from collections.abc import Callable, Sequence
from typing import Any

from sqlalchemy import select

from ai_coding.domain.enums import ProviderName
from ai_coding.llm.base import TokenUsage
from ai_coding.orchestrator.run_queries import get_run
from ai_coding.persistence.artifacts import ArtifactRef, ArtifactStore
from ai_coding.persistence.db import Database
from ai_coding.persistence.models import Artifact, ModelUsage
from ai_coding.persistence.models import Finding as FindingRow
from ai_coding.security import redact
from ai_coding.tools.base import Finding

SaveArtifact = Callable[[str, bytes, str], ArtifactRef]
RecordUsage = Callable[[ProviderName, str, TokenUsage, float], None]
RecordFindings = Callable[[Sequence[Finding]], int]


def redact_json(value: Any) -> Any:
    """``value`` with every string redacted (nested dicts and lists included)."""
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, dict):
        return {str(k): redact_json(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [redact_json(v) for v in value]
    return value


class TaskRecorders:
    """Callbacks handed to a step (``StepContext``) to persist what it produces."""

    def __init__(self, db: Database, artifacts: ArtifactStore) -> None:
        self.db = db
        self.artifacts = artifacts

    def artifact_saver(self, run_id: uuid.UUID, task_id: uuid.UUID) -> SaveArtifact:
        """Store an artifact (text redacted) and register it once per run, name and hash."""

        def save(name: str, data: bytes, media_type: str) -> ArtifactRef:
            ref = self.artifacts.put(redact(data.decode("utf-8")).encode("utf-8")
                                     if media_type.startswith(("text/", "application/json"))
                                     else data)  # fmt: skip
            with self.db.session() as session:
                exists = session.scalar(
                    select(Artifact).where(
                        Artifact.run_id == run_id,
                        Artifact.name == name,
                        Artifact.sha256 == ref.sha256,
                    )
                )
                if exists is None:
                    session.add(
                        Artifact(
                            run_id=run_id,
                            task_id=task_id,
                            name=name,
                            media_type=media_type,
                            sha256=ref.sha256,
                            size_bytes=ref.size_bytes,
                            uri=ref.uri,
                        )
                    )
            return ref

        return save

    def usage_recorder(self, run_id: uuid.UUID, task_id: uuid.UUID) -> RecordUsage:
        """Persist LLM usage (``model_usage``) — the source of truth for budgets."""

        def record(provider: ProviderName, model: str, usage: TokenUsage, cost: float) -> None:
            with self.db.session() as session:
                session.add(
                    ModelUsage(
                        run_id=run_id,
                        task_id=task_id,
                        provider=provider.value,
                        model=model,
                        input_tokens=usage.input_tokens,
                        output_tokens=usage.output_tokens,
                        cost_usd=cost,
                    )
                )

        return record

    def findings_recorder(self, run_id: uuid.UUID) -> RecordFindings:
        """Persist normalised findings (``findings``) for the project; idempotent per run."""

        def record(findings: Sequence[Finding]) -> int:
            if not findings:
                return 0
            with self.db.session() as session:
                run = get_run(session, run_id)
                known = set(
                    session.scalars(
                        select(FindingRow.fingerprint).where(FindingRow.run_id == run_id)
                    )
                )
                added = 0
                for f in findings:
                    if f.fingerprint in known:
                        continue
                    known.add(f.fingerprint)
                    session.add(
                        FindingRow(
                            project_id=run.project_id,
                            run_id=run.id,
                            source=f.tool,
                            rule_id=f.rule_id,
                            severity=f.severity.value,
                            title=redact(f.title)[:500],
                            message=redact(f.message),
                            file_path=f.path,
                            line=f.line,
                            fingerprint=f.fingerprint,
                            evidence=redact_json(f.evidence),
                        )
                    )
                    added += 1
                return added

        return record

"""Database Agent: migrations, indexes and transactions analysis (ТЗ §6, B-505).

Runs when a change touches migrations or schema files. Inputs: ``migrations``
(``[{"path", "content"}]``, untrusted), ``diff``, ``stack``. Advisory, like review.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from ai_coding.agents.base import AgentContext, LLMAgent
from ai_coding.domain.enums import AgentName

_MIGRATION = re.compile(
    r"(^|/)(migrations?|alembic/versions|db/migrate|database/migrations)/"
    r"|(^|/)schema\.(sql|prisma|rb)$|\.sql$"
)


def is_migration_path(path: str) -> bool:
    """Whether ``path`` looks like a migration or schema file."""
    return bool(_MIGRATION.search(path))


class DatabaseAgent(LLMAgent):
    """Reviews schema changes for safety on a live database."""

    name = AgentName.DATABASE
    title = "Database Agent"
    mission = (
        "You analyse database migrations and schema changes: reversibility, locking "
        "and downtime risk on large tables, missing or redundant indexes, transaction "
        "boundaries and data loss."
    )

    def prompt_inputs(self, ctx: AgentContext) -> list[tuple[str, Any]]:
        return [
            ("migrations", ctx.inputs.get("migrations")),
            ("diff", ctx.inputs.get("diff")),
            ("stack", ctx.inputs.get("stack")),
        ]

    def task_prompt(self, ctx: AgentContext) -> str:
        return (
            "Analyse every migration file. For each, list its operations, whether it is "
            "reversible and its locking risk. Report concrete problems with recommendations."
        )

    def check(self, output: Mapping[str, Any], ctx: AgentContext) -> list[str]:
        known = {m["path"] for m in ctx.inputs.get("migrations") or []}
        problems = [
            f"analysis of {m['file']}, which is not a given migration"
            for m in output.get("migrations", [])
            if m["file"] not in known
        ]
        problems += [
            f"finding on {f['file']}, which is not a given migration"
            for f in output.get("findings", [])
            if f["file"] not in known
        ]
        missing = known - {m["file"] for m in output.get("migrations", [])}
        problems += [f"migration {p} is not analysed" for p in sorted(missing)]
        return problems

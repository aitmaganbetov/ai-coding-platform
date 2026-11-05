"""JSON Schema for ``.ai-coding.yml`` — generation and packaged copy (ADR-0004)."""

from __future__ import annotations

import json
from functools import lru_cache
from importlib import resources
from typing import Any

from ai_coding.config.models import AICodingConfig

SCHEMA_RESOURCE = "ai-coding.schema.json"


def generate_schema() -> dict[str, Any]:
    """Generate the JSON Schema (Draft 2020-12) from the Pydantic models."""
    schema = AICodingConfig.model_json_schema(mode="validation")
    # Put the meta keys first for readability; content is unchanged.
    ordered: dict[str, Any] = {
        "$schema": schema.pop("$schema"),
        "$id": schema.pop("$id"),
    }
    ordered.update(schema)
    return ordered


def render_schema(schema: dict[str, Any] | None = None) -> str:
    """Serialise a schema deterministically (stable diff in git)."""
    return json.dumps(schema or generate_schema(), indent=2, ensure_ascii=False) + "\n"


@lru_cache(maxsize=1)
def load_packaged_schema() -> dict[str, Any]:
    """Load the schema shipped inside the package."""
    text = resources.files("ai_coding.schemas").joinpath(SCHEMA_RESOURCE).read_text("utf-8")
    data: dict[str, Any] = json.loads(text)
    return data

"""The packaged JSON Schema must match the models (ADR-0004 drift check)."""

from __future__ import annotations

import json
from pathlib import Path

from jsonschema import Draft202012Validator

from ai_coding.config.schema import generate_schema, load_packaged_schema, render_schema

SCHEMA_PATH = (
    Path(__file__).resolve().parents[2] / "src" / "ai_coding" / "schemas" / "ai-coding.schema.json"
)


def test_packaged_schema_matches_models() -> None:
    assert load_packaged_schema() == generate_schema(), (
        "schema drift: run `uv run ai-coding config schema -o "
        "src/ai_coding/schemas/ai-coding.schema.json`"
    )


def test_packaged_schema_file_is_canonically_rendered() -> None:
    assert SCHEMA_PATH.read_text(encoding="utf-8") == render_schema()


def test_schema_is_valid_draft_2020_12() -> None:
    schema = load_packaged_schema()
    Draft202012Validator.check_schema(schema)
    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == {"schema_version", "project"}


def test_schema_covers_all_tz_sections() -> None:
    """ТЗ §11 lists the mandatory top-level sections."""
    props = set(load_packaged_schema()["properties"])
    assert {
        "schema_version", "project", "mode", "stacks", "execution", "ai", "agents",
        "security", "quality_gates", "budgets", "approvals", "artifacts",
    } <= props  # fmt: skip


def test_schema_is_json_serialisable() -> None:
    assert json.loads(render_schema()) == generate_schema()

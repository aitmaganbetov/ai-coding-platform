"""Project configuration (``.ai-coding.yml``): models, schema, loading, validation."""

from ai_coding.config.loader import CONFIG_FILENAME, ConfigLoadError, find_config, parse_yaml
from ai_coding.config.models import SCHEMA_VERSION, AICodingConfig
from ai_coding.config.schema import generate_schema, load_packaged_schema, render_schema
from ai_coding.config.validator import (
    IssueKind,
    ValidationIssue,
    ValidationReport,
    validate_document,
    validate_file,
    validate_text,
)

__all__ = [
    "CONFIG_FILENAME",
    "SCHEMA_VERSION",
    "AICodingConfig",
    "ConfigLoadError",
    "IssueKind",
    "ValidationIssue",
    "ValidationReport",
    "find_config",
    "generate_schema",
    "load_packaged_schema",
    "parse_yaml",
    "render_schema",
    "validate_document",
    "validate_file",
    "validate_text",
]

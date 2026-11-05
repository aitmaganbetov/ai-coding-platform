"""Validation pipeline for ``.ai-coding.yml``.

Order (each stage only runs when the previous one produced a usable document):

1. read + YAML parse (safe loader, duplicate keys rejected)
2. secret scan — literal secrets are always errors (ТЗ §11)
3. JSON Schema validation (structure, types, enums, ranges)
4. Pydantic validation (cross-field security rules)
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError as SchemaError
from pydantic import ValidationError

from ai_coding.config.loader import ConfigLoadError, parse_yaml, read_config_text
from ai_coding.config.models import SUPPORTED_SCHEMA_VERSIONS, AICodingConfig
from ai_coding.config.schema import load_packaged_schema
from ai_coding.security import find_secrets_in_mapping, redact


class IssueKind(StrEnum):
    """Stage that produced an issue."""

    PARSE = "parse"
    SECRET = "secret"  # noqa: S105 - not a password
    SCHEMA = "schema"
    SEMANTIC = "semantic"


@dataclass(frozen=True, slots=True)
class ValidationIssue:
    """A single configuration problem."""

    kind: IssueKind
    path: str
    message: str
    line: int | None = None

    def to_dict(self) -> dict[str, Any]:
        """JSON-serialisable form."""
        return {
            "kind": self.kind.value,
            "path": self.path,
            "message": self.message,
            "line": self.line,
        }


@dataclass(slots=True)
class ValidationReport:
    """Result of validating one configuration document."""

    source: str
    issues: list[ValidationIssue] = field(default_factory=list)
    config: AICodingConfig | None = None

    @property
    def ok(self) -> bool:
        """``True`` when there are no issues and a typed config was produced."""
        return not self.issues and self.config is not None

    def to_dict(self) -> dict[str, Any]:
        """JSON-serialisable form (used by ``--format json``)."""
        return {
            "source": self.source,
            "valid": self.ok,
            "issues": [i.to_dict() for i in self.issues],
        }


def validate_file(path: Path) -> ValidationReport:
    """Validate a configuration file on disk."""
    report = ValidationReport(source=str(path))
    try:
        text = read_config_text(path)
    except ConfigLoadError as exc:
        report.issues.append(ValidationIssue(IssueKind.PARSE, "", str(exc), exc.line))
        return report
    return validate_text(text, source=str(path))


def validate_text(text: str, *, source: str = "<string>") -> ValidationReport:
    """Validate configuration YAML text."""
    report = ValidationReport(source=source)
    try:
        document = parse_yaml(text)
    except ConfigLoadError as exc:
        report.issues.append(ValidationIssue(IssueKind.PARSE, "", str(exc), exc.line))
        return report
    return validate_document(document, source=source)


def validate_document(document: object, *, source: str = "<document>") -> ValidationReport:
    """Validate an already-parsed document."""
    report = ValidationReport(source=source)
    if document is None:
        report.issues.append(ValidationIssue(IssueKind.PARSE, "", "configuration file is empty"))
        return report
    if not isinstance(document, dict):
        report.issues.append(
            ValidationIssue(IssueKind.PARSE, "", "configuration root must be a mapping")
        )
        return report

    report.issues.extend(
        ValidationIssue(
            IssueKind.SECRET,
            finding.path,
            f"{finding.reason}; reference secrets via environment (e.g. api_key_env) instead",
        )
        for finding in find_secrets_in_mapping(document)
    )

    version = document.get("schema_version")
    if version is not None and str(version) not in SUPPORTED_SCHEMA_VERSIONS:
        supported = ", ".join(sorted(SUPPORTED_SCHEMA_VERSIONS))
        report.issues.append(
            ValidationIssue(
                IssueKind.SCHEMA,
                "schema_version",
                f"unsupported schema_version '{redact(str(version))}' (supported: {supported})",
            )
        )
        return report

    schema_issues = list(_schema_issues(document))
    report.issues.extend(schema_issues)
    if schema_issues:
        return report

    try:
        config = AICodingConfig.model_validate(document)
    except ValidationError as exc:
        report.issues.extend(_pydantic_issues(exc))
        return report

    if not report.issues:
        report.config = config
    return report


def _schema_issues(document: dict[str, Any]) -> Iterable[ValidationIssue]:
    validator = Draft202012Validator(load_packaged_schema())
    errors: list[SchemaError] = sorted(
        validator.iter_errors(document), key=lambda e: [str(p) for p in e.absolute_path]
    )
    for error in errors:
        yield ValidationIssue(
            IssueKind.SCHEMA, _json_path(error.absolute_path), _schema_message(error)
        )


def _schema_message(error: SchemaError) -> str:
    # Never echo the offending value verbatim: it may be a secret.
    validator = error.validator
    if validator == "additionalProperties":
        return redact(error.message)
    if validator == "required":
        return redact(error.message)
    if validator in {"enum", "const"}:
        allowed = error.validator_value
        return (
            f"value is not allowed; expected one of {allowed!r}"
            if isinstance(allowed, list)
            else f"value must be {allowed!r}"
        )
    if validator == "type":
        return f"expected type {error.validator_value!r}"
    if validator == "pattern":
        return f"value does not match pattern {error.validator_value!r}"
    return redact(error.message)


def _pydantic_issues(exc: ValidationError) -> Iterable[ValidationIssue]:
    for err in exc.errors(include_url=False, include_input=False):
        loc = _json_path(err["loc"])
        message = str(err["msg"]).removeprefix("Value error, ")
        yield ValidationIssue(IssueKind.SEMANTIC, loc, redact(message))


def _json_path(parts: Iterable[object]) -> str:
    out = ""
    for part in parts:
        if isinstance(part, int):
            out += f"[{part}]"
        else:
            out += f".{part}" if out else str(part)
    return out

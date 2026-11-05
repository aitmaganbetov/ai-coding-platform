"""Generation of an initial ``.ai-coding.yml``."""

from __future__ import annotations

import re
from collections.abc import Sequence
from importlib import resources
from string import Template

from ai_coding.config.models import AICodingConfig
from ai_coding.config.validator import ValidationReport, validate_text
from ai_coding.domain.enums import Mode, Stack

_TEMPLATE = "ai-coding.yml.tmpl"
_NAME_SANITIZE = re.compile(r"[^A-Za-z0-9._-]+")


class ConfigGenerationError(Exception):
    """The generated configuration failed validation (a bug: defaults must always validate)."""

    def __init__(self, report: ValidationReport) -> None:
        self.report = report
        issues = "; ".join(f"{i.path}: {i.message}" for i in report.issues)
        super().__init__(f"generated configuration is invalid: {issues}")


def sanitize_project_name(raw: str) -> str:
    """Turn a directory name into a valid ``project.name``."""
    name = _NAME_SANITIZE.sub("-", raw.strip()).strip("-._")
    return (name or "project")[:100]


def render_initial_config(
    *,
    name: str,
    mode: Mode = Mode.AUDIT,
    stacks: Sequence[Stack] = (),
    default_branch: str = "main",
) -> tuple[str, AICodingConfig]:
    """Render the commented template and prove it validates.

    Returns the YAML text and the parsed configuration.
    """
    protected = ["main", "master", "release/*"]
    if default_branch not in protected:
        protected.insert(0, default_branch)
    template = Template(
        resources.files("ai_coding.templates").joinpath(_TEMPLATE).read_text("utf-8")
    )
    text = template.substitute(
        name=_yaml_scalar(name),
        mode=mode.value,
        default_branch=_yaml_scalar(default_branch),
        protected_branches=", ".join(_yaml_scalar(p) for p in protected),
        stacks=", ".join(s.value for s in stacks),
    )
    report = validate_text(text, source="<generated>")
    if not report.ok or report.config is None:
        raise ConfigGenerationError(report)
    return text, report.config


def _yaml_scalar(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'

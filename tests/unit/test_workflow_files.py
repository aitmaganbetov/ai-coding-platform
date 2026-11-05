"""Guardrails for the reusable GitHub Actions workflows (ТЗ §9)."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

WORKFLOWS = Path(__file__).resolve().parents[2] / ".github" / "workflows"
REUSABLE = [
    "quality",
    "security",
    "test-python",
    "test-node",
    "test-php",
    "test-go",
    "test-rust",
    "release",
]
SHA_PIN = re.compile(r"^[\w.-]+/[\w./-]+@[0-9a-f]{40}$")


def load(name: str) -> dict:  # type: ignore[type-arg]
    data = yaml.safe_load((WORKFLOWS / f"{name}.yml").read_text(encoding="utf-8"))
    data["on"] = data.pop(True, data.get("on"))  # YAML 1.1 parses `on` as True
    return data


@pytest.mark.parametrize("name", REUSABLE)
def test_reusable_contract(name: str) -> None:
    wf = load(name)
    assert "workflow_call" in wf["on"], "must be callable via workflow_call (FR-14)"
    assert wf["permissions"] == {}, "top-level permissions must be empty; jobs opt in"
    for job in wf["jobs"].values():
        assert "permissions" in job


@pytest.mark.parametrize("path", sorted(WORKFLOWS.glob("*.yml")), ids=lambda p: p.name)
def test_all_actions_pinned_and_no_injection(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    raw = yaml.safe_load(text)
    triggers = raw.get(True, raw.get("on")) or {}
    assert "pull_request_target" not in triggers, "never run untrusted code with a write token"
    for line in text.splitlines():
        stripped = line.strip().removeprefix("- ")
        if stripped.startswith("uses:"):
            ref = stripped.split(":", 1)[1].split("#")[0].strip()
            assert ref.startswith("./") or SHA_PIN.match(ref), f"{path.name}: unpinned {ref}"
    wf = yaml.safe_load(text)
    for job in wf.get("jobs", {}).values():
        for step in job.get("steps", []):
            run = step.get("run", "")
            assert "${{ inputs." not in run and "${{ github.event" not in run, (
                f"{path.name}: template expansion inside a run script (injection risk)"
            )


def test_security_uploads_sarif_only_for_same_repo() -> None:
    steps = {s.get("name"): s for s in load("security")["jobs"]["audit"]["steps"]}
    upload = steps["Upload SARIF to code scanning"]
    assert "SAME_REPO == 'true'" in upload["if"]
    publish = steps["Publish check run and PR comment"]
    assert "SAME_REPO == 'true'" in publish["if"]


def test_release_requires_environment_and_tags() -> None:
    job = load("release")["jobs"]["release"]
    assert job["environment"] == "${{ inputs.environment }}"
    assert "refs/tags/" in job["if"]


def test_pilots_cover_two_stacks() -> None:
    jobs = load("pilots")["jobs"]
    uses = {j["uses"] for j in jobs.values()}
    assert {"./.github/workflows/test-python.yml", "./.github/workflows/test-node.yml"} <= uses
    assert load("pilots")["permissions"] == {"contents": "read"}


def test_workflow_linters_run_in_ci() -> None:
    steps = load("repo-security")["jobs"]["workflows"]["steps"]
    commands = " ".join(str(s.get("run", "")) for s in steps)
    assert "actionlint" in commands and "zizmor" in commands and "--offline" in commands
    ignored = yaml.safe_load((WORKFLOWS.parent / "zizmor.yml").read_text(encoding="utf-8"))
    # Suppressions stay narrow: one rule, one file.
    assert ignored["rules"] == {"self-repository": {"ignore": ["pilots.yml"]}}

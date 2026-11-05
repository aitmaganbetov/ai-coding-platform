"""Tests for the validation pipeline (YAML → secrets → JSON Schema → semantics)."""

from __future__ import annotations

from pathlib import Path

import pytest

from ai_coding.config import IssueKind, validate_document, validate_file, validate_text
from ai_coding.config.loader import MAX_CONFIG_BYTES, ConfigLoadError, find_config, parse_yaml

MINIMAL = 'schema_version: "2.1"\nproject:\n  name: demo\n'


def kinds(report_issues: list) -> set[IssueKind]:  # type: ignore[type-arg]
    return {i.kind for i in report_issues}


def test_fixture_configs_are_valid(fixtures_dir: Path) -> None:
    for name in ("minimal.yml", "full.yml"):
        report = validate_file(fixtures_dir / "configs" / name)
        assert report.ok, report.issues
        assert report.config is not None


def test_repository_own_config_is_valid() -> None:
    root = Path(__file__).resolve().parents[2]
    report = validate_file(root / ".ai-coding.yml")
    assert report.ok, report.issues


def test_empty_document() -> None:
    report = validate_text("")
    assert not report.ok
    assert report.issues[0].message == "configuration file is empty"


def test_non_mapping_root() -> None:
    report = validate_text("- a\n- b\n")
    assert report.issues[0].kind is IssueKind.PARSE
    assert "mapping" in report.issues[0].message


def test_yaml_syntax_error_has_line() -> None:
    report = validate_text("project: [unclosed\n")
    issue = report.issues[0]
    assert issue.kind is IssueKind.PARSE
    assert issue.line is not None


def test_duplicate_keys_rejected() -> None:
    with pytest.raises(ConfigLoadError, match="duplicate key 'mode'") as info:
        parse_yaml("mode: audit\nmode: develop\n")
    assert info.value.line == 2


def test_yaml_python_tags_are_not_executed() -> None:
    report = validate_text("!!python/object/apply:os.system ['echo pwned']\n")
    assert report.issues[0].kind is IssueKind.PARSE


def test_schema_errors_are_collected_and_located() -> None:
    text = MINIMAL + "mode: deploy\nbudgets:\n  max_cost_usd_per_run: -1\nextra: true\n"
    report = validate_text(text)
    assert not report.ok
    paths = {i.path for i in report.issues}
    assert {"mode", "budgets.max_cost_usd_per_run", ""} <= paths
    assert kinds(report.issues) == {IssueKind.SCHEMA}


def test_missing_required_fields() -> None:
    report = validate_text("mode: audit\n")
    messages = " ".join(i.message for i in report.issues)
    assert "schema_version" in messages
    assert "project" in messages


def test_unsupported_schema_version() -> None:
    report = validate_text('schema_version: "9.9"\nproject: {name: x}\n')
    assert report.issues[0].path == "schema_version"
    assert "unsupported" in report.issues[0].message


def test_semantic_errors_after_schema() -> None:
    report = validate_text(MINIMAL + "ai:\n  enabled: true\n")
    assert kinds(report.issues) == {IssueKind.SEMANTIC}
    assert report.issues[0].path == "ai"


def test_literal_secret_rejected_and_not_echoed() -> None:
    secret = "sk-ant-api03-" + "A" * 40
    text = MINIMAL + f"ai:\n  api_key: {secret}\n"
    report = validate_text(text)
    assert not report.ok
    assert IssueKind.SECRET in kinds(report.issues)
    rendered = repr(report.to_dict())
    assert secret not in rendered


def test_secret_value_anywhere_is_detected() -> None:
    token = "ghp_" + "a" * 36
    report = validate_text(MINIMAL + f"project_notes: {token}\n")
    secret_issues = [i for i in report.issues if i.kind is IssueKind.SECRET]
    assert secret_issues and secret_issues[0].path == "project_notes"
    assert token not in repr(report.to_dict())


def test_enum_error_does_not_echo_value() -> None:
    token = "ghp_" + "b" * 36
    report = validate_document({"schema_version": "2.1", "project": {"name": "x"}, "mode": token})
    assert token not in repr(report.to_dict())


def test_env_reference_is_allowed() -> None:
    report = validate_text(MINIMAL + "ai:\n  api_key_env: OPENAI_API_KEY\n")
    assert report.ok, report.issues


def test_validate_file_missing(tmp_path: Path) -> None:
    report = validate_file(tmp_path / "nope.yml")
    assert report.issues[0].kind is IssueKind.PARSE


def test_validate_file_too_large(tmp_path: Path) -> None:
    big = tmp_path / ".ai-coding.yml"
    big.write_bytes(b"#" * (MAX_CONFIG_BYTES + 1))
    report = validate_file(big)
    assert "larger than" in report.issues[0].message


def test_validate_file_not_utf8(tmp_path: Path) -> None:
    bad = tmp_path / ".ai-coding.yml"
    bad.write_bytes(b"\xff\xfe\x00bad")
    report = validate_file(bad)
    assert "UTF-8" in report.issues[0].message


def test_find_config(tmp_path: Path) -> None:
    assert find_config(tmp_path) is None
    alt = tmp_path / ".ai-coding.yaml"
    alt.write_text(MINIMAL, encoding="utf-8")
    assert find_config(tmp_path) == alt
    main = tmp_path / ".ai-coding.yml"
    main.write_text(MINIMAL, encoding="utf-8")
    assert find_config(tmp_path) == main
    assert find_config(main) == main


def test_report_to_dict_shape() -> None:
    report = validate_text(MINIMAL)
    assert report.to_dict() == {"source": "<string>", "valid": True, "issues": []}

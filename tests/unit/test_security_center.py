"""Coverage never equates a missing scan or an execution result with security; findings
are projected through an allowlist; the module is off unless explicitly switched on."""

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest

from ai_coding.api.settings import ServerSettings
from ai_coding.security_center import findings
from ai_coding.security_center.coverage import ScanTask, coverage, reason_code, tool_state

SECRET = "ghp_" + "c" * 36


def _done(tool: str, **output: object) -> ScanTask:
    return ScanTask(tool, "SUCCEEDED", {"tool": tool, **output})


@pytest.mark.parametrize(
    ("task", "expected"),
    [
        (_done("semgrep", succeeded=True), ("completed", None)),
        (_done("semgrep"), ("error", "tool_failed")),
        (_done("semgrep", succeeded=False, error="timed out"), ("error", "timeout")),
        (_done("semgrep", succeeded="true"), ("error", "tool_failed")),  # not a boolean
        (_done("semgrep", succeeded=True, skipped=True), ("skipped", "not_applicable")),
        (ScanTask("semgrep", "SUCCEEDED", {"tool": "trivy", "succeeded": True}),
         ("error", "invalid_result")),
        (ScanTask("semgrep", "SUCCEEDED", None), ("error", "invalid_result")),
        (ScanTask("semgrep", "FAILED", None), ("error", "task_failed")),
        (ScanTask("semgrep", "SKIPPED", None), ("skipped", "task_skipped")),
        (ScanTask("semgrep", "CANCELLED", None), ("not_run", "cancelled")),
        (ScanTask("semgrep", "RUNNING", {"tool": "semgrep", "succeeded": True}),
         ("running", None)),
        (ScanTask("semgrep", "PENDING", None), ("running", None)),
    ],
)  # fmt: skip
def test_tool_states(task: ScanTask, expected: tuple[str, str | None]) -> None:
    assert tool_state(task) == expected


@pytest.mark.parametrize(
    ("error", "code"),
    [
        (None, None),
        ("", None),
        ("timed out", "timeout"),
        ("offline database missing in /home/x (run `ai-coding tools pull`)", "offline_db_missing"),
        ("output exceeded the size limit; result incomplete", "output_too_large"),
        ("could not parse output: Expecting ',' delimiter", "parse_error"),
        ("runs the project's toolchain: Docker sandbox only", "docker_only"),
        ("unavailable: docker CLI not found", "tool_unavailable"),
        (f"exit code 2: {SECRET}", "tool_failed"),
        (f"something new {SECRET}", "other"),
    ],
)
def test_reasons_are_fixed_codes(error: str | None, code: str | None) -> None:
    assert reason_code(error) == code


def test_category_states() -> None:
    result = coverage(
        [
            _done("semgrep", succeeded=True),
            _done("gitleaks", skipped=True, error="not applicable"),
            _done("trivy", succeeded=True),
        ]
    )
    assert result["sast"]["state"] == "completed"
    assert result["secrets"]["state"] == "skipped"  # nothing ran: not coverage
    assert result["sca"]["state"] == "incomplete"  # OSV-Scanner missing
    assert result["sca"]["tools"][0] == {
        "tool": "osv-scanner", "state": "not_run", "reason": "not_planned"
    }  # fmt: skip
    assert result["iac"]["state"] == "completed"
    assert result["dast"] == {"available": False, "tools": [], "state": "unsupported"}
    assert result["container"]["state"] == "unsupported"


def test_least_favourable_task_wins_and_errors_dominate() -> None:
    result = coverage(
        [_done("trivy", succeeded=True), ScanTask("trivy", "FAILED", None),
         _done("osv-scanner", succeeded=True)]
    )  # fmt: skip
    assert result["sca"]["state"] == "error"
    assert result["iac"]["tools"] == [{"tool": "trivy", "state": "error", "reason": "task_failed"}]
    assert {c["state"] for c in coverage([]).values()} == {"incomplete", "unsupported"}


# ----------------------------------------------------------------- findings --
def _row(**over: Any) -> Any:
    data: dict[str, Any] = {
        "id": "f1", "source": "semgrep", "rule_id": "r", "severity": "high", "title": "t",
        "file_path": "a.py", "line": 3, "fingerprint": "0" * 64, "evidence": {},
        "status": "open", "created_at": datetime(2026, 11, 7, tzinfo=UTC),
    }  # fmt: skip
    return SimpleNamespace(**{**data, **over})


@pytest.mark.parametrize(
    ("source", "evidence", "expected"),
    [
        ("gitleaks", {}, "secrets"),
        ("semgrep", {}, "sast"),
        ("osv-scanner", {}, "sca"),
        ("trivy", {"class": "lang-pkgs"}, "sca"),
        ("trivy", {"class": "config"}, "iac"),
        ("trivy", {"class": "secret"}, "secrets"),
        ("trivy", None, "sca"),
        ("ruff", {}, "other"),
    ],
)
def test_categories(source: str, evidence: dict[str, str] | None, expected: str) -> None:
    assert findings.category(source, evidence) == expected


def test_row_is_redacted_and_bounded() -> None:
    row = findings.row(_row(title=f"leak {SECRET}\n<img src=x onerror=alert(1)>" + "x" * 400,
                            file_path=f"cfg/{SECRET}.txt", line="7"))  # fmt: skip
    assert SECRET not in str(row)
    assert "\n" not in row["title"] and len(row["title"]) <= 300
    assert row["line"] is None  # only integers
    assert set(row) == {"id", "source", "category", "rule_id", "severity", "title",
                        "file_path", "line", "fingerprint", "status", "created_at"}  # fmt: skip


def test_secret_findings_hide_their_title_and_evidence() -> None:
    for source, evidence in (("gitleaks", {"match": SECRET}), ("trivy", {"class": "secret"})):
        item = findings.details(_row(source=source, title=SECRET, evidence=evidence))
        assert item["title"] == findings.SECRET_TITLE
        assert item["details"] == {}
        assert SECRET not in str(item)


def test_details_keep_only_typed_allowlisted_facts() -> None:
    item = findings.details(_row(source="trivy", evidence={
        "package": {"nested": SECRET}, "installed": 7, "fixed": True, "class": "lang-pkgs",
        "url": "https://example.org/a b", "extra": SECRET,
    }))  # fmt: skip
    assert item["details"] == {"installed_version": "7"}
    assert item["withheld"] == list(findings.WITHHELD)


@pytest.mark.parametrize(
    "url",
    ["http://example.org/x", "javascript:alert(1)", "https://user:pw@host/x",
     "https://example.org/\"onmouseover=x", "https://osv.dev/" + SECRET, "https://" + "a" * 300],
)  # fmt: skip
def test_references_must_be_plain_https(url: str) -> None:
    item = findings.details(_row(source="osv-scanner", evidence={"url": url}))
    assert "reference" not in item["details"]


# ---------------------------------------------------------------- the switch --
@pytest.mark.parametrize(
    ("value", "enabled"),
    [(None, False), ("", False), ("off", False), ("0", False), ("false", False),
     ("on", True), ("TRUE", True), (" 1 ", True), ("yes", True)],
)  # fmt: skip
def test_switch(value: str | None, enabled: bool) -> None:
    env = {} if value is None else {"AI_CODING_SERVER_SECURITY_CENTER": value}
    assert ServerSettings.from_env(env).security_center is enabled


def test_switch_typo_stops_the_server() -> None:
    with pytest.raises(ValueError, match="SECURITY_CENTER must be on or off"):
        ServerSettings.from_env({"AI_CODING_SERVER_SECURITY_CENTER": "enabled"})

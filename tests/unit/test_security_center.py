"""Coverage never equates a missing scan or an execution result with security."""

import pytest

from ai_coding.security_center.coverage import coverage


@pytest.mark.parametrize(
    ("output", "expected"),
    [
        ({}, "error"),
        ({"succeeded": True}, "completed"),
        ({"succeeded": False}, "error"),
        ({"succeeded": "true"}, "error"),
        ({"succeeded": True, "skipped": True}, "incomplete"),
    ],
)
def test_execution_states(output: dict[str, object], expected: str) -> None:
    result = coverage([{"tool": "semgrep", **output}])
    assert result["sast"]["state"] == expected
    assert result["dast"]["state"] == "unsupported"
    assert result["container"]["state"] == "unsupported"


def test_missing_and_partial_coverage() -> None:
    result = coverage([{"tool": "trivy", "succeeded": True}])
    assert result["sca"]["state"] == "incomplete"
    assert result["iac"]["state"] == "completed"
    assert result["secrets"]["tools"] == [{"tool": "gitleaks", "state": "not_run"}]

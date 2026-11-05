"""The AC-01…AC-10 matrix: an AC is accepted only if it has scenarios and all passed."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from tests import acceptance_plugin as plugin


@pytest.fixture
def results() -> Iterator[dict[str, dict[str, object]]]:
    saved = dict(plugin._results)
    plugin._results.clear()
    yield plugin._results
    plugin._results.clear()
    plugin._results.update(saved)


def test_matrix_requires_every_ac_to_pass(results: dict[str, dict[str, object]]) -> None:
    results["t::a"] = {"ac": "AC-01", "outcome": "passed", "doc": ""}
    results["t::b"] = {"ac": "AC-02", "outcome": "skipped", "doc": ""}
    results["t::c"] = {"ac": "AC-03", "outcome": "passed", "doc": ""}
    results["t::d"] = {"ac": "AC-03", "outcome": "failed", "doc": ""}
    rows = {r["ac"]: r for r in plugin.matrix()}
    assert list(rows) == [f"AC-{i:02d}" for i in range(1, 11)]
    assert rows["AC-01"]["accepted"] is True
    assert rows["AC-02"]["accepted"] is False  # a skip is not an acceptance
    assert rows["AC-03"]["accepted"] is False  # one failing scenario is enough
    assert rows["AC-04"]["accepted"] is False and rows["AC-04"]["tests"] == []  # missing
    text = plugin.render_markdown(plugin.matrix())
    assert "| AC-01 |" in text and "✅" in text and "`t::b` — skipped" in text and "нет" in text


def test_every_ac_has_a_scenario_in_the_suite(pytestconfig: pytest.Config) -> None:
    """Static guard: each AC is marked on at least one test of the repository."""
    root = pytestconfig.rootpath / "tests"
    text = "".join(p.read_text(encoding="utf-8") for p in root.rglob("test_*.py"))
    for ac in plugin.ACCEPTANCE:
        assert f'acceptance("{ac}")' in text, ac

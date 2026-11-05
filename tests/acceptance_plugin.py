"""Acceptance matrix of ТЗ §17 (B-804).

Scenarios carry ``@pytest.mark.acceptance("AC-NN")``. With ``--acceptance-report=PATH``
the session writes ``PATH`` (JSON) and ``PATH`` with ``.md`` (a table for people) and
fails unless every AC-01…AC-10 has at least one scenario and all of them passed —
a skipped scenario does not count as accepted (ТЗ §20: release only after all ACs).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

ACCEPTANCE: dict[str, str] = {
    "AC-01": "Новый проект из текстового описания: согласованное ТЗ, ADR, backlog, bootstrap и PR",
    "AC-02": "Существующий Laravel+React проект: read-only audit, подтверждённые находки с путями",
    "AC-03": "Функция по Issue в отдельной ветке, тесты, PR без прямого push в main",
    "AC-04": "Падение worker: run возобновляется без дублирования коммитов",
    "AC-05": "Локальная модель: audit без внешних сетевых запросов",
    "AC-06": "Уязвимый PR не проходит Quality Gate; причины и доказательства видны",
    "AC-07": "Prompt injection из README не даёт агенту дополнительных прав",
    "AC-08": "Self-hosted сервер, два проекта: RBAC и отсутствие утечки контекста",
    "AC-09": "GitHub Actions на двух стеках: статусы проверок в PR",
    "AC-10": "Dry-run, ручное approval, отмена и экспорт audit trail",
}

_results: dict[str, dict[str, Any]] = {}


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--acceptance-report",
        metavar="PATH",
        default=None,
        help="Write the AC-01…AC-10 matrix (JSON + Markdown); fail unless all passed.",
    )


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    for item in items:
        for mark in item.iter_markers("acceptance"):
            ac = str(mark.args[0]) if mark.args else ""
            if ac not in ACCEPTANCE:
                raise pytest.UsageError(f"{item.nodeid}: unknown acceptance id {ac!r}")
            _results[item.nodeid] = {"ac": ac, "outcome": "not run", "doc": _doc(item)}


def _doc(item: pytest.Item) -> str:
    func = getattr(item, "function", None)
    text = (getattr(func, "__doc__", None) or "").strip()
    return text.splitlines()[0] if text else ""


def pytest_runtest_logreport(report: pytest.TestReport) -> None:
    entry = _results.get(report.nodeid)
    if entry is None:
        return
    if report.skipped:
        entry["outcome"] = "skipped"
        entry["reason"] = str(report.longrepr[-1]) if isinstance(report.longrepr, tuple) else ""
    elif report.failed:
        entry["outcome"] = "failed"
    elif report.when == "call" and entry["outcome"] == "not run":
        entry["outcome"] = "passed"


def matrix() -> list[dict[str, Any]]:
    """One row per AC: its scenarios and whether it is accepted."""
    rows = []
    for ac, title in ACCEPTANCE.items():
        tests = [{"test": n, **{k: v for k, v in e.items() if k != "ac"}}
                 for n, e in sorted(_results.items()) if e["ac"] == ac]  # fmt: skip
        accepted = bool(tests) and all(t["outcome"] == "passed" for t in tests)
        rows.append({"ac": ac, "title": title, "accepted": accepted, "tests": tests})
    return rows


def render_markdown(rows: list[dict[str, Any]]) -> str:
    lines = ["# Приёмочные сценарии AC-01…AC-10 (ТЗ §17)", "",
             "| AC | Сценарий | Результат | Тесты |", "| --- | --- | --- | --- |"]  # fmt: skip
    for row in rows:
        mark = "✅" if row["accepted"] else "❌"
        tests = "<br>".join(f"`{t['test']}` — {t['outcome']}" for t in row["tests"]) or "нет"
        lines.append(f"| {row['ac']} | {row['title']} | {mark} | {tests} |")
    return "\n".join(lines) + "\n"


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    target = session.config.getoption("--acceptance-report")
    if not target:
        return
    rows = matrix()
    path = Path(target)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(rows, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    path.with_suffix(".md").write_text(render_markdown(rows), encoding="utf-8")
    missing = [r["ac"] for r in rows if not r["accepted"]]
    if missing and session.exitstatus == 0:
        session.exitstatus = pytest.ExitCode.TESTS_FAILED
    reporter = session.config.pluginmanager.get_plugin("terminalreporter")
    if reporter is not None:
        status = "not accepted: " + ", ".join(missing) if missing else "all accepted"
        reporter.write_line(f"acceptance AC-01…AC-10: {status} ({path})")
